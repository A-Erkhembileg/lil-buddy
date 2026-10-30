"""Run with: .venv/bin/python -m unittest discover -s tests -v"""
import os
import random
import sys
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
from PyQt6.QtGui import QMouseEvent, QTransform
from PyQt6.QtWidgets import QApplication

from desktop_cat import BUBBLE_SECONDS, CAT_QUOTES, DesktopCat, HEIGHT, Motion, PETTED, WIDTH, draw_cat


class PhysicsTests(unittest.TestCase):
    def setUp(self):
        self.area = QRectF(-1440, -120, 1440, 900)
        self.rng = random.Random(12)
        self.left, self.right, self.top, self.floor = Motion.limits(self.area)

    def test_drop_accelerates_bounces_and_settles(self):
        cat = Motion(x=-700, y=0, state="fall", roaming=False)
        bounces = 0
        for _ in range(3 * 120):
            old_vy = cat.vy
            cat.step(1 / 120, self.area, self.rng)
            bounces += old_vy > 0 and cat.vy < 0
            self.assertLessEqual(cat.y, self.floor)
            self.assertGreaterEqual(cat.y, self.top)
        self.assertEqual(cat.state, "rest")
        self.assertEqual(cat.y, self.floor)
        self.assertEqual(cat.vy, 0)
        self.assertGreater(bounces, 0)
        self.assertLess(bounces, 4)

    def test_freefall_is_independent_of_refresh_rate(self):
        positions = []
        for fps in (30, 60, 120):
            cat = Motion(x=-700, y=-100, state="fall")
            for _ in range(fps // 2):
                cat.step(1 / fps, self.area, self.rng)
            positions.append(cat.y)
        for position in positions:
            self.assertAlmostEqual(position, -100 + 0.5 * 1700 * 0.5 ** 2)

    def test_both_edges_reverse_walking(self):
        for x, direction, edge in ((self.right - 0.1, 1, self.right),
                                   (self.left + 0.1, -1, self.left)):
            with self.subTest(direction=direction):
                cat = Motion(x=x, y=self.floor, direction=direction, state="walk")
                cat.step(1 / 60, self.area, self.rng)
                self.assertEqual(cat.x, edge)
                self.assertEqual(cat.direction, -direction)

    def test_negative_screen_origin_and_new_floor(self):
        cat = Motion(x=-900, y=self.floor)
        larger = QRectF(-1440, -120, 1440, 1000)
        cat.constrain(larger)
        self.assertEqual(cat.state, "fall")
        self.assertEqual(cat.y, self.floor)
        smaller = QRectF(-900, 0, 900, 600)
        cat.constrain(smaller)
        self.assertEqual(cat.y, 600 - HEIGHT)
        self.assertGreaterEqual(cat.x, -900)
        self.assertLessEqual(cat.x, -WIDTH)

    def test_held_cat_ignores_physics(self):
        cat = Motion(x=-500, y=10, state="held")
        cat.step(0.1, self.area, self.rng)
        self.assertEqual((cat.x, cat.y, cat.vy), (-500, 10, 0))
        cat.drop(self.area)
        cat.step(0.1, self.area, self.rng)
        self.assertGreater(cat.y, 10)

    def test_roaming_disabled_still_allows_gravity(self):
        cat = Motion(x=-500, y=self.floor, roaming=False, decision_in=0)
        for _ in range(20):
            cat.step(1, self.area, self.rng)
        self.assertEqual(cat.state, "rest")
        self.assertEqual(cat.x, -500)
        cat.state, cat.y = "held", 0
        cat.drop(self.area)
        cat.step(0.1, self.area, self.rng)
        self.assertGreater(cat.y, 0)


class WidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.cat = DesktopCat()
        self.cat.timer.stop()

    def tearDown(self):
        self.cat.close()
        self.cat.deleteLater()
        self.app.processEvents()

    def send_mouse(self, kind, global_position, button, buttons, modifiers=Qt.KeyboardModifier.NoModifier):
        event = QMouseEvent(kind, QPointF(75, 60), global_position,
                            button, buttons, modifiers)
        QApplication.sendEvent(self.cat, event)

    def test_mouse_drag_keeps_offset_then_releases_into_gravity(self):
        start = QPointF(self.cat.pos())
        click = start + QPointF(75, 60)
        self.send_mouse(QEvent.Type.MouseButtonPress, click,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
        self.assertFalse(self.cat._dragging)
        self.assertNotEqual(self.cat.motion.state, "held")
        delta = QPointF(-120, -250)
        self.send_mouse(QEvent.Type.MouseMove, click + delta,
                        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
        self.assertTrue(self.cat._dragging)
        self.assertEqual(self.cat.motion.state, "held")
        self.assertEqual(self.cat.motion.x, start.x() + delta.x())
        self.assertEqual(self.cat.motion.y, start.y() + delta.y())
        self.send_mouse(QEvent.Type.MouseButtonRelease, click + delta,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
        self.assertEqual(self.cat.motion.state, "fall")
        self.assertEqual(self.cat.motion.vy, 0)
        self.assertFalse(self.cat.bubble.isVisible())

    def test_left_click_pets_without_starting_a_drop(self):
        self.cat.motion.state = "walk"
        click = QPointF(self.cat.pos()) + QPointF(75, 60)
        with patch.object(self.cat, "pet", wraps=self.cat.pet) as pet:
            self.send_mouse(QEvent.Type.MouseButtonPress, click,
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
            self.send_mouse(QEvent.Type.MouseButtonRelease, click,
                            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
            pet.assert_called_once()
        self.assertEqual(self.cat.motion.state, PETTED)
        self.assertFalse(self.cat.motion.sleeping)
        self.assertIn(self.cat.bubble.text, CAT_QUOTES)
        self.assertTrue(self.cat.bubble.isVisible())

    def test_small_pointer_jitter_is_still_one_pet(self):
        click = QPointF(self.cat.pos()) + QPointF(75, 60)
        jitter = QPointF(1, 1)
        with patch.object(self.cat, "pet", wraps=self.cat.pet) as pet:
            self.send_mouse(QEvent.Type.MouseButtonPress, click,
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
            self.send_mouse(QEvent.Type.MouseMove, click + jitter,
                            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
            self.assertFalse(self.cat._dragging)
            self.send_mouse(QEvent.Type.MouseButtonRelease, click + jitter,
                            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
            pet.assert_called_once()

    def test_release_beyond_drag_threshold_does_not_pet_without_a_move_event(self):
        click = QPointF(self.cat.pos()) + QPointF(75, 60)
        with patch.object(self.cat, "pet") as pet:
            self.send_mouse(QEvent.Type.MouseButtonPress, click,
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
            self.send_mouse(QEvent.Type.MouseButtonRelease, click + QPointF(0, -80),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
            pet.assert_not_called()
        self.assertEqual(self.cat.motion.state, "fall")

    def test_drag_dismisses_a_quote_and_never_counts_as_petting(self):
        self.cat.pet()
        click = QPointF(self.cat.pos()) + QPointF(75, 60)
        with patch.object(self.cat, "pet") as pet:
            self.send_mouse(QEvent.Type.MouseButtonPress, click,
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
            self.send_mouse(QEvent.Type.MouseMove, click + QPointF(50, -100),
                            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
            self.assertFalse(self.cat.bubble.isVisible())
            self.assertEqual(self.cat._pet_until, 0)
            self.send_mouse(QEvent.Type.MouseButtonRelease, click + QPointF(50, -100),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
            pet.assert_not_called()

    def test_petting_stops_roaming_for_three_seconds_then_decisions_resume(self):
        self.cat.motion.state = "walk"
        self.cat.motion.sleeping = True
        self.cat._last_time = 100.0
        self.cat.pet(now=100.0)
        position = self.cat.motion.x
        deadline = self.cat.motion.decision_in
        self.assertEqual(self.cat.bubble.expires_at, 100.0 + BUBBLE_SECONDS)
        self.assertTrue(self.cat.motion.roaming)
        self.cat._tick(now=102.9)
        self.assertEqual(self.cat.motion.x, position)
        self.assertEqual(self.cat.motion.decision_in, deadline)
        self.assertEqual(self.cat.motion.state, PETTED)
        self.assertTrue(self.cat.bubble.isVisible())
        self.cat._tick(now=103.1)
        self.assertFalse(self.cat.bubble.isVisible())
        self.assertEqual(self.cat._pet_until, 0)
        self.assertLess(self.cat.motion.decision_in, deadline)

    def test_repeated_petting_refreshes_the_bubble_deadline(self):
        self.cat._last_time = 100.0
        self.cat.pet(now=100.0)
        self.cat.pet(now=102.0)
        self.assertEqual(self.cat.bubble.expires_at, 102.0 + BUBBLE_SECONDS)
        self.cat._tick(now=103.1)
        self.assertTrue(self.cat.bubble.isVisible())
        self.cat._tick(now=105.1)
        self.assertFalse(self.cat.bubble.isVisible())

    def test_quote_expires_even_while_context_menu_pauses_movement(self):
        self.cat._last_time = 100.0
        self.cat.pet(now=100.0)
        self.cat._menu_open = True
        self.cat._tick(now=103.1)
        self.assertFalse(self.cat.bubble.isVisible())
        self.assertEqual(self.cat._pet_until, 0)

    def test_petting_keeps_paused_roaming_paused_and_falling_physics_active(self):
        self.cat._toggle_roaming(False)
        self.cat._last_time = 100.0
        self.cat.pet(now=100.0)
        self.cat._tick(now=103.1)
        self.assertFalse(self.cat.motion.roaming)
        self.assertEqual(self.cat.motion.state, "rest")
        self.cat.motion.state = "fall"
        self.cat.motion.y -= 100
        start_y = self.cat.motion.y
        self.cat.pet(now=104.0)
        self.cat._tick(now=104.1)
        self.assertGreater(self.cat.motion.y, start_y)

    def test_speech_bubble_does_not_capture_clicks_or_keyboard_focus(self):
        bubble = self.cat.bubble
        self.assertTrue(bubble.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        self.assertTrue(bubble.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertTrue(bubble.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))

    def test_neon_eyes_widen_when_held_and_gait_has_two_distinct_poses(self):
        def green_pixels(pixmap):
            image = pixmap.toImage()
            return sum(image.pixelColor(x, y).green() > 190
                       and image.pixelColor(x, y).red() < 190
                       and image.pixelColor(x, y).blue() < 180
                       and image.pixelColor(x, y).alpha() != 0
                       for y in range(HEIGHT) for x in range(WIDTH))
        rest = draw_cat("rest", 0, 1)
        held = draw_cat("held", 0, 1)
        self.assertGreater(green_pixels(rest), 0)
        self.assertGreater(green_pixels(held), green_pixels(rest))
        self.assertNotEqual(draw_cat("walk", 0, 1).toImage(), draw_cat("walk", 1, 1).toImage())
        self.assertEqual(self.cat.timer.interval(), 16)

    def test_idle_animation_changes_tail_and_breath_without_moving_the_cat(self):
        self.cat.motion.state, self.cat.motion.sleeping = "rest", False
        position = self.cat.pos()
        self.cat._animation_time = 0
        self.cat._set_frame()
        first = self.cat._pixmap.toImage()
        self.cat._animation_time = 0.9
        self.cat._set_frame()
        self.assertNotEqual(first, self.cat._pixmap.toImage())
        self.assertEqual(self.cat.pos(), position)

    def test_walking_left_mirrors_both_gait_poses(self):
        mirror = QTransform().scale(-1, 1)
        for frame in (0, 1):
            with self.subTest(frame=frame):
                right = draw_cat("walk", frame, 1).toImage()
                left = draw_cat("walk", frame, -1).toImage()
                self.assertEqual(left, right.transformed(mirror))

    def test_long_quote_wraps_inside_a_negative_coordinate_display(self):
        area = QRectF(-1440, -120, 1440, 900)
        quote = max(CAT_QUOTES, key=len)
        left, right, _, floor = Motion.limits(area)
        self.cat._pet_until = 103
        with patch.object(self.cat, "available_area", return_value=area):
            for x in (left, right):
                with self.subTest(edge=x):
                    self.cat.move(round(x), round(floor))
                    self.cat.bubble.show_quote(quote, now=100)
                    bubble = self.cat.bubble
                    self.assertTrue(area.contains(QRectF(bubble.geometry())))
                    self.assertEqual(" ".join(bubble._lines), quote.upper())
                    self.assertGreater(len(bubble._lines), 1)
                    self.assertEqual(bubble._pixmap.size(), bubble.size())
                    image = bubble._pixmap.toImage()
                    text_pixels = [(x, y) for y in range(image.height())
                                   for x in range(image.width())
                                   if image.pixelColor(x, y).name() == "#e0f4cd"]
                    self.assertTrue(text_pixels)
                    self.assertGreaterEqual(min(x for x, _ in text_pixels), bubble.PADDING)
                    self.assertLess(max(x for x, _ in text_pixels), bubble.width() - bubble.PADDING)
                    self.assertLess(max(y for _, y in text_pixels), bubble.height() - bubble.TAIL)

    def test_bubble_moves_below_cat_when_there_is_no_room_above(self):
        area = QRectF(-1440, -120, 1440, 900)
        self.cat.move(-1300, -120)
        self.cat._pet_until = 103
        with patch.object(self.cat, "available_area", return_value=area):
            self.cat.bubble.show_quote(CAT_QUOTES[0], now=100)
            self.assertTrue(self.cat.bubble._below)
            self.assertGreaterEqual(self.cat.bubble.y(), self.cat.y() + HEIGHT)
            self.assertTrue(area.contains(QRectF(self.cat.bubble.geometry())))
            self.cat.move(-1300, round(area.bottom() - HEIGHT))
            self.cat.bubble.reposition()
            self.assertFalse(self.cat.bubble._below)
            self.assertLessEqual(self.cat.bubble.geometry().bottom(),
                                 self.cat.head_anchor_global().y())

    def test_control_click_and_right_click_open_menu(self):
        control = (Qt.KeyboardModifier.MetaModifier if sys.platform == "darwin"
                   else Qt.KeyboardModifier.ControlModifier)
        with patch.object(self.cat, "_show_menu") as show:
            self.send_mouse(QEvent.Type.MouseButtonPress, QPointF(100, 100),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, control)
            show.assert_called_once()
        with patch.object(self.cat, "_show_menu") as show:
            self.send_mouse(QEvent.Type.MouseButtonPress, QPointF(100, 100),
                            Qt.MouseButton.RightButton, Qt.MouseButton.RightButton)
            show.assert_called_once()

    def test_every_pose_has_transparency_and_a_matching_click_mask(self):
        for key, (pixmap, region) in self.cat._frames.items():
            with self.subTest(pose=key):
                self.assertEqual((pixmap.width(), pixmap.height()), (WIDTH, HEIGHT))
                image = pixmap.toImage()
                self.assertFalse(region.isEmpty())
                for y in range(HEIGHT):
                    for x in range(WIDTH):
                        self.assertEqual(region.contains(QPoint(x, y)),
                                         image.pixelColor(x, y).alpha() != 0)

    def test_pause_stops_walk_but_does_not_suspend_a_fall(self):
        self.cat.motion.state = "walk"
        self.cat._toggle_roaming(False)
        self.assertEqual(self.cat.motion.state, "rest")
        self.cat.motion.state = "fall"
        self.cat.motion.vy = 100
        self.cat._toggle_roaming(False)
        self.assertEqual(self.cat.motion.state, "fall")
        self.assertEqual(self.cat.motion.vy, 100)


if __name__ == "__main__":
    unittest.main()

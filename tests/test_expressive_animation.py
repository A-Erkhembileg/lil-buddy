"""Expressive cat animation and its interaction with physical movement."""
import os
import random
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QMouseEvent
from PyQt6.QtWidgets import QApplication

import desktop_cat as pet


class ImpactTests(unittest.TestCase):
    def setUp(self):
        self.area = QRectF(-1440, -120, 1440, 900)
        self.floor = pet.Motion.limits(self.area)[3]
        self.rng = random.Random(12)

    def test_collision_reports_incoming_speed_without_removing_bounce(self):
        cat = pet.Motion(x=-700, y=self.floor - 1, vy=800, state="fall")
        impact = cat.step(1 / 120, self.area, self.rng)
        self.assertGreaterEqual(impact, pet.HARD_LANDING_SPEED)
        self.assertEqual(cat.y, self.floor)
        self.assertLess(cat.vy, 0)
        self.assertEqual(cat.state, "fall")

    def test_soft_contact_reports_a_small_impact_and_settles(self):
        cat = pet.Motion(x=-700, y=self.floor - 0.1, vy=60, state="fall")
        impact = cat.step(1 / 120, self.area, self.rng)
        self.assertGreater(impact, 0)
        self.assertLess(impact, pet.HARD_LANDING_SPEED)
        self.assertEqual(cat.state, "rest")
        self.assertEqual(cat.vy, 0)

    def test_freefall_without_collision_has_no_impact_event(self):
        cat = pet.Motion(x=-700, y=self.floor - 300, vy=100, state="fall")
        self.assertEqual(cat.step(1 / 120, self.area, self.rng), 0)

    def test_petted_state_stays_grounded_without_advancing_decisions(self):
        cat = pet.Motion(x=-700, y=self.floor, state=pet.PETTED, decision_in=0.05)
        self.assertEqual(cat.step(0.1, self.area, self.rng), 0)
        self.assertEqual(cat.state, pet.PETTED)
        self.assertEqual(cat.decision_in, 0.05)
        self.assertEqual((cat.x, cat.y), (-700, self.floor))


class ExpressiveWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.cat = pet.DesktopCat()
        self.cat.timer.stop()
        self.cat.fullscreen_timer.stop()
        self.area = QRectF(-1440, -120, 1440, 900)
        self.floor = pet.Motion.limits(self.area)[3]
        self.area_patch = patch.object(self.cat, "available_area", return_value=self.area)
        self.area_patch.start()
        self.cat.motion.x, self.cat.motion.y = -700, self.floor
        self.cat.motion.state, self.cat.motion.sleeping = "rest", False
        self.cat.move(-700, round(self.floor))
        self.cat._last_area = self.area
        self.cat._last_time = 100.0

    def tearDown(self):
        self.area_patch.stop()
        self.cat.close()
        self.cat.deleteLater()
        self.app.processEvents()

    def test_grounded_pet_enters_petted_until_the_bubble_deadline(self):
        self.cat.motion.state = "walk"
        self.cat.pet(now=100.0)
        self.assertEqual(self.cat.motion.state, pet.PETTED)
        self.assertEqual(self.cat._frame_key[0], "petted")
        self.assertTrue(self.cat.bubble.isVisible())
        self.cat._tick(now=100.05)
        self.assertEqual(self.cat.motion.state, pet.PETTED)
        self.cat._tick(now=100.0 + pet.BUBBLE_SECONDS + 0.01)
        self.assertEqual(self.cat.motion.state, "rest")
        self.assertFalse(self.cat.bubble.isVisible())

    def test_four_paddling_frames_run_during_both_dragging_and_falling(self):
        for state in ("held", "fall"):
            images = []
            self.cat.motion.state, self.cat.motion.vy = state, 500
            for frame in range(4):
                with self.subTest(state=state, frame=frame):
                    self.cat._animation_time = frame * pet.AIR_FRAME_SECONDS + 0.001
                    self.cat._set_frame()
                    self.assertEqual(self.cat._frame_key[:2], (state, frame))
                    images.append(self.cat._pixmap.toImage())
            for first in range(4):
                for second in range(first + 1, 4):
                    self.assertNotEqual(images[first], images[second])

    def test_fall_speed_and_drag_speed_control_stretch_separately(self):
        for state, speed_field in (("fall", "vy"), ("held", "_drag_speed")):
            with self.subTest(state=state):
                self.cat.motion.state = state
                if speed_field == "vy":
                    self.cat.motion.vy = 0
                else:
                    self.cat._drag_speed = 0
                self.cat._set_frame()
                low = self.cat._frame_key[-1]
                if speed_field == "vy":
                    self.cat.motion.vy = 1600
                else:
                    self.cat._drag_speed = 1600
                self.cat._set_frame()
                high = self.cat._frame_key[-1]
                self.assertGreater(high, low)
                self.assertGreaterEqual(low, 0)
                self.assertLessEqual(high, 3)

    def test_only_hard_contact_starts_a_floor_anchored_landing_effect(self):
        for speed, hard in ((60, False), (800, True)):
            with self.subTest(speed=speed):
                self.cat._landing_elapsed = None
                self.cat.motion.state = "fall"
                self.cat.motion.y, self.cat.motion.vy = self.floor - 0.1, speed
                self.cat._last_time = 100.0
                self.cat._tick(now=100.016)
                if hard:
                    self.assertIsNotNone(self.cat._landing_elapsed)
                    self.assertEqual(self.cat._frame_key[0], "land")
                    self.assertEqual(self.cat.y(), round(self.floor))
                else:
                    self.assertIsNone(self.cat._landing_elapsed)
                    self.assertNotEqual(self.cat._frame_key[0], "land")

    def test_landing_runs_three_frames_then_releases_the_visual_override(self):
        self.cat._landing_elapsed = 0.0
        self.cat.motion.state = "fall"
        self.cat.motion.y, self.cat.motion.vy = self.floor, -80
        for frame in range(3):
            now = 100.0 + frame * pet.LAND_FRAME_SECONDS + 0.001
            self.cat._tick(now=now)
            self.assertEqual(self.cat._frame_key[:2], ("land", frame))
            self.assertEqual(self.cat.y(), round(self.floor))
        self.cat._tick(now=100.0 + 3 * pet.LAND_FRAME_SECONDS + 0.002)
        self.assertIsNone(self.cat._landing_elapsed)
        self.assertNotEqual(self.cat._frame_key[0], "land")

    def test_new_drag_cancels_landing_and_measures_pointer_speed(self):
        origin = QPointF(self.cat.pos()) + QPointF(75, 60)
        self.cat._press_position = origin
        self.cat._drag_offset = QPointF(75, 60)
        self.cat._drag_sample_position = origin
        self.cat._drag_sample_time = 100.0
        self.cat._landing_elapsed = 0.1
        self.cat._drag_to(origin + QPointF(20, -80), now=100.02)
        self.assertIsNone(self.cat._landing_elapsed)
        self.assertEqual(self.cat.motion.state, "held")
        self.assertGreater(self.cat._drag_speed, 0)
        moving_speed = self.cat._drag_speed
        self.cat._last_time = 100.02
        self.cat._tick(now=100.10)
        self.assertLess(self.cat._drag_speed, moving_speed)

    def test_grabbing_the_floor_anchored_landing_pose_uses_its_visible_position(self):
        self.cat._landing_elapsed = pet.LAND_FRAME_SECONDS
        self.cat.motion.state = "fall"
        self.cat.motion.y = self.floor - 12  # Physics is bouncing above the drawn pose.
        local = QPointF(75, 60)
        click = QPointF(self.cat.pos()) + local
        press = QMouseEvent(QEvent.Type.MouseButtonPress, local, click,
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.cat, press)
        delta = QPointF(25, -60)
        self.cat._drag_to(click + delta, now=self.cat._drag_sample_time + 0.02)
        self.assertIsNone(self.cat._landing_elapsed)
        self.assertEqual(self.cat.motion.state, "held")
        self.assertEqual(self.cat.motion.y, self.floor + delta.y())
        self.assertEqual(self.cat.y(), round(self.floor + delta.y()))

    def test_petting_and_latching_cancel_stale_impact_effects(self):
        self.cat._landing_elapsed = 0.1
        self.cat.pet(now=100.0)
        self.assertIsNone(self.cat._landing_elapsed)
        self.assertEqual(self.cat.motion.state, pet.PETTED)
        self.cat._landing_elapsed = 0.1
        self.cat._drag_speed = 1000
        self.cat._set_latched(True, now=100.5)
        self.assertIsNone(self.cat._landing_elapsed)
        self.assertEqual(self.cat._drag_speed, 0)
        self.assertEqual(self.cat.motion.state, "latched")
        self.assertFalse(self.cat.bubble.isVisible())

    def test_color_changes_repaint_the_active_reaction_without_resetting_it(self):
        self.cat.pet(now=100.0)
        deadline = self.cat._pet_until
        before = self.cat._pixmap.toImage()
        self.cat.set_custom_color(QColor(70, 130, 210))
        self.assertEqual(self.cat.motion.state, pet.PETTED)
        self.assertEqual(self.cat._pet_until, deadline)
        self.assertTrue(self.cat.bubble.isVisible())
        self.assertNotEqual(self.cat._pixmap.toImage(), before)


class ExpressiveArtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_air_stretch_and_three_landing_frames_are_visibly_distinct(self):
        self.assertNotEqual(pet.draw_cat("fall", 0, 1, stretch=0).toImage(),
                            pet.draw_cat("fall", 0, 1, stretch=3).toImage())
        images = [pet.draw_cat("land", frame, 1).toImage() for frame in range(3)]
        for first in range(3):
            for second in range(first + 1, 3):
                self.assertNotEqual(images[first], images[second])
        for image in images:
            self.assertTrue(any(image.pixelColor(x, pet.HEIGHT - 1).alpha()
                                for x in range(pet.WIDTH)))

    def test_petted_decorations_float_and_can_be_rendered_without_covering_the_body(self):
        frames = []
        for frame in range(4):
            decorated = pet.draw_cat("petted", frame, 1, effects=True).toImage()
            clean = pet.draw_cat("petted", frame, 1, effects=False).toImage()
            self.assertNotEqual(decorated, clean)
            self.assertGreater(sum(decorated.pixelColor(x, y).alpha() != 0
                                   for y in range(pet.HEIGHT) for x in range(pet.WIDTH)),
                               sum(clean.pixelColor(x, y).alpha() != 0
                                   for y in range(pet.HEIGHT) for x in range(pet.WIDTH)))
            frames.append(decorated)
        self.assertTrue(any(image != frames[0] for image in frames[1:]))

    def test_all_expressive_frames_keep_the_shared_transparent_canvas_and_palette(self):
        for name, palette in pet.PALETTES.items():
            for pose, count in (("held", 4), ("fall", 4), ("land", 3), ("petted", 4)):
                for frame in range(count):
                    with self.subTest(palette=name, pose=pose, frame=frame):
                        pixmap = pet.draw_cat(pose, frame, 1, palette=palette, stretch=3)
                        self.assertEqual((pixmap.width(), pixmap.height()), (pet.WIDTH, pet.HEIGHT))
                        self.assertFalse(pixmap.mask().isNull())
                        image = pixmap.toImage()
                        self.assertTrue(any(image.pixelColor(x, y).name() == palette.fur
                                            and image.pixelColor(x, y).alpha()
                                            for y in range(pet.HEIGHT) for x in range(pet.WIDTH)))


if __name__ == "__main__":
    unittest.main()

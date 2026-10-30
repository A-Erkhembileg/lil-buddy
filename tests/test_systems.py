"""Color/animal menus and fullscreen state transitions, without a desktop window."""
import os
import unittest
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication

import desktop_cat as pet


class FakeScreen:
    """A monitor left of and above the primary, with a reserved menu/Dock area."""

    def geometry(self):
        return QRect(-1920, -300, 1920, 1080)

    def availableGeometry(self):
        return QRect(-1920, -270, 1920, 970)

    def name(self):
        return "Mock external display"


class SystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.cat = pet.DesktopCat()
        self.cat.timer.stop()
        self.cat.fullscreen_timer.stop()
        self.screen = FakeScreen()
        self.cat.display = self.screen
        self.screens = patch.object(pet.QGuiApplication, "screens", return_value=[self.screen])
        self.screens.start()
        self.cat.back_to_floor()
        self.cat.motion.sleeping = False
        self.cat._set_frame()

    def tearDown(self):
        self.screens.stop()
        self.cat.close()
        self.cat.deleteLater()
        self.app.processEvents()

    def test_presets_immediately_repaint_the_same_pose(self):
        pictures = []
        for name in ("Charcoal Grey", "Tuxedo Black", "Calico Orange"):
            with self.subTest(palette=name):
                position = QPoint(self.cat.pos())
                self.cat.set_palette(name)
                self.assertEqual(self.cat.palette_name, name)
                self.assertEqual(self.cat.palette, pet.PALETTES[name])
                self.assertEqual(self.cat.pos(), position)
                pictures.append(self.cat._pixmap.toImage())
        for first in range(len(pictures)):
            for second in range(first + 1, len(pictures)):
                self.assertNotEqual(pictures[first], pictures[second])

    def test_custom_rgb_changes_fur_without_waiting_for_an_animation_tick(self):
        old_picture = self.cat._pixmap.toImage()
        color = QColor(76, 123, 201)
        self.cat.set_custom_color(color)
        self.assertEqual(QColor(self.cat.palette.fur), color)
        self.assertEqual(self.cat.palette_name, "Custom RGB")
        self.assertNotEqual(self.cat._pixmap.toImage(), old_picture)

    def test_custom_rgb_cancel_restores_the_palette_and_name_before_preview(self):
        self.cat.set_palette("Tuxedo Black")
        original_palette, original_picture = self.cat.palette, self.cat._pixmap.toImage()
        self.cat._choose_custom_color()
        dialog = self.cat._color_dialog
        self.assertIsNotNone(dialog)
        dialog.setCurrentColor(QColor(200, 70, 130))
        self.assertEqual(self.cat.palette_name, "Custom RGB")
        self.assertNotEqual(self.cat.palette, original_palette)
        dialog.reject()
        self.assertIsNone(self.cat._color_dialog)
        self.assertEqual(self.cat.palette_name, "Tuxedo Black")
        self.assertEqual(self.cat.palette, original_palette)
        self.assertEqual(self.cat._pixmap.toImage(), original_picture)

    def test_custom_rgb_accept_retains_preview_and_dialog_pauses_motion(self):
        self.cat.motion.state = "walk"
        self.cat._last_time = 100.0
        self.cat._choose_custom_color()
        dialog = self.cat._color_dialog
        self.cat._choose_custom_color()
        self.assertIs(self.cat._color_dialog, dialog)
        position, animation = QPoint(self.cat.pos()), self.cat._animation_time
        self.cat._tick(now=100.1)
        self.assertEqual(self.cat.pos(), position)
        self.assertEqual(self.cat._animation_time, animation)
        color = QColor(44, 160, 210)
        dialog.setCurrentColor(color)
        dialog.accept()
        self.assertIsNone(self.cat._color_dialog)
        self.assertEqual(self.cat.palette_name, "Custom RGB")
        self.assertEqual(QColor(self.cat.palette.fur), color)

    def test_animal_selector_dispatches_and_replaces_cached_cat_art(self):
        for animal in ("Dog", "Fox", "Crow", "Cat"):
            with self.subTest(animal=animal):
                drawer = Mock(wraps=pet.DRAWERS[animal])
                with patch.dict(pet.DRAWERS, {animal: drawer}):
                    self.cat.set_animal_type(animal)
                    self.assertEqual(self.cat.animal_type, animal)
                    self.assertGreater(drawer.call_count, 0)
                self.assertEqual(self.cat._pixmap.size(), self.cat.size())

    def test_placeholder_drawers_use_the_common_canvas_and_have_distinct_art(self):
        images = []
        for name in ("Cat", "Dog", "Fox", "Crow"):
            with self.subTest(animal=name):
                pixmap = pet.DRAWERS[name]("rest", 0, 1)
                self.assertEqual((pixmap.width(), pixmap.height()), (pet.WIDTH, pet.HEIGHT))
                self.assertFalse(pixmap.mask().isNull())
                image = pixmap.toImage()
                self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
                images.append(image)
        for first in range(len(images)):
            for second in range(first + 1, len(images)):
                self.assertNotEqual(images[first], images[second])

    def test_menu_has_all_color_and_animal_actions_with_working_callbacks(self):
        menu = self.cat._build_menu()
        self.addCleanup(menu.deleteLater)
        submenus = {action.text(): action.menu() for action in menu.actions() if action.menu()}
        self.assertIn("Change Color", submenus)
        self.assertIn("Animal Type", submenus)
        colors = {action.text(): action for action in submenus["Change Color"].actions()}
        self.assertTrue({"Charcoal Grey", "Tuxedo Black", "Calico Orange", "Custom RGB"} <= colors.keys())
        animals = {action.text(): action for action in submenus["Animal Type"].actions()}
        self.assertEqual(set(animals), {"Cat", "Dog", "Fox", "Crow"})
        for name in ("Tuxedo Black", "Calico Orange", "Charcoal Grey"):
            colors[name].trigger()
            self.assertEqual(self.cat.palette_name, name)
        for label, animal in (("Dog", "Dog"), ("Fox", "Fox"),
                              ("Crow", "Crow"), ("Cat", "Cat")):
            animals[label].trigger()
            self.assertEqual(self.cat.animal_type, animal)

    def test_monitor_and_animation_have_separate_timer_rates(self):
        self.assertEqual(self.cat.timer.interval(), 16)
        self.assertEqual(self.cat.fullscreen_timer.interval(), 250)

    def test_latch_cancels_speech_and_drag_and_uses_exact_fullscreen_corner(self):
        self.cat.pet(now=100.0)
        self.cat._press_position = QPointF(self.cat.pos())
        self.cat._dragging = True
        self.cat.motion.state = "held"
        self.cat.motion.vy = 160
        self.cat.timer.start(16)
        self.cat._set_latched(True, now=100.5)
        self.assertTrue(self.cat._latched)
        self.assertEqual(self.cat.motion.state, "latched")
        self.assertEqual(self.cat.motion.vy, 0)
        self.assertFalse(self.cat.timer.isActive())
        self.assertFalse(self.cat.bubble.isVisible())
        self.assertEqual(self.cat._pet_until, 0)
        self.assertIsNone(self.cat._press_position)
        self.assertFalse(self.cat._dragging)
        full = self.screen.geometry()
        expected = QPoint(full.x() + full.width() - pet.WIDTH, full.y())
        self.assertEqual(self.cat.pos(), expected)
        self.assertEqual((self.cat.motion.x, self.cat.motion.y), (expected.x(), expected.y()))
        self.assertTrue(self.cat.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))

    def test_latched_pet_is_static_and_ignores_petting_dragging_and_floor_reset(self):
        self.cat._animation_time = 12.0
        self.cat._set_latched(True, now=100.0)
        initial = (self.cat.motion.x, self.cat.motion.y, self.cat.motion.vy,
                   self.cat.motion.decision_in, self.cat._animation_time)
        initial_picture = self.cat._pixmap.toImage()
        for now in (100.1, 110.0, 400.0):
            self.cat._tick(now=now)
            self.cat.pet(now=now)
            self.cat._drag_to(QPointF(-800, 200))
            self.cat.back_to_floor()
        actual = (self.cat.motion.x, self.cat.motion.y, self.cat.motion.vy,
                  self.cat.motion.decision_in, self.cat._animation_time)
        self.assertEqual(actual, initial)
        self.assertEqual(self.cat._pixmap.toImage(), initial_picture)
        self.assertFalse(self.cat.bubble.isVisible())
        self.assertEqual(self.cat._pet_until, 0)

    def test_unlatch_restarts_gravity_without_resuming_stale_speech_or_catchup(self):
        self.cat.motion.roaming = False
        self.cat._set_latched(True, now=100.0)
        self.cat._set_latched(False, now=500.0)
        self.assertFalse(self.cat._latched)
        self.assertEqual(self.cat.motion.state, "fall")
        self.assertEqual(self.cat.motion.vy, 0)
        self.assertFalse(self.cat.motion.roaming)
        self.assertTrue(self.cat.timer.isActive())
        self.assertEqual(self.cat._last_time, 500.0)
        self.assertFalse(self.cat.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        self.assertFalse(self.cat.bubble.isVisible())
        initial_y = self.cat.motion.y
        self.cat._tick(now=500.05)
        self.assertGreater(self.cat.motion.y, initial_y)
        self.assertLess(self.cat.motion.y - initial_y, 10)

    def test_repeated_latch_does_not_change_pose_position_or_decision_timer(self):
        self.cat._set_latched(True, now=100.0)
        initial = (QPoint(self.cat.pos()), self.cat._pixmap.toImage(), self.cat.motion.decision_in)
        self.cat._set_latched(True, now=200.0)
        self.assertEqual((self.cat.pos(), self.cat._pixmap.toImage(), self.cat.motion.decision_in), initial)

    def test_unknown_detection_keeps_latch_and_exit_requires_two_false_samples(self):
        detector = Mock()
        self.cat.fullscreen_detector = detector
        detector.is_fullscreen.side_effect = [True, False, None, False, False]
        expected = (True, True, True, True, False)
        for latched in expected:
            self.cat._poll_fullscreen()
            self.assertEqual(self.cat._latched, latched)
        for call in detector.is_fullscreen.call_args_list:
            self.assertIs(call.args[0], self.screen)

    def test_detector_error_does_not_unlatch_or_advance_physics(self):
        detector = Mock()
        self.cat.fullscreen_detector = detector
        detector.is_fullscreen.return_value = True
        self.cat._poll_fullscreen()
        position = QPoint(self.cat.pos())
        detector.is_fullscreen.side_effect = RuntimeError("Temporary native window-server failure")
        self.cat._poll_fullscreen()
        self.assertTrue(self.cat._latched)
        self.assertFalse(self.cat.timer.isActive())
        self.assertEqual(self.cat.pos(), position)

    def test_latch_closes_menu_and_cancels_color_preview(self):
        self.cat._show_menu(self.cat.pos())
        self.assertTrue(self.cat._menu_open)
        self.cat._set_latched(True, now=100.0)
        self.assertFalse(self.cat._menu_open)
        self.assertIsNone(self.cat._active_menu)
        self.cat._set_latched(False, now=101.0)
        self.cat.timer.stop()
        self.cat.set_palette("Calico Orange")
        original_palette = self.cat.palette
        self.cat._choose_custom_color()
        self.cat._color_dialog.setCurrentColor(QColor(30, 200, 100))
        self.cat._set_latched(True, now=102.0)
        self.assertIsNone(self.cat._color_dialog)
        self.assertEqual(self.cat.palette_name, "Calico Orange")
        self.assertEqual(self.cat.palette, original_palette)
        self.assertFalse(self.cat.timer.isActive())

    def test_color_and_animal_can_change_static_latch_art_without_restarting_animation(self):
        self.cat._set_latched(True, now=100.0)
        initial_position = QPoint(self.cat.pos())
        self.cat.set_palette("Calico Orange")
        self.cat.set_animal_type("Crow")
        self.assertTrue(self.cat._latched)
        self.assertFalse(self.cat.timer.isActive())
        self.assertEqual(self.cat.pos(), initial_position)
        self.assertEqual(self.cat.palette_name, "Calico Orange")
        self.assertEqual(self.cat.animal_type, "Crow")

    def test_native_input_failure_hides_during_fullscreen_and_recovers_on_exit(self):
        self.cat.show()
        with patch.object(pet, "set_native_click_through", side_effect=[OSError("native style failed"), True]):
            self.cat._set_latched(True, now=100.0)
            self.assertTrue(self.cat._latched)
            self.assertFalse(self.cat.isVisible())
            self.assertFalse(self.cat.timer.isActive())
            self.cat._set_latched(False, now=200.0)
        self.assertTrue(self.cat.isVisible())
        self.assertFalse(self.cat._latched)
        self.assertIsNone(self.cat._native_input_error)
        self.assertEqual(self.cat.motion.state, "fall")


if __name__ == "__main__":
    unittest.main()

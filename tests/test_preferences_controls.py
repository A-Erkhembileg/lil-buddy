"""Saved preferences, size choices, and quiet desktop controls."""
import os
from pathlib import Path
import tempfile
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QPoint, QPointF, QSettings
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication

import desktop_cat as pet


class PreferencesAndControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lil-buddy-prefs-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "preferences.ini"
        self.created = []
        self.cat = self.new_cat()

    def tearDown(self):
        for cat in reversed(self.created):
            cat.close()
            cat.deleteLater()
        self.app.processEvents()

    def new_cat(self):
        settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        cat = pet.DesktopCat(settings=settings, enable_tray=False)
        cat.timer.stop()
        cat.fullscreen_timer.stop()
        self.created.append(cat)
        return cat

    def test_chosen_options_survive_a_fresh_settings_reader(self):
        self.cat.set_palette("Tuxedo Black")
        self.cat.set_animal_type("Fox")
        self.cat.set_pet_size("Large")
        self.cat._toggle_roaming(False)
        self.cat.set_paused(True)
        self.cat._save_preferences()
        restored = self.new_cat()
        self.assertEqual(restored.palette_name, "Tuxedo Black")
        self.assertEqual(restored.animal_type, "Fox")
        self.assertEqual(restored.size_name, "Large")
        self.assertFalse(restored.motion.roaming)
        self.assertTrue(restored._paused)
        self.assertFalse(restored.timer.isActive())

    def test_custom_rgb_round_trip_keeps_the_exact_selected_color(self):
        color = QColor(67, 132, 209)
        self.cat.set_custom_color(color)
        self.cat._save_preferences()
        restored = self.new_cat()
        self.assertEqual(restored.palette_name, "Custom RGB")
        self.assertEqual(QColor(restored.palette.fur), color)

    def test_unconfirmed_custom_rgb_preview_is_not_saved(self):
        self.cat.set_palette("Tuxedo Black")
        self.cat._save_preferences()
        self.cat._choose_custom_color()
        dialog = self.cat._color_dialog
        dialog.setCurrentColor(QColor(210, 60, 130))
        self.assertEqual(self.cat.palette_name, "Custom RGB")
        restored_during_preview = self.new_cat()
        self.assertEqual(restored_during_preview.palette_name, "Tuxedo Black")
        dialog.reject()
        self.assertEqual(self.cat.palette_name, "Tuxedo Black")

    def test_accepted_custom_rgb_preview_is_saved(self):
        self.cat._choose_custom_color()
        color = QColor(42, 155, 200)
        self.cat._color_dialog.setCurrentColor(color)
        self.cat._color_dialog.accept()
        restored = self.new_cat()
        self.assertEqual(restored.palette_name, "Custom RGB")
        self.assertEqual(QColor(restored.palette.fur), color)

    def test_last_normal_position_survives_a_latched_corner_save(self):
        area = self.cat.available_area()
        self.cat.motion.state = "rest"
        self.cat.motion.roaming = False
        self.cat.motion.x = area.left() + 80
        self.cat.motion.y = area.bottom() - self.cat.height()
        self.cat.move(round(self.cat.motion.x), round(self.cat.motion.y))
        self.cat._save_preferences()
        normal_position = QPoint(self.cat.pos())
        self.cat._set_latched(True, now=100.0)
        self.assertNotEqual(self.cat.pos(), normal_position)
        self.cat._save_preferences()
        restored = self.new_cat()
        self.assertEqual(restored.pos(), normal_position)
        self.assertFalse(restored._latched)

    def test_hidden_choice_does_not_make_the_next_launch_invisible(self):
        self.cat.show()
        self.cat.set_pet_visible(False)
        self.assertTrue(self.cat._user_hidden)
        self.cat._save_preferences()
        restored = self.new_cat()
        self.assertFalse(restored._user_hidden)

    def test_each_size_changes_drawing_and_collision_dimensions_together(self):
        self.cat.motion.state, self.cat.motion.sleeping = "rest", False
        for name in ("Small", "Medium", "Large"):
            with self.subTest(size=name):
                self.cat.set_pet_size(name)
                expected = (pet.ART_WIDTH * pet.SIZES[name], pet.ART_HEIGHT * pet.SIZES[name])
                self.assertEqual((self.cat.width(), self.cat.height()), expected)
                self.assertEqual((self.cat.motion.width, self.cat.motion.height), expected)
                self.assertEqual(self.cat._pixmap.size(), self.cat.size())
                area = self.cat.available_area()
                right, floor = area.right() - expected[0], area.bottom() - expected[1]
                self.cat.motion.x, self.cat.motion.y = right - 0.1, floor
                self.cat.motion.direction, self.cat.motion.state = 1, "walk"
                self.cat.motion.decision_in = 10
                self.cat.motion.step(1 / 60, area, self.cat.rng)
                self.assertEqual(self.cat.motion.x, right)
                self.assertEqual(self.cat.motion.direction, -1)
                self.assertEqual(self.cat.motion.y, floor)

    def test_resizing_a_grounded_pet_keeps_its_feet_on_the_floor(self):
        self.cat.back_to_floor()
        for name in ("Large", "Small", "Medium"):
            with self.subTest(size=name):
                self.cat.set_pet_size(name)
                self.assertEqual(self.cat.motion.y + self.cat.height(), self.cat.available_area().bottom())

    def test_paused_pet_stays_paused_after_fullscreen_exit(self):
        self.cat.fullscreen_timer.start()
        self.cat.set_paused(True)
        before = (self.cat.motion.x, self.cat.motion.y, self.cat._animation_time)
        self.cat._tick(now=self.cat._last_time + 0.1)
        self.assertEqual((self.cat.motion.x, self.cat.motion.y, self.cat._animation_time), before)
        self.assertFalse(self.cat.timer.isActive())
        self.assertTrue(self.cat.fullscreen_timer.isActive())
        self.cat._set_latched(True, now=100.0)
        self.cat._set_latched(False, now=200.0)
        self.assertTrue(self.cat._paused)
        self.assertFalse(self.cat.timer.isActive())

    def test_hidden_pet_stays_hidden_after_fullscreen_exit(self):
        self.cat.show()
        self.cat.set_pet_visible(False)
        self.cat._set_latched(True, now=100.0)
        self.cat._set_latched(False, now=200.0)
        self.assertTrue(self.cat._user_hidden)
        self.assertFalse(self.cat.isVisible())
        self.assertFalse(self.cat.timer.isActive())
        self.cat.set_pet_visible(True)
        self.assertTrue(self.cat.isVisible())
        self.assertTrue(self.cat.timer.isActive())

    def test_tray_menu_callbacks_offer_size_pause_and_visibility(self):
        menu = self.cat._build_menu(tray=True)
        self.addCleanup(menu.deleteLater)
        actions = {action.text(): action for action in menu.actions()}
        sizes = {action.text(): action for action in actions["Pet Size"].menu().actions()}
        self.assertEqual(set(sizes), {"Small", "Medium", "Large"})
        for name in ("Large", "Small", "Medium"):
            sizes[name].trigger()
            self.assertEqual(self.cat.size_name, name)
        actions["Pause"].trigger()
        self.assertTrue(self.cat._paused)
        actions["Pause"].trigger()
        self.assertFalse(self.cat._paused)
        actions["Show Pet"].trigger()
        self.assertTrue(self.cat._user_hidden)
        actions["Show Pet"].trigger()
        self.assertFalse(self.cat._user_hidden)

    def test_optional_habit_and_cursor_choices_are_saved(self):
        menu = self.cat._build_menu(tray=True)
        self.addCleanup(menu.deleteLater)
        actions = {action.text(): action for action in menu.actions()}
        self.assertTrue(self.cat._habits_enabled)
        self.assertFalse(self.cat._cursor_interaction)
        actions["Idle habits"].trigger()
        actions["Watch cursor"].trigger()
        self.cat._save_preferences()
        restored = self.new_cat()
        self.assertFalse(restored._habits_enabled)
        self.assertTrue(restored._cursor_interaction)

    def test_cursor_watching_is_never_chosen_when_it_is_disabled(self):
        self.cat.motion.state, self.cat.motion.sleeping = "rest", False
        self.cat.motion.roaming = True
        self.cat._cursor_interaction = False
        self.cat.rng.seed(29)
        observed = set()
        for _ in range(30):
            self.cat._habit = None
            self.cat._choose_habit()
            self.assertIn(self.cat._habit, {"groom", "yawn", "stretch"})
            self.assertGreater(self.cat._habit_duration, 0)
            self.assertLess(self.cat._habit_duration, 10)
            observed.add(self.cat._habit)
        self.assertGreater(len(observed), 1)

    def test_finite_habit_pauses_decisions_then_returns_to_normal_idle(self):
        self.cat.motion.state, self.cat.motion.sleeping = "rest", False
        self.cat.motion.decision_in = 3.0
        self.cat._habit, self.cat._habit_elapsed, self.cat._habit_duration = "groom", 0.0, 0.12
        self.cat._last_time = 100.0
        self.cat._tick(now=100.05)
        self.assertEqual(self.cat._habit, "groom")
        self.assertEqual(self.cat._frame_key[0], "groom")
        self.assertEqual(self.cat.motion.decision_in, 3.0)
        self.cat._tick(now=100.10)
        self.cat._tick(now=100.15)
        self.assertIsNone(self.cat._habit)
        self.cat._tick(now=100.20)
        self.assertLess(self.cat.motion.decision_in, 3.0)

    def test_user_controls_and_fullscreen_interrupt_an_idle_habit(self):
        actions = (
            ("petting", lambda cat: cat.pet()),
            ("pause", lambda cat: cat.set_paused(True)),
            ("hide", lambda cat: cat.set_pet_visible(False)),
            ("fullscreen", lambda cat: cat._set_latched(True)),
        )
        for label, action in actions:
            with self.subTest(action=label):
                cat = self.new_cat()
                cat.set_paused(False)
                cat.set_pet_visible(True)
                cat.motion.state, cat.motion.sleeping = "rest", False
                cat._habit, cat._habit_elapsed, cat._habit_duration = "groom", 0.0, 2.0
                action(cat)
                self.assertIsNone(cat._habit)

    def test_resizing_a_latched_pet_keeps_the_static_latched_state(self):
        self.cat._set_latched(True, now=100.0)
        self.cat.set_pet_size("Large")
        self.assertEqual(self.cat.motion.state, "latched")
        self.assertTrue(self.cat._latched)
        self.assertFalse(self.cat.timer.isActive())
        full = self.cat.display.geometry()
        self.assertEqual(self.cat.pos(), QPoint(full.x() + full.width() - self.cat.width(), full.y()))

    def test_resizing_during_drag_does_not_snap_on_the_next_pointer_event(self):
        self.cat.back_to_floor()
        click = QPointF(self.cat.pos()) + QPointF(75, 60)
        self.cat._press_position = click
        self.cat._drag_offset = QPointF(75, 60)
        self.cat._drag_sample_position, self.cat._drag_sample_time = click, 100.0
        pointer = click + QPointF(0, -100)
        self.cat._drag_to(pointer, now=100.05)
        self.cat.set_pet_size("Large")
        resized_position = QPoint(self.cat.pos())
        self.cat._drag_to(pointer, now=100.1)
        self.assertEqual(self.cat.pos(), resized_position)

    def test_resizing_during_a_landing_keeps_the_visual_on_the_floor(self):
        floor = self.cat.motion.bounds(self.cat.available_area())[3]
        self.cat.motion.state, self.cat.motion.vy = "fall", -70
        self.cat.motion.y = floor - 10
        self.cat.move(round(self.cat.motion.x), round(floor))
        self.cat._landing_elapsed = pet.LAND_FRAME_SECONDS
        self.cat.set_pet_size("Large")
        self.assertEqual(self.cat.y(), round(self.cat.motion.bounds(self.cat.available_area())[3]))
        self.assertEqual(self.cat._frame_key[0], "land")

    def test_hidden_fullscreen_cycle_does_not_save_an_unseen_corner_position(self):
        area = self.cat.available_area()
        self.cat.motion.x = area.left() + 80
        self.cat.motion.y = area.bottom() - self.cat.height()
        self.cat.move(round(self.cat.motion.x), round(self.cat.motion.y))
        self.cat._save_preferences()
        normal_position = QPoint(self.cat.pos())
        self.cat.set_pet_visible(False)
        self.cat._set_latched(True, now=100.0)
        self.cat._set_latched(False, now=200.0)
        self.cat._save_preferences()
        restored = self.new_cat()
        self.assertEqual(restored.pos(), normal_position)

    def test_an_existing_tray_menu_refreshes_checks_from_current_choices(self):
        menu = self.cat._build_menu(tray=True)
        self.addCleanup(menu.deleteLater)
        self.cat.set_palette("Tuxedo Black")
        self.cat.set_animal_type("Fox")
        self.cat.set_pet_size("Small")
        self.cat.set_paused(True)
        self.cat.set_pet_visible(False)
        self.cat._sync_menu_checks(menu)
        actions = {action.text(): action for action in menu.actions()}
        for label, chosen in (("Change Color", "Tuxedo Black"), ("Animal Type", "Fox"),
                              ("Pet Size", "Small")):
            checked = [action.text() for action in actions[label].menu().actions() if action.isChecked()]
            self.assertEqual(checked, [chosen])
        self.assertTrue(actions["Pause"].isChecked())
        self.assertFalse(actions["Show Pet"].isChecked())


if __name__ == "__main__":
    unittest.main()

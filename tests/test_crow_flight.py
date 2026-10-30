"""Crow flight dynamics and the shared desktop-pet interaction contract."""
import math
import os
from pathlib import Path
import random
import tempfile
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QEvent, QPointF, QRectF, QSettings, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

import desktop_cat as pet


class FlightDynamicsTests(unittest.TestCase):
    def setUp(self):
        self.area = QRectF(-1440, -120, 1440, 900)
        self.rng = random.Random(23)
        self.motion = pet.Motion(x=-700, roaming=False)
        self.left, self.right, self.top, self.floor = self.motion.bounds(self.area)
        self.motion.y = self.floor

    def test_takeoff_starts_with_momentum_without_teleporting(self):
        original = self.motion.x, self.motion.y
        self.motion.begin_flight(self.area, self.rng)
        self.assertEqual(self.motion.state, "fly")
        self.assertEqual((self.motion.x, self.motion.y), original)
        self.assertLess(self.motion.vy, 0)
        self.assertNotEqual(self.motion.vx, 0)
        self.assertGreater(self.motion.flight_duration, 0)
        self.motion.step(1 / 120, self.area, self.rng)
        self.assertLess(self.motion.y, self.floor)
        self.assertNotEqual(self.motion.x, original[0])

    def test_a_complete_flight_climbs_cruises_and_lands_inside_negative_monitor_bounds(self):
        self.motion.begin_flight(self.area, self.rng)
        highest_y = self.motion.y
        positions = []
        for _ in range(15 * 120):
            before = self.motion.x, self.motion.y
            self.motion.step(1 / 120, self.area, self.rng)
            self.assertTrue(all(math.isfinite(v) for v in
                                (self.motion.x, self.motion.y, self.motion.vx, self.motion.vy)))
            self.assertGreaterEqual(self.motion.x, self.left)
            self.assertLessEqual(self.motion.x, self.right)
            self.assertGreaterEqual(self.motion.y, self.top)
            self.assertLessEqual(self.motion.y, self.floor)
            self.assertLess(math.hypot(self.motion.x - before[0], self.motion.y - before[1]), 10)
            highest_y = min(highest_y, self.motion.y)
            positions.append((self.motion.x, self.motion.y))
            if self.motion.state != "fly":
                break
        self.assertEqual(self.motion.state, "rest")
        self.assertEqual(self.motion.y, self.floor)
        self.assertEqual(self.motion.vy, 0)
        self.assertLess(highest_y, self.floor - 25)
        self.assertGreater(max(x for x, _ in positions) - min(x for x, _ in positions), 30)

    def test_both_horizontal_edges_turn_a_flying_crow_back_inward(self):
        for edge, direction in ((self.left, -1), (self.right, 1)):
            with self.subTest(direction=direction):
                motion = pet.Motion(x=edge, y=self.floor - 100, direction=direction)
                motion.begin_flight(self.area, random.Random(7))
                motion.direction, motion.vx = direction, direction * 90
                for _ in range(120):
                    motion.step(1 / 120, self.area, self.rng)
                    if motion.direction == -direction:
                        break
                self.assertEqual(motion.direction, -direction)
                self.assertGreaterEqual(motion.x, self.left)
                self.assertLessEqual(motion.x, self.right)

    def test_geometry_constraints_preserve_an_active_flight(self):
        self.motion.begin_flight(self.area, self.rng)
        self.motion.y -= 80
        self.motion.width, self.motion.height = 160, 128
        self.motion.constrain(self.area)
        self.assertEqual(self.motion.state, "fly")
        left, right, top, floor = self.motion.bounds(self.area)
        self.assertTrue(left <= self.motion.x <= right)
        self.assertTrue(top <= self.motion.y <= floor)


class CrowInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.created = []
        self.cat = self.new_pet()

    def new_pet(self, settings=None):
        animal = pet.DesktopCat(settings=settings, enable_tray=False)
        animal.timer.stop()
        animal.fullscreen_timer.stop()
        animal.set_animal_type("Crow")
        animal.back_to_floor()
        self.created.append(animal)
        return animal

    def tearDown(self):
        for animal in reversed(self.created):
            animal.close()
            animal.deleteLater()
        self.app.processEvents()

    def test_registry_replaces_bird_with_crow_and_defaults_to_a_natural_coat(self):
        self.assertEqual(set(pet.DRAWERS), {"Cat", "Dog", "Fox", "Crow"})
        self.assertNotIn("Bird", pet.ANIMAL_QUOTES)
        self.assertEqual(self.cat.palette_name, "Natural Coat")

    def test_manual_takeoff_works_without_enabling_autonomous_roaming(self):
        self.cat._toggle_roaming(False)
        self.assertTrue(self.cat.start_crow_flight())
        self.assertEqual(self.cat.motion.state, "fly")
        self.assertFalse(self.cat.motion.roaming)

    def test_only_an_available_crow_can_start_flying(self):
        for animal in ("Cat", "Dog", "Fox"):
            with self.subTest(animal=animal):
                self.cat.set_animal_type(animal)
                self.assertFalse(self.cat.start_crow_flight())
        for name, disable in (("paused", lambda c: c.set_paused(True)),
                              ("hidden", lambda c: c.set_pet_visible(False)),
                              ("latched", lambda c: c._set_latched(True)),
                              ("held", lambda c: setattr(c.motion, "state", "held"))):
            with self.subTest(unavailable=name):
                animal = self.new_pet()
                disable(animal)
                self.assertFalse(animal.start_crow_flight())

    def test_flight_animation_has_eight_distinct_wing_phases(self):
        self.assertTrue(self.cat.start_crow_flight())
        images = []
        for frame in range(8):
            self.cat.motion.flight_time = frame * pet.CROW_FLAP_SECONDS + 0.001
            self.cat._set_frame()
            self.assertEqual(self.cat._frame_key[:2], ("fly", frame))
            image = self.cat._pixmap.toImage()
            images.append(bytes(image.constBits().asstring(image.sizeInBytes())))
        self.assertEqual(len(set(images)), 8)

    def test_pet_reaction_does_not_freeze_flight_physics(self):
        self.cat.start_crow_flight()
        self.cat._last_time = 100.0
        self.cat.pet(now=100.0)
        old_time = self.cat.motion.flight_time
        self.cat._tick(now=100.1)
        self.assertEqual(self.cat.motion.state, "fly")
        self.assertGreater(self.cat.motion.flight_time, old_time)
        self.assertTrue(self.cat.bubble.isVisible())

    def test_pause_freezes_flight_without_stopping_fullscreen_monitor(self):
        self.cat.start_crow_flight()
        self.cat.fullscreen_timer.start()
        self.cat.set_paused(True)
        snapshot = self.cat.motion.x, self.cat.motion.y, self.cat.motion.flight_time
        self.cat._tick(now=self.cat._last_time + 1)
        self.assertEqual((self.cat.motion.x, self.cat.motion.y, self.cat.motion.flight_time), snapshot)
        self.assertFalse(self.cat.timer.isActive())
        self.assertTrue(self.cat.fullscreen_timer.isActive())
        self.cat.set_paused(False)
        self.cat._tick(now=self.cat._last_time + 0.05)
        self.assertGreater(self.cat.motion.flight_time, snapshot[2])

    def test_fullscreen_interrupts_a_flight_and_releases_it_into_gravity(self):
        self.cat.start_crow_flight()
        self.cat._set_latched(True, now=100.0)
        self.assertEqual(self.cat.motion.state, "latched")
        flight_time = self.cat.motion.flight_time
        self.cat._tick(now=200.0)
        self.assertEqual(self.cat.motion.flight_time, flight_time)
        self.cat._set_latched(False, now=300.0)
        self.assertEqual(self.cat.motion.state, "fall")

    def test_switching_to_a_canine_during_flight_restores_gravity(self):
        self.cat.start_crow_flight()
        for _ in range(24):
            self.cat.motion.step(1 / 120, self.cat.available_area(), self.cat.rng)
        self.cat.set_animal_type("Dog")
        self.assertEqual(self.cat.motion.state, "fall")
        old_y = self.cat.motion.y
        self.cat._tick(now=self.cat._last_time + 0.05)
        self.assertGreater(self.cat.motion.y, old_y)

    def test_natural_crow_has_dark_feathers_without_neon_cat_eyes(self):
        image = pet.draw_crow("rest", 0, 1, palette=pet.PALETTES["Natural Coat"]).toImage()
        opaque = [image.pixelColor(x, y) for y in range(image.height())
                  for x in range(image.width()) if image.pixelColor(x, y).alpha()]
        self.assertTrue(opaque)
        self.assertGreater(sum(max(color.red(), color.green(), color.blue()) < 100
                               for color in opaque), len(opaque) * 0.65)
        self.assertFalse(any(color.green() > 170 and color.green() - color.red() > 40
                             and color.green() - color.blue() > 40 for color in opaque))

    def test_natural_fox_keeps_a_white_tail_tip_and_russet_fur(self):
        image = pet.draw_fox("rest", 0, 1, palette=pet.PALETTES["Natural Coat"]).toImage()
        white_tail = 0
        russet = 0
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                if not color.alpha():
                    continue
                if x < image.width() // 3 and min(color.red(), color.green(), color.blue()) > 200:
                    white_tail += 1
                if color.red() > 110 and color.red() > color.green() * 1.3 and color.red() > color.blue() * 1.5:
                    russet += 1
        self.assertGreater(white_tail, 8)
        self.assertGreater(russet, 40)

    def test_resizing_a_flying_crow_preserves_flight_and_matches_collision_dimensions(self):
        self.cat.start_crow_flight()
        self.cat.motion.step(0.1, self.cat.available_area(), self.cat.rng)
        self.cat.set_pet_size("Large")
        self.assertEqual(self.cat.motion.state, "fly")
        self.assertEqual((self.cat.motion.width, self.cat.motion.height),
                         (self.cat.width(), self.cat.height()))
        self.assertEqual(self.cat._pixmap.size(), self.cat.size())

    def test_dragging_crow_high_then_releasing_starts_flight_only_when_roaming_is_enabled(self):
        for roaming in (False, True):
            with self.subTest(roaming=roaming):
                self.cat.back_to_floor()
                self.cat.motion.roaming = roaming
                click = QPointF(self.cat.pos()) + QPointF(75, 60)
                release = click + QPointF(0, -180)
                for kind, point, button, buttons in (
                        (QEvent.Type.MouseButtonPress, click, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
                        (QEvent.Type.MouseMove, release, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
                        (QEvent.Type.MouseButtonRelease, release, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)):
                    event = QMouseEvent(kind, QPointF(75, 60), point, button, buttons,
                                        Qt.KeyboardModifier.NoModifier)
                    QApplication.sendEvent(self.cat, event)
                self.assertEqual(self.cat.motion.state, "fly" if roaming else "fall")

    def test_old_bird_preferences_migrate_without_losing_other_choices(self):
        with tempfile.TemporaryDirectory(prefix="lil-buddy-crow-migration-") as directory:
            path = Path(directory) / "preferences.ini"
            settings = QSettings(str(path), QSettings.Format.IniFormat)
            settings.setValue("appearance/animal", "Bird")
            settings.setValue("appearance/palette", "Charcoal Grey")
            settings.setValue("appearance/size", "Small")
            settings.setValue("behavior/roaming", False)
            settings.sync()
            restored = pet.DesktopCat(settings=QSettings(str(path), QSettings.Format.IniFormat), enable_tray=False)
            self.created.append(restored)
            restored.timer.stop()
            restored.fullscreen_timer.stop()
            self.assertEqual(restored.animal_type, "Crow")
            self.assertEqual(restored.palette_name, "Natural Coat")
            self.assertEqual(restored.size_name, "Small")
            self.assertFalse(restored.motion.roaming)
            restored._save_preferences()
            current = QSettings(str(path), QSettings.Format.IniFormat)
            self.assertEqual(current.value("appearance/animal"), "Crow")

    def test_an_explicit_charcoal_crow_color_survives_reselection_and_restart(self):
        with tempfile.TemporaryDirectory(prefix="lil-buddy-crow-coat-") as directory:
            path = Path(directory) / "preferences.ini"
            animal = self.new_pet(QSettings(str(path), QSettings.Format.IniFormat))
            animal.set_palette("Charcoal Grey")
            animal.set_animal_type("Crow")
            self.assertEqual(animal.palette_name, "Charcoal Grey")
            animal._save_preferences()
            restored = pet.DesktopCat(settings=QSettings(str(path), QSettings.Format.IniFormat), enable_tray=False)
            self.created.append(restored)
            restored.timer.stop()
            restored.fullscreen_timer.stop()
            self.assertEqual(restored.animal_type, "Crow")
            self.assertEqual(restored.palette_name, "Charcoal Grey")

    def test_takeoff_control_tracks_the_current_species_and_availability(self):
        menu = self.cat._build_menu(tray=True)
        self.addCleanup(menu.deleteLater)
        action = next(action for action in menu.actions() if action.text() == "Take flight")
        self.assertTrue(action.isVisible())
        self.assertTrue(action.isEnabled())
        self.cat.set_animal_type("Dog")
        self.cat._sync_menu_checks(menu)
        self.assertFalse(action.isVisible())
        self.cat.set_animal_type("Crow")
        self.cat.set_paused(True)
        self.cat._sync_menu_checks(menu)
        self.assertTrue(action.isVisible())
        self.assertFalse(action.isEnabled())


if __name__ == "__main__":
    unittest.main()

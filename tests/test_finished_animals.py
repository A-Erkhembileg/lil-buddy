"""Completed species art, habits, and personality text stay within the shared engine contract."""
import os
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QTransform
from PyQt6.QtWidgets import QApplication

import desktop_cat as pet


def pixels(image):
    return bytes(image.constBits().asstring(image.sizeInBytes()))


class FinishedAnimalArtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_every_animal_has_its_own_resting_silhouette(self):
        pictures = {name: pixels(draw("rest", 0, 1).toImage()) for name, draw in pet.DRAWERS.items()}
        self.assertEqual(len(set(pictures.values())), len(pet.DRAWERS))

    def test_all_poses_share_a_nonempty_transparent_canvas(self):
        poses = (("rest", 1), ("blink", 1), ("walk", 2), ("sleep", 2),
                 ("held", 4), ("fall", 4), ("petted", 4), ("land", 3),
                 ("groom", 4), ("yawn", 4), ("stretch", 4), ("watch", 4))
        for name, draw in pet.DRAWERS.items():
            for pose, count in poses:
                for frame in range(count):
                    with self.subTest(animal=name, pose=pose, frame=frame):
                        pixmap = draw(pose, frame, 1, stretch=2)
                        self.assertEqual((pixmap.width(), pixmap.height()), (pet.WIDTH, pet.HEIGHT))
                        self.assertFalse(pixmap.mask().isNull())
                        self.assertEqual(pixmap.toImage().pixelColor(0, 0).alpha(), 0)

    def test_every_species_has_four_distinct_airborne_and_petting_poses(self):
        for name, draw in pet.DRAWERS.items():
            for pose in ("held", "fall", "petted"):
                with self.subTest(animal=name, pose=pose):
                    pictures = {pixels(draw(pose, frame, 1, stretch=2).toImage()) for frame in range(4)}
                    self.assertEqual(len(pictures), 4)

    def test_three_landing_frames_are_distinct_and_keep_the_feet_on_the_floor(self):
        for name, draw in pet.DRAWERS.items():
            pictures = []
            for frame in range(3):
                with self.subTest(animal=name, frame=frame):
                    image = draw("land", frame, 1).toImage()
                    self.assertTrue(any(image.pixelColor(x, image.height()-1).alpha()
                                        for x in range(image.width())))
                    pictures.append(pixels(image))
            self.assertEqual(len(set(pictures)), 3)

    def test_presets_and_custom_rgb_keep_the_selected_fur_in_every_species(self):
        palettes = [palette for name, palette in pet.PALETTES.items() if name != "Natural Coat"]
        palettes += [pet.custom_palette(QColor(71, 130, 208))]
        for name, draw in pet.DRAWERS.items():
            for palette in palettes:
                for pose in ("rest", "held", "petted", "groom", "yawn", "stretch", "watch"):
                    with self.subTest(animal=name, fur=palette.fur, pose=pose):
                        image = draw(pose, 1, 1, palette=palette).toImage().scaled(
                            pet.ART_WIDTH, pet.ART_HEIGHT, Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.FastTransformation)
                        color = QColor(palette.fur)
                        self.assertTrue(any(image.pixelColor(x, y) == color
                                            for y in range(image.height()) for x in range(image.width())))

    def test_walking_and_habits_mirror_with_the_direction_of_travel(self):
        for name, draw in pet.DRAWERS.items():
            for pose in ("walk", "groom", "yawn", "stretch", "watch"):
                with self.subTest(animal=name, pose=pose):
                    right = draw(pose, 1, 1, dangle=-1).toImage()
                    left = draw(pose, 1, -1, dangle=-1).toImage()
                    self.assertEqual(left, right.transformed(QTransform().scale(-1, 1)))

    def test_habits_show_a_different_pose_than_resting(self):
        for name, draw in pet.DRAWERS.items():
            resting = draw("rest", 0, 1).toImage()
            for pose in pet.HABIT_DURATIONS:
                with self.subTest(animal=name, pose=pose):
                    self.assertNotEqual(draw(pose, 1, 1, dangle=-1).toImage(), resting)

    def test_corner_faces_are_distinct_and_touch_the_exact_top_and_right_edges(self):
        pictures = []
        for name in pet.DRAWERS:
            image = pet.draw_peek(name, pet.PALETTES["Charcoal Grey"]).toImage()
            self.assertTrue(any(image.pixelColor(x, 0).alpha() for x in range(image.width())), name)
            self.assertTrue(any(image.pixelColor(image.width()-1, y).alpha() for y in range(image.height())), name)
            self.assertFalse(any(image.pixelColor(x, pet.HEIGHT-1).alpha() for x in range(image.width())), name)
            pictures.append(pixels(image))
        self.assertEqual(len(set(pictures)), len(pet.DRAWERS))

    def test_each_personality_quote_can_be_drawn_by_the_embedded_pixel_font(self):
        self.assertEqual(set(pet.ANIMAL_QUOTES), set(pet.DRAWERS))
        for name, quotes in pet.ANIMAL_QUOTES.items():
            with self.subTest(animal=name):
                self.assertGreaterEqual(len(quotes), 4)
                self.assertEqual(len(set(quotes)), len(quotes))
                self.assertFalse({letter for quote in quotes for letter in quote.upper()} - pet.PIXEL_FONT.keys())
                self.assertTrue(all(0 < len(quote) < 100 for quote in quotes))
        self.assertEqual(pet.ANIMAL_QUOTES["Cat"], pet.CAT_QUOTES)


class AnimalHabitDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.cat = pet.DesktopCat(enable_tray=False)
        self.cat.timer.stop()
        self.cat.fullscreen_timer.stop()
        self.cat.motion.state, self.cat.motion.sleeping = "rest", False
        self.cat._pet_until, self.cat._landing_elapsed = 0, None

    def tearDown(self):
        self.cat.close()
        self.cat.deleteLater()
        self.app.processEvents()

    def test_engine_dispatches_each_habit_to_each_species_at_every_size(self):
        for size in pet.SIZES:
            self.cat.set_pet_size(size)
            for name in pet.DRAWERS:
                self.cat.set_animal_type(name)
                for habit in pet.HABIT_DURATIONS:
                    with self.subTest(size=size, animal=name, habit=habit):
                        self.cat._habit, self.cat._habit_elapsed = habit, 0.2
                        self.cat._set_frame()
                        self.assertEqual(self.cat._frame_key[0], habit)
                        self.assertEqual(self.cat._pixmap.size(), self.cat.size())
                        self.assertFalse(self.cat.mask().isEmpty())

    def test_cursor_attention_changes_direction_and_pupil_height(self):
        self.cat._habit, self.cat._habit_elapsed = "watch", 0.2
        center = self.cat.geometry().center()
        for location, direction, vertical in ((center+QPoint(-100, -100), -1, -1),
                                               (center+QPoint(100, 100), 1, 1)):
            with patch.object(pet.QCursor, "pos", return_value=location):
                self.cat._set_frame()
            self.assertEqual(self.cat._frame_key[2], direction)
            self.assertEqual(self.cat._frame_key[5], vertical)

    def test_pet_reaction_uses_the_selected_species_personality(self):
        for name in pet.DRAWERS:
            self.cat.set_animal_type(name)
            self.cat.pet(now=100.0)
            self.assertIn(self.cat.bubble.text, pet.ANIMAL_QUOTES[name])
            self.assertEqual(self.cat._habit, None)


if __name__ == "__main__":
    unittest.main()

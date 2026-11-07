"""Source atlases, continuous motion, and bounded warm rendering."""
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QImage, QPixmap, QRegion
from PyQt6.QtWidgets import QApplication

import desktop_cat as pet


ANIMALS = ("Cat", "Dog", "Fox")
MOTION_ART = Path(pet.__file__).resolve().parent / "artwork" / "motion"


def fingerprint(image):
    return hashlib.sha256(image.constBits().asstring(image.sizeInBytes())).digest()


def trim_position(image):
    """Ignore whole-image translation when checking actual secondary motion."""
    bounds = QRegion(QPixmap.fromImage(image).mask()).boundingRect()
    return image.copy(bounds)


class FluidClipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_native_motion_manifests_preserve_registration_and_complete_clips(self):
        expected = {"walk": 16, "idle": 4, "pet": 6, "air": 6}
        for animal in ANIMALS:
            with self.subTest(animal=animal):
                manifest = json.loads((MOTION_ART / f"{animal.lower()}.json").read_text())
                self.assertEqual(manifest["cell_size"], [400, 320])
                self.assertIsInstance(manifest["registration"], dict)
                registration = manifest["registration"]["walk"]
                self.assertGreater(registration["scale"], 0)
                self.assertEqual(len(registration["frames"]), 16)
                anchors = [frame["canvas_head_anchor"][0] for frame in registration["frames"]]
                self.assertLess(max(anchors) - min(anchors), 3.0,
                                "A swinging tail must not recenter the whole body.")
                atlas = QImage(str(MOTION_ART / manifest["image"]))
                self.assertFalse(atlas.isNull())
                for clip, count in expected.items():
                    self.assertEqual(len(manifest["clips"][clip]), count, clip)
                for frame in manifest["frames"]:
                    x, y, width, height = frame["rect"] if isinstance(frame, dict) else frame
                    self.assertEqual((width, height), (400, 320))
                    self.assertGreaterEqual(min(x, y), 0)
                    self.assertLessEqual(x + width, atlas.width())
                    self.assertLessEqual(y + height, atlas.height())

    def test_live_walk_is_continuous_and_closes_its_loop(self):
        for animal in ANIMALS:
            images = [pet.animal_frame_image(animal, "walk", 0, motion_phase=step/64)
                      for step in range(65)]
            self.assertEqual(images[0], images[-1])
            self.assertGreater(len({fingerprint(image) for image in images}), 60)
            # Whole-body changes between independent sheets are no longer used.
            areas = []
            for image in images:
                rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
                raw = rgba.constBits().asstring(rgba.sizeInBytes())
                areas.append(sum(v > 80 for v in raw[3::4]))
            self.assertLess(max(areas) / min(areas), 1.16)

    def test_husky_does_not_jump_sideways_at_sheet_row_boundaries(self):
        from PIL import Image
        manifest = json.loads((MOTION_ART / "dog.json").read_text())
        noses = []
        with Image.open(MOTION_ART / manifest["image"]) as atlas:
            for position in manifest["registration"]["drawn_walk"]["key_positions"]:
                index = manifest["clips"]["drawn_walk"][position]
                x, y, width, height = manifest["frames"][index]
                alpha = atlas.crop((x, y, x + width, y + height)).getchannel("A")
                noses.append(alpha.point(lambda v: 255 if v > 80 else 0).getbbox()[2])
        self.assertLessEqual(max(abs(x - y) for x, y in zip(noses, noses[1:] + noses[:1])), 8)

    def test_archived_sixteen_pose_source_clip_remains_available_to_art_tools(self):
        for animal in ANIMALS:
            with self.subTest(animal=animal):
                bases, clips = pet._sprite_bases(animal)
                indices = clips["walk"]
                self.assertEqual(len(indices), 16)
                self.assertEqual(len({fingerprint(bases[index]) for index in indices}), 16)
                self.assertTrue(all((bases[index].width(), bases[index].height()) == (400, 320)
                                    for index in indices))

    def test_baked_loading_does_not_repeat_sheet_cutout_or_legacy_preprocessing(self):
        with patch.object(pet, "_SPRITE_BASES", {}), \
                patch.object(pet, "_MOTION_BASES", {}), \
                patch.object(pet, "_sprite_cutout", side_effect=AssertionError("Runtime sheet cutout")), \
                patch.object(pet, "_legacy_sprite_bases", side_effect=AssertionError("Legacy preprocessing")):
            for animal in ANIMALS:
                with self.subTest(animal=animal):
                    bases, clips = pet._sprite_bases(animal)
                    self.assertGreaterEqual(len(bases), 32)
                    self.assertEqual(len(clips["walk"]), 16)

    def test_continuous_phase_moves_art_within_one_drawn_idle_pose(self):
        for animal in ANIMALS:
            with self.subTest(animal=animal):
                first = pet.animal_frame_image(animal, "rest", 0, motion_phase=0.08, effects=False)
                second = pet.animal_frame_image(animal, "rest", 0, motion_phase=0.31, effects=False)
                self.assertNotEqual(first, second)
                self.assertNotEqual(trim_position(first), trim_position(second),
                                    "Continuous motion must do more than slide the whole image.")


class FluidPlaybackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.cat = pet.DesktopCat(enable_tray=False)
        self.cat.timer.stop()
        self.cat.fullscreen_timer.stop()
        self.area = QRectF(0, 0, 5000, 1000)
        self.area_patch = patch.object(self.cat, "available_area", return_value=self.area)
        self.area_patch.start()
        self.cat._habit = None
        self.cat._habits_enabled = False
        self.cat._pet_until = 0.0
        self.cat._landing_elapsed = None

    def tearDown(self):
        self.area_patch.stop()
        self.cat.close()
        self.cat.deleteLater()
        self.app.processEvents()

    def start_walking(self, animal):
        self.cat._animation_time = 0.0
        self.cat.set_animal_type(animal)
        self.cat.timer.stop()
        self.cat.motion.state, self.cat.motion.sleeping = "walk", False
        self.cat.motion.roaming = True
        self.cat.motion.direction = 1
        self.cat.motion.decision_in = 999.0
        self.cat.motion.x = 2000.0
        self.cat.motion.y = self.cat.motion.bounds(self.area)[3]
        self.cat._last_area = self.area
        self.cat._last_time = 100.0
        self.cat._set_frame()

    def test_sixty_hz_walk_covers_the_stride_and_moves_on_most_ticks(self):
        for animal in ANIMALS:
            with self.subTest(animal=animal):
                self.start_walking(animal)
                frames, changes = set(), 0
                previous = fingerprint(self.cat._pixmap.toImage())
                for tick in range(1, 73):
                    self.cat._tick(now=100.0 + tick / 60)
                    frames.add(self.cat._frame_key[1])
                    current = fingerprint(self.cat._pixmap.toImage())
                    changes += current != previous
                    previous = current
                self.assertEqual(frames, set(range(16)))
                self.assertGreater(changes, 55, "Art still holds for several 16 ms updates.")

    def test_releasing_a_drag_changes_to_contact_preparation(self):
        for animal in ANIMALS:
            with self.subTest(animal=animal):
                self.cat.set_animal_type(animal)
                self.cat.timer.stop()
                self.cat.motion.state = "held"
                self.cat.motion.y = self.cat.motion.bounds(self.area)[3]-150
                self.cat._pose_clock_kind = None
                self.cat._set_frame()
                self.cat._animation_time += 0.6
                self.cat._set_frame()
                held = self.cat._pixmap.toImage()
                self.assertEqual(self.cat._frame_key[-2], 1.0)
                self.cat.motion.state, self.cat.motion.vy = "fall", 500
                self.cat._set_frame()
                self.cat._animation_time += 0.31
                self.cat._set_frame()
                self.assertEqual(self.cat._frame_key[-2], 1.0)
                self.assertNotEqual(self.cat._pixmap.toImage(), held)

    def test_blink_tracks_eyelids_without_restarting_the_idle_clock(self):
        self.cat.motion.state, self.cat.motion.sleeping = "rest", False
        self.cat._animation_time = 4.0
        self.cat._pose_clock_kind = None
        self.cat._set_frame()
        self.cat._animation_time += 0.6
        self.cat._blink_left = pet.BLINK_SECONDS / 2
        self.cat._set_frame()
        self.assertEqual(self.cat._frame_key[0], "blink")
        self.assertAlmostEqual(self.cat._frame_key[-2], 0.5)
        self.assertAlmostEqual(self.cat._frame_key[3], 0.6 / pet.ASSET_ANIMATION_SECONDS["idle"])
        self.cat._blink_left = 0
        self.cat._set_frame()
        self.assertAlmostEqual(self.cat._frame_key[-2], 0.6 / pet.ASSET_ANIMATION_SECONDS["idle"])

    def test_one_shot_actions_use_their_full_duration(self):
        self.cat.motion.state, self.cat.motion.sleeping = "rest", False
        for habit in ("yawn", "stretch"):
            self.cat._habit = habit
            for fraction in (0.2, 0.5, 0.9):
                self.cat._habit_elapsed = fraction * pet.HABIT_DURATIONS[habit]
                self.cat._set_frame()
                self.assertAlmostEqual(self.cat._frame_key[-2], fraction)
        self.cat._landing_elapsed = pet.LAND_FRAME_SECONDS / 2
        self.cat._set_frame()
        self.assertAlmostEqual(self.cat._frame_key[-2], pet.LAND_FRAME_SECONDS / 2 / self.cat.motion.landing_duration)

    def test_pet_reaction_finishes_before_speech_and_can_play_again(self):
        for animal in pet.DRAWERS:
            with self.subTest(animal=animal):
                self.cat.set_animal_type(animal)
                self.cat.motion.state, self.cat.motion.sleeping = "rest", False
                self.cat._pet_until = 0
                self.cat._set_frame()
                neutral = self.cat._pixmap.toImage()
                self.cat.pet(now=100.0)
                self.assertEqual(self.cat._pixmap.toImage(), neutral)
                self.cat._animation_time += pet.PET_REACTION_SECONDS/2
                self.cat._set_frame()
                self.assertNotEqual(self.cat._pixmap.toImage(), neutral)
                self.cat._animation_time += pet.PET_REACTION_SECONDS
                self.cat._set_frame()
                self.assertEqual(self.cat._pixmap.toImage(), neutral)
                self.assertTrue(self.cat.bubble.isVisible())
                self.cat.pet(now=102.5)
                self.cat._animation_time += pet.PET_REACTION_SECONDS/2
                self.cat._set_frame()
                self.assertNotEqual(self.cat._pixmap.toImage(), neutral)

    def test_retina_render_keeps_the_hit_region_in_window_coordinates(self):
        self.start_walking("Fox")
        mask = self.cat.mask()
        with patch.object(self.cat, "devicePixelRatioF", return_value=2.0):
            self.cat._set_frame()
        self.assertEqual(self.cat._pixmap.width(), 2 * self.cat.width())
        self.assertEqual(self.cat._pixmap.deviceIndependentSize().toSize(), self.cat.size())
        self.assertTrue(self.cat.rect().contains(self.cat.mask().boundingRect()))
        self.assertLessEqual(abs(mask.boundingRect().width()-self.cat.mask().boundingRect().width()), 2)

    def test_pause_and_fullscreen_latch_freeze_visual_time(self):
        self.start_walking("Fox")
        self.cat._tick(now=100.1)
        self.cat.set_paused(True)
        paused_time, paused_image = self.cat._animation_time, self.cat._pixmap.toImage()
        self.cat._tick(now=self.cat._last_time + 1)
        self.assertEqual(self.cat._animation_time, paused_time)
        self.assertEqual(self.cat._pixmap.toImage(), paused_image)
        self.assertFalse(self.cat.timer.isActive())
        self.cat.set_paused(False)
        self.cat._set_latched(True)
        latched_time, latched_image = self.cat._animation_time, self.cat._pixmap.toImage()
        self.cat._tick(now=self.cat._last_time + 1)
        self.assertEqual(self.cat._animation_time, latched_time)
        self.assertEqual(self.cat._pixmap.toImage(), latched_image)
        self.assertFalse(self.cat.timer.isActive())

    def test_repeated_warm_playback_keeps_the_render_cache_bounded(self):
        self.start_walking("Cat")
        for tick in range(1, 241):
            self.cat._tick(now=100.0 + tick / 60)
            self.assertLessEqual(len(self.cat._frames), 128)
        self.assertEqual(self.cat.motion.state, "walk")


if __name__ == "__main__":
    unittest.main()

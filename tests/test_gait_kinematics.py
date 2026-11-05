"""Walking mechanics: contact, continuous foot paths and fixed limb lengths."""
import math
import os
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import desktop_cat as pet
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QImage


class FootPathTests(unittest.TestCase):
    def foot(self, phase, stride=68.0, lift=15.0):
        return pet._walk_foot(phase, 200.0, 312.0, stride, lift)

    def test_supporting_paw_is_flat_and_cancels_forward_travel(self):
        # The window advances stride/duty per cycle. A supporting paw must move
        # back by exactly that amount in sprite space, at every supported size.
        for scale in (0.2, 0.3, 0.4):
            for direction in (-1, 1):
                world_positions = []
                for tick in range(1, 100):
                    phase = pet.WALK_DUTY * tick / 100
                    x, y, contact = self.foot(phase)
                    self.assertTrue(contact)
                    self.assertAlmostEqual(y, 312.0, places=8)
                    body_travel = 68.0 * phase / pet.WALK_DUTY
                    world_positions.append(direction * scale * (body_travel + x))
                self.assertLess(max(world_positions) - min(world_positions), 1e-8)

    def test_swing_lifts_once_and_returns_to_the_same_contact_height(self):
        transitions = 0
        previous = self.foot(0.0)[2]
        heights = []
        for tick in range(1, 1001):
            _x, y, contact = self.foot(tick / 1000)
            transitions += previous and not contact
            previous = contact
            if not contact:
                heights.append(y)
                self.assertLessEqual(y, 312.0)
        self.assertEqual(transitions, 1, "Each leg must swing once per stride.")
        self.assertAlmostEqual(min(heights), 297.0, delta=0.01)
        self.assertAlmostEqual(self.foot(1.0)[1], 312.0, places=8)

    def test_foot_position_and_velocity_join_smoothly_at_lift_and_wrap(self):
        epsilon = 1e-6
        for phase in (pet.WALK_DUTY, 1.0):
            before = self.foot(phase - epsilon)
            center = self.foot(phase)
            after = self.foot(phase + epsilon)
            for axis in (0, 1):
                self.assertAlmostEqual(before[axis], after[axis], delta=0.001)
                incoming = (center[axis] - before[axis]) / epsilon
                outgoing = (after[axis] - center[axis]) / epsilon
                self.assertAlmostEqual(incoming, outgoing, delta=0.02)

    def test_quarter_staggered_legs_always_leave_two_supporting_paws(self):
        offsets = (0.0, 0.25, 0.5, 0.75)
        for tick in range(1000):
            phase = tick / 1000
            contacts = [self.foot(phase + offset)[2] for offset in offsets]
            self.assertGreaterEqual(sum(contacts), 2)
        # A half-stride shift exchanges the two sides instead of synchronizing
        # all four legs into the old up/down waddle.
        self.assertTrue(any(self.foot(phase)[2] != self.foot(phase + 0.5)[2]
                            for phase in (i / 100 for i in range(100))))


class LimbSolverTests(unittest.TestCase):
    def test_every_rig_target_is_reachable_through_the_entire_stride(self):
        # Testing the solver alone would miss a bad hip/stride configuration:
        # an unreachable target can still leave a visible gap at the ankle.
        for animal, rig in pet.WALK_RIGS.items():
            for name, limb in rig["limbs"].items():
                upper = math.dist(limb["root"], limb["knee"])
                lower = math.dist(limb["knee"], limb["ankle"])
                for far in (False, True):
                    for tick in range(200):
                        with self.subTest(animal=animal, leg=name, far=far, tick=tick):
                            root, knee, ankle, _contact = pet._walk_leg_pose(
                                animal, name, tick / 200, far)
                            self.assertAlmostEqual(math.dist(root, knee), upper, places=6)
                            self.assertAlmostEqual(math.dist(knee, ankle), lower, places=6)

    def test_actual_rig_supporting_paws_do_not_slide_in_world_space(self):
        for animal, rig in pet.WALK_RIGS.items():
            for name in rig["limbs"]:
                for far in (False, True):
                    offset = (0.25 if name == "fore" else 0.0) + (0.5 if far else 0.0)
                    positions = []
                    for tick in range(1, 100):
                        phase = offset + pet.WALK_DUTY * tick / 100
                        _root, _knee, ankle, contact = pet._walk_leg_pose(
                            animal, name, phase, far)
                        self.assertTrue(contact)
                        world_x = ankle[0] + rig["stride"] * phase / pet.WALK_DUTY
                        positions.append((world_x, ankle[1]))
                    for axis in (0, 1):
                        self.assertLess(max(point[axis] for point in positions)
                                        - min(point[axis] for point in positions), 1e-7)

    def test_reachable_targets_preserve_upper_and_lower_bone_lengths(self):
        root = (160.0, 205.0)
        upper, lower = 61.0, 57.0
        for bend in (-1, 1):
            for angle in (-0.55, -0.2, 0.0, 0.3, 0.55):
                for reach in (55.0, 83.0, 110.0):
                    target = (root[0] + math.sin(angle) * reach,
                              root[1] + math.cos(angle) * reach)
                    knee = pet._solve_leg(root, target, upper, lower, bend)
                    self.assertAlmostEqual(math.dist(root, knee), upper, places=7)
                    self.assertAlmostEqual(math.dist(knee, target), lower, places=7)

    def test_opposite_bends_use_opposite_sides_of_the_same_leg(self):
        root, target = (160.0, 205.0), (170.0, 303.0)
        knees = [pet._solve_leg(root, target, 61.0, 57.0, bend)
                 for bend in (-1, 1)]
        cross = [(target[0] - root[0]) * (knee[1] - root[1])
                 - (target[1] - root[1]) * (knee[0] - root[0])
                 for knee in knees]
        self.assertLess(cross[0] * cross[1], 0.0)


class GaitPlaybackTests(unittest.TestCase):
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
        self.cat._habits_enabled = False
        self.cat._habit = None
        self.cat._pet_until = 0.0
        self.cat._landing_elapsed = None

    def tearDown(self):
        self.area_patch.stop()
        self.cat.close()
        self.cat.deleteLater()
        self.app.processEvents()

    def start_walking(self, animal="Cat", size="Medium", direction=1):
        self.cat.set_animal_type(animal)
        self.cat.set_pet_size(size)
        self.cat.timer.stop()
        self.cat.motion.state = "walk"
        self.cat.motion.sleeping = False
        self.cat.motion.roaming = True
        self.cat.motion.direction = direction
        self.cat.motion.decision_in = 999.0
        self.cat.motion.x = 2000.0
        self.cat.motion.y = self.cat.motion.bounds(self.area)[3]
        self.cat._last_area = self.area
        self.cat._last_time = 100.0
        self.cat._animation_time = 0.0
        self.cat._walk_phase = 0.0
        self.cat._pose_clock_kind = None
        self.cat._set_frame()

    def test_phase_tracks_actual_distance_at_every_size_in_both_directions(self):
        for animal, rig in pet.WALK_RIGS.items():
            for size in pet.SIZES:
                for direction in (-1, 1):
                    with self.subTest(animal=animal, size=size, direction=direction):
                        self.start_walking(animal, size, direction)
                        previous_x = self.cat.motion.x
                        self.cat._tick(now=100.05)
                        distance = abs(self.cat.motion.x - previous_x)
                        self.assertGreater(distance, 0.0)
                        world_stride = (rig["stride"] / pet.WALK_DUTY
                                        * self.cat.width() / pet.SPRITE_CANVAS[0])
                        expected = (distance / world_stride) % 1.0
                        self.assertAlmostEqual(self.cat._walk_phase, expected, places=8)
                        self.assertAlmostEqual(self.cat._frame_key[-2], expected, delta=0.000001)

    def test_size_changes_speed_without_speeding_up_the_steps(self):
        for animal,rig in pet.WALK_RIGS.items():
            periods=[]
            for size in pet.SIZES:
                self.start_walking(animal,size)
                stride=rig["stride"]/pet.WALK_DUTY*self.cat.width()/400
                periods.append(stride/self.cat.motion.walk_speed)
            self.assertLess(max(periods)-min(periods),1e-9)
            self.assertGreater(periods[0],1.0,"Short legs must not paddle frantically.")
            self.assertLess(periods[0],1.2)

    def test_visible_paw_soles_match_the_ground_contact_not_just_the_ankles(self):
        for animal,rig in pet.WALK_RIGS.items():
            textures=pet._walk_textures(animal,None)
            for name,limb in rig["limbs"].items():
                source_name=limb.get("texture_from",name)
                source=rig["limbs"][source_name]
                paw=textures[source_name,False][2].convertToFormat(QImage.Format.Format_RGBA8888)
                raw=paw.constBits().asstring(paw.sizeInBytes())
                row_bytes=paw.bytesPerLine()
                sole=max(y for y in range(paw.height())
                         if any(alpha>80 for alpha in raw[y*row_bytes+3:(y+1)*row_bytes:4]))
                self.assertEqual(sole,source["sole"],f"{animal} {name} texture ground changed.")
                for far in (False,True):
                    offset=(0.25 if name=="fore" else 0)+(0.5 if far else 0)
                    _root,_joint,ankle,contact=pet._walk_leg_pose(animal,name,offset+0.2,far)
                    self.assertTrue(contact)
                    visible_bottom=ankle[1]+sole-source["ankle"][1]
                    self.assertAlmostEqual(visible_bottom,pet.WALK_FLOOR-(3 if far else 0))

    def test_stationary_mouse_hold_does_not_run_the_legs_in_place(self):
        self.start_walking()
        self.cat._press_position = QPointF(100, 100)
        previous_phase = self.cat._walk_phase
        previous_x = self.cat.motion.x
        previous_image = self.cat._pixmap.toImage()
        self.cat._tick(now=100.08)
        self.assertEqual(self.cat.motion.x, previous_x)
        self.assertEqual(self.cat._walk_phase, previous_phase)
        self.assertEqual(self.cat._pixmap.toImage(), previous_image)

    def test_long_ui_frame_advances_gait_by_physics_distance(self):
        self.start_walking()
        previous_x = self.cat.motion.x
        self.cat._tick(now=100.5)
        distance = self.cat.motion.x - previous_x
        self.assertAlmostEqual(distance, self.cat.motion.walk_speed * 0.1, places=7)
        world_stride = (pet.WALK_RIGS["Cat"]["stride"] / pet.WALK_DUTY
                        * self.cat.width() / pet.SPRITE_CANVAS[0])
        self.assertAlmostEqual(self.cat._walk_phase, distance / world_stride, places=8)

    def test_pause_freezes_distance_and_gait_together(self):
        self.start_walking()
        self.cat._tick(now=100.05)
        self.cat.set_paused(True)
        previous_phase = self.cat._walk_phase
        previous_x = self.cat.motion.x
        previous_image = self.cat._pixmap.toImage()
        self.cat._tick(now=self.cat._last_time + 1.0)
        self.assertEqual(self.cat.motion.x, previous_x)
        self.assertEqual(self.cat._walk_phase, previous_phase)
        self.assertEqual(self.cat._pixmap.toImage(), previous_image)


if __name__ == "__main__":
    unittest.main()

"""Action poses preserve anatomy and the floor supports used by the app."""
import math
import os
import unittest

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication
import desktop_cat as pet


class ActionGeometryTests(unittest.TestCase):
    def test_articulated_actions_preserve_every_measured_leg_link(self):
        for animal, rig in pet.WALK_RIGS.items():
            for action in ('held', 'fall', 'land', 'stretch', 'turn'):
                for tick in range(121):
                    phase = tick/120
                    body, _angle, _head, _tail, legs = pet._standing_geometry(animal, action, phase)
                    for (name, far), (root, knee, ankle, contact) in legs.items():
                        with self.subTest(animal=animal, action=action, phase=phase, leg=name, far=far):
                            limb = rig['limbs'][name]
                            upper = math.dist(limb['root'], limb['knee'])
                            lower = math.dist(limb['knee'], limb['ankle'])
                            self.assertAlmostEqual(math.dist(root, knee), upper, places=6)
                            self.assertAlmostEqual(math.dist(knee, ankle), lower, places=6)
                            joint = body(limb['root'])
                            self.assertAlmostEqual(root[0], joint[0]+(11 if far else 0))
                            self.assertAlmostEqual(root[1], joint[1])
                            if contact:
                                sole = ankle[1]+limb['sole']-limb['ankle'][1]
                                self.assertAlmostEqual(sole, pet.WALK_FLOOR-(3 if far else 0), places=7)

    def test_torso_volume_and_face_feature_spacing_stay_rigid(self):
        # Face/torso landmarks must keep their pairwise spacing during deep
        # actions, independently of where the floor or cursor carries them.
        for animal,rig in pet.WALK_RIGS.items():
            hip=rig['limbs']['hind']['root']
            shoulder=rig['limbs']['fore']['root']
            nose=(370,105)
            for action in ('held','fall','land','stretch','turn'):
                for tick in range(61):
                    body,*_=pet._standing_geometry(animal,action,tick/60)
                    self.assertAlmostEqual(math.dist(body(hip),body(shoulder)),math.dist(hip,shoulder),places=7)
                    self.assertAlmostEqual(math.dist(body(shoulder),body(nose)),math.dist(shoulder,nose),places=7)

    def test_stretch_holds_front_supports_while_chest_lowers_and_hips_rise(self):
        for animal, rig in pet.WALK_RIGS.items():
            standing, *_ = pet._standing_geometry(animal, 'stretch', 0)
            lowered, *_ = pet._standing_geometry(animal, 'stretch', 0.5)
            hind, fore = rig['limbs']['hind']['root'], rig['limbs']['fore']['root']
            self.assertLess(lowered(hind)[1], standing(hind)[1]-2)
            self.assertGreater(lowered(fore)[1], standing(fore)[1]+30)
            for far in (False, True):
                supports = []
                for phase in (0.43, 0.50, 0.60, 0.68):
                    _body, _angle, _head, _tail, legs = pet._standing_geometry(animal, 'stretch', phase)
                    _root, _knee, ankle, contact = legs['fore', far]
                    self.assertTrue(contact)
                    supports.append(ankle)
                self.assertTrue(all(point == supports[0] for point in supports))
            # Front paws leave the floor during their reach and recovery; the
            # rear pair supplies support rather than sliding with the body.
            for phase in (0.2, 0.85):
                *_ignored, legs = pet._standing_geometry(animal, 'stretch', phase)
                self.assertFalse(legs['fore', False][3])
                self.assertTrue(legs['hind', False][3])

    def test_landing_supports_never_move_through_compression_and_rebound(self):
        for animal in pet.WALK_RIGS:
            supports = {}
            for tick in range(121):
                *_ignored, legs = pet._standing_geometry(animal, 'land', tick/120)
                for key, (_root, _knee, ankle, contact) in legs.items():
                    self.assertTrue(contact)
                    supports.setdefault(key, ankle)
                    self.assertEqual(ankle, supports[key])


class ActionFrameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_completed_airborne_poses_hold_and_differ_by_mechanics(self):
        for animal in pet.WALK_RIGS:
            held = pet.animal_frame_image(animal, 'held', 0, motion_phase=1)
            fall = pet.animal_frame_image(animal, 'fall', 0, motion_phase=1)
            self.assertNotEqual(held, fall)
            for pose, complete in (('held', held), ('fall', fall)):
                self.assertEqual(complete, pet.animal_frame_image(animal, pose, 0, motion_phase=1.5))

    def test_mid_turn_facing_change_does_not_shift_the_projection_pivot(self):
        from PyQt6.QtGui import QPixmap,QRegion
        for animal in pet.WALK_RIGS:
            before=pet.animal_frame_image(animal,'turn',0,direction=1,motion_phase=.499)
            after=pet.animal_frame_image(animal,'turn',0,direction=-1,motion_phase=.501)
            left=QRegion(QPixmap.fromImage(before).mask()).boundingRect().center().x()
            right=QRegion(QPixmap.fromImage(after).mask()).boundingRect().center().x()
            self.assertLess(abs(left-right),15,'Facing must reverse around the same floor support.')

    def test_posture_keys_connect_to_the_live_action_endpoints(self):
        for animal in pet.WALK_RIGS:
            bases,clips=pet._sprite_bases(animal)
            idle=bases[clips['idle'][0]]
            sleep=bases[clips['sleep'][0]]
            standing=pet._articulated_image(animal,'land',1,None)
            for pose,first,last in (('stand',idle,standing),('sit',standing,idle),
                                    ('settle',idle,sleep),('wake',sleep,idle)):
                for phase,expected in ((0,first),(1,last)):
                    actual=pet.animal_frame_image(animal,pose,0,motion_phase=phase)
                    self.assertEqual(actual.convertToFormat(QImage.Format.Format_RGBA8888),
                                     expected.convertToFormat(QImage.Format.Format_RGBA8888))

    def test_pose_transitions_keep_a_nonempty_transparent_character(self):
        for animal in pet.WALK_RIGS:
            for pose in ('stand', 'sit', 'settle', 'wake'):
                for phase in (0, 0.2, 0.5, 0.8, 1):
                    with self.subTest(animal=animal, pose=pose, phase=phase):
                        frame = pet.animal_frame_image(animal, pose, 0, motion_phase=phase)
                        rgba = frame.convertToFormat(QImage.Format.Format_RGBA8888)
                        raw = rgba.constBits().asstring(rgba.sizeInBytes())
                        alpha = raw[3::4]
                        self.assertGreater(sum(value > 80 for value in alpha), 5000)
                        self.assertLess(sum(value > 80 for value in alpha), 90000)
                        self.assertEqual(alpha[0], 0)


if __name__ == '__main__':
    unittest.main()

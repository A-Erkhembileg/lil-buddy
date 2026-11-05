"""Ground recovery is a complete action, including when behavior requests arrive."""
import os
import random
import unittest

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PyQt6.QtCore import QRectF
import desktop_cat as pet


class SleepyRandom(random.Random):
    def random(self):
        return 0.1


class RecoveryTransitionTests(unittest.TestCase):
    def setUp(self):
        self.area = QRectF(0, 0, 1200, 800)
        self.floor = pet.Motion.limits(self.area)[3]
        self.rng = SleepyRandom(7)

    def advance(self, motion, seconds):
        for _ in range(round(seconds*120)):
            motion.step(1/120, self.area, self.rng)

    def test_short_drop_finishes_recovery_before_any_sleep_decision(self):
        motion = pet.Motion(x=300, y=self.floor-0.1, vy=40, state='fall', decision_in=0)
        self.assertGreater(motion.step(1/120, self.area, self.rng), 0)
        self.assertEqual(motion.state, 'land')
        self.advance(motion, 0.60)
        self.assertEqual(motion.state, 'land')
        self.assertFalse(motion.sleeping)
        self.advance(motion, 0.12)
        self.assertEqual(motion.state, 'sit')
        self.assertFalse(motion.sleeping)
        self.advance(motion, 0.60)
        self.assertEqual(motion.state, 'rest')
        self.assertFalse(motion.sleeping)
        self.assertGreater(motion.decision_in, 1)

    def test_sleep_requires_sitting_and_lowering_then_wakes_through_a_pose(self):
        motion = pet.Motion(x=300, y=self.floor, state='walk')
        motion.begin_rest(self.rng)
        self.assertEqual(motion.state, 'sit')
        self.advance(motion, 0.57)
        self.assertEqual(motion.state, 'settle')
        self.assertFalse(motion.sleeping)
        self.advance(motion, 0.57)
        self.assertEqual(motion.state, 'rest')
        self.assertTrue(motion.sleeping)
        motion.decision_in = 0
        motion.step(1/120, self.area, self.rng)
        self.assertEqual(motion.state, 'wake')
        self.assertFalse(motion.sleeping)

    def test_edge_turn_stops_travel_before_reversing_and_keeps_position_bounded(self):
        right = pet.Motion.limits(self.area)[1]
        motion = pet.Motion(x=right-0.1, y=self.floor, direction=1, state='walk', decision_in=10)
        motion.step(1/60, self.area, self.rng)
        self.assertEqual(motion.state, 'turn')
        self.assertEqual(motion.direction, 1)
        self.advance(motion, 0.2)
        self.assertEqual(motion.x, right)
        self.assertEqual(motion.direction, 1)
        self.advance(motion, 0.1)
        self.assertEqual(motion.direction, -1)
        self.assertEqual(motion.x, right)
        self.advance(motion, 0.3)
        self.assertEqual(motion.state, 'walk')
        self.assertLess(motion.x, right)


if __name__ == '__main__':
    unittest.main()

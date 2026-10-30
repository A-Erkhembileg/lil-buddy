"""Native detector logic with mocked OS calls; no Windows runtime is claimed."""
import ctypes
from ctypes import wintypes
import os
import unittest
from unittest.mock import Mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from desktop_cat import FullScreenDetector, covers_display


class GeometryTests(unittest.TestCase):
    def test_complete_display_coverage_accepts_negative_origins(self):
        for display in ((0, 0, 1440, 900), (-1920, -300, 1920, 1080),
                        (0, -1080, 1920, 1080)):
            with self.subTest(display=display):
                self.assertTrue(covers_display(display, display))

    def test_maximized_work_area_does_not_count_as_fullscreen(self):
        display = (-1920, -300, 1920, 1080)
        for work_area in ((-1920, -270, 1920, 1050), (-1920, -300, 1920, 1040),
                          (-1880, -300, 1880, 1080)):
            with self.subTest(work_area=work_area):
                self.assertFalse(covers_display(work_area, display))

    def test_other_monitor_and_empty_rectangles_do_not_count(self):
        self.assertFalse(covers_display((0, 0, 1920, 1080), (-1920, 0, 1920, 1080)))
        self.assertFalse(covers_display((0, 0, 0, 1080), (0, 0, 1920, 1080)))
        self.assertFalse(covers_display((0, 0, 1920, 1080), (0, 0, 0, 1080)))


class ProbeFailureTests(unittest.TestCase):
    def test_unavailable_or_failed_probe_is_unknown_instead_of_fullscreen_exit(self):
        detector = object.__new__(FullScreenDetector)
        detector.error, detector._probe = "Unavailable", None
        self.assertIsNone(detector.is_fullscreen(object()))
        detector._probe = Mock(side_effect=OSError("Window server unavailable"))
        self.assertIsNone(detector.is_fullscreen(object()))
        self.assertIn("Window server unavailable", detector.error)
        detector._probe = Mock(return_value=False)
        self.assertIs(detector.is_fullscreen(object()), False)
        self.assertIsNone(detector.error)


class FakeUser32:
    def __init__(self):
        self.foreground = 1
        self.pid = 9001
        self.visible = True
        self.minimized = False
        self.class_name = "GameWindow"
        self.owner = 1

    def GetForegroundWindow(self):
        return self.foreground

    def GetWindowThreadProcessId(self, handle, pid):
        pid._obj.value = self.pid
        return 1

    def IsWindowVisible(self, handle):
        return self.visible

    def IsIconic(self, handle):
        return self.minimized

    def GetClassNameW(self, handle, result, length):
        result.value = self.class_name
        return len(self.class_name)

    def GetAncestor(self, handle, flags):
        return self.owner


class FakeDwm:
    cloaked = False

    def DwmGetWindowAttribute(self, handle, attribute, value, size):
        value._obj.value = self.cloaked
        return 0


class WindowsProbeTests(unittest.TestCase):
    """Run the Windows branch against fakes, without loading user32 on macOS."""

    def setUp(self):
        self.detector = object.__new__(FullScreenDetector)
        self.detector._pid = 123
        self.detector._wt = wintypes
        self.detector._user32 = self.user32 = FakeUser32()
        self.detector._dwmapi = self.dwm = FakeDwm()
        self.detector._set_dpi_context = None
        self.display = (-1920, -300, 1920, 1080)
        self.detector._windows_monitor = Mock(return_value=self.display)
        self.clients = {1: self.display}
        self.detector._windows_client = self.clients.get
        self.screen = object()

    def test_foreground_client_must_cover_the_requested_native_monitor(self):
        for client, expected in ((self.display, True),
                                 ((-1920, -270, 1920, 1050), False),
                                 ((0, 0, 1920, 1080), False)):
            with self.subTest(client=client):
                self.clients[1] = client
                self.assertIs(self.detector._windows(self.screen), expected)
        self.detector._windows_monitor.assert_called_with(self.screen)

    def test_missing_foreground_own_pet_and_transient_menu_are_unknown(self):
        self.user32.foreground = 0
        self.assertIsNone(self.detector._windows(self.screen))
        self.user32.foreground = 1
        self.user32.pid = self.detector._pid
        self.assertIsNone(self.detector._windows(self.screen))
        self.user32.pid = 9001
        self.user32.class_name = "#32768"
        self.assertIsNone(self.detector._windows(self.screen))

    def test_hidden_minimized_cloaked_and_desktop_windows_are_not_fullscreen(self):
        self.user32.visible = False
        self.assertFalse(self.detector._windows(self.screen))
        self.user32.visible, self.user32.minimized = True, True
        self.assertFalse(self.detector._windows(self.screen))
        self.user32.minimized, self.dwm.cloaked = False, True
        self.assertFalse(self.detector._windows(self.screen))
        self.dwm.cloaked = False
        for class_name in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            with self.subTest(class_name=class_name):
                self.user32.class_name = class_name
                self.assertFalse(self.detector._windows(self.screen))

    def test_dialog_over_a_fullscreen_owner_keeps_fullscreen_observation(self):
        self.clients[1] = (-1700, -100, 600, 400)
        self.clients[2] = self.display
        self.user32.owner, self.user32.class_name = 2, "#32770"
        self.assertTrue(self.detector._windows(self.screen))
        self.clients[2] = (-1920, -270, 1920, 1000)
        self.assertFalse(self.detector._windows(self.screen))

    def test_original_dpi_context_is_restored_after_success_and_failed_measurement(self):
        dpi = self.detector._set_dpi_context = Mock(return_value=77)
        self.assertTrue(self.detector._windows(self.screen))
        self.assertEqual(dpi.call_count, 2)
        self.assertEqual(dpi.call_args_list[0].args[0].value, ctypes.c_void_p(-4).value)
        self.assertEqual(dpi.call_args_list[1].args, (77,))
        dpi.reset_mock()
        self.clients.clear()
        self.assertIsNone(self.detector._windows(self.screen))
        self.assertEqual(dpi.call_args_list[-1].args, (77,))
        dpi.reset_mock()
        self.detector._windows_monitor.side_effect = RuntimeError("Monitor disconnected")
        with self.assertRaisesRegex(RuntimeError, "Monitor disconnected"):
            self.detector._windows(self.screen)
        self.assertEqual(dpi.call_args_list[-1].args, (77,))


if __name__ == "__main__":
    unittest.main()

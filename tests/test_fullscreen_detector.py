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
        self.detector._windows_monitor.assert_called_with()

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


class FakeMonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class WindowsMonitorTests(unittest.TestCase):
    """Exercise native monitor lookup instead of mocking the broken boundary."""

    def setUp(self):
        self.detector = object.__new__(FullScreenDetector)
        self.detector.error = None
        self.detector._pid = 123
        self.detector._wt = wintypes
        self.detector._MonitorInfo = FakeMonitorInfo
        self.detector._set_dpi_context = None
        self.detector._dwmapi = FakeDwm()
        self.detector._user32 = self.user32 = FakeUser32()
        self.detector._window = self.window = Mock()
        # QScreen.name() may be a friendly hardware name, not \\.\DISPLAY1.
        self.window.screen.return_value.name.return_value = "DELL U2720Q (2)"
        self.hwnd = 0x1234567887654321
        self.window.winId.return_value = self.hwnd
        self.window_monitors = {self.hwnd: 100}
        self.monitor_bounds = {100: (0, 0, 3840, 2160)}
        self.user32.MonitorFromWindow = Mock(
            side_effect=lambda hwnd, flags: self.window_monitors.get(hwnd, 0))
        self.user32.GetMonitorInfoW = Mock(side_effect=self.read_monitor_info)
        self.detector._windows_client = Mock(return_value=(0, 0, 3840, 2160))
        self.detector._probe = self.detector._windows
        self.screen = object()

    def read_monitor_info(self, monitor, pointer):
        bounds = self.monitor_bounds.get(monitor)
        if bounds is None:
            return False
        info = pointer._obj
        self.assertEqual(info.cbSize, ctypes.sizeof(FakeMonitorInfo))
        x, y, width, height = bounds
        info.rcMonitor = wintypes.RECT(x, y, x + width, y + height)
        info.rcWork = wintypes.RECT(x, y, x + width, y + height - 48)
        return True

    def test_friendly_monitor_name_does_not_prevent_native_screen_lookup(self):
        self.assertEqual(self.detector._windows_monitor(), (0, 0, 3840, 2160))
        self.user32.MonitorFromWindow.assert_called_once_with(self.hwnd, 2)
        self.assertTrue(self.detector.is_fullscreen(self.screen))

    def test_negative_mixed_scale_monitor_uses_full_native_bounds(self):
        # Its logical size at 150% would be 1707x960; fullscreen comparisons
        # must use the native 2560x1440 rectangle, including a negative origin.
        bounds = (-2560, -240, 2560, 1440)
        self.monitor_bounds[100] = bounds
        self.detector._windows_client.return_value = bounds
        self.assertEqual(self.detector._windows_monitor(), bounds)
        self.assertTrue(self.detector.is_fullscreen(self.screen))
        self.detector._windows_client.return_value = (0, 0, 3840, 2160)
        self.assertFalse(self.detector.is_fullscreen(self.screen))

    def test_current_window_handle_is_read_again_after_window_recreation(self):
        replacement = self.hwnd + 1
        self.window_monitors[replacement] = 200
        self.monitor_bounds[200] = (-1920, 0, 1920, 1080)
        self.assertEqual(self.detector._windows_monitor(), (0, 0, 3840, 2160))
        self.window.winId.return_value = replacement
        self.assertEqual(self.detector._windows_monitor(), (-1920, 0, 1920, 1080))
        self.assertEqual(self.user32.MonitorFromWindow.call_args.args, (replacement, 2))

    def test_missing_pet_window_or_native_handle_is_unknown(self):
        self.detector._window = None
        self.assertIsNone(self.detector.is_fullscreen(self.screen))
        self.assertIsNotNone(self.detector.error)
        self.detector._window = self.window
        self.window.winId.return_value = 0
        self.assertIsNone(self.detector.is_fullscreen(self.screen))
        self.assertIsNotNone(self.detector.error)

    def test_missing_monitor_or_failed_monitor_info_is_unknown_and_recovers(self):
        self.window_monitors.clear()
        self.assertIsNone(self.detector.is_fullscreen(self.screen))
        self.assertIsNotNone(self.detector.error)
        self.window_monitors[self.hwnd] = 100
        self.monitor_bounds.clear()
        self.assertIsNone(self.detector.is_fullscreen(self.screen))
        self.assertIsNotNone(self.detector.error)
        self.monitor_bounds[100] = (0, 0, 3840, 2160)
        self.assertTrue(self.detector.is_fullscreen(self.screen))
        self.assertIsNone(self.detector.error)


if __name__ == "__main__":
    unittest.main()

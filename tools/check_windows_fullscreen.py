#!/usr/bin/env python3
"""Check Windows monitor/client geometry against real User32 calls.

    python tools/check_windows_fullscreen.py

The temporary test windows never activate. Foreground-window selection is
controlled so this can run on a CI desktop without taking keyboard focus; all
monitor, visibility, ownership, and client measurements use the native APIs.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys


def main() -> None:
    if sys.platform != "win32":
        print(json.dumps({"skipped": "Native Windows APIs are required."}))
        return

    from ctypes import wintypes
    from PyQt6.QtWidgets import QApplication, QWidget

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from desktop_cat import FullScreenDetector, OVERLAY_FLAGS, prepare_overlay

    application = QApplication([])
    if application.platformName() != "windows":
        raise RuntimeError("Run this native check with QT_QPA_PLATFORM=windows.")
    screen = application.primaryScreen()
    if screen is None:
        raise RuntimeError("The Windows runner has no display.")
    pet = QWidget(None, OVERLAY_FLAGS)
    prepare_overlay(pet)
    pet.resize(120, 96)
    pet.move(screen.geometry().x() + 20, screen.geometry().y() + 20)
    pet.winId()  # Create its real HWND without showing or activating it.
    detector = FullScreenDetector(window=pet)
    if detector.error:
        raise RuntimeError(detector.error)
    native = detector._user32
    native.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    native.CreateWindowExW.restype = wintypes.HWND
    native.DestroyWindow.argtypes = [wintypes.HWND]
    native.DestroyWindow.restype = wintypes.BOOL
    native.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    native.SetWindowPos.restype = wintypes.BOOL

    class ForegroundSelection:
        """Override selection only; every measurement still uses User32."""
        hwnd = None

        def GetForegroundWindow(self):
            return self.hwnd

        def __getattr__(self, name):
            return getattr(native, name)

    class FriendlyScreen:
        def name(self):
            return "Friendly Monitor Name (not a GDI display identifier)"

        def __getattr__(self, name):
            return getattr(screen, name)

    selection = ForegroundSelection()
    detector._user32 = selection
    detector._pid = -1  # Treat the temporary same-process target as another app.
    previous_dpi = None
    hwnd = None
    try:
        if detector._set_dpi_context is not None:
            previous_dpi = detector._set_dpi_context(ctypes.c_void_p(-4))
        monitor = native.MonitorFromWindow(int(pet.winId()), 2)
        info = detector._MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not monitor or not native.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        bounds = info.rcMonitor
        display = (bounds.left, bounds.top, bounds.right - bounds.left,
                   bounds.bottom - bounds.top)
        if detector._windows_monitor() != display:
            raise AssertionError("The Qt pet HWND resolved to incorrect native monitor bounds.")

        # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW; WS_POPUP | WS_VISIBLE.
        hwnd = native.CreateWindowExW(
            0x08000000 | 0x00000080, "STATIC", "Lil Buddy geometry regression",
            0x80000000 | 0x10000000, *display, None, None, None, None,
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        selection.hwnd = hwnd
        application.processEvents()
        if detector._windows_client(hwnd) != display:
            raise AssertionError("The native fullscreen test window has incorrect client bounds.")
        if detector.is_fullscreen(FriendlyScreen()) is not True:
            raise AssertionError(f"Fullscreen failed with a friendly screen name: {detector.error}")

        work = info.rcWork
        work_area = (work.left, work.top, work.right - work.left,
                     work.bottom - work.top)
        if work_area == display:
            work_area = (display[0], display[1] + 32, display[2], display[3] - 32)
        # SWP_NOZORDER | SWP_NOACTIVATE. Reuse the native target at work-area size.
        if not native.SetWindowPos(hwnd, None, *work_area, 0x0004 | 0x0010):
            raise ctypes.WinError(ctypes.get_last_error())
        application.processEvents()
        if detector.is_fullscreen(FriendlyScreen()) is not False:
            raise AssertionError("A work-area window incorrectly counts as fullscreen.")
        detector._pid = os.getpid()
        if detector.is_fullscreen(FriendlyScreen()) is not None:
            raise AssertionError("The pet's own process must remain a transient observation.")
        print(json.dumps({
            "success": True,
            "platform": application.platformName(),
            "screen": screen.name(),
            "native_monitor": display,
            "checks": ["pet HWND monitor lookup", "friendly screen name",
                       "native fullscreen client", "work-area fullscreen exit",
                       "own-process exclusion"],
        }, indent=2))
    finally:
        if hwnd:
            native.DestroyWindow(hwnd)
        if previous_dpi:
            detector._set_dpi_context(previous_dpi)
        pet.close()
        application.processEvents()


if __name__ == "__main__":
    main()

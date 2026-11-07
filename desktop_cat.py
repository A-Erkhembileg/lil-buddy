#!/usr/bin/env python3
"""Lil Buddy: a small, self-contained PyQt6 desktop cat.

Python 3.10+. Install dependencies with the same Python used to run this file:
    python3 -m pip install PyQt6
    # macOS also needs the native window bridge (no permissions required):
    python3 -m pip install pyobjc-framework-Cocoa
    python3 desktop_cat.py

Click to pet, or drag with the left mouse button; release to drop. Right-click or Control-click
for roaming, reset, and quit. Detailed artwork is included in artwork/motion;
the crow and interface graphics are drawn in code.
macOS can still place secure/system surfaces above ordinary application windows.
"""
from __future__ import annotations

import ctypes
import functools
import ctypes.util
import os
import argparse
from pathlib import Path
import tempfile
import json
from dataclasses import dataclass
import math
import random
import signal
import sys
import time
import textwrap

try:
    from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, QSettings, QStandardPaths, QSignalBlocker
    from PyQt6.QtGui import (
        QColor, QCursor, QGuiApplication, QIcon, QImage, QMouseEvent, QPainter, QPixmap,
        QActionGroup, QPolygon, QPolygonF, QRegion, QTransform, QPainterPath,
    )
    from PyQt6.QtWidgets import QApplication, QColorDialog, QMenu, QSystemTrayIcon, QWidget
except ImportError as exc:
    raise SystemExit(
        f"PyQt6 is required. Install it with:\n{sys.executable} -m pip install PyQt6"
    ) from exc

if sys.platform == "darwin":
    try:
        import AppKit
        import objc
    except ImportError as exc:
        raise SystemExit(
            "macOS needs the Cocoa bridge to hide the Dock icon. Install it with:\n"
            f"{sys.executable} -m pip install pyobjc-framework-Cocoa"
        ) from exc


PIXEL_SCALE = 3
ART_WIDTH, ART_HEIGHT = 40, 32
WIDTH, HEIGHT = ART_WIDTH * PIXEL_SCALE, ART_HEIGHT * PIXEL_SCALE
GRAVITY = 1700.0              # Logical pixels per second squared (Retina safe).
WALK_SPEED = 48.0
BOUNCE = 0.24
BOUNCE_CUTOFF = 110.0         # Stop small impacts so the cat settles completely.
BUBBLE_SECONDS = 3.0
PETTED = "petted"
AIR_FRAME_SECONDS = 0.065
CROW_FLAP_SECONDS = 0.055
BLINK_SECONDS = 0.16
IDLE_SECONDS = 5.6
PET_REACTION_SECONDS = 1.65
LAND_FRAME_SECONDS = 0.06
HARD_LANDING_SPEED = 420.0
SIZES = {"Small": 2, "Medium": 3, "Large": 4}
HABIT_DURATIONS = {"groom": 3.0, "yawn": 2.0, "stretch": 2.6, "watch": 2.4}
CAT_QUOTES = (
    "Do you mind? I'm busy doing nothing.",
    "Humans... always demanding attention.",
    "You may gaze upon me, but do not touch the fur.",
    "Ah, the hairless ape returns.",
    "I tolerate this only because I cannot open the treat cabinet myself.",
    "Your work can wait. My nap cannot.",
    "One pet. That is your allowance.",
    "I see the cursor. I choose to ignore it.",
    "A wise cat knows when to do absolutely nothing.",
    "The keyboard is warm. Your deadlines are irrelevant.",
)
OVERLAY_FLAGS = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.NoDropShadowWindowHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus)


def configure_macos_window(widget: QWidget, click_through: bool = False) -> None:
    if sys.platform != "darwin" or QGuiApplication.platformName() != "cocoa":
        return
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory
    )
    # QWidget.winId() is an NSView pointer on macOS, not an NSWindow.
    view = objc.objc_object(c_void_p=ctypes.c_void_p(int(widget.winId())))
    window = view.window()
    if window is None:
        raise RuntimeError("Qt did not create the native pet window.")
    window.setOpaque_(False)
    window.setBackgroundColor_(AppKit.NSColor.clearColor())
    window.setHasShadow_(False)
    window.setHidesOnDeactivate_(False)
    window.setIgnoresMouseEvents_(click_through)
    # Replace Qt's MoveToActiveSpace behavior; it conflicts with AllSpaces.
    behavior = (AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
    behavior |= getattr(AppKit, "NSWindowCollectionBehaviorCanJoinAllApplications", 0)
    window.setCollectionBehavior_(behavior)
    window.setLevel_(AppKit.NSModalPanelWindowLevel)


def prepare_overlay(widget: QWidget) -> None:
    for attribute in (Qt.WidgetAttribute.WA_TranslucentBackground,
                      Qt.WidgetAttribute.WA_ShowWithoutActivating,
                      Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow):
        widget.setAttribute(attribute)
    widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)


def set_corner_window_level(widget: QWidget, latched: bool) -> None:
    if sys.platform == "darwin" and QGuiApplication.platformName() == "cocoa":
        view = objc.objc_object(c_void_p=ctypes.c_void_p(int(widget.winId())))
        window = view.window()
        if window is not None:
            # AppKit constrains ordinary panels below the menu bar. Status level
            # permits the tiny input-transparent face to reach the physical edge.
            window.setLevel_(AppKit.NSStatusWindowLevel if latched else AppKit.NSModalPanelWindowLevel)


def covers_display(window: tuple[float, float, float, float],
                   display: tuple[float, float, float, float],
                   tolerance: float = 2.0) -> bool:
    """Compare x/y/width/height rectangles in the same native coordinate space."""
    x, y, w, h = window
    sx, sy, sw, sh = display
    return (w > 0 and h > 0 and sw > 0 and sh > 0
            and x <= sx + tolerance and y <= sy + tolerance
            and x + w >= sx + sw - tolerance
            and y + h >= sy + sh - tolerance)


class FullScreenDetector:
    """Call is_fullscreen(screen) at low frequency, e.g. every 250 ms.

    True/False are geometry observations; None means unavailable/transient.
    Secure/exclusive presentation can prevent an ordinary pet overlay appearing.
    """
    def __init__(self, window: QWidget | None = None) -> None:
        self.error: str | None = None
        self._pid = os.getpid()
        self._window = window
        self._probe = None
        try:
            if sys.platform == "darwin":
                self._setup_macos()
                self._probe = self._macos
            elif sys.platform == "win32":
                self._setup_windows()
                self._probe = self._windows
            else:
                self.error = "Automatic fullscreen detection supports macOS and Windows."
        except (OSError, AttributeError, ImportError) as exc:
            self.error = f"Fullscreen detection is unavailable: {exc}"

    def is_fullscreen(self, screen) -> bool | None:
        if self._probe is None or screen is None:
            return None
        try:
            result = self._probe(screen)
            self.error = None
            return result
        except (OSError, ValueError, AttributeError, RuntimeError) as exc:
            # Do not treat a failed observation as fullscreen exit.
            self.error = f"Fullscreen observation failed: {exc}"
            return None

    def _setup_macos(self) -> None:
        import AppKit
        self._workspace = AppKit.NSWorkspace.sharedWorkspace()
        self._cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        self._cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        self._cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        self._cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
        self._cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        self._cf.CFArrayGetCount.restype = ctypes.c_long
        self._cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        self._cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        self._cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        self._cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        self._cf.CFNumberGetValue.restype = ctypes.c_bool
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._cf.CFRelease.restype = None
        self._keys = {key: ctypes.c_void_p.in_dll(self._cg, key).value for key in (
            "kCGWindowOwnerPID", "kCGWindowLayer", "kCGWindowAlpha", "kCGWindowBounds")}

        class Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        class Size(ctypes.Structure):
            _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

        class Rect(ctypes.Structure):
            _fields_ = [("origin", Point), ("size", Size)]

        self._CGRect = Rect
        self._cg.CGRectMakeWithDictionaryRepresentation.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(Rect)]
        self._cg.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool

    def _mac_number(self, dictionary, key: str, floating: bool = False):
        value = self._cf.CFDictionaryGetValue(dictionary, self._keys[key])
        if not value:
            return None
        result = ctypes.c_double() if floating else ctypes.c_longlong()
        # CFNumberDoubleType = 6; CFNumberSInt64Type = 4.
        if not self._cf.CFNumberGetValue(value, 6 if floating else 4, ctypes.byref(result)):
            return None
        return result.value

    def _macos(self, screen) -> bool | None:
        front = self._workspace.frontmostApplication()
        if front is None:
            return None
        pid = int(front.processIdentifier())
        if pid == self._pid:
            return None  # The pet's menus/color dialog should not make it unlatch.
        if front.bundleIdentifier() in {"com.apple.dock", "com.apple.systemuiserver",
                                        "com.apple.controlcenter", "com.apple.loginwindow"}:
            return False
        geometry = screen.geometry()
        display = (geometry.x(), geometry.y(), geometry.width(), geometry.height())
        # OnScreenOnly | ExcludeDesktopElements; this does not capture image data.
        windows = self._cg.CGWindowListCopyWindowInfo(1 | 16, 0)
        if not windows:
            raise RuntimeError("The window server did not return visible-window metadata.")
        unknown = False
        try:
            for index in range(self._cf.CFArrayGetCount(windows)):
                entry = self._cf.CFArrayGetValueAtIndex(windows, index)
                if self._mac_number(entry, "kCGWindowOwnerPID") != pid:
                    continue
                layer = self._mac_number(entry, "kCGWindowLayer")
                alpha = self._mac_number(entry, "kCGWindowAlpha", floating=True)
                if layer is None or layer < 0 or (alpha is not None and alpha <= 0):
                    continue
                bounds = self._cf.CFDictionaryGetValue(entry, self._keys["kCGWindowBounds"])
                rectangle = self._CGRect()
                if not bounds or not self._cg.CGRectMakeWithDictionaryRepresentation(
                        bounds, ctypes.byref(rectangle)):
                    unknown = True
                    continue
                rect = (rectangle.origin.x, rectangle.origin.y,
                        rectangle.size.width, rectangle.size.height)
                # CG window bounds and Qt geometry are logical screen coordinates
                # with the main display's upper-left as origin, including negatives.
                if covers_display(rect, display):
                    return True
            return None if unknown else False
        finally:
            self._cf.CFRelease(windows)

    def _setup_windows(self) -> None:
        from ctypes import wintypes
        self._wt = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        self._user32.GetForegroundWindow.argtypes = []
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        for name in ("IsWindowVisible", "IsIconic"):
            function = getattr(self._user32, name)
            function.argtypes, function.restype = [wintypes.HWND], wintypes.BOOL
        self._user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._user32.GetClassNameW.restype = ctypes.c_int
        self._user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self._user32.GetAncestor.restype = wintypes.HWND
        self._user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self._user32.GetClientRect.restype = wintypes.BOOL
        self._user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self._user32.ClientToScreen.restype = wintypes.BOOL
        self._dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        self._dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
        self._user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self._user32.MonitorFromWindow.restype = wintypes.HANDLE

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        self._MonitorInfo = MonitorInfo
        self._user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        self._user32.GetMonitorInfoW.restype = wintypes.BOOL
        self._set_dpi_context = getattr(self._user32, "SetThreadDpiAwarenessContext", None)
        if self._set_dpi_context is not None:
            self._set_dpi_context.argtypes = [ctypes.c_void_p]
            self._set_dpi_context.restype = ctypes.c_void_p

    def _windows_monitor(self):
        # Qt screen names can be friendly hardware names rather than the GDI
        # device names returned by Windows. Resolve the actual pet window's
        # monitor instead, keeping all measurements in native pixels.
        if self._window is None:
            raise RuntimeError("Fullscreen detection needs the pet's native window.")
        hwnd = int(self._window.winId())
        if not hwnd:
            raise RuntimeError("The pet's native window is unavailable.")
        monitor = self._user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST.
        if not monitor:
            raise RuntimeError("The pet's Windows monitor is unavailable.")
        info = self._MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise RuntimeError("The pet's Windows monitor geometry is unavailable.")
        rectangle = info.rcMonitor
        return (rectangle.left, rectangle.top,
                rectangle.right - rectangle.left, rectangle.bottom - rectangle.top)

    def _windows_client(self, hwnd):
        rectangle, origin = self._wt.RECT(), self._wt.POINT(0, 0)
        if not self._user32.GetClientRect(hwnd, ctypes.byref(rectangle)):
            return None
        if not self._user32.ClientToScreen(hwnd, ctypes.byref(origin)):
            return None
        return (origin.x, origin.y, rectangle.right - rectangle.left,
                rectangle.bottom - rectangle.top)

    def _windows(self, screen) -> bool | None:
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = self._wt.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == self._pid:
            return None
        if not self._user32.IsWindowVisible(hwnd) or self._user32.IsIconic(hwnd):
            return False
        name = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(hwnd, name, len(name))
        if name.value in {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
            return False
        if name.value == "#32768":
            return None  # Native transient menu; retain the previous observation.
        cloaked = self._wt.DWORD()
        if self._dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked),
                                            ctypes.sizeof(cloaked)) == 0 and cloaked.value:
            return False
        # Keep every comparison in native monitor pixels, even with mixed scaling.
        # Qt's logical virtual desktop can contain gaps and is not used here.
        previous_dpi = None
        if self._set_dpi_context is not None:
            previous_dpi = self._set_dpi_context(ctypes.c_void_p(-4))  # Per-monitor V2.
        try:
            display = self._windows_monitor()
            client = self._windows_client(hwnd)
            if client is None:
                return None
            if covers_display(client, display):
                return True
            # A dialog over a fullscreen owner should not resume the pet beneath it.
            owner = self._user32.GetAncestor(hwnd, 3)  # GA_ROOTOWNER.
            if owner and owner != hwnd and self._user32.IsWindowVisible(owner):
                owner_client = self._windows_client(owner)
                if owner_client is not None and covers_display(owner_client, display):
                    return True
            return False
        finally:
            if previous_dpi:
                self._set_dpi_context(previous_dpi)


def set_native_click_through(widget, enabled: bool) -> bool:
    """Change native input pass-through without recreating Qt's window/flags."""
    from PyQt6.QtGui import QGuiApplication
    platform = QGuiApplication.platformName()
    if sys.platform == "darwin" and platform == "cocoa":
        import objc
        view = objc.objc_object(c_void_p=ctypes.c_void_p(int(widget.winId())))
        window = view.window()
        if window is None:
            return False
        window.setIgnoresMouseEvents_(enabled)
        return True
    if sys.platform == "win32" and platform == "windows":
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # LONG_PTR is pointer-sized; HWND must not be truncated on 64-bit Windows.
        pointer_suffix = "PtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "W"
        get_style = getattr(user32, "GetWindowLong" + pointer_suffix)
        set_style = getattr(user32, "SetWindowLong" + pointer_suffix)
        get_style.argtypes, get_style.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t
        set_style.argtypes, set_style.restype = [
            wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t
        hwnd, index, transparent = int(widget.winId()), -20, 0x00000020
        ctypes.set_last_error(0)
        style = get_style(hwnd, index)  # GWL_EXSTYLE; keep Qt's layered/no-activate bits.
        if not style and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        updated = style | transparent if enabled else style & ~transparent
        if updated != style:
            ctypes.set_last_error(0)
            previous = set_style(hwnd, index, updated)
            if not previous and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
        # WS_EX_TRANSPARENT on a layered window updates hit testing immediately;
        # there is no non-client frame change requiring SetWindowPos.
        return True
    return False

@dataclass(frozen=True)
class Palette:
    ink: str = "#14191f"
    fur: str = "#353c46"
    shade: str = "#252c35"
    highlight: str = "#56616e"
    muzzle: str = "#65717d"
    pink: str = "#b58296"
    neon: str = "#9dff38"
    eye_light: str = "#d8ffae"
    pattern: str = "solid"
    marking: str = "#e6e6da"


PALETTES = {
    "Natural Coat": Palette(pattern="natural"),
    "Charcoal Grey": Palette(),
    "Tuxedo Black": Palette(fur="#20252b", shade="#12161d", highlight="#444c59",
                            muzzle="#e6e6da", pattern="tuxedo"),
    "Calico Orange": Palette(ink="#392b2c", fur="#e6a366", shade="#b97749",
                             highlight="#ffcd99", muzzle="#fff0d5", pink="#cb8291",
                             pattern="calico", marking="#58423b"),
}


def custom_palette(color: QColor) -> Palette:
    return Palette(fur=color.name(), shade=color.darker(145).name(),
                   highlight=color.lighter(145).name(),
                   muzzle=color.lighter(180).name())


@dataclass
class Motion:
    """Screen-space movement, independent of rendering and native windows."""

    x: float = 0.0
    y: float = 0.0
    vy: float = 0.0
    direction: int = 1
    state: str = "rest"
    decision_in: float = 3.0
    sleeping: bool = False
    roaming: bool = True
    width: float = WIDTH
    height: float = HEIGHT
    vx: float = 0.0
    flight_time: float = 0.0
    flight_duration: float = 0.0
    flight_target_y: float = 0.0
    flight_start_y: float = 0.0
    walk_speed: float = WALK_SPEED
    transition_time: float = 0.0
    transition_duration: float = 0.56
    landing_duration: float = 0.68
    turn_duration: float = 0.48
    turn_target: int = 1
    turn_from: int = 1
    turn_resume: str = "walk"
    impact_speed: float = 0.0
    sleep_after_sit: bool = False

    @staticmethod
    def limits(area: QRectF, width: float = WIDTH, height: float = HEIGHT) -> tuple[float, float, float, float]:
        left, top = area.left(), area.top()
        return left, max(left, area.right() - width), top, max(top, area.bottom() - height)

    def bounds(self, area: QRectF) -> tuple[float, float, float, float]:
        return self.limits(area, self.width, self.height)

    def constrain(self, area: QRectF) -> None:
        left, right, top, floor = self.bounds(area)
        self.x = min(right, max(left, self.x))
        self.y = min(floor, max(top, self.y))
        if self.state not in ("held", "fall", "fly") and self.y < floor:
            self.state, self.vy = "fall", 0.0
        if self.state == "fly":
            self.flight_target_y = min(floor, max(top, self.flight_target_y))

    def rest(self, rng: random.Random) -> None:
        self.state, self.vy = "rest", 0.0
        self.vx, self.transition_time = 0.0, 0.0
        self.sleeping = False
        self.decision_in = rng.uniform(3.0, 7.0)

    def begin_turn(self, direction: int, resume: str = "walk") -> None:
        """Stop travelling while the paws reposition under the turning body."""
        self.turn_from, self.turn_target = self.direction, direction
        self.turn_resume = resume
        self.state, self.transition_time = "turn", 0.0
        self.sleeping, self.vx, self.sleep_after_sit = False, 0.0, False

    def begin_rest(self, rng: random.Random) -> None:
        self.sleep_after_sit = self.roaming and rng.random() < 0.22
        self.sleeping, self.vx = False, 0.0
        self.transition_time = 0.0
        if self.state in ("walk", "turn", "stand"):
            self.state = "sit"
        elif self.sleep_after_sit:
            self.state = "settle"
        else:
            self.rest(rng)

    def begin_landing(self, speed: float) -> None:
        """Absorb the impact through the legs instead of bouncing the window."""
        self.state, self.transition_time = "land", 0.0
        self.impact_speed = max(0.0, speed)
        self.vy, self.vx, self.sleeping = 0.0, 0.0, False

    def _ground_transition(self, dt: float, rng: random.Random) -> None:
        self.transition_time += dt
        if self.state == "turn":
            if self.transition_time >= self.turn_duration * 0.5:
                self.direction = self.turn_target
            if self.transition_time >= self.turn_duration:
                self.state, self.transition_time = self.turn_resume, 0.0
            return
        duration = self.landing_duration if self.state == "land" else self.transition_duration
        if self.transition_time < duration:
            return
        previous = self.state
        self.transition_time = 0.0
        if previous == "stand":
            self.state = "walk" if self.roaming else "sit"
            if self.roaming and self.turn_target != self.direction:
                self.begin_turn(self.turn_target)
        elif previous == "sit" and self.sleep_after_sit:
            self.state = "settle"
        elif previous == "settle":
            self.rest(rng)
            self.sleeping = True
        elif previous == "wake":
            if self.turn_resume == "rest":
                self.rest(rng)
            else:
                self.state = self.turn_resume
        elif previous == "land":
            self.state, self.sleep_after_sit = "sit", False
            self.decision_in = max(self.decision_in, 2.0)
        else:
            self.rest(rng)

    def drop(self, area: QRectF) -> None:
        self.constrain(area)
        self.state, self.vy, self.sleeping = "fall", 0.0, False
        self.vx, self.transition_time = 0.0, 0.0

    def begin_flight(self, area: QRectF, rng: random.Random) -> None:
        """Begin a short flap-driven excursion without teleporting the pet."""
        self.constrain(area)
        _, _, top, floor = self.bounds(area)
        self.state, self.sleeping = "fly", False
        self.flight_time, self.flight_duration = 0.0, rng.uniform(4.8, 7.2)
        self.flight_start_y = self.y
        self.flight_target_y = max(top, floor - min(210.0, area.height() * 0.28))
        self.vx, self.vy = self.direction * 58.0, -55.0
        self.turn_target = self.direction

    def _flight_step(self, dt: float, area: QRectF, rng: random.Random) -> float:
        left, right, top, floor = self.bounds(area)
        self.flight_time += dt

        def ease(value):
            value = min(1.0, max(0.0, value))
            return value * value * (3.0 - 2.0 * value)

        climb = ease(self.flight_time / 1.1)
        target = self.flight_start_y + (self.flight_target_y - self.flight_start_y) * climb
        target += 9.0 * math.sin(self.flight_time * 1.8) * climb
        approach = ease((self.flight_time - self.flight_duration + 2.0) / 2.0)
        target += (floor - target) * approach
        # Gravity, mean aerodynamic lift, a downstroke impulse, and drag act on
        # velocity. Steering changes momentum, rather than snapping along a path.
        wing_phase = math.tau * self.flight_time / (8 * CROW_FLAP_SECONDS)
        lift = GRAVITY + 9.0 * (self.y - target) + 85.0 * math.sin(wing_phase)
        self.vy += (GRAVITY - lift - 4.5 * self.vy) * dt
        self.vy = min(160.0, max(-160.0, self.vy))
        margin = min(28.0, max(0.0, (right - left) * 0.15))
        if self.x <= left + margin:
            self.turn_target = 1
        elif self.x >= right - margin:
            self.turn_target = -1
        self.vx += (self.turn_target * 72.0 - self.vx) * min(1.0, 3.2 * dt)
        # The bird changes facing after braking through the turn, so it never
        # mirrors while still travelling quickly in the original direction.
        if abs(self.vx) > 6.0:
            self.direction = 1 if self.vx > 0 else -1
        self.x += self.vx * dt
        self.y += self.vy * dt
        if self.x < left or self.x > right:
            self.x = min(right, max(left, self.x))
            self.direction = 1 if self.x <= left else -1
            self.vx = self.direction * abs(self.vx) * 0.65
        if self.y < top:
            self.y, self.vy = top, max(0.0, self.vy)
        if (self.flight_time >= self.flight_duration - 0.1
                and self.y >= floor - 3.0 and self.vy >= -15.0):
            impact = max(0.0, self.vy)
            self.y = floor
            self.begin_landing(impact)
            return impact
        self.y = min(floor, self.y)
        return 0.0

    def step(self, dt: float, area: QRectF, rng: random.Random,
             autonomous: bool = True) -> float:
        """Advance physics and report the incoming speed of each floor impact."""
        if self.state in ("held", "latched"):
            return 0.0
        left, right, top, floor = self.bounds(area)
        self.x = min(right, max(left, self.x))
        if self.state == "fly":
            return self._flight_step(dt, area, rng)
        if self.state == "fall":
            impact_speed = 0.0
            self.y += self.vy * dt + 0.5 * GRAVITY * dt * dt
            self.vy += GRAVITY * dt
            if self.y >= floor:
                self.y = floor
                impact_speed = max(0.0, self.vy)
                self.begin_landing(impact_speed)
            self.y = max(top, self.y)
            return impact_speed
        self.y = floor
        if self.state in ("land", "turn", "stand", "sit", "settle", "wake"):
            self._ground_transition(dt, rng)
            return 0.0
        if self.state == "walk":
            self.x += self.direction * self.walk_speed * dt
            if self.x <= left:
                self.x = left
                self.begin_turn(1)
            elif self.x >= right:
                self.x = right
                self.begin_turn(-1)
            if self.state == "turn":
                return 0.0
        if self.state == PETTED or not self.roaming or not autonomous:
            return 0.0
        self.decision_in -= dt
        if self.decision_in <= 0.0:
            if self.state == "walk" or rng.random() < 0.45:
                if self.sleeping:
                    self.state, self.turn_resume = "wake", "rest"
                    self.sleeping, self.transition_time = False, 0.0
                else:
                    self.begin_rest(rng)
            else:
                self.turn_target = rng.choice((-1, 1))
                self.turn_resume = "stand"
                self.state = "wake" if self.sleeping else "stand"
                self.sleeping, self.transition_time = False, 0.0
                self.decision_in = rng.uniform(2.0, 5.0)
        return 0.0


def _draw_cat_classic(pose: str, frame: int, direction: int, tail_offset: int = 0,
             breath: int = 0, dangle: int = 0, palette: Palette = PALETTES["Charcoal Grey"]) -> QPixmap:
    """Compose a charcoal cat on a pixel grid; all pose offsets are art pixels."""
    image = QImage(ART_WIDTH, ART_HEIGHT, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setPen(Qt.PenStyle.NoPen)
    ink, fur, shade = palette.ink, palette.fur, palette.shade
    highlight, muzzle, pink = palette.highlight, palette.muzzle, palette.pink
    neon, eye_light = palette.neon, palette.eye_light

    def box(x: int, y: int, w: int, h: int, color: str) -> None:
        p.fillRect(x, y, w, h, QColor(color))

    def shape(points: list[tuple[int, int]], color: str) -> None:
        p.setBrush(QColor(color))
        p.drawPolygon(QPolygon([QPoint(x, y) for x, y in points]))

    if pose == "sleep":
        # A breathing loaf with its plume wrapped around tucked paws.
        p.translate(0, breath)
        shape([(6, 25), (9, 21), (22, 21), (27, 24), (29, 30),
               (26, 31), (8, 31), (5, 29)], ink)
        shape([(7, 25), (10, 22), (22, 22), (26, 25), (27, 29),
               (25, 30), (8, 30), (6, 28)], fur)
        shape([(22, 23), (23, 18), (27, 21), (31, 21), (35, 18),
               (36, 25), (35, 29), (23, 29), (21, 27)], ink)
        shape([(23, 24), (24, 20), (27, 22), (31, 22), (34, 20),
               (35, 25), (34, 28), (24, 28), (22, 26)], fur)
        box(24, 25, 3, 1, ink)
        box(31, 25, 3, 1, ink)
        box(28, 27, 2, 1, pink)
        box(8, 28, 14, 2, shade)
        if palette.pattern == "tuxedo":
            box(20, 28, 7, 2, palette.marking)
        elif palette.pattern == "calico":
            box(11, 23, 6, 4, palette.marking)
        box(9, 30, 13, 1, highlight)
        if frame:
            box(14, 21, 5, 1, shade)
    else:
        bob = -1 if pose == "walk" and frame % 2 else breath
        held = pose in ("held", "fall")
        if held:
            bob = -2  # Lift the torso so the loose, longer legs read as airborne.
        # A thick plume with stepped tufts; only the upper tail sways.
        shape([(12, 27), (6, 27), (3, 25), (1, 22), (2, 20),
               (0, 19 + tail_offset), (1, 15 + tail_offset),
               (3, 11 + tail_offset), (5, 9 + tail_offset),
               (7, 10 + tail_offset), (8, 13 + tail_offset),
               (6, 17 + tail_offset), (6, 20), (9, 22), (13, 23)], ink)
        shape([(11, 26), (6, 26), (4, 24), (2, 22), (3, 20),
               (1, 18 + tail_offset), (2, 15 + tail_offset),
               (4, 11 + tail_offset), (5, 10 + tail_offset),
               (6, 11 + tail_offset), (7, 13 + tail_offset),
               (5, 17 + tail_offset), (5, 21), (9, 23), (12, 24)], fur)
        box(4, 12 + tail_offset, 2, 3, highlight)
        box(2, 17 + tail_offset, 2, 2, highlight)
        # Two gait poses trade planted and lifted paws; movement itself is 60 Hz.
        stride = (1 if frame % 2 == 0 else -1) if pose == "walk" else 0
        front = pose == "pet"
        for x, lift in ((16, max(0, stride)), (26, max(0, -stride))):
            box(x, 24 + bob, 4, 7 - lift - bob, ink)
            box(x + 1, 25 + bob, 2, 5 - lift - bob, shade)
        if front:
            shape([(14, 19 + bob), (25, 19 + bob), (28, 27),
                   (27, 31), (12, 31), (11, 27)], ink)
            shape([(15, 20 + bob), (24, 20 + bob), (26, 27),
                   (25, 30), (14, 30), (13, 27)], fur)
            box(16, 22, 7, 5, shade)
        else:
            shape([(9, 20 + bob), (12, 17 + bob), (25, 17 + bob),
                   (29, 21 + bob), (28, 28), (10, 28)], ink)
            shape([(10, 20 + bob), (13, 18 + bob), (25, 18 + bob),
                   (28, 21 + bob), (27, 27), (11, 27)], fur)
            box(13, 18 + bob, 9, 1, highlight)
            box(11, 25, 16, 2, shade)
        if palette.pattern == "tuxedo":
            box(17 if front else 23, 22, 6, 5, palette.marking)
        elif palette.pattern == "calico":
            box(15, 20 + bob, 6, 4, palette.marking)
            box(23, 25, 4, 2, "#fff0d5")
        for index, (x, lift) in enumerate(((11 - stride, max(0, -stride)),
                                          (23 + stride, max(0, stride)))):
            if front:
                x, lift = (14, 22)[index], 0
            if held:
                x += dangle if index == 0 else -dangle
                y, length = 21, 11 - (1 if index == 0 else 0)
            else:
                y, length = 25, 7 - lift
            box(x, y, 5, length, ink)
            box(x + 1, y, 3, length - 1, fur)
            box(x + 1, y + length - 3, 3, 2,
                palette.marking if palette.pattern == "tuxedo" else highlight)
        # The petting pose centers the head and eyes toward the viewer.
        hx = -9 if front else 0
        p.save()
        p.translate(hx, bob)
        shape([(20, 12), (21, 5), (23, 4), (27, 8), (31, 8),
               (35, 4), (37, 5), (38, 13), (37, 20), (34, 23),
               (24, 23), (21, 20), (19, 18), (20, 17)], ink)
        shape([(21, 13), (22, 6), (23, 6), (27, 10), (31, 10),
               (35, 6), (36, 6), (37, 13), (36, 20), (33, 22),
               (24, 22), (22, 19), (21, 18)], fur)
        shape([(23, 7), (26, 11), (23, 11)], pink)
        shape([(35, 7), (35, 11), (32, 11)], pink)
        if palette.pattern == "tuxedo":
            shape([(29, 16), (32, 20), (31, 22), (26, 22)], palette.marking)
        elif palette.pattern == "calico":
            shape([(32, 10), (36, 8), (37, 14), (34, 18), (31, 16)], palette.marking)
        box(27, 10, 5, 1, highlight)
        box(25, 20, 9, 1, muzzle)
        for x in (24, 32):
            if pose == "blink":
                box(x, 16, 3, 1, neon)
            else:
                w, h = (4, 4) if held else (4, 3) if front else (3, 2)
                box(x, 14, w, h, neon)
                box(x + (1 if front or held else 2), 14, 1, h, ink)
                box(x, 14, 1, 1, eye_light)
        box(29, 18, 2, 1, pink)
        box(29, 20, 1, 1, ink)
        box(22, 18, 2, 1, highlight)
        box(35, 18, 2, 1, highlight)
        p.restore()
    p.end()
    if direction < 0:
        image = image.transformed(QTransform().scale(-1, 1))
    return QPixmap.fromImage(image).scaled(
        WIDTH, HEIGHT, Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )


def _draw_cat_expression(pose: str, frame: int, direction: int, palette: Palette,
                         stretch: int, effects: bool) -> QPixmap:
    """Expressive grids fit the same canvas and never change physics bounds."""
    image = QImage(ART_WIDTH, ART_HEIGHT, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setPen(Qt.PenStyle.NoPen)
    ink, fur, shade = palette.ink, palette.fur, palette.shade
    highlight, muzzle = palette.highlight, palette.muzzle
    airborne = pose in ("held", "fall")
    frame %= 4
    stretch = max(0, min(3, stretch))

    def box(x, y, w, h, color):
        p.fillRect(x, y, w, h, QColor(color))

    def shape(points, color):
        p.setBrush(QColor(color))
        p.drawPolygon(QPolygon([QPoint(x, y) for x, y in points]))

    def markings(front=False):
        if palette.pattern == "tuxedo":
            box(17 if front else 23, 22, 6, 5, palette.marking)
        elif palette.pattern == "calico":
            box(15, 20, 6, 4, palette.marking)
            box(23, 25, 4, 2, "#fff0d5")

    def limb(root, foot, rear=False):
        rx, ry = root
        fx, fy = foot
        shape([(rx-1, ry), (rx+2, ry), (fx+2, fy), (fx-1, fy)], ink)
        shape([(rx, ry), (rx+1, ry), (fx+1, fy), (fx, fy)], shade if rear else fur)
        box(fx-1, fy-1, 4, 3, ink)
        box(fx, fy-1, 2, 2, highlight if palette.pattern != "tuxedo" else palette.marking)

    if airborne:
        # A rigid upward plume reads as shock, even while the paws scramble.
        shape([(6, 23), (6, 8), (5, 6), (6, 2), (8, 0), (10, 2),
               (11, 5), (10, 7), (11, 20), (13, 24)], ink)
        shape([(7, 22), (7, 8), (6, 6), (7, 2), (8, 1), (9, 3),
               (10, 5), (9, 8), (10, 21), (12, 23)], fur)
        box(7, 3, 2, 3, highlight)
        # Four genuinely different foot targets create a fast paddling cycle.
        feet = (
            ((11, 27), (19, 30), (24, 30), (34, 26)),
            ((13, 30), (21, 26), (24, 26), (31, 30)),
            ((16, 30), (19, 25), (28, 29), (34, 29)),
            ((11, 25), (17, 29), (29, 30), (32, 25)),
        )[frame]
        limb((16, 21), feet[1], rear=True)
        limb((26, 21), feet[3], rear=True)
        bottom = 24 + stretch
        shape([(11, 18), (14, 16), (25, 16), (29, 20),
               (28, bottom), (24, bottom+1), (12, bottom)], ink)
        shape([(12, 18), (15, 17), (25, 17), (28, 20),
               (27, bottom-1), (24, bottom), (13, bottom-1)], fur)
        box(15, 17, 8, 1, highlight)
        markings()
        limb((14, 21), feet[0])
        limb((26, 21), feet[2])
        # Flattened ears lie nearly level with the crown instead of pointing up.
        shape([(20, 12), (20, 10), (23, 10), (22, 8), (27, 10),
               (32, 10), (37, 8), (36, 11), (38, 12), (38, 19),
               (35, 22), (24, 22), (21, 19)], ink)
        shape([(21, 12), (23, 11), (26, 11), (32, 11), (36, 10),
               (35, 12), (37, 13), (37, 19), (34, 21), (24, 21),
               (22, 18)], fur)
        box(23, 10, 3, 1, palette.pink)
        box(33, 10, 3, 1, palette.pink)
        if palette.pattern == "calico":
            shape([(32, 11), (36, 11), (37, 16), (33, 18)], palette.marking)
        elif palette.pattern == "tuxedo":
            shape([(29, 17), (33, 20), (31, 21), (26, 21)], palette.marking)
        for x in (24, 32):
            box(x, 14, 4, 4, palette.neon)
            box(x+2, 14, 1, 4, ink)
            box(x, 14, 1, 1, palette.eye_light)
        box(29, 19, 2, 1, palette.pink)
        box(29, 21, 2, 1, muzzle)
    else:
        # The plume jitters sideways in contentment while the head nuzzles.
        jitter = (-1, 1, -1, 1)[frame]
        p.save()
        p.translate(jitter, 0)
        shape([(13, 28), (7, 28), (4, 25), (2, 21), (3, 19),
               (2, 17), (4, 13), (6, 11), (8, 12), (9, 15),
               (7, 18), (7, 22), (10, 24), (14, 25)], ink)
        shape([(12, 27), (7, 27), (5, 24), (3, 21), (4, 19),
               (3, 17), (5, 14), (6, 12), (7, 13), (8, 15),
               (6, 18), (6, 22), (10, 25), (13, 26)], fur)
        box(5, 14, 2, 3, highlight)
        p.restore()
        shape([(14, 19), (25, 19), (28, 27), (27, 31),
               (12, 31), (11, 27)], ink)
        shape([(15, 20), (24, 20), (26, 27), (25, 30),
               (14, 30), (13, 27)], fur)
        box(16, 22, 7, 5, shade)
        markings(front=True)
        for x in (14, 22):
            box(x, 25, 5, 7, ink)
            box(x+1, 25, 3, 6, fur)
            box(x+1, 29, 3, 2, palette.marking if palette.pattern == "tuxedo" else highlight)
        lean, nod = ((0, 0), (1, 0), (2, 1), (1, 0))[frame]
        p.save()
        p.translate(lean, nod)
        shape([(10, 12), (11, 5), (13, 4), (17, 8), (21, 9),
               (25, 6), (27, 7), (29, 14), (28, 20),
               (25, 23), (14, 22), (11, 19)], ink)
        shape([(11, 13), (12, 6), (13, 6), (17, 10), (21, 11),
               (25, 8), (26, 8), (28, 14), (27, 20),
               (24, 22), (14, 21), (12, 18)], fur)
        shape([(13, 7), (16, 11), (13, 11)], palette.pink)
        shape([(25, 9), (26, 12), (23, 12)], palette.pink)
        if palette.pattern == "calico":
            shape([(23, 12), (27, 11), (28, 16), (24, 18)], palette.marking)
        elif palette.pattern == "tuxedo":
            shape([(20, 17), (24, 21), (22, 22), (16, 21)], palette.marking)
        # Hollow ^ arcs are closed happy eyes, not pupils in green rectangles.
        for x, y in ((14, 15), (22, 16)):
            box(x, y, 1, 1, palette.neon)
            box(x+1, y-1, 2, 1, palette.neon)
            box(x+3, y, 1, 1, palette.neon)
        box(19, 19, 2, 1, palette.pink)
        box(18, 21, 1, 1, muzzle)
        box(20, 21, 1, 1, muzzle)
        p.restore()
        if effects:
            # Keep the decorative pixels inside the same transparent art canvas.
            for hx, hy in ((3+frame, 3-frame), (33-(frame%2), 8-frame)):
                for row, bits in enumerate((10, 31, 31, 14, 4)):
                    for col in range(5):
                        if bits & (1 << (4-col)):
                            box(hx+col, hy+row, 1, 1, "#f3a7bd")
            for x, y in ((3, 19-frame), (6, 17-frame), (34, 21-frame)):
                box(x, y, 2, 1, palette.highlight)
    p.end()
    if direction < 0:
        image = image.transformed(QTransform().scale(-1, 1))
    return QPixmap.fromImage(image).scaled(WIDTH, HEIGHT,
        Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)


def draw_cat(pose, frame, direction, tail_offset=0, breath=0, dangle=0,
             palette=PALETTES["Natural Coat"], stretch=0, effects=True, target_size=None,
             motion_phase=None):
    return _asset_pixmap("Cat",pose,frame,direction,tail_offset,breath,dangle,
                         palette,stretch,effects,target_size,motion_phase)

class _PetGrid:
    """Pixel primitives shared by the species; all motion remains in the engine."""
    def __init__(self, palette):
        self.palette = palette
        self.image = QImage(ART_WIDTH, ART_HEIGHT, QImage.Format.Format_ARGB32)
        self.image.fill(Qt.GlobalColor.transparent)
        self.p = QPainter(self.image)
        self.p.setPen(Qt.PenStyle.NoPen)

    def box(self, x, y, w, h, color):
        self.p.fillRect(int(x), int(y), int(w), int(h), QColor(color))

    def poly(self, points, color):
        self.p.setBrush(QColor(color))
        self.p.drawPolygon(QPolygon([QPoint(int(x), int(y)) for x, y in points]))

    def limb(self, root, foot, rear=False):
        rx, ry = root
        fx, fy = foot
        c = self.palette
        self.poly([(rx-1,ry),(rx+2,ry),(fx+2,fy),(fx-1,fy)], c.ink)
        self.poly([(rx,ry),(rx+1,ry),(fx+1,fy),(fx,fy)], c.shade if rear else c.fur)
        self.box(fx-1,fy-1,4,3,c.ink)
        self.box(fx,fy-1,2,2,c.marking if c.pattern == "tuxedo" else c.highlight)

    def eye(self, x, y, pose, frame, vertical=0, sly=False):
        c = self.palette
        if pose in ("petted", "pet", "groom", "sleep", "yawn"):
            self.box(x,y,1,1,c.neon)
            self.box(x+1,y-1,2,1,c.neon)
            self.box(x+3,y,1,1,c.neon)
        elif pose == "blink":
            self.box(x,y+1,3,1,c.neon)
        else:
            h = 4 if pose in ("held", "fall") else 2 if sly else 3
            self.box(x,y,3,h,c.neon)
            self.box(x+1,y+max(0,min(h-1,1+vertical)),1,1,c.ink)
            if h > 2:
                self.box(x,y,1,1,c.eye_light)

    def heart(self, x, y):
        for row,bits in enumerate((10,31,31,14,4)):
            for col in range(5):
                if bits & (1 << (4-col)):
                    self.box(x+col,y+row,1,1,"#f3a7bd")

    def finish(self, direction=1):
        self.p.end()
        image = self.image if direction >= 0 else self.image.transformed(QTransform().scale(-1,1))
        return QPixmap.fromImage(image).scaled(WIDTH, HEIGHT,
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)


class _NaturalGrid:
    """A 60 x 48 grid for side-view animal anatomy; integer pixels only."""
    def __init__(self):
        self.image = QImage(60, 48, QImage.Format.Format_ARGB32)
        self.image.fill(Qt.GlobalColor.transparent)
        self.p = QPainter(self.image)
        self.p.setPen(Qt.PenStyle.NoPen)

    def box(self, x, y, w, h, color):
        self.p.fillRect(int(x), int(y), int(w), int(h), QColor(color))

    def poly(self, points, color):
        self.p.setBrush(QColor(color))
        self.p.drawPolygon(QPolygon([QPoint(int(x), int(y)) for x, y in points]))

    def line(self, points, color):
        self.p.setPen(QColor(color))
        self.p.drawPolyline(QPolygon([QPoint(int(x), int(y)) for x, y in points]))
        self.p.setPen(Qt.PenStyle.NoPen)

    def heart(self, x, y):
        for row, bits in enumerate((10, 31, 31, 14, 4)):
            for col in range(5):
                if bits & (1 << (4-col)):
                    self.box(x+col, y+row, 1, 1, "#dc8c9c")

    def finish(self, direction=1):
        self.p.end()
        image = self.image if direction >= 0 else self.image.transformed(QTransform().scale(-1, 1))
        return QPixmap.fromImage(image).scaled(WIDTH, HEIGHT,
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)


def _natural_colors(animal, palette):
    natural = palette is None or palette.pattern == "natural"
    if natural:
        if animal == "Crow":
            return dict(ink="#080d13", fur="#17212d", shade="#101722", highlight="#34485c",
                        light="#546b7b", cream="#222f3c", eye="#050809", glint="#c4d0cf",
                        nose="#10171c", pink="#6f7180", pattern="natural")
        if animal == "Fox":
            return dict(ink="#743825", fur="#f5852b", shade="#d65a2c", highlight="#ffb63e",
                        light="#ffd679", cream="#fff0d8", eye="#38234e", glint="#fff7ed",
                        nose="#302337", pink="#cf6b85", pattern="natural")
        return dict(ink="#684329", fur="#dba353", shade="#b77939", highlight="#f3c979",
                    light="#ffe3a6", cream="#ffe8bd", eye="#30251f", glint="#fff6d8",
                    nose="#382b27", pink="#e69291", pattern="natural")
    return dict(ink=palette.ink, fur=palette.fur, shade=palette.shade,
                highlight=palette.highlight, light=palette.muzzle,
                cream="#efe6d2" if animal == "Fox" else palette.muzzle,
                eye="#151a17", glint=palette.eye_light, nose=palette.ink,
                pink=palette.pink, pattern=palette.pattern)


def _natural_landing(drawer, frame, direction, palette):
    source = drawer("rest", 0, 1, palette=palette).toImage().scaled(60, 48,
        Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
    height, width = ((35,60), (48,56), (48,60))[frame % 3]
    g = _NaturalGrid()
    scaled = source.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.FastTransformation)
    g.p.drawImage((60-width)//2, 48-height, scaled)
    return g.finish(direction)


def _canine_leg(g, hip, foot, colors, rear=False, fox=False):
    """Hip/knee/hock silhouette instead of a straight rectangular cat paw."""
    hx, hy = hip
    fx, fy = foot
    bend = -3 if fox else -4
    knee = (hx+bend, hy+5)
    hock = (fx+2, fy-5)
    g.poly([(hx-2,hy),(hx+2,hy),(knee[0]+3,knee[1]),
            (hock[0]+1,hock[1]),(fx+2,fy),(fx-2,fy),
            (hock[0]-2,hock[1]),(knee[0]-1,knee[1])], colors["ink"])
    g.poly([(hx-1,hy),(hx+1,hy),(knee[0]+1,knee[1]),
            (hock[0],hock[1]),(fx+1,fy-1),(fx,fy-1),
            (hock[0]-1,hock[1]),(knee[0],knee[1])], colors["shade"] if rear else colors["fur"])
    if fox:
        g.poly([(hock[0]-1,fy-7),(hock[0]+1,fy-7),(fx+2,fy),
                (fx-2,fy),(fx-1,fy-2)], "#302b23")
    g.box(fx-2, fy-1, 5 if not fox else 4, 2, colors["ink"])
    if not fox:
        g.box(fx-1,fy-1,3,1,colors["cream"])


def _canine_foreleg(g, shoulder, foot, colors, rear=False, fox=False):
    sx, sy = shoulder
    fx, fy = foot
    elbow = (sx-1, sy+6)
    g.poly([(sx-2,sy),(sx+2,sy),(elbow[0]+1,elbow[1]),
            (fx+1,fy-2),(fx+3,fy),(fx-2,fy),
            (fx-1,fy-2),(elbow[0]-2,elbow[1])], colors["ink"])
    g.poly([(sx-1,sy),(sx+1,sy),(elbow[0],elbow[1]),
            (fx,fy-2),(fx+1,fy-1),(fx-1,fy-1)], colors["shade"] if rear else colors["fur"])
    if fox:
        g.poly([(fx-1,fy-7),(fx+1,fy-7),(fx+1,fy-2),
                (fx+2,fy),(fx-2,fy)], "#302b23")
    else:
        g.box(fx-1,fy-2,3,2,colors["cream"])
    g.box(fx-2,fy,5 if not fox else 4,1,colors["ink"])


def _side_eye(g, x, y, colors, closed=False, vertical=0, wide=False):
    if closed:
        g.line([(x-1,y),(x,y-1),(x+2,y-1),(x+3,y)], colors["ink"])
    else:
        g.box(x,y,3,2 if not wide else 3,colors["eye"])
        g.box(x+1,y+max(0,min(1,vertical)),1,1,colors["glint"])


def _dog_paw(g, root, foot, colors, far=False, hind=False):
    """Broad furred legs with a small hock and a rounded, weight-bearing paw."""
    rx, ry = root
    fx, fy = foot
    bend = -2 if hind else 0
    knee_x, knee_y = rx + bend, ry + max(2, (fy-ry)//2)
    fill = colors["shade"] if far else colors["fur"]
    g.poly([(rx-3,ry),(rx+3,ry),(knee_x+2,knee_y),(fx+2,fy-3),
            (fx+4,fy-2),(fx+4,fy),(fx-3,fy),(fx-3,fy-3),
            (knee_x-2,knee_y)], colors["ink"])
    g.box(fx-3,fy,8,1,colors["ink"])
    g.poly([(rx-2,ry),(rx+2,ry),(knee_x+1,knee_y),(fx+1,fy-3),
            (fx+3,fy-2),(fx+3,fy-1),(fx-2,fy-1),(fx-2,fy-3),
            (knee_x-1,knee_y)], fill)
    g.box(fx-1,fy-2,4,2,colors["shade"] if far else colors["cream"])
    if not far:
        g.box(fx+1,fy-1,1,1,colors["shade"])


def _dog_head(g, colors, pose, phase, dangle=0):
    """Rounded retriever skull, a broad projecting muzzle, and a hanging ear."""
    c = colors
    happy = pose in ("petted", "pet")
    # The far ear is rounded too; there are no upright, cat-like ear points.
    g.poly([(44,14),(48,14),(51,17),(51,24),(48,28),(44,26)],c["ink"])
    g.poly([(45,15),(48,15),(50,18),(50,23),(48,26),(45,24)],c["shade"])
    g.poly([(37,15),(41,12),(47,12),(51,15),(53,20),(53,23),
            (57,24),(59,26),(59,30),(56,33),(48,33),(44,35),
            (38,32),(35,27),(35,20)],c["ink"])
    g.poly([(38,16),(42,13),(47,13),(50,16),(52,20),(52,24),
            (56,25),(58,27),(58,30),(55,32),(48,32),(44,33),
            (39,31),(36,27),(36,20)],c["fur"])
    g.poly([(40,15),(44,14),(48,15),(50,18),(47,18),(44,17),
            (40,18)],c["highlight"])
    g.poly([(46,25),(50,23),(54,24),(57,26),(58,28),(57,31),
            (53,32),(47,31),(44,29)],c["cream"])
    g.box(55,26,4,3,c["nose"])
    g.box(55,26,1,1,c["light"])
    g.line([(49,30),(52,31),(55,30)],c["ink"])
    # Large soft floppy ear hangs below the cheek in a separate, readable mass.
    g.poly([(37,15),(41,16),(44,20),(43,28),(41,33),(37,34),
            (34,31),(33,25),(34,19)],c["ink"])
    g.poly([(37,17),(40,17),(42,21),(41,28),(40,31),(37,32),
            (35,30),(34,25),(35,20)],c["shade"])
    g.poly([(37,19),(39,20),(39,27),(38,29),(36,27),(36,22)],c["highlight"])
    closed = happy or pose in ("blink", "yawn", "groom", "sleep")
    if closed:
        g.line([(46,22),(47,21),(49,21),(50,22)],c["eye"])
    else:
        height = 4 if pose in ("held", "fall") else 3
        g.box(47,20,3,height,c["eye"])
        g.box(47,20+max(0,min(height-1,1+dangle)),1,1,c["glint"])
        g.box(46,18,3,1,c["shade"])
    if pose == "yawn":
        opening = (2,4,5,3)[phase]
        g.box(50,30,6,opening,c["ink"])
        g.box(51,29+opening,4,2,c["pink"])
    elif happy or pose == "groom":
        g.box(52,31,3,(2,4,3,1)[phase],c["pink"])


def draw_dog(pose, frame, direction, tail_offset=0, breath=0, dangle=0,
             palette=None, stretch=0, target_size=None, motion_phase=None):
    return _asset_pixmap("Dog",pose,frame,direction,tail_offset,breath,dangle,
                         palette,stretch,True,target_size,motion_phase)

def _fox_brush(g, c, pose, wag=0):
    """A plush brush with a broad cream tip and a clear curved silhouette."""
    air = pose in ("held", "fall")
    moving = pose in ("walk", "stretch")
    if air:
        outline = [(27,34),(18,30),(12,22),(10,13),(12,5),(16,1),
                   (19,2),(18,8),(19,15),(24,23),(31,28)]
        inside = [(27,32),(20,28),(14,21),(12,13),(14,6),(17,3),
                  (16,9),(17,16),(22,24),(29,29)]
        tip = [(12,12),(13,6),(16,2),(18,2),(17,8),(18,13),
               (16,12),(15,15)]
    elif moving:
        outline = [(26,32),(19,31),(13,28+wag),(8,22+wag),(7,18+wag),
                   (4,20+wag),(2,26+wag),(3,33+wag),(8,39+wag),
                   (16,42),(24,39),(29,35)]
        inside = [(26,33),(19,32),(12,30+wag),(8,25+wag),(6,21+wag),
                  (4,26+wag),(5,32+wag),(10,37+wag),(17,40),(24,37)]
        tip = [(7,19+wag),(5,21+wag),(3,26+wag),(4,30+wag),
               (6,32+wag),(7,28+wag),(10,30+wag),(9,26+wag)]
    else:
        outline = [(31,43),(27,34),(22,29),(17,22),(16,16+wag),
                   (18,10+wag),(16,7+wag),(12,8+wag),(8,13+wag),
                   (5,20+wag),(3,29),(3,37),(7,43),(14,47),(25,47),(32,45)]
        inside = [(29,43),(26,35),(20,30),(15,23),(14,16+wag),
                  (16,10+wag),(13,10+wag),(10,14+wag),(7,21+wag),
                  (5,29),(5,36),(9,42),(15,45),(25,45)]
        tip = [(16,8+wag),(12,10+wag),(9,14+wag),(7,20+wag),
               (8,24+wag),(10,21+wag),(13,24+wag),(12,18+wag),
               (15,20+wag),(15,15+wag),(17,11+wag)]
    g.poly(outline, c["ink"])
    g.poly(inside, c["fur"])
    if not air:
        if moving:
            g.poly([(6,30+wag),(10,35+wag),(17,38),(24,36),(23,39),
                    (16,41),(9,38+wag)], c["shade"])
            g.line([(10,30+wag),(14,34+wag),(20,35)], c["highlight"])
        else:
            g.poly([(5,31),(10,36),(18,38),(26,36),(28,42),
                    (24,45),(15,45),(9,41),(5,36)], c["shade"])
            g.poly([(7,26),(10,20+wag),(13,19+wag),(16,26),
                    (21,31),(17,31),(13,28),(12,32),(9,29)], c["highlight"])
            g.line([(9,35),(14,39),(23,41)], c["fur"])
    g.poly(tip, c["cream"])
    if not moving and not air:
        g.poly([(13,11+wag),(11,16+wag),(10,20+wag),(13,19+wag),
                (14,21+wag),(13,15+wag),(16,11+wag)], c["light"])


def _fox_face(g, c, pose, phase, origin, dangle=0):
    """The reference fox's tall ears, cheek ruff, pointed nose and glossy eyes."""
    air = pose in ("held", "fall")
    happy = pose in ("petted", "pet")
    g.p.save()
    g.p.translate(*origin)
    if air:
        outline = [(0,16),(1,11),(-2,8),(0,6),(8,9),(14,8),(21,6),
                   (24,9),(21,13),(23,15),(28,17),(29,19),(26,22),
                   (19,25),(10,26),(3,23)]
    else:
        outline = [(0,17),(1,10),(3,2),(5,0),(10,9),(14,8),(17,2),
                   (19,3),(20,11),(23,15),(28,17),(29,19),
                   (26,22),(19,25),(10,26),(3,23)]
    g.poly(outline, c["ink"])
    if air:
        fill = [(1,16),(3,12),(0,8),(7,11),(14,10),(21,8),(22,9),
                (19,13),(22,16),(27,18),(27,20),(24,22),(18,24),(10,25),(4,22)]
    else:
        fill = [(1,17),(3,11),(5,4),(9,12),(14,10),(18,6),(19,14),
                (22,16),(27,18),(27,20),(24,22),(18,24),(10,25),(4,22)]
    g.poly(fill, c["fur"])
    if not air:
        g.poly([(4,5),(5,4),(8,12),(5,15),(3,14)], c["cream"])
        g.poly([(17,6),(18,5),(19,12),(16,11)], c["shade"])
        g.line([(3,17),(5,11),(8,10)], c["highlight"])
    else:
        g.poly([(0,8),(5,10),(2,12)], c["shade"])
        g.poly([(18,9),(21,8),(20,11)], c["shade"])
    g.poly([(2,19),(6,17),(11,17),(16,20),(22,20),(27,18),
            (27,21),(23,23),(17,25),(10,26),(5,23),(2,23),(4,21)], c["cream"])
    g.poly([(9,10),(14,10),(19,14),(22,17),(17,18),(13,15),(9,15)], c["highlight"])
    closed = happy or pose in ("blink", "groom", "yawn", "sleep")
    for ex, ey, far in ((12,13,True),(20,14,False)):
        if closed:
            g.line([(ex-1,ey+2),(ex,ey+1),(ex+2,ey+1),(ex+3,ey+2)], c["eye"])
        else:
            width, height = (3,4) if far else (4,5) if air else (4,4)
            g.box(ex,ey,width,height,c["eye"])
            g.box(ex+1,ey-1,width-2,1,c["eye"])
            g.box(ex+1,ey+max(0,min(2,1+dangle)),1,2,c["glint"])
            if not far:
                g.box(ex+2,ey+height-1,1,1,c["pink"])
    g.box(27,17,3,2,c["nose"])
    g.box(28,17,1,1,c["glint"])
    g.line([(22,23),(25,22)],c["ink"])
    if pose == "yawn":
        opening = (1,3,4,2)[phase]
        g.box(23,22,4,opening,c["nose"])
        g.box(24,22+opening-1,2,1,c["pink"])
    elif happy and phase % 2:
        g.box(24,23,2,2,c["pink"])
    g.p.restore()


def _fox_paw(g, c, root, foot, far=False, hind=False):
    """Short, solid furred limbs; dark stockings stay attached to the paws."""
    rx, ry = root
    fx, fy = foot
    knee = rx - 2 if hind else rx
    shade = c["shade"] if far else c["fur"]
    g.poly([(rx-2,ry),(rx+2,ry),(knee+2,ry+4),(fx+1,fy-3),
            (fx+3,fy-1),(fx+3,fy),(fx-2,fy),(fx-2,fy-3),(knee-2,ry+4)],c["ink"])
    g.poly([(rx-1,ry),(rx+1,ry),(knee+1,ry+4),(fx,fy-2),
            (fx-1,fy-2),(knee-1,ry+4)],shade)
    g.poly([(fx-1,fy-5),(fx+1,fy-5),(fx+1,fy-2),
            (fx+2,fy-1),(fx+2,fy),(fx-2,fy),(fx-1,fy-2)],c["ink"])
    g.box(fx,fy-1,1,1,c["shade"])
    g.box(fx-2,fy,5,1,c["ink"])


def draw_fox(pose, frame, direction, tail_offset=0, breath=0, dangle=0,
             palette=None, stretch=0, target_size=None, motion_phase=None):
    return _asset_pixmap("Fox",pose,frame,direction,tail_offset,breath,dangle,
                         palette,stretch,True,target_size,motion_phase)

# The shoulder, elbow and wrist lead the primary fan in sequence. The first
# three samples are the powered downstroke; the folded recovery takes five.
_CROW_WING_ANGLES = (
    (-125,-100,-145), (-177,-177,-179), (-238,-200,-225),
    (-220,-180,-207), (-175,-192,-207), (-151,-130,-145),
    (-139,-110,-140), (-130,-100,-145),
)


def _crow_wing_points(frame, fold=0.0):
    upper,lower,fan = _CROW_WING_ANGLES[int(frame)%8]
    upper+=( -205-upper)*fold
    lower+=( -26-lower)*fold
    fan+=( -202-fan)*fold
    def point(origin,length,angle):
        angle=math.radians(angle)
        return origin[0]+length*math.cos(angle),origin[1]+length*math.sin(angle)
    shoulder=(31.0,26.0)
    elbow=point(shoulder,10.5,upper)
    wrist=point(elbow,10.0,lower)
    angle=math.radians(fan)
    ux,uy=math.cos(angle),math.sin(angle)
    nx,ny=-uy,ux
    def primary(length,width):
        return wrist[0]+length*ux+width*nx,wrist[1]+length*uy+width*ny
    # Broad coverts taper into three separate primary tips. Their lengths are
    # fixed; recovery folds at the elbow/wrist rather than shrinking the wing.
    points=[shoulder,(elbow[0]+2*nx,elbow[1]+2*ny),
            (wrist[0]+3*nx,wrist[1]+3*ny),primary(9,3),primary(10,1),
            primary(5,0),primary(10,-1),primary(10,-3),primary(4,-3),
            primary(8,-5),primary(7,-7),(wrist[0]-5*nx,wrist[1]-5*ny),
            (elbow[0]-4*nx,elbow[1]-4*ny),(shoulder[0]-3,shoulder[1]+3)]
    return [(round(max(1,min(58,x))),round(max(1,min(46,y)))) for x,y in points],shoulder,elbow,wrist


def _crow_wing(g, frame, colors, far=False, fold=0.0):
    points,root,elbow,wrist=_crow_wing_points(frame,fold)
    dx,dy=(4,-1) if far else (0,0)
    points=[(min(59,x+dx),max(1,y+dy)) for x,y in points]
    root,elbow,wrist=[(round(x+dx),round(y+dy)) for x,y in (root,elbow,wrist)]
    g.poly(points,colors["ink"])
    g.poly([root,elbow,wrist,points[-3],points[-2],points[-1]],
           colors["shade"] if far else colors["fur"])
    g.line([root,elbow,wrist],colors["cream"] if far else colors["highlight"])
    g.line([(root[0]-2,root[1]+1),(elbow[0]-1,elbow[1]+1)],colors["shade"])


def draw_crow(pose, frame, direction, tail_offset=0, breath=0, dangle=0,
              palette=None, stretch=0, motion_phase=None) -> QPixmap:
    """Articulated corvid movement drawn directly on its original pixel grid."""
    c,g=_natural_colors("Crow",palette),_NaturalGrid()
    phase=int(frame)%8 if pose in ("fly","held","fall") else int(frame)%4
    progress=max(0.0,min(1.0,motion_phase)) if motion_phase is not None else min(1.0,frame/3)
    def ease(t):
        t=max(0.0,min(1.0,t))
        return t*t*(3-2*t)
    # Anticipation / reach / quiet hold / recovery, shared by intentional acts.
    gesture=ease(progress/0.3)*(1-ease((progress-0.68)/0.32))
    happy=pose in ("petted","pet")
    perched=pose in ("rest","idle","blink")
    flying=pose=="fly"
    falling=pose=="fall"
    held=pose=="held"
    landing=pose=="land"
    if landing and motion_phase is None:
        progress=(0.22,0.62,1.0)[min(2,int(frame))]
    compression=(3*ease(progress/0.18)*(1-ease((progress-0.18)/0.65))) if landing else 0
    bob=breath
    head_x,head_y,head_angle,tail_flick=0,0,0,0
    bow=0.0
    if perched:
        # A quick inspection holds at its peak. The tail tips follow the head
        # instead of every part of the bird shifting on the same beat.
        glance=_gesture(min(1.0,progress/.80),.12,.25,.59)
        settle=_gesture(progress,.53,.63,.70)
        bob-=round(0.85*math.sin(math.pi*progress)**4)
        head_x=-round(1.5*glance)
        head_y=-round(glance)
        head_angle=round(-10*glance+3*settle)
        tail_flick=-round(2*settle)
    elif pose=="watch":
        gaze=max(-1,min(1,dangle))
        head_x=-round(gesture)
        head_y=round((gaze-1)*gesture)
        head_angle=round(10*(gaze-.3)*gesture)
        tail_flick=-round(_gesture(progress,.35,.47,.64))
    elif pose=="walk":
        walk_phase=(motion_phase if motion_phase is not None else phase/4)%1.0
        step=(walk_phase*2)%1.0
        reach=ease(step/.22)*(1-ease((step-.25)/.75))
        bob=-round(math.sin(math.pi*step)**2)
        head_x=-round(2*(1-reach))
    elif happy:
        if motion_phase is None:
            progress=phase/4
        # Bow into the touch, close the eye at the peak, and ease back to perch.
        bow=_gesture(min(1.0,progress/.90),.04,.27,.59)
        bob=round(2*bow)
        head_x=-round(bow)
        head_y=round(bow)
        head_angle=round(9*bow)
        tail_flick=-round(2*_gesture(progress,.22,.42,.58))
    if pose=="sleep":
        inhale=math.sin(math.pi*progress)**2
        rise=round(1.6*inhale)
        feathers=round(.8*inhale)
        g.poly([(9,38),(17,31-feathers),(27,28-rise),(38,30),(44,36),(43,43),(37,47),(18,47)],c["ink"])
        g.poly([(13,38),(19,32-feathers),(28,29-rise),(37,31),(42,36),(41,42),(36,45),(20,46)],c["fur"])
        g.poly([(19,36),(25,32-feathers),(34,33),(39,37),(34,43),(22,44),(16,41)],c["shade"])
        g.line([(22,35-feathers),(29,35-feathers),(35,38),(31,42)],c["highlight"])
        g.box(33,32,4,1,c["light"])
        return g.finish(direction)
    body_y=round(compression) if landing else round(math.sin(math.pi*progress)) if pose=="turn" else 2 if held else 0 if flying or falling else bob
    wing_frame=phase if flying else (5,5,4,4,5,5,6,5)[phase] if falling else 4
    open_wing=flying or falling or (landing and progress<0.78)
    fold=ease((progress-0.04)/0.66) if landing else 0.0
    if landing:wing_frame=4
    # Pitch is a small rigid adjustment of body parts, never a face warp.
    pitch=max(-1,min(1,int(dangle))) if flying else 0
    if open_wing:
        g.p.save();g.p.translate(0,body_y)
        _crow_wing(g,wing_frame,c,True,fold)
        g.p.restore()
    tail_root=body_y-pitch
    if held:tail_root+=(0,0,1,1,1,0,0,0)[phase]
    tail_y=tail_root+tail_flick
    # Original tail, coat polygons, markings and face retain their dimensions.
    g.poly([(24,31+tail_root),(17,31+tail_root),(6,35+tail_y),(4,38+tail_y),
            (8,39+tail_y),(10,37+tail_y),(10,40+tail_y),(14,39+tail_y),(23,35+tail_root)],c["ink"])
    g.poly([(22,32+tail_root),(16,33+tail_root),(7,36+tail_y),(7,37+tail_y),(14,36+tail_y),
            (13,38+tail_y),(20,35+tail_root)],c["shade"])
    g.line([(17,33+tail_root),(9,37+tail_y)],c["highlight"])
    g.poly([(18,27+body_y),(24,22+body_y),(33,20+body_y),(40,23+body_y),
            (42,28+body_y),(38,35+body_y),(31,39+body_y),(23,37+body_y),(18,33+body_y)],c["ink"])
    g.poly([(20,27+body_y),(25,23+body_y),(33,21+body_y),(39,24+body_y),
            (40,28+body_y),(37,34+body_y),(31,37+body_y),(24,36+body_y),(20,32+body_y)],c["fur"])
    g.poly([(25,34+body_y),(31,35+body_y),(37,31+body_y),(35,36+body_y),(30,38+body_y)],c["shade"])
    if open_wing:
        g.p.save();g.p.translate(0,body_y)
        _crow_wing(g,wing_frame,c,fold=fold)
        g.p.restore()
    else:
        g.poly([(22,27+body_y),(29,23+body_y),(35,25+body_y),(37,29+body_y),
                (32,35+body_y),(25,37+body_y),(21,34+body_y)],c["ink"])
        g.poly([(23,27+body_y),(29,24+body_y),(34,26+body_y),(35,29+body_y),
                (31,33+body_y),(25,35+body_y),(22,33+body_y)],c["shade"])
        g.line([(24,27+body_y),(30,26+body_y),(33,28+body_y)],c["highlight"])
        g.line([(24,30+body_y),(29,30+body_y),(33,28+body_y)],c["cream"])
        g.line([(25,33+body_y),(30,32+body_y)],c["highlight"])
        if pose in ("groom","preen","stretch"):
            spread=round(5*gesture)
            g.poly([(27,27),(18,23-spread),(12,23-spread),(15,28),(23,34),(30,33)],c["ink"])
            g.poly([(26,28),(18,25-spread),(15,25-spread),(23,32),(29,32)],c["highlight"])
    turn_toes=[]
    if flying:
        # Toes remain tucked throughout cruise; no frantic alternating pedal.
        for x in (29,35):
            if dangle>0:
                g.line([(x,36),(x-2,40),(x+1,44)],c["ink"])
                g.line([(x-2,45),(x+1,44),(x+5,45)],c["shade"])
            else:
                g.line([(x,36),(x-2,39),(x+1,40)],c["ink"])
                g.line([(x+1,40),(x+4,40),(x+3,41)],c["shade"])
    elif held:
        # Shoulder support is stable while the relaxed feet hang under the body.
        for index,x in enumerate((29,36)):
            delay=(0,1,1,0,-1,-1,0,0)[(phase+index)%8]
            g.line([(x,38),(x-1,42),(x+delay,46)],c["ink"])
            g.line([(x+delay,46),(x+3+delay,47)],c["shade"])
    else:
        for index,x in enumerate((29,36)):
            hip_x=x
            lift=0
            if pose=="walk":
                # A stance moves backwards at the exact speed of the window.
                stride=8.112  # 48 logical px/s * .52 s * .65 stance / two px per art pixel
                local=(walk_phase+index*.5)%1.0
                if local<0.65:x+=round(stride*(0.5-local/0.65))
                else:
                    swing=(local-0.65)/0.35
                    x+=round(stride*(ease(swing)-0.5))
                    lift=round(3*math.sin(math.pi*swing))
            if pose=="turn":
                # Step around the other planted foot, then exchange support.
                start=0.08 if index==0 else 0.54
                step=max(0.0,min(1.0,(progress-start)/0.36))
                arc=math.sin(math.pi*step)
                lift=round(2*arc)
                x+=round((2 if index==0 else -2)*arc*arc)
            foot_y=47-lift
            if pose=="turn":turn_toes.append((x+5,foot_y))
            # Falling opens toes before contact, in contrast to tucked flight.
            if falling:foot_y=43+min(3,int(stretch))+((0,0),(0,1),(1,1),(1,0))[phase%4][index]
            root=(hip_x,36+body_y)
            ankle=(x+1,foot_y-1)
            knee=_solve_leg(root,ankle,5.8,6.0,1)
            g.line([root,knee,ankle],c["ink"])
            g.box(x,foot_y-2,2,2,c["shade"])
            g.line([(x+1,foot_y-1),(x+6,foot_y)],c["ink"])
            g.line([(x+1,foot_y-1),(x-3,foot_y)],c["ink"])
            g.box(x+5,foot_y,2,1,c["ink"])
            g.box(x-3,foot_y,1,1,c["ink"])
    head_y+=body_y+pitch
    if pose in ("groom","preen"):
        head_x=-round(gesture)
        head_y=-round(4*gesture)
        head_angle=round(130*gesture)
    elif pose=="yawn":
        head_y-=round(2*gesture)
    g.p.save()
    g.p.translate(head_x,head_y)
    if head_angle:
        g.p.translate(40,24)
        g.p.rotate(head_angle)
        g.p.translate(-40,-24)
    g.poly([(34,24),(36,18),(40,14),(45,14),(49,17),(50,20),(55,22),
            (59,25),(59,26),(51,27),(47,28),(43,30),(37,28)],c["ink"])
    g.poly([(36,24),(38,19),(41,15),(45,15),(48,18),(49,21),(53,23),
            (56,25),(50,26),(46,27),(43,29),(38,27)],c["fur"])
    g.poly([(48,20),(54,22),(59,25),(54,25),(49,24)],c["nose"])
    g.line([(50,23),(56,25)],c["highlight"])
    g.line([(39,18),(43,16),(46,17)],c["highlight"])
    g.line([(38,24),(41,26),(44,27)],c["cream"])
    if (pose in ("blink","groom","preen") or bow>.5
            or (pose=="yawn" and gesture>.35)):
        g.line([(44,20),(47,20)],c["light"])
    else:
        g.box(45,19+max(-1,min(1,dangle)),2,2,c["eye"])
        g.box(46,19+max(-1,min(1,dangle)),1,1,c["glint"])
    if pose=="yawn":
        gap=max(0,round(5*gesture))
        g.poly([(50,25),(58,26),(51,26+gap),(48,26+gap)],c["ink"])
        if gap:g.box(50,26,3,gap,c["pink"])
    g.p.restore()
    if happy:
        for x,y,appear,leave in ((13,15,.13,.69),(51,13,.27,.81)):
            if appear<=progress<leave:
                drift=(progress-appear)/(leave-appear)
                g.heart(x+round(3*drift),y-round(10*drift))
    if pose=="turn":
        # The short turn projects toward an end-on view before facing changes.
        # Sample on the original integer grid: no blur, fading or double limbs.
        source=g.image.copy()
        g.p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        g.p.fillRect(g.image.rect(),Qt.GlobalColor.transparent)
        g.p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        width=1.0-0.76*math.sin(math.pi*progress)**2
        g.p.translate(30,0);g.p.scale(width,1);g.p.translate(-30,0)
        g.p.drawImage(0,0,source)
        g.p.resetTransform()
        # A claw stays at least one native pixel wide in the end-on view.
        for toe_x,toe_y in turn_toes:
            g.box(round(30+(toe_x-30)*width),toe_y,1,1,c["ink"])
    return g.finish(direction)


def draw_natural_peek(animal, palette=None) -> QPixmap:
    """Keep each character's face while it peeks from the physical screen edge."""
    g=_NaturalGrid();c=_natural_colors(animal,palette)
    if animal=="Crow":
        g.poly([(43,16),(43,7),(47,1),(50,0),(54,1),(57,6),(56,10),
                (59,12),(59,14),(54,14),(50,19),(45,19)],c["ink"])
        g.poly([(45,15),(45,8),(48,2),(51,1),(54,3),(55,7),(54,11),(56,13),(51,17),(47,18)],c["fur"])
        g.line([(48,4),(51,3),(53,5)],c["highlight"])
        g.box(52,7,2,2,c["eye"]);g.box(53,7,1,1,c["glint"])
        g.poly([(54,10),(59,12),(59,14),(54,13)],c["nose"])
        g.box(59,12,1,2,c["ink"])
    elif animal=="Dog":
        g.p.save()
        g.p.translate(0,-12)
        _dog_head(g,c,"rest",0)
        g.p.restore()
        g.box(42,0,5,1,c["ink"])
        g.box(59,14,1,3,c["nose"])
    else:
        _fox_face(g,c,"rest",0,(30,0))
        g.box(35,0,1,1,c["ink"])
        g.box(59,17,1,2,c["nose"])
    return g.finish(1)


def draw_cat_habit(pose, frame, direction, tail_offset=0, breath=0, dangle=0,
                   palette=PALETTES["Charcoal Grey"], stretch=0) -> QPixmap:
    """Quiet grooming, yawns, play stretches and cursor attention for the cat."""
    frame%=4
    if pose=="groom":
        source=_draw_cat_expression("petted",0,1,palette,0,False)
    else:
        source=_draw_cat_classic("rest",0,1,tail_offset,breath,0,palette)
    base=source.toImage().scaled(ART_WIDTH,ART_HEIGHT,Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.FastTransformation)
    g,c=_PetGrid(palette),palette
    if pose=="stretch":
        g.p.drawImage(0,7,base.scaled(40,25,Qt.AspectRatioMode.IgnoreAspectRatio,
                                    Qt.TransformationMode.FastTransformation))
        g.poly([(10,22),(12,16-frame%2),(19,16),(26,25),(26,28),(11,27)],c.ink)
        g.poly([(11,22),(13,18-frame%2),(18,17),(24,25),(24,27),(12,26)],c.fur)
        g.box(25,30,13,2,c.ink);g.box(26,30,11,1,c.muzzle)
    else:
        g.p.drawImage(0,0,base)
        if pose=="groom":
            target=((21,24),(22,22),(23,21),(22,23))[frame]
            g.limb((23,28),target);g.box(target[0]-1,target[1]-2,2,2,c.pink)
        elif pose=="yawn":
            for x in (24,32):
                g.box(x,14,4,4,c.fur);g.eye(x,15,"yawn",frame)
            h=(1,3,4,2)[frame]
            g.box(28,19,4,h,c.ink);g.box(29,18+h,2,1,c.pink)
        elif pose=="watch":
            for x in (24,32):
                g.box(x,13,4,5,c.fur)
                g.eye(x,14,"watch",frame,max(-1,min(1,dangle)))
            # One ear flick moves through four quiet attention poses.
            if frame%2:g.box(23,6,1,2,c.pink)
    return g.finish(direction)


def draw_peek(animal, palette):
    if animal in ("Cat","Dog","Fox"):
        return _asset_peek(animal,palette)
    return draw_natural_peek(animal,palette)


# The supplied character sheets are the runtime art. Keep them at full detail;
# the physics engine still uses the same logical-pixel window dimensions.
SPRITE_CANVAS = (400, 320)
_SPRITE_IMAGES = {}
_SPRITE_BASES = {}
_SPRITE_TINTS = {}


def _sprite_sheet(filename):
    if filename not in _SPRITE_IMAGES:
        path = Path(__file__).resolve().parent / "artwork" / filename
        image = QImage(str(path))
        if image.isNull():
            raise RuntimeError(f"Missing character artwork: {path}. Reinstall the complete app or source folder.")
        _SPRITE_IMAGES[filename] = image
    return _SPRITE_IMAGES[filename]


def _sprite_cutout(filename, rectangle):
    sheet = _sprite_sheet(filename)
    if filename in ("fox-walk-sheet.png", "fox-expression-sheet.png"):
        x,y,w,h = rectangle
        left,right = max(0,x-24),min(sheet.width(),x+w+24)
        rectangle = left,y,right-left,h
    image = sheet.copy(*rectangle).convertToFormat(QImage.Format.Format_RGBA8888)
    # The supplied Cat/Dog sheets have white backdrops. Treat only the connected
    # outside backdrop as transparent; enclosed white fur, eyes and paws survive.
    if filename in ("cat-character-sheet.png", "dog-character-sheet.png"):
        width, height = image.width(), image.height()
        raw = bytearray(image.constBits().asstring(image.sizeInBytes()))
        stride = image.bytesPerLine()
        from collections import deque
        queue = deque()
        seen = bytearray(width * height)
        def offer(x, y):
            pos = y * width + x
            if seen[pos]:
                return
            seen[pos] = 1
            i = y * stride + x * 4
            red, green, blue = raw[i:i+3]
            if min(red, green, blue) >= 238 and max(red, green, blue) - min(red, green, blue) <= 18:
                raw[i+3] = 0
                queue.append((x, y))
        for x in range(width):
            offer(x, 0)
            offer(x, height-1)
        for y in range(height):
            offer(0, y)
            offer(width-1, y)
        while queue:
            x, y = queue.popleft()
            if x: offer(x-1, y)
            if x+1 < width: offer(x+1, y)
            if y: offer(x, y-1)
            if y+1 < height: offer(x, y+1)
        image = QImage(bytes(raw), width, height, stride, QImage.Format.Format_RGBA8888).copy()
    if filename in ("fox-walk-sheet.png", "fox-expression-sheet.png"):
        # Sheet spacing is not a perfect grid. Select the main connected fox,
        # removing fragments of a neighbouring tail while retaining soft edges.
        from collections import deque
        width,height = image.width(),image.height()
        raw = bytearray(image.constBits().asstring(image.sizeInBytes()))
        alpha = raw[3::4]
        seen = bytearray(width*height)
        largest = []
        for start in range(width*height):
            if seen[start] or alpha[start] < 80:
                continue
            seen[start] = 1
            queue,component = deque([start]),[]
            while queue:
                pos = queue.popleft()
                component.append(pos)
                x,y = pos%width,pos//width
                neighbours = []
                if x: neighbours.append(pos-1)
                if x+1<width: neighbours.append(pos+1)
                if y: neighbours.append(pos-width)
                if y+1<height: neighbours.append(pos+width)
                for other in neighbours:
                    if not seen[other] and alpha[other] >= 80:
                        seen[other] = 1
                        queue.append(other)
            if len(component)>len(largest):
                largest = component
        keep = bytearray(width*height)
        for pos in largest:
            x,y = pos%width,pos//width
            for yy in range(max(0,y-1),min(height,y+2)):
                for xx in range(max(0,x-1),min(width,x+2)):
                    keep[yy*width+xx] = 1
        for pos in range(width*height):
            if not keep[pos]: raw[pos*4+3] = 0
        image = QImage(bytes(raw),width,height,width*4,QImage.Format.Format_RGBA8888).copy()
    bounds = QRegion(QPixmap.fromImage(image).mask()).boundingRect()
    if bounds.isEmpty():
        raise RuntimeError(f"Empty character frame in {filename}: {rectangle}")
    return image.copy(bounds)


def _sprite_cells(animal):
    if animal == "Cat":
        name = "cat-character-sheet.png"
        cells = [(name,(left,top,right-left,bottom-top))
                 for top,bottom,edges in ((0,330,(0,311,622,938,1264)),
                    (330,590,(0,319,595,942,1264)),(590,843,(0,320,619,947,1264)))
                 for left,right in zip(edges,edges[1:])]
        return cells, dict(rest=[1], pet=[0], air=[2,3], sleep=[4], groom=[5], walk=[6,7,8,9,10,11])
    if animal == "Dog":
        name = "dog-character-sheet.png"
        cells = [(name,(left,0,right-left,331)) for left,right in zip((0,319,611,895),(319,611,895,1200))]
        cells += [(name,(column*400,331,400,262)) for column in range(3)]
        cells += [(name,(left,593,right-left,303)) for left,right in zip((0,319,609,895),(319,609,895,1200))]
        return cells, dict(rest=[0], pet=[1], air=[2,3], sleep=[4,5], groom=[6], walk=[7,8,9,10])
    cells = [("fox-character-model.png",(0,0,1402,1122))]
    for name in ("fox-expression-sheet.png","fox-walk-sheet.png"):
        cells += [(name,(column*512,row*512,512,512)) for row in range(2) for column in range(3)]
    return cells, dict(rest=[0], pet=[1,2], air=[3,4], sleep=[5], groom=[6], walk=[7,8,9,10,11,12])


def _legacy_sprite_bases(animal):
    if animal not in _SPRITE_BASES:
        specifications, clips = _sprite_cells(animal)
        cutouts = [_sprite_cutout(name, rect) for name, rect in specifications]
        width, height = SPRITE_CANVAS
        # Cat and Dog share their sheet's original scale across ALL poses, so a
        # sleeping pet does not suddenly grow to fill the entire window.
        common = cutouts[1:] if animal == "Fox" else cutouts
        max_width = max(image.width() for image in common)
        max_height = max(image.height() for image in common)
        factor = min((width-18)/max_width, (height-6)/max_height)
        bases = []
        for index, cutout in enumerate(cutouts):
            scale = min((width-18)/cutout.width(), (height-6)/cutout.height()) if animal == "Fox" and index == 0 else factor
            sized = cutout.scaled(max(1,round(cutout.width()*scale)), max(1,round(cutout.height()*scale)),
                                 Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            canvas = QImage(width,height,QImage.Format.Format_ARGB32)
            canvas.fill(Qt.GlobalColor.transparent)
            painter = QPainter(canvas)
            painter.drawImage((width-sized.width())//2,height-sized.height(),sized)
            painter.end()
            bases.append(canvas)
        _SPRITE_BASES[animal] = bases, clips
    return _SPRITE_BASES[animal]


_MOTION_BASES = {}
ASSET_ANIMATION_SECONDS = {
    "idle": IDLE_SECONDS, "pet": PET_REACTION_SECONDS, "air": 0.72,
    "sleep": 3.6, "groom": 1.0, "watch": HABIT_DURATIONS["watch"],
}


def _sprite_bases(animal):
    """Load prepared frame canvases once, retaining the artist's frame anchors."""
    if animal in _MOTION_BASES:
        return _MOTION_BASES[animal]
    directory = Path(__file__).resolve().parent / "artwork" / "motion"
    manifest_path = directory / (animal.lower() + ".json")
    if not manifest_path.is_file():
        # Kept for the offline atlas baker and older source checkouts. Packaged
        # releases include the baked atlases, so normal playback does no cutouts.
        return _legacy_sprite_bases(animal)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    filename = manifest.get("image")
    if (manifest.get("format_version") != 1 or not isinstance(filename, str)
            or Path(filename).name != filename
            or manifest.get("cell_size") != list(SPRITE_CANVAS)):
        raise RuntimeError(f"Invalid motion atlas manifest: {manifest_path}")
    atlas = QImage(str(directory / filename))
    if atlas.isNull():
        raise RuntimeError(f"Missing motion artwork: {directory / filename}. Reinstall the complete app.")
    atlas = atlas.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    frames = []
    for entry in manifest.get("frames", []):
        rectangle = entry.get("rect") if isinstance(entry, dict) else entry
        if (not isinstance(rectangle, list) or len(rectangle) != 4
                or any(not isinstance(value, int) for value in rectangle)):
            raise RuntimeError(f"Invalid frame coordinates in {manifest_path}")
        x, y, width, height = rectangle
        if (width, height) != SPRITE_CANVAS or min(x, y) < 0 or x+width > atlas.width() or y+height > atlas.height():
            raise RuntimeError(f"Out-of-bounds frame in {manifest_path}: {rectangle}")
        frames.append(atlas.copy(x, y, width, height))
    clips = manifest.get("clips", {})
    for clip in ("rest", "walk", "pet", "air", "sleep", "groom"):
        indices = clips.get(clip)
        if (not isinstance(indices, list) or not indices
                or any(not isinstance(index, int) or not 0 <= index < len(frames) for index in indices)):
            raise RuntimeError(f"Invalid {clip} clip in {manifest_path}")
    for name, indices in clips.items():
        if (not isinstance(name, str) or not isinstance(indices, list) or not indices
                or any(not isinstance(index, int) or not 0 <= index < len(frames) for index in indices)):
            raise RuntimeError(f"Invalid motion clip in {manifest_path}")
    # Short registered key-pose inbetweens are ordinary shipped PNGs. OpenCV
    # is used only by the offline authoring tool, never the desktop app.
    transition_file = directory / (animal.lower()+"-transitions.json")
    if transition_file.is_file():
        transitions = json.loads(transition_file.read_text(encoding="utf-8"))
        transition_atlas = QImage(str(directory / (animal.lower()+"-transitions.png")))
        if transition_atlas.isNull() or transitions.get("cell_size") != list(SPRITE_CANVAS):
            raise RuntimeError(f"Invalid transition artwork: {transition_file}")
        offset = len(frames)
        count = max(index for indices in transitions["clips"].values() for index in indices)+1
        columns = transitions["columns"]
        for index in range(count):
            frames.append(transition_atlas.copy(index%columns*400,index//columns*320,400,320))
        for name,indices in transitions["clips"].items():
            clips["transition_"+name] = [offset+index for index in indices]
    _MOTION_BASES[animal] = frames, clips
    return _MOTION_BASES[animal]


def _sprite_tinted(animal, index, palette):
    bases, _ = _sprite_bases(animal)
    if palette is None or palette.pattern == "natural":
        return bases[index]
    key = animal, index, palette
    if key not in _SPRITE_TINTS:
        # Walking now displays a different full drawing on most ticks. Build the
        # coat mask with native image operations so a first lap in a new colour
        # fits the same frame budget as subsequent, cached laps.
        from PIL import Image, ImageChops
        image = bases[index].convertToFormat(QImage.Format.Format_RGBA8888)
        size = image.width(), image.height()
        source = Image.frombytes("RGBA", size, image.constBits().asstring(image.sizeInBytes()))
        red, green, blue, alpha = source.split()
        bright = ImageChops.lighter(red, ImageChops.lighter(green, blue))
        green_limit = green.point([min(255, int(v * 1.08)) for v in range(256)])
        blue_limit = blue.point([min(255, int(v * 1.35)) for v in range(256)])
        orange = ImageChops.subtract(red, ImageChops.lighter(green_limit, blue_limit))
        orange = ImageChops.darker(orange.point(lambda v: 255 if v else 0),
                                   red.point(lambda v: 255 if v > 95 else 0))
        target = QColor(palette.fur)
        def apply_coat(mask, base, scale):
            shades = []
            for value in range(256):
                ratio = base + scale * value / 255
                if 0.91 <= ratio <= 1.05:
                    ratio = 1.0
                # Preserve the target hue when the brightest channel saturates.
                shades.append(min(ratio, 255 / max(1, target.red(), target.green(), target.blue())))
            channels = [bright.point([round(channel * ratio) for ratio in shades])
                        for channel in (target.red(), target.green(), target.blue())]
            source.paste(Image.merge("RGBA", (*channels, alpha)), mask=mask)
        if animal == "Dog":
            dim = ImageChops.darker(red, ImageChops.darker(green, blue))
            neutral = ImageChops.subtract(bright, dim).point(lambda v: 255 if v < 48 else 0)
            charcoal = ImageChops.darker(neutral, bright.point(lambda v: 255 if 35 < v < 155 else 0))
            apply_coat(charcoal, 0.65, 0.90)
        apply_coat(orange, 0.55, 0.55)
        tinted = QImage(source.tobytes(), *size, image.bytesPerLine(), QImage.Format.Format_RGBA8888).copy()
        if len(_SPRITE_TINTS) >= 96:
            _SPRITE_TINTS.pop(next(iter(_SPRITE_TINTS)))
        _SPRITE_TINTS[key] = tinted
    return _SPRITE_TINTS[key]


WALK_DUTY = 0.68
WALK_FLOOR = 318.0
# One intact standing drawing supplies every texture in a walk. Limbs are
# connected at fixed joints; independently generated poses are never mixed.
WALK_RIGS = {
    "Cat": {
        "frame": 6, "stride": 48.0, "lift": 18.0, "body_drop": 12.0, "speed": 19.0,
        "body": ((0,0),(400,0),(400,216),(345,216),(320,232),(299,237),
                 (294,222),(245,220),(231,228),(204,229),(183,237),
                 (166,242),(139,242),(122,227),(109,215),(0,215)),
        "limbs": {
            "hind": {"root": (155,215), "knee": (135,262), "ankle": (132,289),
                     "foot": (143,310), "sole": 310, "bend": 1,
                     "outline": ((131,200),(180,204),(185,232),(164,254),
                                 (154,275),(153,292),(168,300),(168,315),
                                 (124,318),(109,307),(101,280),(103,257),(122,233))},
            "fore": {"root": (272,213), "knee": (269,259), "ankle": (273,289),
                     "foot": (283,313), "sole": 313, "bend": 1,
                     "outline": ((249,199),(299,202),(301,235),(296,261),
                                 (294,288),(309,300),(308,316),(259,318),
                                 (247,303),(247,270),(245,233))},
        },
    },
    "Dog": {
        "frame": 0, "stride": 62.0, "lift": 21.0, "body_drop": 13.0, "speed": 25.0,
        "body": ((0,0),(400,0),(400,225),(300,225),(291,230),(267,234),
                 (248,236),(222,230),(203,229),(185,231),(171,227),
                 (156,220),(146,214),(131,220),(112,230),(92,232),
                 (75,225),(0,225)),
        "limbs": {
            "hind": {"root": (111,215), "knee": (109,259), "ankle": (119,285),
                     "foot": (120,313), "sole": 313, "bend": 1,
                     "texture_from": "fore",
                     "outline": ((77,205),(108,203),(126,220),(119,240),
                                 (103,254),(95,267),(91,280),(86,291),(91,302),
                                 (89,310),(75,315),(52,312),(46,304),(48,282),
                                 (51,260),(56,242),(69,226))},
            "fore": {"root": (236,220), "knee": (231,259), "ankle": (240,285),
                     "foot": (241,313), "sole": 313, "bend": 1,
                     "outline": ((208,212),(250,208),(252,249),(250,273),
                                 (254,287),(263,297),(264,307),(255,314),
                                 (218,314),(208,302),(204,281),(202,257),(201,238))},
        },
    },
    "Fox": {
        "frame": 6, "stride": 38.0, "lift": 15.0, "body_drop": 9.0, "speed": 16.5,
        "body": ((0,0),(400,0),(400,229),(333,229),(320,238),(303,245),
                 (283,248),(268,246),(251,240),(228,238),(208,242),
                 (194,248),(174,249),(151,246),(135,240),(126,232),
                 (111,235),(102,243),(78,243),(49,226),(0,226)),
        "limbs": {
            "hind": {"root": (165,224), "knee": (164,260), "ankle": (168,288),
                     "foot": (178,315), "sole": 315, "bend": 1,
                     "texture_from": "fore", "paw_width": 0.78,
                     "outline": ((140,207),(166,201),(191,209),(204,225),
                                 (199,243),(180,255),(148,267),(137,277),
                                 (135,288),(130,296),(119,301),(109,296),
                                 (105,288),(108,272),(113,255),(120,241),(130,226))},
            "fore": {"root": (280,234), "knee": (283,266), "ankle": (287,288),
                     "foot": (300,315), "sole": 315, "bend": 1,
                     "outline": ((252,221),(287,216),(310,234),(305,260),
                                 (302,283),(303,290),(316,294),(323,301),
                                 (323,313),(313,319),(283,319),(274,311),
                                 (269,296),(267,275),(256,253))},
        },
    },
}
_WALK_TEXTURES = {}


def _walking_speed(animal, width=WIDTH):
    # A smaller animal covers fewer pixels per step, rather than paddling
    # faster to maintain the same screen speed. Crow keeps its existing pace.
    rig = WALK_RIGS.get(animal)
    return rig["speed"]*width/WIDTH if rig else WALK_SPEED


def _walk_foot(phase, center_x, floor_y, stride, lift):
    """A planted stance followed by one low swing, with matching end tangents."""
    phase = float(phase) % 1.0
    if phase < WALK_DUTY:
        return center_x + stride*(0.5-phase/WALK_DUTY), floor_y, True
    t = (phase-WALK_DUTY)/(1-WALK_DUTY)
    # Hermite tangents match the backward ground speed at both contact joins.
    tangent = -(1-WALK_DUTY)/WALK_DUTY
    progress = (-2*t**3+3*t*t) + (2*t**3-3*t*t+t)*tangent
    return (center_x + stride*(progress-0.5),
            floor_y-lift*math.sin(math.pi*t)**2, False)


def _solve_leg(root, target, upper_length, lower_length, bend):
    """Two-bone inverse kinematics; link lengths never depend on the frame."""
    dx, dy = target[0]-root[0], target[1]-root[1]
    distance = max(1e-8, math.hypot(dx,dy))
    reach = min(upper_length+lower_length-1e-7,
                max(abs(upper_length-lower_length)+1e-7,distance))
    along = (upper_length**2-lower_length**2+reach**2)/(2*reach)
    height = math.sqrt(max(0.0,upper_length**2-along**2)) * bend
    ux, uy = dx/distance, dy/distance
    return root[0]+ux*along-uy*height, root[1]+uy*along+ux*height


def _walk_path(points):
    path = QPainterPath()
    path.moveTo(*points[0])
    for point in points[1:]:
        path.lineTo(*point)
    path.closeSubpath()
    return path


def _walk_textures(animal, palette):
    key = animal, palette
    if key not in _WALK_TEXTURES:
        rig = WALK_RIGS[animal]
        _bases, clips = _sprite_bases(animal)
        source = _sprite_tinted(animal,clips["walk"][rig["frame"]],palette)
        def cut(points, top=0, bottom=320, dark=False):
            image = QImage(*SPRITE_CANVAS,QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            painter.setClipPath(_walk_path(points))
            painter.setClipRect(QRectF(0,top,400,bottom-top),Qt.ClipOperation.IntersectClip)
            painter.drawImage(0,0,source)
            if dark:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
                painter.fillRect(image.rect(),QColor(25,17,30,42))
            painter.end()
            return image
        textures = {"body": cut(rig["body"])}
        sources = {limb.get("texture_from",name) for name,limb in rig["limbs"].items()}
        for name in sources:
            limb = rig["limbs"][name]
            for far in (False,True):
                textures[name,far] = (
                    cut(limb["outline"],bottom=limb["knee"][1]+1.2,dark=far),
                    cut(limb["outline"],top=limb["knee"][1]-1.2,
                        bottom=limb["ankle"][1]+1.2,dark=far),
                    cut(limb["outline"],top=limb["ankle"][1]-1.2,dark=far),
                )
        if len(_WALK_TEXTURES) >= 6:
            _WALK_TEXTURES.pop(next(iter(_WALK_TEXTURES)))
        _WALK_TEXTURES[key] = textures
    return _WALK_TEXTURES[key]


def _walk_body_drop(rig, phase):
    step = math.sin(phase*math.tau*2)
    # Weight settles down into each step, leaving reach for the extended paw.
    return rig["body_drop"] + 0.45*step + 2.8*step**2


def _walk_leg_pose(animal, name, phase, far=False):
    rig = WALK_RIGS[animal]
    limb = rig["limbs"][name]
    # A four-beat walk: same-side hind, fore, opposite hind, fore. Far legs
    # attach a little behind the visible side, rather than fanning sideways.
    offset = (0.25 if name == "fore" else 0.0) + (0.5 if far else 0.0)
    side_x, side_y = (13.0,-3.0) if far else (0.0,0.0)
    root = limb["root"][0]+side_x, limb["root"][1]+_walk_body_drop(rig, phase)
    center = limb["root"][0]+side_x-2.0
    target_x,target_y,contact = _walk_foot(phase-offset,center,
        limb["ankle"][1]+WALK_FLOOR-limb["sole"]+side_y,rig["stride"],rig["lift"])
    target = target_x,target_y
    upper = math.dist(limb["root"],limb["knee"])
    lower = math.dist(limb["knee"],limb["ankle"])
    knee = _solve_leg(root,target,upper,lower,limb["bend"])
    return root,knee,target,contact


def _ease(value):
    value = max(0.0, min(1.0, value))
    return value*value*(3-2*value)


def _gesture(phase, anticipate=0.16, arrive=0.43, release=0.68):
    """Reach, hold, recover. The hold is genuinely still, not a sine wave."""
    if phase < anticipate:
        return 0.0
    if phase < arrive:
        return _ease((phase-anticipate)/(arrive-anticipate))
    return 1.0-_ease((phase-release)/(1-release))


def _standing_geometry(animal, action, phase, dangle=0, stretch=0):
    """Rigid trunk and two fixed-length links per leg, with floor-space targets."""
    rig = WALK_RIGS[animal]
    hip = rig["limbs"]["hind"]["root"]
    angle, dx, dy, head_angle, tail_angle = 0.0,0.0,rig["body_drop"],0.0,0.0
    amount = 0.0
    if action == "stretch":
        amount = _gesture(phase,0.08,0.40,0.69)
        angle, dx, dy = 23*amount, {"Cat":-25,"Dog":-15,"Fox":-20}[animal]*amount, rig["body_drop"]-8*amount
        head_angle = -8*amount
        tail_angle = 5*_gesture(phase, 0.14, 0.53, 0.75)
    elif action == "land":
        # Contact -> deceleration -> restrained rebound -> settled stance.
        compression = (_ease(phase/0.25) if phase < 0.25 else
                       1-_ease((phase-0.25)/0.43) if phase < 0.68 else
                       -0.10*math.sin(math.pi*(phase-0.68)/0.32))
        dy += 18*compression
        angle = 3*compression
        head_angle = -5*compression
        tail_angle = 9*_gesture(phase, 0.08, 0.34, 0.43)
    elif action in ("held","fall"):
        settle = _ease(phase/0.60)
        angle = -3.5*settle if action == "held" else -3*(1-settle)
        # Support under the shoulders. The hindquarters hang below that point;
        # no running/paddling cycle is played while suspended.
        hip = rig["limbs"]["fore"]["root"]
        dy = 3
        dx = 0
        head_angle = -angle*0.65
        if action == "held":
            tail_angle = (17*math.sin(phase*math.pi*2)*math.exp(-phase*3)
                          + max(-9,min(9,dangle*3)))
        else:
            tail_angle = 8*(1-settle)
    elif action == "turn":
        amount = math.sin(math.pi*phase)**2
        dy += 5*amount
        head_angle = -8*_gesture(phase, 0.02, 0.28, 0.52)
        tail_angle = 8*_gesture(phase, 0.18, 0.55, 0.71)
    def body_point(point):
        rad = math.radians(angle)
        x,y = point[0]-hip[0],point[1]-hip[1]
        return hip[0]+dx+x*math.cos(rad)-y*math.sin(rad),hip[1]+dy+x*math.sin(rad)+y*math.cos(rad)
    def foot_target(name, limb, far):
        side_x,side_y = (11.0,-3.0) if far else (0.0,0.0)
        ankle_y = limb["ankle"][1]+WALK_FLOOR-limb["sole"]+side_y
        target = (limb["root"][0]-2+side_x,ankle_y)
        contact = True
        if action == "stretch" and name == "fore":
            # A small swing reaches to the forward support. Once the chest is
            # low, the paws stay exactly there through the quiet stretch hold.
            lift = 10*math.sin(math.pi*amount)**2
            reach_forward = {"Cat": 40.0, "Dog": 20.0, "Fox": 15.0}[animal]
            target = target[0]+reach_forward*amount,target[1]-lift
            contact = amount < 1e-7 or amount > 1-1e-7
        elif action == "turn":
            lift = math.sin(math.pi*min(1,phase*2)) if far else math.sin(math.pi*max(0,(phase-.5)*2))
            target = target[0],target[1]-7*max(0,lift)
            contact = lift < 1e-7
        return target,contact

    if action not in ("held","fall"):
        # Solve body height against all planted/swing targets, never clamp an
        # ankle off its intended path. This is a rigid torso translation; leg
        # links retain their measured lengths and contact soles stay level.
        extra_drop = 0.0
        for far in (True,False):
            for name,limb in rig["limbs"].items():
                root = body_point(limb["root"])
                root = root[0]+(11 if far else 0),root[1]
                target,_contact = foot_target(name,limb,far)
                reach = math.dist(limb["root"],limb["knee"])+math.dist(limb["knee"],limb["ankle"])-0.5
                horizontal = target[0]-root[0]
                needed_y = target[1]-math.sqrt(max(0,reach*reach-horizontal*horizontal))
                extra_drop = max(extra_drop,needed_y-root[1])
        dy += extra_drop

    legs = {}
    for far in (True,False):
        side_x = 11.0 if far else 0.0
        for name,limb in rig["limbs"].items():
            root = body_point(limb["root"])
            root = root[0]+side_x,root[1]
            target,contact = foot_target(name,limb,far)
            if action in ("held","fall"):
                ready = _ease(phase) if action=="fall" else 0.0
                hang = 0.90 if name=="fore" else 0.86
                length = math.dist(limb["root"],limb["knee"])+math.dist(limb["knee"],limb["ankle"])
                target = (root[0]+(5 if name=="fore" else -7)*(1-ready),
                          root[1]+length*(hang+0.04*ready))
                contact = False
            upper = math.dist(limb["root"],limb["knee"])
            lower = math.dist(limb["knee"],limb["ankle"])
            knee = _solve_leg(root,target,upper,lower,limb["bend"])
            legs[name,far] = root,knee,target,contact
    return body_point,angle,head_angle,tail_angle,legs


def _articulated_image(animal, action, phase, palette, dangle=0, stretch=0):
    rig,textures = WALK_RIGS[animal],_walk_textures(animal,palette)
    image = QImage(*SPRITE_CANVAS,QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    pivot = rig["limbs"]["fore" if action in ("held", "fall") else "hind"]["root"]
    if action == "walk":
        drop = _walk_body_drop(rig, phase)
        body_point = lambda p: (p[0], p[1]+drop)
        angle = 0.0
        head_angle = 3*math.sin(phase*math.tau*2-0.5)
        tail_angle = 12*math.sin(phase*math.tau-0.7)
        legs = {(n,f):_walk_leg_pose(animal,n,phase,f) for n in rig["limbs"] for f in (True,False)}
    else:
        body_point,angle,head_angle,tail_angle,legs = _standing_geometry(animal,action,phase,dangle,stretch)
    def segment(texture, source_start, source_end, start, end):
        # Connected cross-sections share endpoints; fixed-length IK determines
        # the joint positions. Overlapping fur hides both joint cross-sections.
        source_dy = source_end[1]-source_start[1]
        shear = ((end[0]-start[0])-(source_end[0]-source_start[0]))/source_dy
        scale_y = (end[1]-start[1])/source_dy
        painter.save()
        painter.setWorldTransform(QTransform(1,0,shear,scale_y,
            start[0]-source_start[0]-shear*source_start[1],start[1]-scale_y*source_start[1]))
        painter.drawImage(0,0,texture)
        painter.restore()
    def leg(name,far):
        limb = rig["limbs"][name]
        source_name = limb.get("texture_from",name)
        source_limb = rig["limbs"][source_name]
        root,knee,ankle,_contact = legs[name,far]
        upper,lower,_paw = textures[source_name,far]
        segment(upper,source_limb["root"],source_limb["knee"],root,knee)
        segment(lower,source_limb["knee"],source_limb["ankle"],knee,ankle)
        paw_name = limb.get("paw_from",source_name)
        paw_limb = rig["limbs"][paw_name]
        painter.save()
        painter.translate(*ankle)
        painter.scale(limb.get("paw_width",1.0),1.0)
        painter.drawImage(QPointF(-paw_limb["ankle"][0],-paw_limb["ankle"][1]),textures[paw_name,far][2])
        painter.restore()
    for name in ("hind","fore"):
        leg(name,True)
    for name in ("hind","fore"):
        leg(name,False)
    # Bend the continuous painting at the neck and tail. The leg roots sit
    # below the deformation, where the body and leg textures meet.
    body_image = textures["body"]
    follow = tail_angle*_ANIMATION_STYLE[animal][0]
    if abs(follow) > .01 or abs(head_angle) > .01:
        body_image = _deform_action(body_image, animal, "standing",
                                    ((follow, -0.45*follow), (0, 0)), head_angle)
    painter.translate(*body_point(pivot))
    painter.rotate(angle)
    painter.drawImage(QPointF(-pivot[0], -pivot[1]), body_image)
    painter.end()
    return image


def _walk_image(animal, phase, palette):
    return _articulated_image(animal,"walk",float(phase)%1.0,palette)


# Each point is a landmark in the 400 × 320 approved drawing. Its influence
# tapers into the surrounding fur. The floor is pinned for seated poses.
#                   tail tip  face     chest     forward paw  rear paw
_ACTION_LANDMARKS = {
    "Cat": {"idle": ((91,130),(283,94),(239,195),(220,49)), "pet": ((89,149),(278,108),(244,202)),
            "air": ((65,53),(306,91),(235,170),(342,236),(88,267),(153,291)),
            "sleep": ((175,274),(265,211),(158,177)),
            "groom": ((111,185),(251,145),(240,233),(278,207)),
            "standing": ((73,75),(314,94)),
            "stretch": ((82,95),(318,88),(263,191),(279,298))},
    "Dog": {"idle": ((126,187),(260,101),(247,195),(216,40)), "pet": ((116,178),(261,99),(243,192)),
            "air": ((87,72),(279,86),(223,179),(290,284),(41,264),(118,280)),
            "sleep": ((179,284),(250,202),(155,182)),
            "groom": ((115,222),(212,165),(208,238),(201,229)),
            "standing": ((102,91),(296,94)),
            "stretch": ((102,91),(296,94),(245,188),(263,296))},
    "Fox": {"idle": ((132,104),(270,91),(250,191),(239,36)), "pet": ((122,101),(271,89),(247,184)),
            "air": ((116,78),(286,77),(241,181),(294,241),(195,287),(228,279)),
            "sleep": ((243,256),(268,178),(146,159)),
            "groom": ((91,103),(283,115),(241,206),(331,172)),
            "standing": ((109,57),(312,92)),
            "stretch": ((113,56),(312,92),(264,203),(302,298))},
}

# Tail amplitude, idle head pitch, and direction of the petting lean.
_ANIMATION_STYLE = {
    "Cat": (1.0, 3.0, (3, 12)),
    "Dog": (1.25, -4.0, (8, 8)),
    "Fox": (0.85, -5.0, (-4, -9)),
}

# Open and closed eye centres from the matching idle and petting drawings.
_EYES = {
    "Cat": ((280,102,269,117,21,16),(326,91,321,104,12,15)),
    "Dog": ((256,108,257,106,19,16),(304,103,309,97,13,15)),
    "Fox": ((274,100,264,107,15,14),(307,95,304,100,11,12)),
}


def _expression_image(animal, source, closed, eyelid, yawn):
    """Lower the actual drawn eyelids over the seated face without moving its features."""
    source = source.copy()
    painter = QPainter(source)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for x, y, closed_x, closed_y, rx, ry in _EYES[animal]:
        # Cover the iris from above as the eyelid travels down.
        target = QRectF(x-rx, y-ry, 2*rx, 2*ry)
        outline = QPainterPath()
        outline.addEllipse(target)
        painter.save()
        painter.setClipPath(outline)
        painter.setClipRect(QRectF(x-rx, y-ry, 2*rx, 2*ry*eyelid),
                            Qt.ClipOperation.IntersectClip)
        painter.drawImage(target, closed, QRectF(closed_x-rx,closed_y-ry,2*rx,2*ry))
        painter.restore()
    if yawn > 0.03:
        # The jaw hinges below the muzzle; leave the nose and whiskers intact.
        x, y, w = {"Cat": (311,131,19), "Dog": (287,138,26), "Fox": (312,124,17)}[animal]
        depth = (24 if animal == "Dog" else 21)*yawn
        mouth = QPainterPath(QPointF(x-w/2,y))
        mouth.quadTo(x,y+2, x+w/2,y-2)
        mouth.cubicTo(x+w/2,y+depth, x-w/4,y+depth+4, x-w/2,y)
        painter.fillPath(mouth, QColor("#59323a"))
        painter.setClipPath(mouth)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#d88986"))
        painter.drawEllipse(QRectF(x-w/4,y+depth*0.7,w*0.65,depth*0.55))
    painter.end()
    return source


def _action_motion(animal, action, phase, dangle, stretch):
    """Offsets of the painted body regions at this point in the gesture."""
    theta = phase*math.tau
    wave, follow = math.sin(theta), math.sin(theta-0.6)
    lift = (1-math.cos(theta))*0.5
    if action == "sleep":
        return ((0,0),(0,-1.1*lift),(0,-4.0*lift))
    if action == "groom":
        return ((5*follow,-2*follow),(3.5*wave,5*wave),(1.4*wave,1.5*wave),
                (5*follow,-8*follow))
    if action == "stretch":
        return ((-8*lift,-10*lift),(1*lift,33*lift),(0,21*lift),(14*lift,0))
    if action == "air":
        # Extend one pair as the other tucks. The return takes the paws under
        # the body, so each foot follows an arc rather than a straight shuttle.
        speed_stretch = min(3,max(0,stretch))
        return ((9*follow,-4*follow),(-2.3*wave,-1.7*follow),
                (-1.5*wave,-1.8*speed_stretch),(20*wave,-8*lift-1+speed_stretch),
                (-18*wave,-7*(1-lift)),(15*wave,-6*lift))
    if action == "yawn":
        return ((4*follow,-2*follow),(-4*lift,-10*lift),(0,-4*lift))
    if action == "land":
        # Compress immediately on impact, rebound, then settle to neutral.
        compression = math.sin(phase*math.pi*1.8)*math.sin(phase*math.pi)*(1-phase)*3.0
        return ((-4*compression,6*compression),(0,17*compression),(0,10*compression))
    return ((0, 0), (0, 0), (0, 0))


def _seated_motion(animal, action, phase, dangle):
    wag, pitch, (lean_x, lean_y) = _ANIMATION_STYLE[animal]
    if action == "pet":
        lean = _gesture(phase, 0.02, 0.27, 0.56)
        follow = _gesture(phase, 0.11, 0.32, 0.64)
        wag_count = 2.5 if animal == "Dog" else 1
        tail = 19*wag*follow*math.sin(phase*math.tau*wag_count)
        return ((tail, -0.45*tail), (lean_x*lean, lean_y*lean),
                (2*lean, 4*lean), (0, -3*follow)), 5*lean, lean
    if action == "watch":
        look = _gesture(phase, 0, 0.22, 0.63)
        tail = 9*wag*_gesture(phase, 0.16, 0.41, 0.63)
        tilt = look*(4*dangle-2)
        return ((tail, -0.45*tail), (5*look, 5*dangle*look),
                (2*look, -look), (0, -4*look)), tilt, 0

    # A quick ear flick leads a held head pose. The tail settles after the head.
    breath = 2*(1-math.cos(math.tau*2*phase))
    ear = _gesture((phase-0.21)/0.19, 0, 0.25, 0.36)
    look = _gesture(phase, 0.27, 0.40, 0.57)
    swish = _gesture((phase-0.36)/0.39, 0, 0.36, 0.46)
    counter = _gesture((phase-0.68)/0.24, 0, 0.36, 0.43)
    tail = wag*(15*swish-6*counter)
    return ((tail, -0.48*tail), (3*look, -breath*0.55),
            (0, -breath), (-6*ear, -7*ear)), pitch*look, 0


@functools.lru_cache(maxsize=24)
def _action_mesh(animal, rig):
    """Precompute skinning weights on a common lattice. Adjacent cells share edges."""
    points = _ACTION_LANDMARKS[animal][rig]
    radii = ((118,154),(134,133),(95,127),(72,82),(65,75),(53,66))
    vertices = []
    for y in range(0,321,32):
        for x in range(0,401,40):
            weights = []
            for index, ((cx,cy), (rx,ry)) in enumerate(zip(points,radii)):
                if rig == "idle" and index == 3:
                    rx, ry = 59, 67
                radial = ((x-cx)/rx)**2+((y-cy)/ry)**2
                # Move the face together, with a wider taper at its boundary.
                weights.append(_ease((1-radial)/0.56) if index == 1 and rig in ("idle", "standing")
                               else _ease(1-radial))
            if rig == "standing":
                pin = _ease((202-y)/40)
                weights = [w*pin for w in weights]
            elif rig not in ("air","stretch"):
                pin = max(0,min(1,(310-y)/38))
                weights = [w*pin for w in weights]
            vertices.append((x,y,weights))
    tiles = []
    for row in range(10):
        for col in range(10):
            a = row*11+col
            corners = (a,a+1,a+12,a+11)
            tile = QRectF(col*40,row*32,40,32)
            polygon = QPolygonF([QPointF(vertices[k][0],vertices[k][1]) for k in corners])
            tiles.append((corners,tile,polygon))
    return vertices, tiles


def _deform_action(source, animal, rig, offsets, head_angle=0):
    # Each destination tile samples one continuous patch of the original
    # drawing. Clip in destination coordinates to avoid cracks or overlapping fur.
    vertices, tiles = _action_mesh(animal,rig)
    angle = math.radians(head_angle)
    sin_angle, cos_angle = math.sin(angle), math.cos(angle)
    neck_x, neck_y = _ACTION_LANDMARKS[animal][rig][1]
    neck_y += 49
    samples = []
    for x,y,weights in vertices:
        dx, dy = 0.0, 0.0
        for weight,(ox,oy) in zip(weights,offsets):
            dx += weight*ox
            dy += weight*oy
        if head_angle:
            rx, ry = x-neck_x, y-neck_y
            dx += weights[1]*(rx*(cos_angle-1)-ry*sin_angle)
            dy += weights[1]*(rx*sin_angle+ry*(cos_angle-1))
        samples.append(QPointF(x-dx,y-dy))
    image = QImage(*SPRITE_CANVAS,QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    transform = QTransform()
    for corners,tile,destination in tiles:
        src = QPolygonF([samples[k] for k in corners])
        if QTransform.quadToQuad(src,destination,transform):
            painter.resetTransform()
            painter.setClipRect(tile)
            painter.setTransform(transform)
            painter.drawImage(0,0,source)
    painter.end()
    return image


def _key_transition(animal, pose, phase, palette):
    _, clips = _sprite_bases(animal)
    indices = clips.get("transition_"+pose)
    if indices is None:
        raise RuntimeError(f"Missing {animal} {pose} transition artwork; reinstall the complete app.")
    last = len(indices)-1
    index = round(_ease(phase)*last)
    if index == 0 or index == last:
        # Use the live pose at each join. The baked PNG and the live renderer
        # can round translucent edge colors differently on Windows.
        start, end = {
            "stand": ("idle", "standing"), "sit": ("standing", "idle"),
            "settle": ("idle", "sleep"), "wake": ("sleep", "idle"),
            "groom": ("idle", "groom"), "ungroom": ("groom", "idle"),
        }[pose]
        target = start if index == 0 else end
        if target == "standing":
            return _articulated_image(animal, "land", 1, palette)
        return _sprite_tinted(animal, clips[target][0], palette)
    return _sprite_tinted(animal, indices[index], palette)


def _groom_image(animal, phase, palette):
    # Raise the paw and incline the head into contact before licking. A second
    # lick follows a quiet pause; lowering uses its independently authored path.
    if phase < .22:
        return _key_transition(animal,"groom",phase/.22,palette)
    if phase > .79:
        return _key_transition(animal,"ungroom",(phase-.79)/.21,palette)
    _,clips = _sprite_bases(animal)
    source = _sprite_tinted(animal,clips["groom"][0],palette)
    t = (phase-.22)/.57
    lick = (_gesture(t,0.02,.13,.21) if t < .48 else _gesture(t,.51,.63,.74))
    # Contact motion stays local to head, wrist and tongue. The supporting paw
    # and haunches remain completely still throughout the seated action.
    offsets = ((0,0),(0,1.8*lick),(0,0),(1.5*lick,-2.0*lick))
    image = _deform_action(source,animal,"groom",offsets)
    if lick > .25:
        x,y = {"Cat":(273,197),"Dog":(200,226),"Fox":(337,161)}[animal]
        painter = QPainter(image)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#d88986"))
        painter.drawRoundedRect(QRectF(x-2,y,5,5*lick),2,2)
        painter.end()
    return image


def _yawn_image(animal, phase, palette):
    _,clips = _sprite_bases(animal)
    source = _sprite_tinted(animal,clips["idle"][0],palette)
    closed = _sprite_tinted(animal,clips["pet"][0],palette)
    opening = _gesture(phase,.10,.49,.63)
    source = _expression_image(animal,source,closed,.86*opening,opening)
    # Only the jaw opens. A small rigid head extension follows the breath.
    nx,ny = {"Cat":(267,177),"Dog":(267,189),"Fox":(273,191)}[animal]
    image = QImage(*SPRITE_CANVAS,QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    head = QPainterPath();head.addRect(QRectF(170,0,230,ny+12))
    body = QPainterPath();body.addRect(QRectF(0,ny-12,400,320-ny+12))
    left = QPainterPath();left.addRect(QRectF(0,0,170,320))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setClipPath(body.united(left));painter.drawImage(0,0,source)
    painter.setClipping(False)
    painter.translate(nx,ny);painter.rotate(-3.5*opening);painter.translate(-nx,-ny)
    painter.setClipPath(head);painter.drawImage(0,0,source)
    painter.end()
    return image


def animal_frame_image(animal, pose, frame, direction=1, tail_offset=0, breath=0,
                       dangle=0, palette=None, stretch=0, effects=True, motion_phase=None):
    """Sample the same articulated drawings for the desktop and artwork exports."""
    if pose == "walk":
        phase = frame/16 if motion_phase is None else motion_phase
        image = _walk_image(animal,phase,palette)
    elif pose in ("stand","sit","settle","wake"):
        phase = frame/3 if motion_phase is None else motion_phase
        image = _key_transition(animal,pose,phase,palette)
    elif pose in ("groom","yawn"):
        phase = frame/4 if motion_phase is None else max(0.0,min(1.0,motion_phase))
        image = (_groom_image if pose=="groom" else _yawn_image)(animal,phase,palette)
    elif pose == "stretch":
        phase = frame/4 if motion_phase is None else max(0.0,min(1.0,motion_phase))
        if phase < .18:
            image = _key_transition(animal,"stand",phase/.18,palette)
        elif phase > .82:
            image = _key_transition(animal,"sit",(phase-.82)/.18,palette)
        else:
            image = _articulated_image(animal,pose,(phase-.18)/.64,palette,dangle,stretch)
    elif pose in ("held","fall","land","turn"):
        count = 6 if pose in ("held","fall") else 3 if pose=="land" else 4
        phase = frame/count if motion_phase is None else max(0.0,min(1.0,motion_phase))
        image = _articulated_image(animal,pose,phase,palette,dangle,stretch)
        if pose == "turn":
            # Project toward the narrow end-on silhouette while the paws step
            # around their support. Facing changes only at this narrow midpoint.
            width = 1.0-.76*math.sin(math.pi*phase)**2
            turned = QImage(*SPRITE_CANVAS,QImage.Format.Format_ARGB32_Premultiplied)
            turned.fill(Qt.GlobalColor.transparent)
            painter = QPainter(turned)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.translate(200,0);painter.scale(width,1);painter.translate(-200,0)
            painter.drawImage(0,0,image);painter.end()
            image = turned
    else:
        _bases, clips = _sprite_bases(animal)
        action = {"petted": "pet", "rest": "idle", "blink": "idle"}.get(pose, pose)
        seated = action in ("idle", "pet", "watch")
        rig = "idle" if seated else action
        indices = clips.get(rig, clips["rest"])
        phase = motion_phase
        if phase is None:
            count = 6 if action == "air" else 4
            phase = frame%count/count
            if action == "pet":
                phase = 0.10 + 0.20*(frame%4)
        if action in ("pet", "watch"):
            phase = max(0.0,min(1.0,phase))
        else:
            phase %= 1.0
        source = _sprite_tinted(animal,indices[0],palette)
        if pose == "blink":
            eyelid = 1.0 if motion_phase is None else _gesture(phase, 0, 0.33, 0.41)
            # The eyelids travel on a separate clock from the seated body.
            phase = tail_offset
        else:
            eyelid = 0
        if seated:
            offsets, head_angle, lean = _seated_motion(animal, action, phase, dangle)
            if action == "pet":
                eyelid = _ease(1.8*lean)
        else:
            offsets, head_angle = _action_motion(animal,action,phase,dangle,stretch), 0
        if eyelid > 0.001:
            closed = _sprite_tinted(animal, clips["pet"][0], palette)
            source = _expression_image(animal, source, closed, eyelid, 0)
        image = (_deform_action(source, animal, rig, offsets, head_angle)
                 if head_angle or any(x or y for x, y in offsets) else source)
        if effects and action == "pet" and 0.05 < phase < 0.94:
            image = image.copy()
            painter = QPainter(image)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#d77c9d"))
            for x, y, start in ((49, 83, 0.05), (349, 75, 0.24)):
                travel = (phase-start)/0.70
                if not 0 < travel < 1:
                    continue
                painter.setOpacity(_ease(13*travel)*_ease(3.5*(1-travel)))
                hx, hy = round(x+7*travel), round(y-43*travel)
                painter.drawPolygon(QPolygon([QPoint(hx,hy+4),QPoint(hx+4,hy),
                    QPoint(hx+8,hy+4),QPoint(hx+12,hy),QPoint(hx+16,hy+4),
                    QPoint(hx+16,hy+9),QPoint(hx+8,hy+17),QPoint(hx,hy+9)]))
            painter.end()
    return image if direction >= 0 else image.transformed(QTransform().scale(-1,1))


def fox_frame_image(pose, frame, direction=1, tail_offset=0, breath=0, dangle=0,
                    palette=None, stretch=0, motion_phase=None):
    return animal_frame_image("Fox",pose,frame,direction,tail_offset,breath,dangle,palette,stretch,
                              motion_phase=motion_phase)


def _asset_pixmap(animal, pose, frame, direction=1, tail_offset=0, breath=0,
                  dangle=0, palette=None, stretch=0, effects=True, target_size=None,
                  motion_phase=None):
    image = animal_frame_image(animal,pose,frame,1,tail_offset,breath,dangle,palette,stretch,effects,motion_phase)
    width,height = target_size or (WIDTH,HEIGHT)
    image = image.scaled(int(width),int(height),Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    if direction < 0:
        image = image.transformed(QTransform().scale(-1,1))
    return QPixmap.fromImage(image)


def _asset_peek(animal,palette):
    bases,clips = _sprite_bases(animal)
    image = _sprite_tinted(animal,clips["rest"][0],palette)
    bounds = QRegion(QPixmap.fromImage(image).mask()).boundingRect()
    crop = image.copy(round(bounds.x()+bounds.width()*0.43),bounds.y(),
                      round(bounds.width()*0.57),round(bounds.height()*0.57))
    crop_bounds = QRegion(QPixmap.fromImage(crop).mask()).boundingRect()
    crop = crop.copy(crop_bounds).scaled(54,48,Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
    visible = QRegion(QPixmap.fromImage(crop).mask()).boundingRect()
    crop = crop.copy(visible)
    result = QImage(WIDTH,HEIGHT,QImage.Format.Format_ARGB32)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.drawImage(WIDTH-crop.width(),0,crop)
    painter.end()
    return QPixmap.fromImage(result)

ANIMAL_QUOTES = {
    "Cat": CAT_QUOTES,
    "Dog": (
        "I heard a bag. Was it a bag?",
        "Your break is now a walk break.",
        "Excellent petting. Please continue.",
        "I guarded the cursor. You are welcome.",
        "Every snack is technically a team snack.",
        "I believe in you. Also, treats.",
    ),
    "Fox": (
        "That was your idea. I just improved it.",
        "A clever plan starts with a quiet nap.",
        "No, I have not seen your missing snack.",
        "I prefer my chaos neatly organized.",
        "You work. I shall look mysterious.",
        "My tail has its own agenda.",
    ),
    "Crow": (
        "I noticed the shiny thing before you did.",
        "Caw. Consider that expert advice.",
        "Your secrets are safe. Your snacks are not.",
        "A clever bird knows when to take flight.",
        "I remember faces. Especially generous ones.",
        "A fine perch. Questionable management.",
    ),
}

DRAWERS = {"Cat": draw_cat, "Dog": draw_dog, "Fox": draw_fox, "Crow": draw_crow}


# A built-in 5 x 7 bitmap alphabet keeps speech pixel-sharp on every machine.
PIXEL_FONT = {
    "A": (14,17,17,31,17,17,17), "B": (30,17,17,30,17,17,30),
    "C": (14,17,16,16,16,17,14), "D": (30,17,17,17,17,17,30),
    "E": (31,16,16,30,16,16,31), "F": (31,16,16,30,16,16,16),
    "G": (14,17,16,23,17,17,15), "H": (17,17,17,31,17,17,17),
    "I": (14,4,4,4,4,4,14), "J": (7,2,2,2,18,18,12),
    "K": (17,18,20,24,20,18,17), "L": (16,16,16,16,16,16,31),
    "M": (17,27,21,21,17,17,17), "N": (17,25,21,19,17,17,17),
    "O": (14,17,17,17,17,17,14), "P": (30,17,17,30,16,16,16),
    "Q": (14,17,17,17,21,18,13), "R": (30,17,17,30,20,18,17),
    "S": (15,16,16,14,1,1,30), "T": (31,4,4,4,4,4,4),
    "U": (17,17,17,17,17,17,14), "V": (17,17,17,17,17,10,4),
    "W": (17,17,17,21,21,21,10), "X": (17,17,10,4,10,17,17),
    "Y": (17,17,10,4,4,4,4), "Z": (31,1,2,4,8,16,31),
    " ": (0,0,0,0,0,0,0), ".": (0,0,0,0,0,6,6),
    ",": (0,0,0,0,6,6,4), "'": (4,4,8,0,0,0,0),
    "?": (14,17,1,2,4,0,4), "!": (4,4,4,4,4,0,4),
    "-": (0,0,0,31,0,0,0),
}


class SpeechBubble(QWidget):
    """An input-transparent pixel bubble, separate from the cat's hit region."""
    PADDING, ADVANCE, LINE_HEIGHT, TAIL = 12, 12, 20, 12

    def __init__(self, cat: DesktopCat) -> None:
        super().__init__(cat, OVERLAY_FLAGS | Qt.WindowType.WindowTransparentForInput)
        prepare_overlay(self)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.cat = cat
        self.text = ""
        self.expires_at = 0.0
        self._lines: list[str] = []
        self._pointer_x = -1
        self._below = False
        self._pixmap = QPixmap()

    def show_quote(self, text: str, now: float) -> None:
        self.text, self.expires_at = text, now + BUBBLE_SECONDS
        area = self.cat.available_area()
        columns = min(26, max(8, int((area.width() - 2 * self.PADDING) / self.ADVANCE)))
        self._lines = textwrap.wrap(text.upper(), width=columns, break_on_hyphens=False)
        width = max(map(len, self._lines)) * self.ADVANCE - 2 + 2 * self.PADDING
        height = len(self._lines) * self.LINE_HEIGHT - 6 + 2 * self.PADDING + self.TAIL
        self.setFixedSize(width, height)
        self._pointer_x = -1
        self.reposition()
        self.show()
        configure_macos_window(self, click_through=True)

    def dismiss(self) -> None:
        self.hide()
        self.text, self.expires_at = "", 0.0

    def reposition(self) -> None:
        if not self.text:
            return
        area = self.cat.available_area()
        anchor = self.cat.head_anchor_global()
        x = min(max(area.left(), anchor.x() - self.width() / 2),
                max(area.left(), area.right() - self.width()))
        y = anchor.y() - self.height() - 6
        below = y < area.top()
        if below:
            y = self.cat.y() + self.cat.height() + 6
        y = min(max(area.top(), y), max(area.top(), area.bottom() - self.height()))
        self.move(round(x), round(y))
        pointer = max(15, min(self.width() - 15, anchor.x() - round(x)))
        if pointer != self._pointer_x or below != self._below:
            self._pointer_x, self._below = pointer, below
            self._render()

    def _render(self) -> None:
        image = QImage(self.size(), QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        p = QPainter(image)
        p.save()
        if self._below:
            p.translate(0, self.height())
            p.scale(1, -1)
        body_height = self.height() - self.TAIL

        def stair_rect(inset: int, color: str) -> None:
            x, y = inset, inset
            w, h = self.width() - 2 * inset, body_height - 2 * inset
            for dx, dy in ((6, 0), (3, 3), (0, 6)):
                p.fillRect(x + dx, y + dy, w - 2 * dx, h - 2 * dy, QColor(color))

        stair_rect(0, "#141e18")
        stair_rect(3, "#a2df6e")
        stair_rect(6, "#222c27")
        for inset, color in ((0, "#141e18"), (3, "#a2df6e")):
            for row in range(4 - inset // 3):
                half = 9 - inset - row * 3
                p.fillRect(self._pointer_x - half, body_height - 6 + row * 3,
                           max(3, half * 2), 3, QColor(color))
        p.restore()
        text_top = self.PADDING + (self.TAIL if self._below else 0)
        for line_number, line in enumerate(self._lines):
            for index, letter in enumerate(line):
                glyph = PIXEL_FONT.get(letter, PIXEL_FONT["?"])
                for row, bits in enumerate(glyph):
                    for col in range(5):
                        if bits & (1 << (4 - col)):
                            p.fillRect(self.PADDING + index * self.ADVANCE + col * 2,
                                       text_top + line_number * self.LINE_HEIGHT + row * 2,
                                       2, 2, QColor("#e0f4cd"))
        p.end()
        self._pixmap = QPixmap.fromImage(image)
        self.update()

    def paintEvent(self, event) -> None:
        QPainter(self).drawPixmap(0, 0, self._pixmap)


class DesktopCat(QWidget):
    def __init__(self, settings: QSettings | None = None, enable_tray: bool = True) -> None:
        super().__init__(None, OVERLAY_FLAGS)
        self.setWindowTitle("Lil Buddy")
        prepare_overlay(self)
        self.setFixedSize(WIDTH, HEIGHT)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.rng = random.Random()
        self.motion = Motion()
        self.palette_name = "Natural Coat"
        self.palette = PALETTES[self.palette_name]
        self.animal_type = "Cat"
        self.motion.walk_speed = _walking_speed(self.animal_type,self.width())
        self.size_name, self.pixel_scale = "Medium", SIZES["Medium"]
        self._paused, self._user_hidden = False, False
        self._habits_enabled, self._cursor_interaction = True, False
        self._habit: str | None = None
        self._habit_elapsed, self._habit_duration = 0.0, 0.0
        self._habit_in = self.rng.uniform(6.0, 12.0)
        self._last_normal_position: tuple[float, float] | None = None
        self._last_normal_screen = ""
        self._restoring = True
        self._shutting_down = False
        self._color_restore: tuple[Palette, str] | None = None
        self._tray: QSystemTrayIcon | None = None
        self._tray_menu: QMenu | None = None
        self.settings = settings
        if settings is None and QGuiApplication.platformName() != "offscreen":
            directory = os.environ.get("LIL_BUDDY_SETTINGS_DIR")
            if not directory:
                directory = str(Path(QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.GenericConfigLocation)) / "Lil Buddy")
            self.settings = QSettings(str(Path(directory) / "preferences.ini"), QSettings.Format.IniFormat)
        self._latched = False
        self._hidden_for_fullscreen = False
        self._native_input_error: str | None = None
        self._fullscreen_clear_samples = 0
        self._active_menu: QMenu | None = None
        self._color_dialog: QColorDialog | None = None
        self.display = QGuiApplication.primaryScreen()
        if self.display is None:
            raise RuntimeError("No display is available for the desktop cat.")
        self._drag_offset = QPointF()
        self._press_position: QPointF | None = None
        self._dragging = False
        self._drag_speed = 0.0
        self._drag_sway = 0.0
        self._drag_sample_time = 0.0
        self._drag_sample_position = QPointF()
        self._landing_elapsed: float | None = None
        self._pet_until = 0.0
        self.bubble = SpeechBubble(self)
        self._menu_open = False
        self._last_time = time.monotonic()
        self._animation_time = 0.0
        self._pose_clock_kind = None
        self._pose_clock_started = 0.0
        self._walk_phase = 0.0
        self._blink_in = self.rng.uniform(2.0, 5.0)
        self._blink_left = 0.0
        self._frames: dict[tuple, tuple[QPixmap, QRegion]] = {}
        self._frame_key: tuple | None = None
        self._pixmap = QPixmap()
        self._last_area = QRectF()
        self.back_to_floor()
        self._restore_preferences()
        self._restoring = False
        self._set_frame()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self._update_animation_timer()
        # Keep detection independent: the animation timer stops entirely when tucked away.
        self.fullscreen_detector = FullScreenDetector(self)
        self.fullscreen_timer = QTimer(self)
        self.fullscreen_timer.setInterval(250)
        self.fullscreen_timer.timeout.connect(self._poll_fullscreen)
        self.fullscreen_timer.start()
        if enable_tray and QGuiApplication.platformName() != "offscreen":
            self._setup_tray()
        QApplication.instance().aboutToQuit.connect(self._shutdown)

    def _restore_preferences(self) -> None:
        if self.settings is None:
            return

        def value(key, default):
            return self.settings.value(key, default)

        def flag(key, default):
            result = value(key, default)
            if isinstance(result, bool):
                return result
            return str(result).lower() in ("true", "1") if str(result).lower() in ("true", "false", "1", "0") else default

        palette_name = str(value("appearance/palette", "Natural Coat"))
        if palette_name in PALETTES:
            self.palette, self.palette_name = PALETTES[palette_name], palette_name
        elif palette_name == "Custom RGB":
            color = QColor(str(value("appearance/custom_rgb", "#353c46")))
            if color.isValid():
                self.palette, self.palette_name = custom_palette(color), palette_name
        animal = str(value("appearance/animal", "Cat"))
        legacy_bird = animal == "Bird"
        if legacy_bird:
            animal = "Crow"
        if (legacy_bird or str(value("appearance/model_revision", "1")) == "1") and self.palette_name == "Charcoal Grey":
            self.palette_name, self.palette = "Natural Coat", PALETTES["Natural Coat"]
        self.animal_type = animal if animal in DRAWERS else "Cat"
        size = str(value("appearance/size", "Medium"))
        self.set_pet_size(size if size in SIZES else "Medium")
        self.motion.roaming = flag("behavior/roaming", True)
        self._paused = flag("behavior/paused", False)
        self._habits_enabled = flag("behavior/habits", True)
        self._cursor_interaction = flag("behavior/watch_cursor", False)
        screen_name = str(value("position/screen", ""))
        for screen in QGuiApplication.screens():
            if screen.name() == screen_name:
                self.display = screen
                break
        try:
            x, y = float(value("position/x", self.motion.x)), float(value("position/y", self.motion.y))
            if math.isfinite(x) and math.isfinite(y):
                # Recover saved coordinates even when monitor names have changed.
                if not screen_name or self.display.name() != screen_name:
                    candidate = QGuiApplication.screenAt(QPoint(round(x), round(y)))
                    self.display = candidate or self.display
                self.motion.x, self.motion.y = x, y
                self.motion.constrain(self.available_area())
                self.move(round(self.motion.x), round(self.motion.y))
        except (ValueError, TypeError, OverflowError):
            self.back_to_floor()
        self._frames.clear()
        self._frame_key = None
        self._remember_position()

    def _remember_position(self) -> None:
        if not self._latched and not self._user_hidden:
            self._last_normal_position = (self.motion.x, self.motion.y)
            self._last_normal_screen = self.display.name() if self.display else ""

    def _save_preferences(self) -> None:
        if self.settings is None or self._restoring:
            return
        self._remember_position()
        palette, name = self._color_restore or (self.palette, self.palette_name)
        values = {
            "appearance/palette": name, "appearance/custom_rgb": palette.fur,
            "appearance/model_revision": 2,
            "appearance/animal": self.animal_type, "appearance/size": self.size_name,
            "behavior/roaming": self.motion.roaming, "behavior/paused": self._paused,
            "behavior/habits": self._habits_enabled, "behavior/watch_cursor": self._cursor_interaction,
            "position/screen": self._last_normal_screen,
        }
        if self._last_normal_position is not None:
            values.update({"position/x": self._last_normal_position[0], "position/y": self._last_normal_position[1]})
        for key, setting in values.items():
            self.settings.setValue(key, setting)
        self.settings.sync()

    def set_pet_size(self, name: str) -> None:
        if name not in SIZES:
            raise ValueError(f"Unknown size: {name}")
        previous_position = QPoint(self.pos())
        previous_height = self.motion.height
        feet = self.motion.y + previous_height
        self.size_name, self.pixel_scale = name, SIZES[name]
        self.motion.width, self.motion.height = ART_WIDTH * self.pixel_scale, ART_HEIGHT * self.pixel_scale
        self.setFixedSize(int(self.motion.width), int(self.motion.height))
        self.motion.walk_speed = _walking_speed(self.animal_type,self.width())
        self.motion.y = feet - self.motion.height
        if self._latched:
            self._pin_to_corner()
        else:
            area = self.available_area()
            self.motion.constrain(area)
            visual_y = self.motion.bounds(area)[3] if self._landing_elapsed is not None else self.motion.y
            self.move(round(self.motion.x), round(visual_y))
        if self._dragging:
            self._drag_offset -= QPointF(self.pos() - previous_position)
        if (self._latched or self._user_hidden) and self._last_normal_position is not None:
            x, y = self._last_normal_position
            self._last_normal_position = (x, y + previous_height - self.motion.height)
        self._frames.clear()
        self._frame_key = None
        self._set_frame()
        self.bubble.reposition()
        self._save_preferences()

    def _update_animation_timer(self) -> None:
        if self._paused or self._user_hidden or self._latched:
            self.timer.stop()
        else:
            self.timer.start(16)

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        self._dismiss_reaction()
        self._cancel_habit()
        self._cancel_drag()
        self._last_time = time.monotonic()
        self._update_animation_timer()
        self._save_preferences()

    def set_pet_visible(self, visible: bool) -> None:
        if not visible:
            self._remember_position()
        self._user_hidden = not visible
        if visible:
            self.show()
            self.configure_macos()
            if self._latched:
                set_native_click_through(self, True)
                set_corner_window_level(self, True)
                self._pin_to_corner()
        else:
            self._dismiss_reaction()
            self._cancel_habit()
            self._cancel_drag()
            self.hide()
        self._last_time = time.monotonic()
        self._update_animation_timer()

    def _cancel_drag(self) -> None:
        self._press_position, self._dragging = None, False
        self._drag_speed, self._drag_sway = 0.0, 0.0
        if self.motion.state == "held":
            self.motion.drop(self.available_area())
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()
        self.setCursor(Qt.CursorShape.ArrowCursor if self._latched else Qt.CursorShape.OpenHandCursor)

    def _cancel_habit(self) -> None:
        self._habit, self._habit_elapsed = None, 0.0
        self._habit_in = self.rng.uniform(6.0, 12.0)

    def _choose_habit(self) -> None:
        if not self._habits_enabled or not self.motion.roaming or self.motion.sleeping or self.motion.state != "rest":
            return
        if self.animal_type == "Crow" and self.rng.random() < 0.45:
            self.start_crow_flight()
            return
        choices = {"Cat": ("groom", "groom", "yawn", "stretch"),
                   "Dog": ("groom", "yawn", "stretch", "stretch"),
                   "Fox": ("groom", "yawn", "stretch"),
                   "Crow": ("groom", "groom", "yawn", "stretch")}[self.animal_type]
        if self._cursor_interaction:
            choices += ("watch", "watch")
        self._habit = self.rng.choice(choices)
        self._habit_elapsed, self._habit_duration = 0.0, HABIT_DURATIONS[self._habit]

    def start_crow_flight(self) -> bool:
        if (self.animal_type != "Crow" or self._latched or self._paused
                or self._user_hidden or self.motion.state in ("held", "land", "sit")):
            return False
        self._dismiss_reaction()
        self._cancel_habit()
        self._landing_elapsed = None
        self.motion.begin_flight(self.available_area(), self.rng)
        self._last_time = time.monotonic()
        self._set_frame()
        self._update_animation_timer()
        return True

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self)
        self._tray_menu = self._build_menu(tray=True)
        self._tray_menu.aboutToShow.connect(self._tray_menu_opened)
        self._tray_menu.aboutToHide.connect(self._menu_closed)
        self._tray.setContextMenu(self._tray_menu)
        self._tray.activated.connect(self._tray_activated)
        self._refresh_tray_icon()
        self._tray.show()

    def _refresh_tray_icon(self) -> None:
        if self._tray is None:
            return
        face = draw_peek(self.animal_type, self.palette).copy(26 * PIXEL_SCALE, 0, 14 * PIXEL_SCALE, 14 * PIXEL_SCALE)
        icon = QIcon(face)
        if sys.platform == "darwin":
            icon.setIsMask(True)
        self._tray.setIcon(icon)
        self._tray.setToolTip(f"Lil Buddy — {self.animal_type}")

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.set_pet_visible(not self.isVisible())

    def _tray_menu_opened(self) -> None:
        self._menu_open, self._active_menu = True, self._tray_menu
        self._sync_menu_checks(self._tray_menu)

    def _sync_menu_checks(self, menu: QMenu) -> None:
        choices = {"palette": self.palette_name, "animal": self.animal_type, "size": self.size_name,
                   "paused": self._paused, "visible": not self._user_hidden, "roaming": self.motion.roaming,
                   "habits": self._habits_enabled, "cursor": self._cursor_interaction}
        for action in menu.actions():
            if action.menu():
                self._sync_menu_checks(action.menu())
            tag = action.data()
            if tag == "command:fly":
                action.setVisible(self.animal_type == "Crow")
                action.setEnabled(not self._latched and not self._paused and not self._user_hidden)
            if isinstance(tag, str) and ":" in tag and action.isCheckable():
                kind, expected = tag.split(":", 1)
                blocked = QSignalBlocker(action)
                action.setChecked(choices.get(kind) == (expected if kind in ("palette", "animal", "size") else True))
                del blocked

    def available_area(self) -> QRectF:
        # If a monitor is unplugged, recover onto the current primary display.
        if self.display not in QGuiApplication.screens():
            self.display = QGuiApplication.primaryScreen()
        if self.display is None:
            return self._last_area
        return QRectF(self.display.availableGeometry())

    def configure_macos(self) -> None:
        configure_macos_window(self)

    def back_to_floor(self) -> None:
        if self._latched:
            return
        self._dismiss_reaction()
        self._cancel_habit()
        self._landing_elapsed, self._drag_speed = None, 0.0
        area = self.available_area()
        left, right, _, floor = self.motion.bounds(area)
        self.motion.x = left + (right - left) * 0.68
        self.motion.y = floor
        self.motion.rest(self.rng)
        self._last_area = area
        self.move(round(self.motion.x), round(self.motion.y))
        self._save_preferences()

    def _set_frame(self) -> None:
        state = self.motion.state
        pose, frame = state, 0
        idle_wave = math.sin(self._animation_time * math.tau / 3.6)
        tail_offset = round(2 * idle_wave)
        breath = -1 if idle_wave > 0.4 else 0
        dangle, stretch = 0, 0
        if self._latched:
            pose, frame, tail_offset, breath, dangle = "peek", 0, 0, 0, 0
        elif state == "fly":
            frame = int(self.motion.flight_time / CROW_FLAP_SECONDS) % 8
            tail_offset, breath = 0, 0
            dangle = max(-1, min(1, round(self.motion.vy / 80.0)))
        elif state == "land" or (self._landing_elapsed is not None and state != "held"):
            progress = (self.motion.transition_time if state == "land" else self._landing_elapsed) / self.motion.landing_duration
            pose, frame = "land", min(2, int(progress * 3))
            tail_offset, breath = 0, 0
        elif state in ("turn", "stand", "sit", "settle", "wake"):
            duration = self.motion.turn_duration if state == "turn" else self.motion.transition_duration
            frame = min(3, int(self.motion.transition_time / duration * 4))
            tail_offset, breath = 0, 0
        elif state in ("held", "fall"):
            count, seconds = (8, CROW_FLAP_SECONDS) if self.animal_type == "Crow" else (4, AIR_FRAME_SECONDS)
            frame = int(self._animation_time / seconds) % count
            speed = self._drag_speed if state == "held" else abs(self.motion.vy)
            stretch = min(3.0, speed / 350.0) if self.animal_type != "Crow" else min(3, int(speed / 350.0))
            tail_offset, breath = 0, 0
            if state == "held":
                dangle = round(self._drag_sway, 2)
        elif self._pet_until > 0:
            pose, frame = PETTED, int(self._animation_time / 0.09) % 4
            tail_offset, breath = 0, 0
        elif self._habit is not None:
            pose, frame = self._habit, int(self._habit_elapsed / 0.18) % 4
            tail_offset, breath = 0, 0
            if pose == "watch":
                cursor = QCursor.pos()
                center = self.geometry().center()
                dangle = -1 if cursor.y() < center.y() - 12 else 1 if cursor.y() > center.y() + 12 else 0
        elif state == "rest":
            pose = "sleep" if self.motion.sleeping else "blink" if self._blink_left > 0 else "rest"
            frame = int(idle_wave > 0.3) if pose == "sleep" else 0
        elif state == "walk":
            count, seconds = ((4, 0.09) if self.animal_type == "Dog" else
                              (6, 0.09) if self.animal_type in ("Cat", "Fox") else (4, 0.13))
            frame = int(self._animation_time / seconds) % count
            breath = 0

        animated_art = self.animal_type in ("Cat", "Dog", "Fox")
        motion_phase = None
        group = ("idle" if pose in ("rest", "blink") else
                 "pet" if pose in ("pet", "petted") else pose)
        kind = self.animal_type, group
        if kind != self._pose_clock_kind:
            self._pose_clock_kind = kind
            self._pose_clock_started = self._animation_time
        elapsed = max(0.0, self._animation_time-self._pose_clock_started)
        if group == "pet":
            # The reaction plays once while the speech bubble keeps its own deadline.
            motion_phase = min(1.0, elapsed/PET_REACTION_SECONDS)
        if animated_art:
            if pose != "peek":
                tail_offset, breath = 0, 0
                if pose == "walk":
                    # Backward paw travel in stance cancels forward window travel.
                    motion_phase = self._walk_phase
                elif pose == "land":
                    landing_time = self.motion.transition_time if state == "land" else self._landing_elapsed
                    motion_phase = min(1.0, landing_time / self.motion.landing_duration)
                elif pose in ("turn", "stand", "sit", "settle", "wake"):
                    duration = self.motion.turn_duration if pose == "turn" else self.motion.transition_duration
                    motion_phase = min(1.0, self.motion.transition_time / duration)
                elif pose == "held":
                    motion_phase = min(1.0, elapsed / 0.48)
                elif pose == "fall":
                    # Extend the paws before contact, even on a short drop.
                    distance = max(0.0, self.motion.bounds(self.available_area())[3]-self.motion.y)
                    eta = (math.sqrt(max(0.0,self.motion.vy**2+2*GRAVITY*distance))-max(0.0,self.motion.vy))/GRAVITY
                    motion_phase = min(1.0, max(elapsed/0.30, 1.0-eta/0.28))
                elif pose == "blink":
                    # The eyelids have their own short timeline. Resting chest
                    # and tail motion continue underneath on the idle clock.
                    tail_offset = (elapsed / ASSET_ANIMATION_SECONDS["idle"]) % 1.0
                    motion_phase = 1.0 - min(1.0, self._blink_left / BLINK_SECONDS)
                elif pose in HABIT_DURATIONS:
                    motion_phase = min(1.0, self._habit_elapsed / HABIT_DURATIONS[pose])
                elif group != "pet":
                    duration = ASSET_ANIMATION_SECONDS[group]
                    motion_phase = (elapsed / duration) % 1.0
                if pose != "land":
                    _bases, clips = _sprite_bases(self.animal_type)
                    clip = {"held": "air", "fall": "air", "stretch": "air",
                            "petted": "pet", "yawn": "pet", "rest": "idle",
                            "watch": "idle"}.get(pose, pose)
                    indices = clips.get(clip, clips["rest"])
                    frame = min(len(indices)-1, int(motion_phase*len(indices)))
        if self.animal_type == "Crow":
            if pose == "walk":
                motion_phase = self._walk_phase
            elif pose == "land":
                landing_time = self.motion.transition_time if state == "land" else self._landing_elapsed
                motion_phase = min(1.0, landing_time / self.motion.landing_duration)
            elif pose in ("turn", "stand", "sit", "settle", "wake"):
                duration = self.motion.turn_duration if pose == "turn" else self.motion.transition_duration
                motion_phase = min(1.0, self.motion.transition_time / duration)
            elif pose in HABIT_DURATIONS:
                motion_phase = min(1.0, self._habit_elapsed / HABIT_DURATIONS[pose])
            elif group in ("idle", "sleep"):
                duration = ASSET_ANIMATION_SECONDS[group]
                samples = 56 if group == "idle" else 36
                motion_phase = (int(elapsed/duration*samples) % samples)/samples
                tail_offset, breath, frame = 0, 0, 0
            elif group == "pet":
                frame = min(3, int(motion_phase*4))
        direction = 1 if pose == "peek" else self.motion.direction
        key = pose, frame, direction, tail_offset, breath, dangle, self.animal_type, self.palette, stretch
        display_scale = self.devicePixelRatioF() if animated_art and pose != "peek" else 1.0
        offset_x = offset_y = 0
        if display_scale > 1 and pose in ("walk", "fall"):
            # The window can move only in whole desktop points. Put the
            # remaining half-point on the Retina pixel grid so slow walks do
            # not alternate between a frozen body and a two-pixel jump.
            offset_x = round((self.motion.x - round(self.motion.x)) * display_scale)
            offset_y = round((self.motion.y - round(self.motion.y)) * display_scale)
        if animated_art:
            key = key[:-1] + (display_scale, offset_x, offset_y, motion_phase, stretch)
        else:
            key += (motion_phase,)
        if key != self._frame_key:
            if key not in self._frames:
                if pose == "peek":
                    pixmap = draw_peek(self.animal_type, self.palette)
                elif animated_art:
                    pixmap = DRAWERS[self.animal_type](*key[:6], palette=self.palette,
                        stretch=stretch, target_size=(round(self.width()*display_scale),
                                                      round(self.height()*display_scale)),
                        motion_phase=motion_phase)
                else:
                    pixmap = DRAWERS[self.animal_type](*key[:6], palette=self.palette,
                                                     stretch=stretch, motion_phase=motion_phase)
                if display_scale != 1.0:
                    # Sample the drawing at display resolution. Qt window masks
                    # use logical coordinates even when the display uses Retina.
                    if offset_x or offset_y:
                        placed = QPixmap(pixmap.size())
                        placed.fill(Qt.GlobalColor.transparent)
                        painter = QPainter(placed)
                        painter.drawPixmap(offset_x, offset_y, pixmap)
                        painter.end()
                        pixmap = placed
                    hit_image = pixmap.scaled(self.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation)
                    mask = QRegion(hit_image.mask())
                    pixmap.setDevicePixelRatio(display_scale)
                else:
                    if pixmap.size() != self.size():
                        pixmap = pixmap.scaled(self.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                               Qt.TransformationMode.FastTransformation)
                    mask = QRegion(pixmap.mask())
                if len(self._frames) >= 128:
                    self._frames.pop(next(iter(self._frames)))
                self._frames[key] = pixmap, mask
            self._pixmap, mask = self._frames[key]
            if sys.platform == "darwin" and animated_art and pose != "peek":
                # Cocoa applies the window shape independently of Qt presenting
                # the next backing frame. Leave one logical point for the fur
                # fringe and keep the previous drawing visible until repaint.
                exact_mask = mask
                previous = getattr(self, "_previous_art_mask", None)
                if previous is not None:
                    mask = mask.united(previous)
                self._previous_art_mask = exact_mask
                horizontal = mask.united(mask.translated(-1, 0)).united(mask.translated(1, 0))
                mask = horizontal.united(horizontal.translated(0, -1)).united(horizontal.translated(0, 1))
                mask = mask.intersected(QRegion(self.rect()))
            else:
                self._previous_art_mask = None
            if self.mask() != mask:
                self.setMask(mask)
            self._frame_key = key
            self.update()

    def _tick(self, now: float | None = None) -> None:
        if self._latched or self._paused or self._user_hidden:
            return
        now = time.monotonic() if now is None else now
        elapsed = max(0.0, now - self._last_time)
        dt, self._last_time = min(elapsed, 0.1), now
        # Speech uses a real deadline, even if a menu or a system sleep stalls physics.
        if self._pet_until and now >= self._pet_until:
            self._dismiss_reaction()
        if self._menu_open or self._color_dialog is not None:
            return
        area = self.available_area()
        if area != self._last_area and self.motion.state != "held":
            previous_state = self.motion.state
            self.motion.constrain(area)
            if previous_state == "land" and self.motion.state == "fall":
                self._landing_elapsed = None
            self._last_area = area
        # Artwork follows elapsed active time even when a slow frame requires
        # limiting the physics step. Pause/latch update _last_time on resume.
        self._animation_time += elapsed
        if self._habit is not None:
            if self.motion.state != "rest" or self.motion.sleeping or self._pet_until:
                self._cancel_habit()
            else:
                self._habit_elapsed += dt
                if self._habit == "watch":
                    cursor, center = QCursor.pos(), self.geometry().center()
                    if abs(cursor.x()-center.x()) > 6:
                        facing = 1 if cursor.x() > center.x() else -1
                        if facing != self.motion.direction:
                            self.motion.begin_turn(facing, "sit")
                            self._cancel_habit()
                if self._habit is not None and self._habit_elapsed >= self._habit_duration:
                    self._cancel_habit()
        elif (self._habits_enabled and self.motion.roaming and self.motion.state == "rest"
              and not self.motion.sleeping and not self._pet_until and self._press_position is None
              and self._landing_elapsed is None):
            self._habit_in -= dt
            if self._habit_in <= 0:
                self._choose_habit()
        if self._landing_elapsed is not None and self.motion.state != "land":
            self._landing_elapsed += dt
            if self._landing_elapsed >= self.motion.landing_duration:
                self._landing_elapsed = None
        if self.motion.state == "held" and now - self._drag_sample_time >= 0.08 - 1e-9:
            self._drag_speed *= math.exp(-8.0 * dt)
            self._drag_sway *= math.exp(-7.0 * dt)
            if self._drag_speed < 0.5:
                self._drag_speed = 0.0
        self._blink_in -= dt
        self._blink_left = max(0.0, self._blink_left - dt)
        if self._blink_in <= 0:
            self._blink_left, self._blink_in = BLINK_SECONDS, self.rng.uniform(2.5, 6.0)
        # Small substeps keep collisions stable if an occasional UI frame is late.
        was_landing = self.motion.state == "land"
        while dt > 0:
            step = min(dt, 1.0 / 120.0)
            if (self._press_position is None or self._dragging
                    or self.motion.state in ("fall", "fly", "land", "turn", "stand", "sit", "settle", "wake")):
                was_walking, previous_x = self.motion.state == "walk", self.motion.x
                impact = self.motion.step(step, area, self.rng,
                                          autonomous=self._pet_until == 0 and self._habit is None
                                          and self._landing_elapsed is None)
                if not was_walking and self.motion.state == "walk":
                    self._walk_phase = 0.0
                if was_walking and self.motion.state == "walk":
                    if self.animal_type in WALK_RIGS:
                        stride = WALK_RIGS[self.animal_type]["stride"] / WALK_DUTY
                        world_stride = stride * self.width() / SPRITE_CANVAS[0]
                    else:
                        # Crow's 8.112 art-pixel stance travels on a two-pixel
                        # source grid before the selected size is applied.
                        world_stride = 8.112 / 0.65 * 2 * self.width() / WIDTH
                    self._walk_phase = (self._walk_phase +
                        abs(self.motion.x-previous_x)/world_stride) % 1.0
                if self.motion.state == "land":
                    self._landing_elapsed = self.motion.transition_time
                elif impact > 0:
                    self._landing_elapsed = 0.0
            dt -= step
        if was_landing and self.motion.state != "land":
            self._landing_elapsed = None
        if self._pet_until and self.motion.state not in ("held", "fall", "fly", "land", "turn", "stand", "sit", "settle", "wake"):
            self.motion.state, self.motion.sleeping = PETTED, False
        # Anchor the impact poses to the floor while the small physical bounce
        # runs underneath, so the cat never squashes above the desktop.
        visual_y = self.motion.bounds(area)[3] if self._landing_elapsed is not None else self.motion.y
        self.move(round(self.motion.x), round(visual_y))
        self._set_frame()
        self.bubble.reposition()

    def head_anchor_global(self) -> QPoint:
        if self.animal_type != "Cat":
            x = 32 if self.motion.direction > 0 else 8
        elif self._pet_until > 0 and self.motion.state not in ("held", "fall", "fly"):
            x = 20
        else:
            x = 29 if self.motion.direction > 0 else ART_WIDTH - 1 - 29
        # Leave room above the highest floating heart and the upright airborne tail.
        y = 0 if self._pet_until else 4 * self.pixel_scale
        return self.pos() + QPoint(x * self.pixel_scale, y)

    def pet(self, now: float | None = None) -> None:
        if self._latched or self._paused or self._user_hidden:
            return
        now = time.monotonic() if now is None else now
        if self.motion.state != "land":
            self._landing_elapsed = None
        self._cancel_habit()
        if self.motion.sleeping:
            self.motion.state, self.motion.turn_resume = "wake", PETTED
            self.motion.transition_time = 0.0
        self.motion.sleeping = False
        if self.motion.state == "walk":
            self.motion.state, self.motion.transition_time = "sit", 0.0
            self.motion.sleep_after_sit = False
        if self.motion.state not in ("held", "fall", "fly", "land", "turn", "stand", "sit", "settle", "wake"):
            self.motion.state, self.motion.vy = PETTED, 0.0
        self.motion.decision_in = self.rng.uniform(1.5, 3.0)
        if (self._pose_clock_kind == (self.animal_type, "pet")
                and self._animation_time-self._pose_clock_started >= PET_REACTION_SECONDS):
            self._pose_clock_started = self._animation_time
        self._pet_until = now + BUBBLE_SECONDS
        quotes = ANIMAL_QUOTES.get(self.animal_type, CAT_QUOTES)
        choices = [quote for quote in quotes if quote != self.bubble.text]
        self._set_frame()
        self.bubble.show_quote(self.rng.choice(choices), now)

    def _dismiss_reaction(self) -> None:
        self._pet_until = 0.0
        if self.motion.state == PETTED:
            self.motion.state, self.motion.sleeping = "rest", False
        self.bubble.dismiss()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pixmap)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._latched:
            event.ignore()
            return
        # Qt maps physical Control to MetaModifier on macOS by default.
        control_modifier = (Qt.KeyboardModifier.MetaModifier if sys.platform == "darwin"
                            else Qt.KeyboardModifier.ControlModifier)
        control_click = (event.button() == Qt.MouseButton.LeftButton
                         and event.modifiers() & control_modifier)
        if event.button() == Qt.MouseButton.RightButton or control_click:
            self._show_menu(event.globalPosition().toPoint())
        elif event.button() == Qt.MouseButton.LeftButton:
            if self._paused:
                return
            self._cancel_habit()
            self._press_position = QPointF(event.globalPosition())
            self._drag_offset = event.globalPosition() - QPointF(self.pos())
            self._drag_sample_position = QPointF(event.globalPosition())
            self._drag_sample_time = time.monotonic()
            self._drag_speed, self._drag_sway = 0.0, 0.0
        else:
            event.ignore()

    def _drag_to(self, global_position: QPointF, now: float | None = None) -> None:
        if self._latched or self._press_position is None:
            return
        distance = (global_position - self._press_position).manhattanLength()
        if not self._dragging and distance >= QApplication.startDragDistance():
            self._dragging = True
            self._dismiss_reaction()
            self._cancel_habit()
            self._landing_elapsed = None
            self.motion.state, self.motion.vy, self.motion.vx = "held", 0.0, 0.0
            self.motion.sleeping = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        if self._dragging:
            now = time.monotonic() if now is None else now
            elapsed = max(1.0 / 120.0, now - self._drag_sample_time)
            delta = global_position - self._drag_sample_position
            instantaneous = min(2400.0, math.hypot(delta.x(), delta.y()) / elapsed)
            self._drag_speed = self._drag_speed * 0.35 + instantaneous * 0.65
            sway = max(-1.0, min(1.0, delta.x()/elapsed/700.0))
            self._drag_sway = 0.65*self._drag_sway + 0.35*sway
            self._drag_sample_time = now
            self._drag_sample_position = QPointF(global_position)
            position = global_position - self._drag_offset
            self.motion.x, self.motion.y = position.x(), position.y()
            self.move(round(self.motion.x), round(self.motion.y))
            # A QWidget automatically retains mouse capture until release.
            self.display = QGuiApplication.screenAt(global_position.toPoint()) or self.display
            self._set_frame()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._drag_to(event.globalPosition())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._press_position is not None:
            # Check the release too: a fast drag can arrive without a move event.
            self._drag_to(event.globalPosition())
            was_dragging = self._dragging
            self._press_position, self._dragging = None, False
            if was_dragging:
                self.display = QGuiApplication.screenAt(event.globalPosition().toPoint()) or self.display
                self.motion.drop(self.available_area())
                self._drag_speed, self._drag_sway = 0.0, 0.0
                if (self.animal_type == "Crow" and self.motion.roaming
                        and self.motion.y < self.motion.bounds(self.available_area())[3] - 25):
                    self.start_crow_flight()
                self._save_preferences()
            else:
                self.pet()
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self._last_time = time.monotonic()
            self._set_frame()

    def _build_menu(self, tray: bool = False) -> QMenu:
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background: #222c27; color: #e0f4cd;
                    border: 2px solid #8fb960; padding: 5px; }
            QMenu::item { padding: 6px 28px 6px 12px; }
            QMenu::item:selected { background: #425d3b; }
            QMenu::separator { height: 1px; background: #56664f; margin: 5px; }
        """)
        colors = menu.addMenu("Change Color")
        color_group = QActionGroup(colors)
        for name in PALETTES:
            action = colors.addAction(name)
            action.setCheckable(True)
            action.setChecked(self.palette_name == name)
            action.setData(f"palette:{name}")
            color_group.addAction(action)
            action.triggered.connect(lambda checked=False, value=name: self.set_palette(value))
        custom = colors.addAction("Custom RGB")
        custom.setCheckable(True)
        custom.setChecked(self.palette_name == "Custom RGB")
        custom.setData("palette:Custom RGB")
        color_group.addAction(custom)
        custom.triggered.connect(self._choose_custom_color)
        animals = menu.addMenu("Animal Type")
        animal_group = QActionGroup(animals)
        for animal in DRAWERS:
            action = animals.addAction(animal)
            action.setCheckable(True)
            action.setChecked(self.animal_type == animal)
            action.setData(f"animal:{animal}")
            animal_group.addAction(action)
            action.triggered.connect(lambda checked=False, value=animal: self.set_animal_type(value))
        sizes = menu.addMenu("Pet Size")
        size_group = QActionGroup(sizes)
        for name in SIZES:
            action = sizes.addAction(name)
            action.setCheckable(True)
            action.setChecked(self.size_name == name)
            action.setData(f"size:{name}")
            size_group.addAction(action)
            action.triggered.connect(lambda checked=False, value=name: self.set_pet_size(value))
        flight = menu.addAction("Take flight")
        flight.setData("command:fly")
        flight.setVisible(self.animal_type == "Crow")
        flight.setEnabled(not self._paused and not self._latched and not self._user_hidden)
        flight.triggered.connect(self.start_crow_flight)
        menu.addSeparator()
        for label, checked, callback, tag in (
                ("Show Pet", not self._user_hidden, self.set_pet_visible, "visible"),
                ("Pause", self._paused, self.set_paused, "paused"),
                ("Idle habits", self._habits_enabled, self._set_habits_enabled, "habits"),
                ("Watch cursor", self._cursor_interaction, self._set_cursor_interaction, "cursor")):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(checked)
            action.setData(f"{tag}:true")
            action.toggled.connect(callback)
            if tag == "visible" and not tray and self._tray is None:
                action.setEnabled(False)
                action.setToolTip("Show/hide is available from the menu-bar or tray icon.")
        roaming = menu.addAction("Allow wandering")
        roaming.setCheckable(True)
        roaming.setChecked(self.motion.roaming)
        roaming.setData("roaming:true")
        roaming.toggled.connect(self._toggle_roaming)
        menu.addAction("Back to floor", self.back_to_floor)
        menu.addSeparator()
        menu.addAction("Quit Lil Buddy", self.close)
        return menu

    def _show_menu(self, position: QPoint) -> None:
        if self._latched or self._menu_open or self._color_dialog is not None:
            return
        self._menu_open = True
        menu = self._active_menu = self._build_menu()
        menu.aboutToHide.connect(self._menu_closed)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(position)

    def _menu_closed(self) -> None:
        self._menu_open = False
        self._active_menu = None
        self._last_time = time.monotonic()

    def _apply_palette(self, palette: Palette, name: str) -> None:
        self.palette, self.palette_name = palette, name
        self._frames.clear()
        self._frame_key = None
        self._set_frame()
        self._refresh_tray_icon()

    def set_palette(self, name: str) -> None:
        if name not in PALETTES:
            raise ValueError(f"Unknown palette: {name}")
        self._apply_palette(PALETTES[name], name)
        self._save_preferences()

    def set_custom_color(self, color: QColor) -> None:
        if color.isValid():
            self._apply_palette(custom_palette(color), "Custom RGB")
            if self._color_dialog is None:
                self._save_preferences()

    def set_animal_type(self, animal: str) -> None:
        if animal not in DRAWERS:
            raise ValueError(f"Unknown animal type: {animal}")
        changed = self.animal_type != animal
        self.animal_type = animal
        self.motion.walk_speed = _walking_speed(animal,self.width())
        if self.motion.state == "fly" and animal != "Crow":
            self.motion.drop(self.available_area())
        if changed and animal != "Cat" and self.palette_name == "Charcoal Grey":
            self.palette_name, self.palette = "Natural Coat", PALETTES["Natural Coat"]
        self._cancel_habit()
        self._frames.clear()
        self._frame_key = None
        self._set_frame()
        self.bubble.reposition()
        self._refresh_tray_icon()
        self._save_preferences()

    def _choose_custom_color(self) -> None:
        if self._color_dialog is not None or self._latched:
            return
        old_palette, old_name = self.palette, self.palette_name
        self._color_restore = (old_palette, old_name)
        dialog = self._color_dialog = QColorDialog(QColor(self.palette.fur), self)
        dialog.setWindowTitle("Custom RGB")
        dialog.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)
        dialog.currentColorChanged.connect(self.set_custom_color)

        def finished(result: int) -> None:
            if result != QColorDialog.DialogCode.Accepted:
                self._apply_palette(old_palette, old_name)
            self._color_dialog = None
            self._color_restore = None
            self._last_time = time.monotonic()
            self._save_preferences()
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.open()

    def _pin_to_corner(self) -> None:
        self.available_area()  # Recover if the pet's monitor was unplugged.
        if self.display is None:
            return
        area = QRectF(self.display.geometry())  # Full monitor, including menu/task bars.
        self.motion.x = max(area.left(), area.right() - self.width())
        self.motion.y = area.top()
        position = QPoint(round(self.motion.x), round(self.motion.y))
        if self.pos() != position:
            self.move(position)

    def _set_latched(self, enabled: bool, now: float | None = None) -> None:
        if enabled == self._latched:
            if enabled:
                self._pin_to_corner()
            return
        now = time.monotonic() if now is None else now
        self._remember_position()
        self._latched = enabled
        self._fullscreen_clear_samples = 0
        self._dismiss_reaction()
        self._cancel_habit()
        self._landing_elapsed, self._drag_speed = None, 0.0
        self._press_position, self._dragging = None, False
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()
        if self._active_menu is not None:
            self._active_menu.close()
        if self._color_dialog is not None:
            self._color_dialog.reject()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, enabled)
        try:
            set_native_click_through(self, enabled)
            self._native_input_error = None
        except (OSError, RuntimeError, ValueError) as exc:
            self._native_input_error = str(exc)
            if enabled:
                # If native input pass-through fails, hiding is safer than blocking a game.
                self._hidden_for_fullscreen = True
                self.hide()
        if not enabled and self._hidden_for_fullscreen:
            self._hidden_for_fullscreen = False
            if not self._user_hidden:
                self.show()
                self.configure_macos()
        set_corner_window_level(self, enabled)
        self.motion.vy, self.motion.vx, self.motion.sleeping = 0.0, 0.0, False
        if enabled:
            self.timer.stop()
            self.motion.state = "latched"
            self._pin_to_corner()
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.motion.drop(self.available_area())
            self.move(round(self.motion.x), round(self.motion.y))
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self._update_animation_timer()
        self._last_time = now
        self._set_frame()

    def _poll_fullscreen(self) -> None:
        self.available_area()
        if self.display is None:
            return
        try:
            detected = self.fullscreen_detector.is_fullscreen(self.display)
        except (OSError, RuntimeError, ValueError):
            detected = None
        if detected is True:
            self._fullscreen_clear_samples = 0
            self._set_latched(True)
        elif detected is False and self._latched:
            self._fullscreen_clear_samples += 1
            # Brief window-server transitions should not make the pet pop back out.
            if self._fullscreen_clear_samples >= 2:
                self._set_latched(False)
        else:
            self._fullscreen_clear_samples = 0
        if self._latched:
            self._pin_to_corner()

    def _toggle_roaming(self, enabled: bool) -> None:
        self.motion.roaming = enabled
        if not enabled:
            self._cancel_habit()
            if self.motion.state == "fly":
                self.motion.flight_duration = min(self.motion.flight_duration, self.motion.flight_time + 1.6)
            elif self.motion.state == "turn":
                self.motion.turn_resume = "sit"
            elif self.motion.state == "walk":
                self.motion.begin_rest(self.rng)
        if self.motion.state not in ("held", "fall", "fly", "land", "turn", "stand", "sit", "settle", "wake"):
            if self._pet_until:
                self.motion.state, self.motion.sleeping = PETTED, False
            else:
                self.motion.rest(self.rng)
        self._save_preferences()

    def _set_habits_enabled(self, enabled: bool) -> None:
        self._habits_enabled = enabled
        if not enabled:
            self._cancel_habit()
        self._save_preferences()

    def _set_cursor_interaction(self, enabled: bool) -> None:
        self._cursor_interaction = enabled
        if not enabled and self._habit == "watch":
            self._cancel_habit()
        self._save_preferences()

    def _shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if self._color_dialog is not None:
            self._color_dialog.reject()
        self._save_preferences()
        self.timer.stop()
        self.fullscreen_timer.stop()
        self.bubble.dismiss()
        if self._tray is not None:
            self._tray.hide()

    def closeEvent(self, event) -> None:
        self._shutdown()
        event.accept()
        QApplication.instance().quit()


def run_smoke_test(cat: DesktopCat, app: QApplication, report_path: Path | None) -> None:
    """Exercise the actual frozen Qt application without changing user preferences."""
    cat.fullscreen_timer.stop()
    errors, checks = [], []

    def check() -> None:
        try:
            for animal in DRAWERS:
                cat.set_animal_type(animal)
                for size in SIZES:
                    cat.set_pet_size(size)
                    assert cat._pixmap.deviceIndependentSize().toSize() == cat.size(), (animal, size)
                    assert cat.motion.width == cat.width() and cat.motion.height == cat.height()
                cat.motion.state = "walk"
                cat._set_frame()
                cat._walk_phase = 0.37
                cat._set_frame()
                assert not cat._pixmap.isNull(), animal
                for pose in ("held","fall","land","groom","yawn","stretch","stand","sit","settle","wake"):
                    for progress in (0.0,0.5,1.0):
                        drawing = DRAWERS[animal](pose,0,1,palette=cat.palette,motion_phase=progress)
                        assert not drawing.isNull() and not drawing.mask().isNull(), (animal,pose,progress)
            checks.append("all animals and sizes")
            checks.append("pickup, contact, gestures and posture transition artwork")
            checks.append("continuous walking artwork")
            cat.set_pet_size("Medium")
            cat.set_animal_type("Cat")
            cat.back_to_floor()
            cat.pet()
            assert cat.motion.state == PETTED and cat.bubble.isVisible()
            checks.append("petting and speech")
            cat.set_paused(True)
            assert not cat.timer.isActive()
            cat.set_paused(False)
            cat.set_pet_visible(False)
            assert not cat.isVisible() and not cat.timer.isActive()
            cat.set_pet_visible(True)
            assert cat.isVisible() and cat.timer.isActive()
            checks.append("pause and visibility")
            cat._set_latched(True)
            assert cat._latched and not cat.timer.isActive()
            checks.append("fullscreen latch")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    def finish() -> None:
        try:
            assert cat._latched
            assert cat.x() == cat.display.geometry().x() + cat.display.geometry().width() - cat.width()
            assert cat.y() == cat.display.geometry().y()
            cat._set_latched(False)
            assert cat.motion.state == "fall" and cat.timer.isActive()
            checks.append("fullscreen release and gravity")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        report = {"success": not errors, "platform": sys.platform, "checks": checks, "errors": errors}
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if sys.stdout is not None:
            print(json.dumps(report), flush=True)
        app.exit(1 if errors else 0)

    QTimer.singleShot(100, check)
    QTimer.singleShot(650, finish)


def main() -> int:
    parser = argparse.ArgumentParser(description="Lil Buddy desktop pets")
    parser.add_argument("--smoke-test", action="store_true", help="Run isolated startup checks and exit")
    parser.add_argument("--smoke-test-report", type=Path, help="Save startup-check results as JSON")
    options, qt_arguments = parser.parse_known_args()
    # Accessory apps have windows but no Dock icon. Set before Qt to avoid a flash.
    if sys.platform == "darwin":
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory
        )
    app = QApplication([sys.argv[0], *qt_arguments])
    app.setApplicationName("Lil Buddy")
    app.setOrganizationName("LilBuddy")
    app.setQuitOnLastWindowClosed(False)  # Tool windows do not count as main windows.
    temporary_settings = tempfile.TemporaryDirectory(prefix="lil-buddy-smoke-") if options.smoke_test else None
    settings = (QSettings(str(Path(temporary_settings.name) / "preferences.ini"), QSettings.Format.IniFormat)
                if temporary_settings is not None else None)
    cat = DesktopCat(settings=settings, enable_tray=not options.smoke_test)
    cat.show()
    cat.configure_macos()
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    if options.smoke_test:
        run_smoke_test(cat, app, options.smoke_test_report)
    result = app.exec()
    if temporary_settings is not None:
        temporary_settings.cleanup()
    return result


if __name__ == "__main__":
    raise SystemExit(main())

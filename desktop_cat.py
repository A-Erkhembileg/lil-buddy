#!/usr/bin/env python3
"""Lil Buddy: a small, self-contained PyQt6 desktop cat.

Python 3.10+. Install dependencies with the same Python used to run this file:
    python3 -m pip install PyQt6
    # macOS also needs the native window bridge (no permissions required):
    python3 -m pip install pyobjc-framework-Cocoa
    python3 desktop_cat.py

Click to pet, or drag with the left mouse button; release to drop. Right-click or Control-click
for roaming, reset, and quit. All artwork is drawn here, with no asset files.
macOS can still place secure/system surfaces above ordinary application windows.
"""
from __future__ import annotations

import ctypes
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
        QActionGroup, QPolygon, QRegion, QTransform,
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
        self.vx = 0.0
        self.sleeping = self.roaming and rng.random() < 0.22
        self.decision_in = rng.uniform(3.0, 7.0)

    def drop(self, area: QRectF) -> None:
        self.constrain(area)
        self.state, self.vy, self.sleeping = "fall", 0.0, False
        self.vx = 0.0

    def begin_flight(self, area: QRectF, rng: random.Random) -> None:
        """Begin a short flap-driven excursion without teleporting the pet."""
        self.constrain(area)
        _, _, top, floor = self.bounds(area)
        self.state, self.sleeping = "fly", False
        self.flight_time, self.flight_duration = 0.0, rng.uniform(4.8, 7.2)
        self.flight_start_y = self.y
        self.flight_target_y = max(top, floor - min(210.0, area.height() * 0.28))
        self.vx, self.vy = self.direction * 58.0, -55.0

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
            self.direction = 1
        elif self.x >= right - margin:
            self.direction = -1
        self.vx += (self.direction * 72.0 - self.vx) * min(1.0, 3.2 * dt)
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
            self.rest(rng)
            self.sleeping = False
            return impact
        self.y = min(floor, self.y)
        return 0.0

    def step(self, dt: float, area: QRectF, rng: random.Random,
             autonomous: bool = True) -> float:
        """Advance physics and report the pre-bounce speed of any floor impact."""
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
                if self.vy > BOUNCE_CUTOFF:
                    self.vy = -self.vy * BOUNCE
                else:
                    self.rest(rng)
            self.y = max(top, self.y)
            return impact_speed
        self.y = floor
        if self.state == "walk":
            self.x += self.direction * WALK_SPEED * dt
            if self.x <= left:
                self.x, self.direction = left, 1
            elif self.x >= right:
                self.x, self.direction = right, -1
        if self.state == PETTED or not self.roaming or not autonomous:
            return 0.0
        self.decision_in -= dt
        if self.decision_in <= 0.0:
            if self.state == "walk" or rng.random() < 0.45:
                self.rest(rng)
            else:
                self.state, self.sleeping = "walk", False
                self.direction = rng.choice((-1, 1))
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


def draw_cat(pose: str, frame: int, direction: int, tail_offset: int = 0,
             breath: int = 0, dangle: int = 0,
             palette: Palette = PALETTES["Charcoal Grey"], stretch: int = 0,
             effects: bool = True) -> QPixmap:
    """Cat grid renderer: ordinary poses, four-frame reactions and grounded impacts."""
    if pose in HABIT_DURATIONS:
        return draw_cat_habit(pose, frame, direction, tail_offset, breath, dangle, palette, stretch)
    if pose in ("held", "fall", "pet", "petted"):
        return _draw_cat_expression(pose, frame, direction, palette, stretch, effects)
    if pose == "land":
        # The last row remains the foot anchor through squash, rebound and settle.
        base = _draw_cat_classic("rest", 0, direction, 0, 0, 0, palette)
        source = base.toImage().scaled(ART_WIDTH, ART_HEIGHT,
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
        image = QImage(ART_WIDTH, ART_HEIGHT, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        height, width = ((23, 40), (32, 36), (32, 40))[frame % 3]
        resized = source.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                                Qt.TransformationMode.FastTransformation)
        p = QPainter(image)
        p.drawImage((ART_WIDTH-width)//2, ART_HEIGHT-height, resized)
        p.end()
        return QPixmap.fromImage(image).scaled(WIDTH, HEIGHT,
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
    return _draw_cat_classic(pose, frame, direction, tail_offset, breath, dangle, palette)


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
            return dict(ink="#28241f", fur="#c66b2c", shade="#93451f", highlight="#eaa05a",
                        light="#f0bc7e", cream="#efe6d2", eye="#201d17", glint="#e5ca87",
                        nose="#191b19", pink="#b67c6e", pattern="natural")
        return dict(ink="#29251f", fur="#b88a58", shade="#815a38", highlight="#d3ad7b",
                    light="#e4c998", cream="#e5dbc7", eye="#1b1b16", glint="#ede7d3",
                    nose="#24261f", pink="#b87579", pattern="natural")
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


def draw_dog(pose, frame, direction, tail_offset=0, breath=0, dangle=0,
             palette=None, stretch=0) -> QPixmap:
    """A side-view domestic retriever: long muzzle, hanging ears, jointed legs."""
    if pose == "land":
        return _natural_landing(draw_dog,frame,direction,palette)
    c, g = _natural_colors("Dog",palette), _NaturalGrid()
    air = pose in ("held","fall")
    happy = pose in ("petted","pet")
    walking = pose == "walk"
    phase = frame % 6 if walking else frame % 4
    bob = (0,-1,-1,0,1,0)[phase] if walking else (-2 if air else breath)
    if happy: bob += (0,0,-1,0)[phase]
    if pose == "sleep":
        g.poly([(7,37),(13,31),(31,30),(42,33),(50,38),(51,45),(47,47),(10,47),(6,43)],c["ink"])
        g.poly([(9,37),(15,32),(30,31),(41,34),(48,39),(49,44),(46,46),(11,46),(8,42)],c["fur"])
        g.poly([(34,36),(40,33),(47,36),(51,40),(49,44),(38,44),(33,40)],c["shade"])
        g.poly([(41,39),(49,39),(56,42),(55,45),(44,46),(39,43)],c["cream"])
        g.box(53,41,3,2,c["nose"]);g.line([(43,38),(46,38)],c["ink"])
        g.line([(10,44),(15,45),(27,45)],c["highlight"])
        return g.finish(direction)
    # The tail attaches to the rump and tapers upward, separate from the body.
    wag = (0,-2,1,2)[phase] if happy else max(-2,min(2,tail_offset))
    tail = [(17,28+bob),(11,26+bob),(7,22+wag+bob),(4,16+wag+bob),
            (3,15+wag+bob),(2,17+wag+bob),(4,24+wag+bob),(10,30+bob),(17,32+bob)]
    g.poly(tail,c["ink"])
    g.poly([(16,29+bob),(11,27+bob),(6,22+wag+bob),(4,18+wag+bob),
            (5,24+wag+bob),(11,29+bob),(16,31+bob)],c["fur"])
    # Six-phase trot: diagonal support pairs and a bent rear hock.
    strides=(-4,-2,3,4,1,-3)
    lifts=(0,3,3,0,1,2)
    if walking:
        hind=(19+strides[phase],47-lifts[phase])
        front=(41-strides[phase],47-lifts[(phase+3)%6])
        hind_far=(24-strides[phase],47-lifts[(phase+3)%6])
        front_far=(38+strides[phase],47-lifts[phase])
    elif air:
        hind,front,hind_far,front_far=(
            ((16,42),(45,44),(26,45),(37,40)),
            ((23,45),(38,39),(17,41),(47,45)),
            ((27,42),(44,40),(20,45),(36,45)),
            ((17,40),(38,45),(25,43),(47,40)),
        )[phase]
    else:
        hind,front,hind_far,front_far=(19,47),(41,47),(25,47),(37,47)
    if pose=="groom":front=(45+phase%2,34-phase%2)
    _canine_leg(g,(23,31+bob),hind_far,c,True)
    _canine_foreleg(g,(36,29+bob),front_far,c,True)
    bow = pose == "stretch"
    body_top = 22+bob if not bow else 23
    body_bottom = 35+min(3,max(0,stretch)) if air else 35+bob
    g.poly([(14,27+bob),(18,body_top),(29,21+bob),(38,23+bob),
            (44,29+bob),(41,body_bottom),(34,body_bottom+1),(20,body_bottom),(14,32+bob)],c["ink"])
    g.poly([(16,27+bob),(19,body_top+1),(29,22+bob),(37,24+bob),
            (42,29+bob),(40,body_bottom-1),(33,body_bottom),(21,body_bottom-1),(16,31+bob)],c["fur"])
    g.line([(20,24+bob),(29,23+bob),(36,25+bob)],c["highlight"])
    g.poly([(35,28+bob),(40,27+bob),(41,33+bob),(36,36+bob),(31,35+bob)],c["cream"])
    if c["pattern"]=="calico":g.poly([(20,25+bob),(27,24+bob),(30,28+bob),(26,32+bob),(20,30+bob)],palette.marking)
    if c["pattern"]=="tuxedo":g.box(34,29+bob,7,6,c["cream"])
    if bow:
        g.poly([(15,27),(20,21),(29,24),(41,36),(38,39),(25,34),(16,33)],c["fur"])
        hind=(19,47);front=(52,47)
        _canine_leg(g,(20,32),hind,c)
        _canine_foreleg(g,(37,36),front,c)
    else:
        _canine_leg(g,(20,31+bob),hind,c)
        _canine_foreleg(g,(40,30+bob),front,c)
    # The skull is smaller than the chest and a clear muzzle projects beyond it.
    hx=(0,1,0,-1)[phase] if happy else (-2 if pose=="groom" else 0)
    hy=bob+(7 if bow else 0)+(1 if pose=="groom" else 0)
    if pose=="watch":hy+=max(-1,min(1,dangle))
    g.p.save();g.p.translate(hx,hy)
    g.poly([(36,23),(37,16),(40,12),(47,12),(51,15),(52,20),
            (57,22),(59,24),(59,27),(56,29),(47,28),(43,29),(38,27)],c["ink"])
    g.poly([(38,22),(39,17),(41,13),(47,13),(50,16),(51,21),
            (56,23),(58,24),(58,26),(55,28),(47,27),(43,28),(39,26)],c["fur"])
    g.poly([(46,22),(52,21),(57,23),(58,24),(58,26),(54,28),(47,27),(44,25)],c["cream"])
    g.poly([(39,15),(42,14),(45,17),(45,23),(43,28),(40,29),(37,26),(36,21)],c["ink"])
    g.poly([(39,16),(42,16),(44,18),(43,24),(42,27),(40,27),(38,24),(38,20)],c["shade"])
    g.line([(40,18),(41,24)],c["highlight"])
    _side_eye(g,47,18,c,closed=happy or pose in ("blink","yawn","groom"),vertical=dangle,wide=air)
    g.box(56,23,3,3,c["nose"]);g.box(56,23,1,1,c["highlight"])
    g.line([(49,27),(54,27),(56,26)],c["ink"])
    if pose=="yawn":
        opening=(1,3,5,2)[phase]
        g.box(50,26,7,opening,c["ink"]);g.box(51,26+opening-1,4,2,c["pink"])
    elif happy or pose=="groom":
        g.box(52,27,3,(2,4,3,1)[phase],c["pink"])
    g.p.restore()
    if pose=="groom":_canine_foreleg(g,(39,32),(45+phase%2,34-phase%2),c)
    if happy:g.heart(8+phase,9-phase);g.heart(51,6+phase%2)
    return g.finish(direction)


def draw_fox(pose, frame, direction, tail_offset=0, breath=0, dangle=0,
             palette=None, stretch=0) -> QPixmap:
    """A slender wild red fox with narrow muzzle, black stockings and a white brush tip."""
    if pose=="land":return _natural_landing(draw_fox,frame,direction,palette)
    c,g=_natural_colors("Fox",palette),_NaturalGrid()
    walking=pose=="walk";air=pose in ("held","fall");happy=pose in ("petted","pet")
    phase=frame%6 if walking else frame%4
    bob=(0,-1,-1,0,1,0)[phase] if walking else (-2 if air else breath)
    if pose=="sleep":
        g.poly([(6,36),(12,31),(29,30),(41,34),(47,40),(44,46),(9,47),(4,43)],c["ink"])
        g.poly([(8,36),(14,32),(29,31),(40,35),(45,40),(42,45),(10,46),(6,42)],c["fur"])
        g.poly([(33,35),(36,28),(40,33),(45,31),(49,36),(50,41),(44,44),(36,42)],c["ink"])
        g.poly([(35,36),(37,31),(40,35),(45,33),(47,37),(48,40),(43,42),(37,40)],c["fur"])
        g.poly([(38,40),(43,41),(49,38),(48,42),(43,44)],c["cream"])
        g.box(41,37,3,1,c["eye"])
        g.poly([(6,39),(11,37),(17,40),(25,43),(39,43),(44,41),(43,46),(32,47),(12,47),(5,44)],c["ink"])
        g.poly([(7,39),(12,39),(18,42),(26,44),(39,44),(42,43),(40,45),(32,46),(13,46),(7,43)],c["fur"])
        g.poly([(33,44),(39,44),(42,43),(40,45),(34,46)],c["cream"])
        return g.finish(direction)
    wag=(-1,0,1,0)[phase] if happy else max(-2,min(2,tail_offset))
    # Long, heavy brush trailing behind the hips, with a white terminal third.
    g.poly([(23,30+bob),(18,32+bob),(12,33+wag),(6,32+wag),(1,35+wag),
            (0,39+wag),(4,43+wag),(12,44+wag),(21,39+bob),(26,34+bob)],c["ink"])
    g.poly([(22,31+bob),(18,33+bob),(12,34+wag),(6,33+wag),(2,36+wag),
            (1,39+wag),(5,42+wag),(12,43+wag),(20,38+bob),(24,34+bob)],c["fur"])
    g.poly([(1,38+wag),(3,35+wag),(7,33+wag),(10,35+wag),(8,37+wag),
            (10,39+wag),(7,40+wag),(8,42+wag),(5,42+wag),(2,40+wag)],c["cream"])
    g.line([(12,35+wag),(17,35+wag),(21,33+bob)],c["highlight"])
    strides=(-4,-2,3,4,1,-3);lifts=(0,3,3,0,1,2)
    if walking:
        hind=(23+strides[phase],47-lifts[phase]);front=(43-strides[phase],47-lifts[(phase+3)%6])
        hind_far=(27-strides[phase],47-lifts[(phase+3)%6]);front_far=(39+strides[phase],47-lifts[phase])
    elif air:
        hind,front,hind_far,front_far=(
            ((20,42),(47,44),(29,45),(39,40)),((27,45),(40,39),(21,41),(49,45)),
            ((31,42),(46,40),(24,45),(38,45)),((21,40),(40,45),(29,43),(49,40)))[phase]
    else:hind,front,hind_far,front_far=(23,47),(43,47),(29,47),(39,47)
    if pose=="groom":front=(47,34-phase%2)
    _canine_leg(g,(27,31+bob),hind_far,c,True,True)
    _canine_foreleg(g,(39,28+bob),front_far,c,True,True)
    lower=33+min(3,max(0,stretch)) if air else 33+bob
    g.poly([(20,26+bob),(25,23+bob),(34,23+bob),(41,26+bob),
            (45,30+bob),(42,lower),(35,lower+1),(24,lower),(20,30+bob)],c["ink"])
    g.poly([(22,26+bob),(26,24+bob),(34,24+bob),(40,27+bob),
            (43,30+bob),(41,lower-1),(35,lower),(25,lower-1),(22,29+bob)],c["fur"])
    g.line([(26,25+bob),(33,25+bob),(38,27+bob)],c["highlight"])
    g.poly([(38,26+bob),(42,27+bob),(44,32+bob),(40,36+bob),(35,33+bob)],c["cream"])
    if c["pattern"]=="calico":g.poly([(25,25+bob),(31,24+bob),(34,28+bob),(29,31+bob),(24,29+bob)],palette.marking)
    if pose=="stretch":
        g.poly([(22,27),(26,21),(31,24),(42,35),(43,38),(32,33),(23,31)],c["fur"])
        hind=(23,47);front=(54,47)
        _canine_leg(g,(25,31),hind,c,fox=True)
        _canine_foreleg(g,(40,35),front,c,fox=True)
    else:
        _canine_leg(g,(25,31+bob),hind,c,fox=True)
        _canine_foreleg(g,(42,29+bob),front,c,fox=True)
    hy=bob+(6 if pose=="stretch" else 0)+(1 if happy and phase==2 else 0)
    hx=(-1,0,0,-1)[phase] if happy else 0
    if pose=="watch":hy+=max(-1,min(1,dangle))
    g.p.save();g.p.translate(hx,hy)
    # Small wedge-shaped skull and genuinely long, pointed rostrum.
    g.poly([(39,24),(40,16),(43,10),(45,8),(48,15),(51,16),
            (54,20),(59,23),(59,25),(55,28),(48,30),(42,28)],c["ink"])
    g.poly([(41,23),(42,17),(44,12),(45,11),(47,17),(50,17),
            (53,21),(58,23),(58,24),(54,27),(48,29),(43,27)],c["fur"])
    g.poly([(48,16),(50,10),(52,10),(53,18)],c["ink"])
    g.poly([(50,14),(51,12),(52,17)],c["shade"])
    g.poly([(44,13),(45,12),(47,18),(44,17)],"#573729")
    g.poly([(43,23),(47,25),(52,25),(58,23),(57,26),(53,28),(48,29),(44,27)],c["cream"])
    _side_eye(g,50,19,c,closed=happy or pose in ("blink","yawn","groom"),vertical=dangle,wide=air)
    g.box(57,23,3,2,c["nose"])
    g.line([(51,27),(55,26)],c["ink"])
    g.box(46,18,2,1,c["highlight"])
    if pose=="yawn":
        opening=(1,3,4,2)[phase];g.box(53,25,5,opening,c["ink"]);g.box(54,25+opening-1,3,1,c["pink"])
    if happy and phase%2:g.box(55,27,2,2,c["pink"])
    g.p.restore()
    if pose=="groom":_canine_foreleg(g,(41,31),(47,34-phase%2),c,fox=True)
    if happy:g.heart(12+phase,13-phase);g.heart(49,3+phase%2)
    return g.finish(direction)


_CROW_WINGS = (
    [(31,26),(25,18),(23,9),(19,1),(17,2),(18,8),(14,4),(12,5),(15,12),(10,8),(8,10),(16,22),(24,29)],
    [(31,26),(24,18),(19,11),(11,5),(9,6),(13,12),(7,9),(5,11),(10,17),(4,14),(3,17),(16,26),(25,29)],
    [(32,26),(23,20),(15,17),(5,15),(2,16),(8,19),(1,19),(1,21),(9,23),(3,24),(5,26),(17,28),(27,29)],
    [(32,25),(24,24),(17,27),(7,30),(5,33),(12,32),(7,36),(9,38),(16,34),(13,40),(16,41),(24,33),(29,29)],
    [(32,24),(26,26),(21,32),(14,43),(15,46),(19,40),(18,47),(21,47),(24,40),(24,46),(27,44),(30,34),(33,28)],
    [(32,25),(28,27),(25,32),(20,39),(21,42),(24,37),(24,43),(27,42),(28,36),(30,39),(32,35),(33,28)],
    [(31,26),(25,23),(19,22),(10,23),(8,21),(15,19),(9,18),(10,16),(18,18),(15,13),(17,12),(25,20),(32,24)],
    [(31,26),(27,20),(25,13),(22,6),(19,5),(20,12),(16,8),(14,9),(18,16),(12,13),(11,15),(20,25),(27,29)],
)


def _crow_wing(g, frame, colors, far=False):
    points=_CROW_WINGS[frame%8]
    if far:points=[(min(59,x+4),max(1,y-1)) for x,y in points]
    g.poly(points, colors["ink"])
    # The second surface follows the elbow and wrist, preserving individual primaries.
    root=(points[0][0]-1,points[0][1])
    elbow=(points[2][0]+1,points[2][1]+1)
    wrist=(points[3][0]+2,points[3][1]+1)
    g.poly([root,(points[1][0]+1,points[1][1]+1),elbow,wrist,
            (points[-2][0],points[-2][1]),(points[-1][0],points[-1][1]-1)],
           colors["shade"] if far else colors["fur"])
    g.line([root,elbow,wrist],colors["cream"] if far else colors["highlight"])
    g.line([(root[0]-2,root[1]+1),(elbow[0]-1,elbow[1]+2)],colors["shade"])


def draw_crow(pose, frame, direction, tail_offset=0, breath=0, dangle=0,
              palette=None, stretch=0) -> QPixmap:
    """A black corvid: heavy pointed beak, layered feathers, long tail and eight wing strokes."""
    if pose=="land":return _natural_landing(draw_crow,frame,direction,palette)
    c,g=_natural_colors("Crow",palette),_NaturalGrid()
    flying=pose in ("fly","held","fall")
    phase=frame%8 if flying else frame%4
    happy=pose in ("petted","pet")
    bob=(0,-1,0,1)[phase] if pose=="walk" else breath
    if happy:bob+=(0,-1,0,1)[phase]
    if pose=="sleep":
        g.poly([(9,38),(17,31),(27,28),(38,30),(44,36),(43,43),(37,47),(18,47)],c["ink"])
        g.poly([(13,38),(19,32),(28,29),(37,31),(42,36),(41,42),(36,45),(20,46)],c["fur"])
        g.poly([(19,36),(25,32),(34,33),(39,37),(34,43),(22,44),(16,41)],c["shade"])
        g.line([(22,35),(29,35),(35,38),(31,42)],c["highlight"])
        g.box(33,32,4,1,c["light"])
        return g.finish(direction)
    if flying:
        _crow_wing(g,phase,c,True)
        body_y=0
    else:body_y=bob
    # Long, graduated tail feathers give a corvid silhouette rather than a round songbird.
    g.poly([(24,31+body_y),(17,31+body_y),(6,35+body_y),(4,38+body_y),
            (8,39+body_y),(10,37+body_y),(10,40+body_y),(14,39+body_y),(23,35+body_y)],c["ink"])
    g.poly([(22,32+body_y),(16,33+body_y),(7,36+body_y),(7,37+body_y),(14,36+body_y),
            (13,38+body_y),(20,35+body_y)],c["shade"])
    g.line([(17,33+body_y),(9,37+body_y)],c["highlight"])
    g.poly([(18,27+body_y),(24,22+body_y),(33,20+body_y),(40,23+body_y),
            (42,28+body_y),(38,35+body_y),(31,39+body_y),(23,37+body_y),(18,33+body_y)],c["ink"])
    g.poly([(20,27+body_y),(25,23+body_y),(33,21+body_y),(39,24+body_y),
            (40,28+body_y),(37,34+body_y),(31,37+body_y),(24,36+body_y),(20,32+body_y)],c["fur"])
    g.poly([(25,34+body_y),(31,35+body_y),(37,31+body_y),(35,36+body_y),(30,38+body_y)],c["shade"])
    if flying:
        _crow_wing(g,phase,c)
    else:
        # Folded scapulars and primary tips overlap in three clear feather groups.
        g.poly([(22,27+body_y),(29,23+body_y),(35,25+body_y),(37,29+body_y),
                (32,35+body_y),(25,37+body_y),(21,34+body_y)],c["ink"])
        g.poly([(23,27+body_y),(29,24+body_y),(34,26+body_y),(35,29+body_y),
                (31,33+body_y),(25,35+body_y),(22,33+body_y)],c["shade"])
        g.line([(24,27+body_y),(30,26+body_y),(33,28+body_y)],c["highlight"])
        g.line([(24,30+body_y),(29,30+body_y),(33,28+body_y)],c["cream"])
        g.line([(25,33+body_y),(30,32+body_y)],c["highlight"])
        if pose in ("groom","preen","stretch"):
            spread=(0,2,4,1)[phase]
            g.poly([(27,27),(18,20-spread),(12,20-spread),(15,25),(23,34),(30,33)],c["ink"])
            g.poly([(26,28),(18,22-spread),(15,22-spread),(23,32),(29,32)],c["highlight"])
    # Toes bend naturally beneath the belly; flight tucks them back.
    if flying:
        for x,shift in ((29,phase%2),(35,(phase+1)%2)):
            g.line([(x,36),(x-2,39),(x+1,40+shift)],c["ink"])
            g.line([(x+1,40+shift),(x+4,40+shift),(x+3,41+shift)],c["shade"])
    else:
        for index,x in enumerate((29,36)):
            if pose=="walk":x+=((-1,1),(0,0),(1,-1),(0,0))[phase][index]
            lift=2 if pose=="walk" and phase%2==index else 0
            g.line([(x,36+bob),(x-1,41),(x+1,46-lift)],c["ink"])
            g.box(x,45-lift,2,2,c["shade"])
            g.line([(x+1,46-lift),(x+6,47-lift)],c["ink"])
            g.line([(x+1,46-lift),(x-3,47-lift)],c["ink"])
            g.box(x+5,47-lift,2,1,c["ink"])
            g.box(x-3,47-lift,1,1,c["ink"])
    # The head and beak maintain a stable pivot through the wing cycle.
    hx=-3 if pose in ("groom","preen") else (0,1,0,-1)[phase%4] if happy else 0
    hy=5 if pose in ("groom","preen") else (0 if flying else bob)
    if pose=="watch":hy+=max(-1,min(1,dangle))
    g.p.save();g.p.translate(hx,hy)
    g.poly([(34,24),(36,18),(40,14),(45,14),(49,17),(50,20),(55,22),
            (59,25),(59,26),(51,27),(47,28),(43,30),(37,28)],c["ink"])
    g.poly([(36,24),(38,19),(41,15),(45,15),(48,18),(49,21),(53,23),
            (56,25),(50,26),(46,27),(43,29),(38,27)],c["fur"])
    g.poly([(48,20),(54,22),(59,25),(54,25),(49,24)],c["nose"])
    g.line([(50,23),(56,25)],c["highlight"])
    g.line([(39,18),(43,16),(46,17)],c["highlight"])
    g.line([(38,24),(41,26),(44,27)],c["cream"])
    if pose in ("blink","petted","pet","groom","preen"):
        g.line([(44,20),(47,20)],c["light"])
    else:
        g.box(45,19+max(-1,min(1,dangle)),2,2,c["eye"])
        g.box(46,19+max(-1,min(1,dangle)),1,1,c["glint"])
    if pose=="yawn":
        gap=(1,2,3,1)[phase]
        g.poly([(50,25),(58,26),(51,26+gap),(48,26+gap)],c["ink"])
        g.box(50,26,3,gap,c["pink"])
    g.p.restore()
    if happy:g.heart(12+phase,11-phase);g.heart(51,6+phase%2)
    return g.finish(direction)


def draw_natural_peek(animal, palette=None) -> QPixmap:
    """Small side-profile corner heads, with no feline ears or frontal eyes."""
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
        g.poly([(43,5),(46,0),(52,0),(55,4),(55,9),(59,11),(59,16),(53,17),(49,20),(43,18),(40,11)],c["ink"])
        g.poly([(44,6),(47,1),(52,1),(54,4),(54,10),(58,12),(58,15),(53,16),(49,18),(44,17),(42,11)],c["fur"])
        g.poly([(42,5),(46,5),(47,12),(45,20),(42,20),(40,16)],c["shade"])
        g.poly([(52,11),(57,11),(59,12),(59,16),(53,16),(51,14)],c["cream"])
        g.box(58,11,2,3,c["nose"]);_side_eye(g,51,7,c)
    else:
        g.poly([(42,8),(44,0),(47,1),(49,7),(53,6),(55,9),(59,13),(59,15),(53,18),(46,20),(42,16)],c["ink"])
        g.poly([(43,9),(45,2),(47,4),(48,9),(52,8),(54,10),(58,13),(58,14),(53,17),(46,18),(43,15)],c["fur"])
        g.poly([(46,14),(50,15),(58,13),(57,16),(52,18),(47,18)],c["cream"])
        _side_eye(g,51,10,c);g.box(58,13,2,2,c["nose"])
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


def draw_peek(animal, palette) -> QPixmap:
    """Species-specific static corner faces; input stays transparent in the engine."""
    if animal != "Cat":
        return draw_natural_peek(animal, palette)
    g,c=_PetGrid(palette),palette
    if animal=="Cat" or animal=="Fox":
        g.poly([(27,3),(28,0),(31,3),(35,3),(38,0),(39,1),(39,10),(37,12),(29,12),(27,10)],c.ink)
        g.box(29,4,9,6,c.fur)
        if animal=="Fox":g.poly([(28,9),(33,10),(39,8),(37,12),(30,12)],"#eee8db")
    elif animal=="Dog":
        g.poly([(28,2),(31,0),(36,0),(39,3),(39,11),(36,13),(29,12),(27,9)],c.ink)
        g.box(29,3,9,8,c.fur);g.box(27,4,3,9,c.shade);g.box(34,9,5,3,c.muzzle)
    else:
        g.poly([(27,4),(31,1),(35,0),(39,3),(39,10),(36,13),(29,12),(26,9)],c.ink)
        g.box(29,4,10,6,c.fur);g.box(37,8,3,2,"#e9ac59")
    for x in (30,35):
        g.box(x,6,2,2,c.neon);g.box(x,12,3,2,c.highlight)
    if animal!="Crow":g.box(33,9,2,1,c.pink)
    g.box(39,3,1,7,c.ink)
    return g.finish(1)


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
        self.palette_name = "Charcoal Grey"
        self.palette = PALETTES[self.palette_name]
        self.animal_type = "Cat"
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
        self._drag_sample_time = 0.0
        self._drag_sample_position = QPointF()
        self._landing_elapsed: float | None = None
        self._pet_until = 0.0
        self.bubble = SpeechBubble(self)
        self._menu_open = False
        self._last_time = time.monotonic()
        self._animation_time = 0.0
        self._blink_in = self.rng.uniform(2.0, 5.0)
        self._blink_left = 0.0
        self._frames: dict[tuple, tuple[QPixmap, QRegion]] = {}
        for pose, count in (("rest", 1), ("blink", 1), ("walk", 2),
                            ("sleep", 2), ("held", 4), ("fall", 4), (PETTED, 4), ("land", 3)):
            for frame in range(count):
                for direction in (-1, 1):
                    pixmap = draw_cat(pose, frame, direction)
                    key = pose, frame, direction, 0, 0, 0, self.animal_type, self.palette, 0
                    self._frames[key] = (pixmap, QRegion(pixmap.mask()))
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

        palette_name = str(value("appearance/palette", "Charcoal Grey"))
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
        if legacy_bird and self.palette_name == "Charcoal Grey":
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
        self._drag_speed = 0.0
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
                or self._user_hidden or self.motion.state == "held"):
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
        phase = math.sin(self._animation_time * math.tau / 3.6)
        tail_offset = round(2 * phase)
        breath = -1 if phase > 0.4 else 0
        dangle, stretch = 0, 0
        if self._latched:
            pose, frame, tail_offset, breath, dangle = "peek", 0, 0, 0, 0
        elif state == "fly":
            frame = int(self.motion.flight_time / CROW_FLAP_SECONDS) % 8
            tail_offset, breath = 0, 0
            dangle = max(-1, min(1, round(self.motion.vy / 80.0)))
        elif self._landing_elapsed is not None and state != "held":
            pose, frame = "land", min(2, int((self._landing_elapsed + 1e-9) / LAND_FRAME_SECONDS))
            tail_offset, breath = 0, 0
        elif state in ("held", "fall"):
            count, seconds = (8, CROW_FLAP_SECONDS) if self.animal_type == "Crow" else (4, AIR_FRAME_SECONDS)
            frame = int(self._animation_time / seconds) % count
            speed = self._drag_speed if state == "held" else abs(self.motion.vy)
            stretch = min(3, int(speed / 350.0))
            tail_offset, breath = 0, 0
        elif self._pet_until > 0:
            pose, frame = PETTED, int(self._animation_time / 0.09) % 4
            tail_offset, breath = 0, 0
        elif self._habit is not None:
            pose, frame = self._habit, int(self._habit_elapsed / 0.18) % 4
            tail_offset, breath = 0, 0
            if pose == "watch":
                cursor = QCursor.pos()
                center = self.geometry().center()
                self.motion.direction = 1 if cursor.x() >= center.x() else -1
                dangle = -1 if cursor.y() < center.y() - 12 else 1 if cursor.y() > center.y() + 12 else 0
        elif state == "rest":
            pose = "sleep" if self.motion.sleeping else "blink" if self._blink_left > 0 else "rest"
            frame = int(phase > 0.3) if pose == "sleep" else 0
        elif state == "walk":
            count, seconds = ((6, 0.09) if self.animal_type in ("Dog", "Fox") else
                              (4, 0.13) if self.animal_type == "Crow" else (2, 0.18))
            frame = int(self._animation_time / seconds) % count
            breath = 0
        direction = 1 if pose == "peek" or (pose == PETTED and self.animal_type == "Cat") else self.motion.direction
        key = pose, frame, direction, tail_offset, breath, dangle, self.animal_type, self.palette, stretch
        if key != self._frame_key:
            if key not in self._frames:
                pixmap = (draw_peek(self.animal_type, self.palette) if pose == "peek" else
                          DRAWERS[self.animal_type](*key[:6], palette=self.palette, stretch=stretch))
                if pixmap.size() != self.size():
                    pixmap = pixmap.scaled(self.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                           Qt.TransformationMode.FastTransformation)
                # Cache only a bounded set of quantized grid poses, never 60 images/sec.
                if len(self._frames) >= 128:
                    self._frames.pop(next(iter(self._frames)))
                self._frames[key] = pixmap, QRegion(pixmap.mask())
            self._pixmap, mask = self._frames[key]
            # Transparent pixels pass clicks through; only the cat is draggable.
            if self.mask() != mask:
                self.setMask(mask)
            self._frame_key = key
            self.update()

    def _tick(self, now: float | None = None) -> None:
        if self._latched or self._paused or self._user_hidden:
            return
        now = time.monotonic() if now is None else now
        dt, self._last_time = min(max(0.0, now - self._last_time), 0.1), now
        # Speech uses a real deadline, even if a menu or a system sleep stalls physics.
        if self._pet_until and now >= self._pet_until:
            self._dismiss_reaction()
        if self._menu_open or self._color_dialog is not None:
            return
        area = self.available_area()
        if area != self._last_area and self.motion.state != "held":
            self.motion.constrain(area)
            self._last_area = area
        self._animation_time += dt
        if self._habit is not None:
            if self.motion.state != "rest" or self.motion.sleeping or self._pet_until:
                self._cancel_habit()
            else:
                self._habit_elapsed += dt
                if self._habit_elapsed >= self._habit_duration:
                    self._cancel_habit()
        elif (self._habits_enabled and self.motion.roaming and self.motion.state == "rest"
              and not self.motion.sleeping and not self._pet_until and self._press_position is None):
            self._habit_in -= dt
            if self._habit_in <= 0:
                self._choose_habit()
        if self._landing_elapsed is not None:
            self._landing_elapsed += dt
            if self._landing_elapsed >= 3 * LAND_FRAME_SECONDS:
                self._landing_elapsed = None
        if self.motion.state == "held" and now - self._drag_sample_time >= 0.08 - 1e-9:
            self._drag_speed *= math.exp(-8.0 * dt)
            if self._drag_speed < 0.5:
                self._drag_speed = 0.0
        self._blink_in -= dt
        self._blink_left = max(0.0, self._blink_left - dt)
        if self._blink_in <= 0:
            self._blink_left, self._blink_in = 0.14, self.rng.uniform(2.5, 6.0)
        # Small substeps keep collisions stable if an occasional UI frame is late.
        while dt > 0:
            step = min(dt, 1.0 / 120.0)
            if self._press_position is None or self._dragging or self.motion.state in ("fall", "fly"):
                was_flying = self.motion.state == "fly"
                impact = self.motion.step(step, area, self.rng,
                                          autonomous=self._pet_until == 0 and self._habit is None)
                if impact >= HARD_LANDING_SPEED or (was_flying and self.motion.state == "rest"):
                    self._landing_elapsed = 0.0
            dt -= step
        if self._pet_until and self.motion.state not in ("held", "fall", "fly"):
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
        self._landing_elapsed = None
        self._cancel_habit()
        self.motion.sleeping = False
        if self.motion.state not in ("held", "fall", "fly"):
            self.motion.state, self.motion.vy = PETTED, 0.0
        self.motion.decision_in = self.rng.uniform(1.5, 3.0)
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
            self._drag_speed = 0.0
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
                self._drag_speed = 0.0
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
        if self.motion.state not in ("held", "fall", "fly"):
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
                    assert cat._pixmap.size() == cat.size(), (animal, size)
                    assert cat.motion.width == cat.width() and cat.motion.height == cat.height()
            checks.append("all animals and sizes")
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

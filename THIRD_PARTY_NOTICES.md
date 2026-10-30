# Third-party software and source

Copyright © 2026 A-Erkhembileg. Lil Buddy's original source code and artwork are
available under the MIT license in `LICENSE`. The packaged application combines
that code with GPL-licensed PyQt6 and is distributed as a whole under **GNU GPL
version 3**, with the component permissions and exceptions below. You may run,
study, modify, and redistribute it under those terms. It comes without warranty.
The complete GPL and LGPL texts and upstream notices are in `licenses/`.

## Corresponding source and rebuilding

The application's matching source and build scripts are published with every
release at <https://github.com/A-Erkhembileg/lil-buddy/releases>. The `v1.0.0`
application source is also available at
<https://github.com/A-Erkhembileg/lil-buddy/tree/v1.0.0>. Use the release source
archive and the dependency/version manifest accompanying your particular binary;
the manifest identifies the exact Python and dependency versions used by its
native runner. `README.md` explains how to run and rebuild the application.
The dependencies are unmodified upstream distributions.

Upstream source for the pinned runtime dependencies is available without charge:

- **PyQt6 6.12.0**, copyright Riverbank Computing Limited: GPL-3.0-only.
  [Source distribution](https://pypi.org/project/PyQt6/6.12.0/#files) and
  [build instructions](https://www.riverbankcomputing.com/static/Docs/PyQt6/installation.html).
- **Qt 6.12.0**, copyright The Qt Company Ltd. and contributors: LGPL-3.0,
  with third-party components under their respective licenses. The bundled Qt
  Core, GUI, Widgets, Network and D-Bus components are in
  [qtbase source](https://download.qt.io/archive/qt/6.12/6.12.0/submodules/qtbase-everywhere-src-6.12.0.tar.xz).
  The plugins additionally use
  [qtsvg source](https://download.qt.io/archive/qt/6.12/6.12.0/submodules/qtsvg-everywhere-src-6.12.0.tar.xz)
  and [qtimageformats source](https://download.qt.io/archive/qt/6.12/6.12.0/submodules/qtimageformats-everywhere-src-6.12.0.tar.xz).
  [Qt source build instructions](https://doc.qt.io/qt-6/build-sources.html).
  `licenses/Qt-6.12.0-Third-Party.txt` preserves the official upstream attribution
  and license statements for these components, including optional platform code.
- **PyQt6-sip 13.13.0**, copyright Phil Thompson: BSD-2-Clause.
  [Source distribution](https://pypi.org/project/PyQt6-sip/13.13.0/#files).
- **PyObjC Core / Cocoa 12.2.2** (macOS only), copyright Ronald Oussoren,
  Bill Bumgarner, Steve Majewski, Lele Gaifax and contributors: MIT.
  [Core source](https://pypi.org/project/pyobjc-core/12.2.2/#files) and
  [Cocoa source](https://pypi.org/project/pyobjc-framework-Cocoa/12.2.2/#files).
  PyObjC's libffi portions retain their separate libffi license notices.
- **CPython**, copyright Python Software Foundation and contributors: PSF
  License Agreement and historical licenses. The exact patch version is listed
  in the release manifest. Obtain its matching source from
  <https://www.python.org/downloads/source/>; build instructions and the full
  bundled library notices are included in that source distribution.
- **PyInstaller 6.22.3**, copyright its development team, Giovanni Bajo and
  McMillan Enterprises: GPL-2.0-or-later with the bootloader distribution
  exception; runtime hooks use Apache-2.0. Its exception permits redistribution
  of the frozen executable without imposing additional restrictions from the
  bootloader. [Source](https://pypi.org/project/pyinstaller/6.22.3/#files).

The source links above are part of the source-access instructions accompanying
the binary downloads. They identify the upstream libraries needed in addition
to Lil Buddy's own source archive. Retain access to the matching sources and
these notices when redistributing a binary or a modified build.

Qt is dynamically linked in the packaged app. To replace it, rebuild from the
published application source with your compatible modified Qt/PyQt build; no
private signing key, activation code, account, or downloaded application asset
is required. The macOS build script applies an ad-hoc signature, which you can
regenerate locally. The application imposes no restriction on reverse
engineering for debugging changes to the linked libraries.

Pillow is used only by the build/export tools and is not an application runtime
dependency. Other build tools and operating-system libraries retain their own
licenses. The project does not bundle Steam game assets, trademarks, downloaded
fonts, or third-party animal artwork.

Official licensing references:
[PyQt licensing](https://www.riverbankcomputing.com/software/pyqt/intro),
[Qt third-party code](https://doc.qt.io/qt-6/licenses-used-in-qt.html), and
[PyInstaller license](https://pyinstaller.org/en/stable/license.html).

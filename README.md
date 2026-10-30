# Lil Buddy

A quiet pixel pet for macOS and Windows. Choose a cat, domestic retriever, wild
fox, or black crow. Every animal is drawn and animated in code, with no image files,
AI service, account, or internet connection needed at runtime.

Free to download, use, modify, and share. Original project source and artwork
use the [MIT License](LICENSE). Packaged applications combine Lil Buddy with
PyQt6 and are distributed under GPL version 3; see [Licensing](#licensing).

![Crow, dog, and fox sprite sheet](artwork/animal-sprite-sheet.png)

## Download and install

**[Download for Windows (64-bit)](https://github.com/A-Erkhembileg/lil-buddy/releases/latest/download/Lil-Buddy-Windows-amd64.zip)**
· **[Download for Mac (Apple silicon)](https://github.com/A-Erkhembileg/lil-buddy/releases/latest/download/Lil-Buddy-macOS-arm64.dmg)**
· **[Download for Mac (Intel)](https://github.com/A-Erkhembileg/lil-buddy/releases/latest/download/Lil-Buddy-macOS-x86_64.dmg)**

Downloads are public and do not require a GitHub account or Python. Find all
packages and the source ZIP at [GitHub Releases](https://github.com/A-Erkhembileg/lil-buddy/releases/latest).

**Cloning, `git pull`, and Code → Download ZIP download the Python source.**
They do not include a Windows executable. To run the app without Python, use
the Windows download above.

- **macOS:** download the DMG for your Mac's processor, open it, and drag
  **Lil Buddy.app** to Applications. Open the app and use the menu-bar face for
  settings and quit. An `arm64` build is for Apple silicon; an `x86_64` build is
  for Intel.
- **Windows:** download **Lil-Buddy-Windows-amd64.zip**, right-click it and choose
  **Extract All**, then open **Lil Buddy.exe** in the extracted folder. The
  executable's name contains a space; Windows may display it as **Lil Buddy**
  when filename extensions are hidden. Use its system-tray icon for settings
  and quit.

These are unsigned preview builds: the Mac bundle is ad-hoc signed, without
Apple notarization, and the Windows executable has no publisher certificate.
Your operating system may show a security prompt. Source instructions below
are available if you prefer to run or build the app yourself.

## Controls

Left-click to pet your buddy. It responds for three seconds with happy eyes,
a nuzzle, a vibrating tail, floating hearts, and a species-specific quote.
Left-drag to pick it up; release to drop it. Fast movement stretches its body,
its ears flatten, and its paws paddle. Hard landings squash and rebound.

Right-click the pet, or use its menu-bar/tray icon:

- **Change Color:** Natural Coat, Charcoal Grey, Tuxedo Black, Calico Orange, or Custom RGB.
  RGB previews live; Cancel restores the previous choice.
- **Animal Type:** Cat, Dog, Fox, or Crow, each with its own animations and quotes.
  Dog, fox, and crow use independently drawn side-view anatomy on a finer pixel
  grid. Natural Coat gives the crow black feathers and bill, the dog a fawn coat,
  and the fox russet fur with dark stockings and a white chest and tail tip.
- **Take flight:** available for Crow. Eight wing phases accompany momentum,
  flap-driven lift, air drag, gentle steering, and a controlled descent. A crow
  can also take short spontaneous flights during idle habits. Releasing it
  above the floor starts a flight when roaming is enabled; disabling roaming
  during flight brings it down gently. Pausing and fullscreen mode still apply.
- **Pet Size:** Small, Medium, or Large. Drawing, hit testing, and physics resize
  together; feet remain on the floor.
- **Show Pet:** hide or restore the pet without quitting. Hiding is available
  when a tray/menu-bar icon is present, so the pet can always be brought back.
- **Pause:** freeze movement and animation while keeping the controls available.
- **Allow wandering:** enable autonomous walking and naps.
- **Idle habits:** quiet grooming, yawning, stretching, preening, and play bows.
- **Watch cursor:** occasional short attention poses; disabled by default. The
  pet never grabs, moves, or chases your pointer.
- **Back to floor / Quit Lil Buddy:** recover its position or exit.

Control-click also opens the pet menu on macOS. Ctrl-C exits when launched from
an interactive terminal. Double-clicking the Windows tray icon toggles visibility.

## Remembered preferences

Colors (including RGB), animal, size, roaming, pause, habit choices, display, and
last normal position are saved automatically. Temporary full-screen corner
positions never replace the normal saved position. The next launch is always
visible, even if the previous session was hidden.

Preferences live in `Lil Buddy/preferences.ini` under the operating system's
standard configuration directory. Set `LIL_BUDDY_SETTINGS_DIR` to choose another
location. Automated checks use temporary settings and do not alter your choices.

## Full-screen behavior

A separate 250 ms monitor checks foreground-window geometry on the pet's display.
A full-display window activates corner mode; ordinary maximized work-area windows
do not. Borderless windows covering the whole display count as fullscreen.

The pet cancels speech and dragging, stops its animation/physics timer, and snaps
to the exact top-right edge as a small static, input-transparent face. Two
consecutive non-fullscreen observations (about half a second) release it into
gravity. Pause and hide preferences remain in effect. Its menu-bar/tray controls
stay available throughout.

Detection reads window geometry and process identifiers, without screenshots,
window titles, Accessibility access, or Screen Recording requests. The operating
system can still place secure/exclusive fullscreen surfaces above the pet.

**Known macOS issue:** petting the animal may switch desktops when another app
uses a fullscreen Space. This remains unresolved in the current preview.

## Run from source

Requires Python 3.10+, Git, and a graphical macOS or Windows desktop.
Clone the project first, or use GitHub's **Code → Download ZIP** and extract it:

```sh
git clone https://github.com/A-Erkhembileg/lil-buddy.git
cd lil-buddy
```

On macOS:

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python desktop_cat.py
```

On Windows, in PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe desktop_cat.py
```

Skip virtual-environment creation if `.venv` exists. Cocoa is installed only on
macOS. The runtime remains consolidated in `desktop_cat.py`; `tools/` and
`packaging/` contain only build tooling and app icons.

## Sprite sheets and flight preview

`artwork/animal-sprite-sheet.png` shows the crow, dog, and fox on a solid
background. The crow row contains its perch and all eight flight phases; the
canine rows contain their rest, six walking phases, petting, and sleep poses.
`artwork/crow-flight.gif` previews the complete wing cycle.

The transparent 60 by 48 pixel frame atlas, JSON clip definitions, and crow flight
strip are also in `artwork/`. These are exports of the actual in-code renderers,
so the running app requires no image files. Regenerate them with:

```sh
./.venv/bin/python -B tools/export_sprite_sheet.py
```

## Build native apps

```sh
./.venv/bin/python -m pip install -r requirements-build.txt
./.venv/bin/python tools/build_app.py --dmg
```

On Windows, use `.\.venv\Scripts\python.exe tools\build_app.py` without `--dmg`.
The build uses PyInstaller on the target operating system. Mac output contains
`dist/Lil Buddy.app`, an architecture-specific ZIP, and optionally a DMG. Windows
output contains `dist/Lil Buddy.exe` and a ZIP with instructions. Packages include
the application source, third-party notices, and dependency license texts.

The **Build apps** workflow builds macOS Apple silicon, macOS Intel, and Windows
x64 packages on native GitHub runners. It runs the test suite and launches every
frozen executable in isolated smoke mode before publishing. Branch builds
provide temporary Actions artifacts. A successful main build publishes a new
public GitHub Release when the version in `tools/build_app.py` has no existing
release; version tags such as `v1.0.0` can also trigger publication. Existing
releases are left unchanged, so increment `APP_VERSION` for the next release.

Releases include DMGs, application ZIPs, the matching source ZIP, and
`SHA256SUMS` checksums. Anyone can download them without signing in.

## Verify

```sh
./.venv/bin/python -B -m unittest discover -s tests -v
./.venv/bin/python desktop_cat.py --smoke-test --smoke-test-report build/source-smoke.json
```

Unit checks cover physics, animations, animals, palettes, preferences, sizes,
controls, habits, and fullscreen transitions. The packaged smoke test checks real
startup, all animals and sizes, speech, pause/hide, and latch/release in temporary
settings. A Windows runner verifies the executable builds and starts; manual
checks of exclusive games and different display layouts remain useful on actual
user machines.

## Project layout

```text
desktop_cat.py               Complete pet engine and embedded pixel art
requirements.txt             Runtime dependencies
requirements-build.txt       Packaging and sprite-export dependencies
artwork/                     Sprite sheet, atlas, and crow flight preview
packaging/icons/             Application icons
tools/                       Native app builder and sprite exporter
tests/                       Automated checks
.github/workflows/           macOS/Windows builds and tagged releases
LICENSE                      MIT license for original source and artwork
THIRD_PARTY_NOTICES.md        Bundled dependency notices
licenses/                    Dependency license texts
```

There is one project folder and one application entry point. `.venv/` is a local
Python environment, `build/` contains generated working files, and `dist/`
contains local installers. These generated folders, caches, and personal
settings are excluded from Git. They are not needed in a source download.

To update a clone, run `git pull` from the project folder, then reinstall the
requirements if they changed. Quit and reopen the pet to load updated code.
To update a packaged app, quit it and download the latest package from Releases;
`git pull` does not update a previously downloaded executable.

## Licensing

Original Lil Buddy source code and artwork are provided under the
[MIT License](LICENSE). The packaged applications include PyQt6, so the combined
executables are distributed under GPL version 3. This does not require a fee to
use, download, or share the app; redistribution must preserve the applicable
license terms and source availability.

Packages include the application source, [third-party notices](THIRD_PARTY_NOTICES.md),
and [dependency license texts](licenses/). Each GitHub Release includes a source
ZIP matching that exact release. See the notices for the licenses and source
locations of bundled dependencies.

## Contributing

Bug reports and pull requests are welcome at
[the GitHub repository](https://github.com/A-Erkhembileg/lil-buddy).
Include your operating system, processor architecture, and steps to reproduce a
problem. Run the checks above before submitting code changes. Keep animal art
and runtime behavior in `desktop_cat.py` so source launches stay self-contained.

"""Packaging configuration checks; do not build or publish applications."""
from pathlib import Path
import tempfile
import unittest
import zipfile
import json

from tools.build_app import render_spec, write_windows_zip, prepare_distribution_notices, ROOT


class BuildConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/a project with spaces/Lil Buddy")
        self.icons = {kind: self.root / "packaging" / f"icon.{kind}"
                      for kind in ("icns", "ico")}

    def test_macos_bundle_is_windowed_and_has_no_dock_icon(self):
        source = render_spec("darwin", self.root, self.icons)
        compile(source, "generated.spec", "exec")
        self.assertIn('"LSUIElement": True', source)
        self.assertIn("console=False", source)
        self.assertIn("com.lilbuddy.desktop", source)
        self.assertIn("BUNDLE(collection", source)
        self.assertIn("'AppKit'", source)
        self.assertIn(repr(str(self.root / "desktop_cat.py")), source)

    def test_windows_executable_is_windowed_and_does_not_collect_cocoa(self):
        source = render_spec("win32", self.root, self.icons)
        compile(source, "generated.spec", "exec")
        self.assertIn("console=False", source)
        self.assertNotIn("BUNDLE(", source)
        self.assertIn("hiddenimports=[]", source)
        self.assertNotIn("AppKit", source)
        self.assertIn("a.binaries, a.datas", source)

    def test_unsupported_build_host_is_rejected(self):
        with self.assertRaises(ValueError):
            render_spec("linux", self.root, self.icons)

    def test_windows_archive_contains_executable_and_installation_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            executable, license_path = folder / "Lil Buddy.exe", folder / "LICENSE"
            executable.write_bytes(b"test executable")
            license_path.write_text("test license")
            output = folder / "bundle.zip"
            write_windows_zip(executable, output, license_path)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), {"Lil Buddy.exe", "LICENSE.txt", "Start Here.txt"})
                self.assertEqual(archive.read("Lil Buddy.exe"), b"test executable")
                self.assertIn(b"Python is not required", archive.read("Start Here.txt"))
                self.assertEqual(archive.getinfo("Lil Buddy.exe").date_time, (2026,1,1,0,0,0))

    def test_release_packages_include_licenses_and_matching_application_source(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            notices = folder / "notices"
            prepare_distribution_notices(ROOT, notices)
            self.assertTrue((notices / "THIRD_PARTY_NOTICES.md").is_file())
            self.assertTrue(any((notices / "licenses").rglob("*GPL*")))
            with zipfile.ZipFile(notices / "Application-Source.zip") as source:
                self.assertEqual(source.read("desktop_cat.py"), (ROOT / "desktop_cat.py").read_bytes())
                self.assertIn("tools/build_app.py", source.namelist())
                self.assertFalse(any(".venv" in name or name.startswith("build/") for name in source.namelist()))
            self.assertEqual(json.loads((notices / "build-info.json").read_text())["version"], "1.0.0")
            executable = folder / "Lil Buddy.exe"
            executable.write_bytes(b"test executable")
            output = folder / "bundle.zip"
            write_windows_zip(executable, output, ROOT / "LICENSE", notices)
            with zipfile.ZipFile(output) as archive:
                self.assertIn("Licenses/Application-Source.zip", archive.namelist())
                self.assertIn("Licenses/THIRD_PARTY_NOTICES.md", archive.namelist())
                self.assertEqual(archive.read("Licenses/build-info.json"), (notices / "build-info.json").read_bytes())
            spec = render_spec("darwin", ROOT, self.icons, notices)
            self.assertIn(repr((str(notices), "Licenses")), spec)


if __name__ == "__main__":
    unittest.main()

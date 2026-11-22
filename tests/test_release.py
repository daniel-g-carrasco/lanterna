#!/usr/bin/env python3
"""What a release needs to be right, checked before the tag: the version
in the code, the changelog and the metainfo agree; the texts shown to the
user and the documents have no em dash; the identity constants agree with
the files named after them."""

import re
import unittest

import _setup

import lanterna

ROOT = _setup.ROOT
EM_DASH = chr(0x2014)       # (never written here as itself: the rule applies to this file too)


class VersionTest(unittest.TestCase):
    def test_the_changelog_and_the_metainfo_know_this_version(self):
        version = lanterna.__version__
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{version}] - ", changelog, "CHANGELOG.md has no entry for this version")
        metainfo = (ROOT / "data" / f"{lanterna.APP_ID}.metainfo.xml").read_text(encoding="utf-8")
        self.assertIn(f'<release version="{version}" date="', metainfo, "the metainfo has no release for this version")
        # the newest release first, in both
        self.assertEqual(re.search(r"^## \[(\d[^\]]*)\]", changelog, re.M).group(1), version)
        self.assertEqual(re.search(r'<release version="([^"]+)"', metainfo).group(1), version)


class IdentityTest(unittest.TestCase):
    def test_the_files_are_named_after_the_app_id(self):
        for name in (f"{lanterna.APP_ID}.desktop", f"{lanterna.APP_ID}.metainfo.xml"):
            self.assertTrue((ROOT / "data" / name).is_file(), name)
        self.assertTrue((ROOT / "data" / "icons" / "hicolor" / "scalable" / "apps" / f"{lanterna.APP_ID}.svg").is_file())
        self.assertTrue((ROOT / "build-aux" / "flatpak" / f"{lanterna.APP_ID}.yaml").is_file())
        self.assertTrue((ROOT / "build-aux" / "windows" / f"{lanterna.SLUG}.spec").is_file())
        self.assertTrue((ROOT / "build-aux" / "windows" / f"{lanterna.SLUG}.iss").is_file())
        self.assertTrue((ROOT / "bin" / lanterna.SLUG).is_file())
        desktop = (ROOT / "data" / f"{lanterna.APP_ID}.desktop").read_text(encoding="utf-8")
        self.assertIn(f"Exec={lanterna.SLUG}", desktop)
        self.assertIn(f"Icon={lanterna.APP_ID}\n", desktop)
        iss = (ROOT / "build-aux" / "windows" / f"{lanterna.SLUG}.iss").read_text(encoding="utf-8")
        self.assertIn(f"{lanterna.EXE_NAME}.exe", iss)
        self.assertIn(f"OutputBaseFilename={lanterna.SLUG}-setup-v", iss)

    def test_the_identity_is_derived_not_repeated(self):
        self.assertEqual(lanterna.TEXTDOMAIN, lanterna.SLUG)
        self.assertEqual(lanterna.ENV_PREFIX, lanterna.SLUG.upper().replace("-", "_"))
        self.assertTrue(lanterna.APP_ID.endswith("." + lanterna.SLUG))
        self.assertNotIn(" ", lanterna.EXE_NAME)


class IconsTest(unittest.TestCase):
    def test_the_svg_files_are_recognised_on_linux_too(self):
        # GdkPixbuf on Linux tells an SVG by "<svg" within its first 256 bytes,
        # not by the name; on Windows the name is enough, so a comment before
        # the root breaks make_icons.py and make_ico.py on Linux only
        svgs = [path for folder in ("data", "build-aux", "site") for path in (ROOT / folder).rglob("*.svg")]
        self.assertTrue(svgs)
        for path in svgs:
            self.assertIn(b"<svg", path.read_bytes()[:256], f"{path.name}: the comments go inside <svg>")


class TextsTest(unittest.TestCase):
    def test_no_em_dash_anywhere_the_user_reads(self):
        # a rule of the house: a colon, a comma or a full stop instead
        offenders = []
        for folder, pattern in (("src", "*.py"), ("po", "*.po"), ("data", "*.xml"), ("data", "*.desktop"),
                                ("docs", "*.md"), (".", "*.md")):
            for path in (ROOT / folder).glob(pattern):
                if EM_DASH in path.read_text(encoding="utf-8"):
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

"""Lanterna: photos, slides, videos and films from this computer to a TV.

The package is split so that everything deciding what happens is plain
Python without GTK, covered by the tests in tests/;
the GTK side (app, window, dialogs) only displays and triggers. The
constants below are the one place the app's identity lives: the code
derives every name (the executable, the installer, the text domain, the
environment variables) from them.
"""

import sys

__version__ = "0.3.0"

WINDOWS = sys.platform == "win32"

APP_ID = "io.github.daniel_g_carrasco.lanterna"
APP_NAME = "Lanterna"
SLUG = "lanterna"                 # the repository, the binary on Linux, the file names of a release
EXE_NAME = "Lanterna"              # the Windows executable and its folder (no spaces, as Setup likes it)
TEXTDOMAIN = SLUG
ENV_PREFIX = "LANTERNA"           # the developer aids: LANTERNA_SCREENSHOT and the like
GITHUB_REPO = "daniel-g-carrasco/lanterna"
WEBSITE = f"https://github.com/{GITHUB_REPO}"

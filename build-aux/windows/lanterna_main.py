"""Entry point of the Windows build (PyInstaller freezes this file)."""

import sys

from lanterna.__main__ import main

sys.exit(main())

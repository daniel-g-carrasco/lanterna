"""Unexpected failures said out loud. The app has no log of its own: what
goes wrong in a thread or in the main loop must at least reach stderr (the
journal, when it runs as a service; the console, from the sources), with
its traceback, instead of vanishing with the thread or the idle source that
raised it. Never `except Exception: pass`: a failure nobody hears of is a
bug nobody can fix."""

from __future__ import annotations

import sys
import traceback

from . import SLUG


def complain(where: str, error: BaseException) -> None:
    print(f"{SLUG}: {where}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    traceback.print_exception(error, file=sys.stderr)

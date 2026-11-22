"""What a report of a problem needs, in one text: the app's version and
paths, the toolkit's versions, the settings, the room its files take. The
About window shows it under Troubleshooting, with a Save button. Nothing
in it is a secret: what a user pastes in an issue must be safe to paste."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from . import APP_NAME, WINDOWS, __version__
from .paths import Paths, in_flatpak


def human(size: int | None) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{size} B"


def tree_size(path: Path) -> tuple[int, int]:
    """(files, bytes) under `path`, links not followed."""
    files = size = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            files += 1
            size += st.st_size
    return files, size


def toolkit_versions() -> str:
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, GLib, Gtk
        return (f"GLib {GLib.MAJOR_VERSION}.{GLib.MINOR_VERSION}.{GLib.MICRO_VERSION}, "
                f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}, "
                f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}")
    except Exception:   # the command line, or a GTK too old to say
        return "GTK not loaded"


def summary(cfg, paths: Paths) -> str:
    """The quick part, read on the main loop: versions, settings, paths."""
    where = "Flatpak" if in_flatpak() else ("Windows" if WINDOWS else "host")
    if not in_flatpak():        # the Flatpak runs the package with the runtime's Python: neither
        where += ", frozen" if getattr(sys, "frozen", False) else ", from the sources"
    lines = [
        f"{APP_NAME} {__version__} ({where}), Python {platform.python_version()}, {platform.platform()}",
        toolkit_versions(),
        f"settings: {paths.config_file}",
        f"state: {paths.state}",
        f"data: {paths.data}",
        f"preferences: theme {cfg.theme!r}, language {cfg.language!r}, check updates {cfg.check_updates}",
    ]
    if cfg.notices:
        lines.append("settings file: " + "; ".join(f"{n.kind} {n.detail} {n.reason}".strip() for n in cfg.notices))
    return "\n".join(lines)


def storage(paths: Paths) -> str:
    """The slow part, for a thread: the room taken by the app's folders."""
    lines = []
    for label, folder in (("state", paths.state), ("data", paths.data)):
        if folder.is_dir():
            files, size = tree_size(folder)
            lines.append(f"{label}: {files} files, {human(size)}")
        else:
            lines.append(f"{label}: none")
    lines.append(f"python: {sys.executable}")
    return "\n".join(lines)

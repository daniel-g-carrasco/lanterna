"""What the window and the dialogs share: threads that report back on
the main loop, markup escaping, a file shown in the file manager."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .paths import nearest_folder  # noqa: E402


def run_in_thread(work: Callable, done: Callable) -> None:
    """work() in a thread, done(result, error) back on the main loop. The
    window never does the disk's or the network's work itself: a file of
    gigabytes or a server that does not answer would stop it."""
    def target():
        try:
            result, error = work(), None
        except Exception as e:  # shown to the user, never swallowed
            result, error = None, e
        GLib.idle_add(lambda: done(result, error) and False)
    threading.Thread(target=target, daemon=True).start()


def escape(text: str) -> str:
    """For every libadwaita title and subtitle: they read Pango markup, and
    a file named "<b>" or "a & b" would break them."""
    return GLib.markup_escape_text(text)


def reveal(window, path: str) -> None:
    """Show the file in the file manager, selected (the OpenURI portal's
    OpenDirectory inside the sandbox); a file that is not there any more
    opens the nearest folder that is."""
    if os.path.lexists(path):
        Gtk.FileLauncher.new(Gio.File.new_for_path(path)).open_containing_folder(window, None, None, None)
        return
    Gtk.FileLauncher.new(Gio.File.new_for_path(nearest_folder(path))).launch(window, None, None, None)


def open_path(window, path: str) -> None:
    """The file or folder with the default app."""
    Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(window, None, None, None)

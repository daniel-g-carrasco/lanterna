"""The computer stays awake while something is on the TV: the TV reads from
it, so a laptop that suspends in the middle of a film ends the film.

Asked of logind directly, on the system bus: GTK's own inhibit goes to a
session manager, and niri has none. The block is a file descriptor, and it
lasts until it is closed, or until this process ends, which is the right
moment for it too. During a file the screen may go dark ("sleep"); while the
screen is what is being shown, it is what the idle block ("sleep:idle") is for.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from . import APP_NAME

LOGIN1 = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
FILE, SCREEN = "sleep", "sleep:idle"


class SleepBlock:
    """hold() and release() on the main loop; asking is asynchronous, so the window does not wait for logind."""

    def __init__(self, on_error: Callable[[str], None] | None = None):
        self.on_error = on_error
        self.what = ""              # what is wanted now
        self._held = ""             # what the descriptor below blocks
        self._fd = -1
        self._asking = False

    def hold(self, what: str, reason: str) -> None:
        self.what = what
        if what != self._held and not self._asking:
            self._ask(what, reason)

    def release(self) -> None:
        self.what = ""
        self._drop()

    def _drop(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
        self._fd, self._held = -1, ""

    def _ask(self, what: str, reason: str) -> None:
        from gi.repository import Gio, GLib
        self._asking = True
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as e:
            self._asking = False
            self._fail(e.message)
            return

        def answered(bus, result):
            self._asking = False
            try:
                reply, fds = bus.call_with_unix_fd_list_finish(result)
            except GLib.Error as e:
                self._fail(e.message)
                return
            fd = fds.get(reply.unpack()[0])
            if self.what != what:       # it changed while logind was answering
                os.close(fd)
                if self.what:
                    self._ask(self.what, reason)
                return
            self._drop()
            self._fd, self._held = fd, what
        bus.call_with_unix_fd_list(LOGIN1, LOGIN1_PATH, f"{LOGIN1}.Manager", "Inhibit",
                                   GLib.Variant("(ssss)", (what, APP_NAME, reason, "block")), None,
                                   Gio.DBusCallFlags.NONE, 5000, None, None, answered)

    def _fail(self, message: str) -> None:
        if self.on_error:
            self.on_error(message)

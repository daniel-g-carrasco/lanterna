"""This computer stays quiet while its sound plays on the TV.

The TV plays the sound about three seconds late, so the two together are an
echo. Muting the output does not touch what the TV gets: the monitor PipeWire
offers is taken before the mute, a tone played on a muted output read -26 dB
on it [V 03/11/2026]. Through pactl, which the GNOME runtime has and which
reaches the sound server over the socket the app has for capturing anyway.

From a Flatpak WirePlumber lets a client look and capture but not change
anything: "Access denied" [V 03/11/2026, it does not show from the sources]. A
client that says it is a mixer may, by the rule in find-flatpak-access.lua, so
this one pactl says so and the rest of the app stays as it is.

Only an output this app has muted is unmuted again: a computer that was
already silent stays so. The name of the output is kept in the settings until
it is restored, so that a crash in the middle does not leave the computer mute.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable

TIMEOUT_S = 3
MIXER = "media.category=Manager"


def pactl(*args: str) -> str:
    """OSError or subprocess.SubprocessError if the sound server cannot be asked."""
    done = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=TIMEOUT_S, check=True,
                          env={**os.environ, "PULSE_PROP": MIXER})
    return done.stdout.strip()


class Silence:
    """silence() and restore() block on pactl: from a thread, in any order, any number of times."""

    def __init__(self, run: Callable[..., str] = pactl):
        self._run = run
        self._lock = threading.Lock()
        self.muted = ""                 # the output this app has muted

    def silence(self) -> str:
        """The name of the output muted now, "" if there was nothing to do."""
        with self._lock:
            if self.muted:
                return self.muted
            output = self._run("get-default-sink")
            if "yes" in self._run("get-sink-mute", output).lower():
                return ""
            self._run("set-sink-mute", output, "1")
            self.muted = output
            return output

    def restore(self) -> None:
        with self._lock:
            output, self.muted = self.muted, ""
            if output:
                self._run("set-sink-mute", output, "0")

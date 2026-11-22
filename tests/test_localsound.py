#!/usr/bin/env python3
"""The computer's own output: muted once, given back only if this app muted it. A fake pactl: the real output is the user's."""

import os
import stat
import tempfile
import unittest
from unittest import mock

import _setup  # noqa: F401

from lanterna.localsound import Silence, pactl


class FakePactl:
    def __init__(self, muted=False):
        self.is_muted = muted
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "get-default-sink":
            return "speakers"
        if args[0] == "get-sink-mute":
            return "Mute: yes" if self.is_muted else "Mute: no"
        self.is_muted = args[2] == "1"
        return ""


class SilenceTest(unittest.TestCase):
    def test_it_mutes_and_gives_the_sound_back(self):
        pactl = FakePactl()
        silence = Silence(pactl)
        self.assertEqual(silence.silence(), "speakers")
        self.assertTrue(pactl.is_muted)
        silence.restore()
        self.assertFalse(pactl.is_muted)

    def test_a_computer_already_silent_stays_silent(self):
        pactl = FakePactl(muted=True)
        silence = Silence(pactl)
        self.assertEqual(silence.silence(), "")
        silence.restore()
        self.assertTrue(pactl.is_muted)

    def test_asking_twice_changes_nothing(self):
        pactl = FakePactl()
        silence = Silence(pactl)
        silence.silence()
        silence.silence()
        self.assertEqual(sum(c[0] == "set-sink-mute" for c in pactl.calls), 1)
        silence.restore()
        silence.restore()
        self.assertEqual(sum(c[0] == "set-sink-mute" for c in pactl.calls), 2)


class PactlTest(unittest.TestCase):
    def test_it_says_it_is_a_mixer_or_a_flatpak_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            program = os.path.join(folder, "pactl")
            with open(program, "w", encoding="utf-8") as out:
                out.write('#!/bin/sh\necho "$PULSE_PROP $*"\n')
            os.chmod(program, os.stat(program).st_mode | stat.S_IXUSR)
            with mock.patch.dict(os.environ, {"PATH": f"{folder}:{os.environ['PATH']}"}):
                self.assertEqual(pactl("get-default-sink"), "media.category=Manager get-default-sink")


if __name__ == "__main__":
    unittest.main()

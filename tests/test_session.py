#!/usr/bin/env python3
"""The session against the fake TV and the real media server: a film as it
is, the next way when the TV refuses one, a refusal said in words, seeking
in a file and in a live stream, photos with next and previous, stop, and
a TV that goes away. The file reading and the live producer are stand-ins,
so these tests need neither GStreamer nor real films."""

import contextlib
import io
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import _setup  # noqa: F401
from _fake_tv import FakeTV

from lanterna import media, server, transcode
from lanterna.castv2 import Channel
from lanterna.devices import Device
from lanterna.media import MediaInfo
from lanterna.session import Cancelled, Session, SessionError, State, clock

INFOS = {
    "film.mp4": MediaInfo("mp4", "h264", "high", ("aac",), duration_s=5400.0, width=1920, height=1080),
    "film.mkv": MediaInfo("matroska", "h264", "high", ("ac3",), duration_s=7200.0, width=1920, height=800),
    "a.jpg": MediaInfo("jpeg", image=True, width=4000, height=3000),
    "b.jpg": MediaInfo("jpeg", image=True, width=4000, height=3000),
    "notes.txt": None,
}


def fake_probe(path: str) -> MediaInfo:
    info = INFOS[Path(path).name]
    if info is None:
        raise media.ProbeError("Could not determine type of stream.")
    return info


def fake_producer(path, way, info, out, start_s=0.0, job=None):
    out.write(f"{way.value} from {start_s}".encode())


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        for name in INFOS:
            (self.dir / name).write_bytes(b"x" * 1000)
        self.tvs: list[FakeTV] = []
        self.tv_options = {}
        self.states: list[State] = []
        self.server = server.MediaServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.device = Device("a1b2", "Living Room TV", "Philips", "127.0.0.1", 8009)

    def connect(self, _device):
        ours, theirs = socket.socketpair()
        tv = FakeTV(theirs, **self.tv_options)
        tv.start()
        self.tvs.append(tv)
        self.addCleanup(theirs.close)
        return Channel(ours)

    def session(self) -> Session:
        s = Session(self.device, self.states.append, connect=self.connect, media_server=self.server,
                    probe=fake_probe, producer=fake_producer)
        self.addCleanup(s.close)
        return s

    def path(self, name):
        return str(self.dir / name)


class SessionTest(SessionCase):
    def test_a_film_goes_as_it_is_and_plays(self):
        s = self.session()
        s.show([self.path("film.mp4")])
        st = s.state
        self.assertEqual((st.state, st.kind, st.way, st.seekable, st.duration_s), ("playing", "file", "direct", True, 5400.0))
        self.assertEqual(st.device_name, "Living Room TV")
        load = self.tvs[-1].loads[-1]
        self.assertEqual((load["contentType"], load["streamType"]), ("video/mp4", "BUFFERED"))
        with urllib.request.urlopen(load["contentUrl"], timeout=5) as r:
            self.assertEqual(r.read(), b"x" * 1000)

    def test_when_the_tv_refuses_a_way_the_next_one_is_tried(self):
        self.tv_options = {"refuse_types": {"video/mp4"}}
        s = self.session()
        s.show([self.path("film.mp4")])
        self.assertEqual((s.state.state, s.state.way), ("playing", "audio"))
        loads = self.tvs[-1].loads
        self.assertEqual([lo["contentType"] for lo in loads], ["video/mp4", "video/x-matroska"])
        self.assertEqual(loads[-1]["streamType"], "LIVE")
        with urllib.request.urlopen(loads[-1]["contentUrl"], timeout=5) as r:
            self.assertEqual(r.read(), b"audio from 0.0")

    def test_a_film_with_ac3_says_why_it_is_converted(self):
        s = self.session()
        s.show([self.path("film.mkv")])
        self.assertEqual(s.state.way, "audio")
        self.assertIn("sound", s.state.note)

    def test_a_refusal_of_every_way_is_said_in_words(self):
        self.tv_options = {"refuse_load": True}
        s = self.session()
        with self.assertRaises(SessionError):
            s.show([self.path("film.mp4")])
        self.assertEqual(s.state.state, "error")
        self.assertIn("refused", s.state.error)

    def test_a_file_that_is_not_media_is_an_error(self):
        s = self.session()
        with self.assertRaises(SessionError):
            s.show([self.path("notes.txt")])
        self.assertIn("notes.txt", s.state.error)

    def test_seeking_in_a_file_asks_the_tv(self):
        s = self.session()
        s.show([self.path("film.mp4")])
        s.seek(600)
        seek = next(m for m in reversed(self.tvs[-1].seen) if m.payload.get("type") == "SEEK")
        self.assertEqual(seek.payload["currentTime"], 600)

    def test_seeking_in_a_live_stream_starts_a_new_one_there(self):
        s = self.session()
        s.show([self.path("film.mkv")])
        first = self.tvs[-1].loads[-1]["contentUrl"]
        s.seek(600)
        second = self.tvs[-1].loads[-1]["contentUrl"]
        self.assertNotEqual(first, second)
        with urllib.request.urlopen(second, timeout=5) as r:
            self.assertEqual(r.read(), b"audio from 600")
        self.assertAlmostEqual(s.state.position_s, 600, delta=1)

    def test_the_position_of_a_live_stream_counts_from_its_start(self):
        st = State(state="playing", position_s=600.0, at=1000.0, rate=1.0, duration_s=7200.0)
        self.assertEqual(st.position_now(1010.0), 610.0)
        self.assertEqual(State(position_s=600.0, at=1000.0, rate=0.0).position_now(2000.0), 600.0)
        self.assertEqual(replace_end(st).position_now(10**9), 7200.0)

    def test_photos_with_next_and_previous(self):
        s = self.session()
        s.show([self.path("a.jpg"), self.path("b.jpg")])
        self.assertEqual((s.state.state, s.state.kind, s.state.index, s.state.count), ("playing", "photo", 0, 2))
        self.assertEqual(s.state.rate, 0.0)
        s.previous()                                    # already the first: nothing happens
        self.assertEqual(s.state.index, 0)
        s.next()
        self.assertEqual((s.state.index, s.state.title), (1, "b.jpg"))
        self.assertEqual(self.tvs[-1].loads[-1]["contentType"], "image/jpeg")
        s.next()                                        # already the last
        self.assertEqual(s.state.index, 1)

    def test_stop_closes_the_player_and_the_url(self):
        s = self.session()
        s.show([self.path("film.mp4")])
        url = self.tvs[-1].loads[-1]["contentUrl"]
        s.stop()
        self.assertEqual(s.state.state, "idle")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()
        self.assertTrue(any(m.payload.get("type") == "STOP" for m in self.tvs[-1].seen))

    def test_a_tv_that_goes_away_is_an_error_naming_it(self):
        s = self.session()
        s.show([self.path("film.mp4")])
        self.tvs[-1].channel.sock.shutdown(socket.SHUT_RDWR)
        deadline = time.monotonic() + 5
        while s.state.state != "error" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(s.state.state, "error")
        self.assertIn("Living Room TV", s.state.error)

    def test_commands_with_nothing_on_are_refused(self):
        s = self.session()
        with self.assertRaises(SessionError):
            s.pause()
        with self.assertRaises(SessionError):
            s.seek(10)


class StoppingWhileLoadingTest(SessionCase):
    """The user does not wait for the TV: a stop, or another file, wins."""

    def show_in_thread(self, s, *paths):
        outcome = []

        def run():
            try:
                s.show(list(paths))
                outcome.append("shown")
            except SessionError as e:
                outcome.append(e)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not (self.tvs and self.tvs[-1].loads) and time.monotonic() < deadline:
            time.sleep(0.01)
        return thread, outcome

    def test_a_stop_while_the_tv_buffers_tries_no_other_way(self):
        self.tv_options = {"stalls": True}
        s = self.session()
        thread, outcome = self.show_in_thread(s, self.path("film.mp4"))
        s.stop()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome[0], Cancelled)
        self.assertEqual(len(self.tvs[-1].loads), 1)
        self.assertEqual((s.state.state, s.state.error), ("idle", ""))

    def test_another_file_while_the_first_loads_takes_over(self):
        self.tv_options = {"stalls": True}
        s = self.session()
        thread, outcome = self.show_in_thread(s, self.path("film.mp4"))
        self.tvs[-1].stalls = False
        s.show([self.path("a.jpg")])
        thread.join(5)
        self.assertIsInstance(outcome[0], Cancelled)
        self.assertEqual((s.state.state, s.state.title), ("playing", "a.jpg"))

    def launching_then_stopped(self, s):
        """A first LAUNCH takes the real TV 4 s: stopped in the middle of it."""
        thread = threading.Thread(target=lambda: self.quiet_show(s, "film.mp4"), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not self.tvs and time.monotonic() < deadline:
            time.sleep(0.01)
        s.stop()
        return thread

    def quiet_show(self, s, name):
        try:
            s.show([self.path(name)])
        except Cancelled:
            pass  # what these tests do to it

    def test_a_stop_during_launch_closes_the_player_it_opened(self):
        self.tv_options = {"launch_delay": 0.3}
        s = self.session()
        self.launching_then_stopped(s).join(5)
        self.assertTrue(any(m.payload.get("type") == "STOP" for m in self.tvs[0].seen))
        self.assertEqual(s.state.state, "idle")

    def test_a_file_chosen_during_that_launch_keeps_the_player(self):
        # the old thread, when its LAUNCH came back, closed the player the new file was loading on: on the real
        # TV the LOAD then had no answer [V 03/11/2026]
        self.tv_options = {"launch_delay": 0.3}
        s = self.session()
        thread = self.launching_then_stopped(s)
        s.show([self.path("a.jpg")])
        thread.join(5)
        self.assertFalse(any(m.payload.get("type") == "STOP" for m in self.tvs[0].seen))
        self.assertEqual((s.state.state, s.state.title), ("playing", "a.jpg"))
        self.assertEqual(len(self.tvs), 1)      # the same connection: the receiver quits when its last sender leaves

    def test_a_player_closed_by_the_tv_during_a_load_is_said_at_once(self):
        self.tv_options = {"closes_on_load": True}
        s = self.session()
        started = time.monotonic()
        with self.assertRaises(SessionError):
            s.show([self.path("a.jpg")])
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(s.state.state, "error")

    def test_what_stop_says_arrives_before_the_tv_answers(self):
        self.tv_options = {"silent_on_stop": True}
        s = self.session()
        s.show([self.path("film.mp4")])
        heard = threading.Event()
        s.on_state = lambda st: heard.set() if st.state == "idle" else None
        thread = threading.Thread(target=s.stop, daemon=True)
        thread.start()
        self.assertTrue(heard.wait(2))


class LiveTest(SessionCase):
    """Something with no file behind it: the screen."""

    def live(self, s):
        ended = threading.Event()
        s.show_live("screen", "The Screen", lambda out, job: out.write(b"frames"), ended.set)
        return ended

    def test_it_goes_on_the_tv_as_a_live_stream_and_cannot_be_sought(self):
        s = self.session()
        self.live(s)
        st = s.state
        self.assertEqual((st.state, st.kind, st.way, st.seekable, st.duration_s), ("playing", "screen", "screen", False, None))
        load = self.tvs[-1].loads[-1]
        self.assertEqual(load["streamType"], "LIVE")
        with urllib.request.urlopen(load["contentUrl"], timeout=5) as r:
            self.assertEqual(r.read(), b"frames")
        with self.assertRaises(SessionError):
            s.seek(10)

    def test_stop_lets_the_source_go(self):
        s = self.session()
        ended = self.live(s)
        self.assertFalse(ended.is_set())
        s.stop()
        self.assertTrue(ended.is_set())
        self.assertEqual(s.state.state, "idle")

    def test_a_file_after_the_screen_lets_the_source_go(self):
        s = self.session()
        ended = self.live(s)
        s.show([self.path("film.mp4")])
        self.assertTrue(ended.is_set())
        self.assertEqual((s.state.kind, s.state.way), ("file", "direct"))

    def test_a_refusal_lets_the_source_go_and_is_said(self):
        self.tv_options = {"refuse_load": True}
        s = self.session()
        ended = threading.Event()
        with self.assertRaises(SessionError):
            s.show_live("screen", "The Screen", lambda out, job: None, ended.set)
        self.assertTrue(ended.is_set())
        self.assertEqual(s.state.state, "error")

    def test_a_tv_that_goes_away_lets_the_source_go(self):
        s = self.session()
        ended = self.live(s)
        self.tvs[-1].channel.sock.shutdown(socket.SHUT_RDWR)
        self.assertTrue(ended.wait(5))


class SeekingLiveTest(SessionCase):
    """A new stream at the new point: the TV announces the end of the old one, and the film is not over."""

    def seek_and_listen(self, when):
        self.tv_options = {"old_media_ends": when}
        s = self.session()
        s.show([self.path("film.mkv")])
        self.assertEqual(s.state.way, "audio")
        self.states.clear()
        s.seek(600)
        time.sleep(0.2)                 # what comes after the answer has time to arrive
        return s

    def test_the_old_stream_ending_during_the_load_changes_nothing(self):
        s = self.seek_and_listen("before")
        self.assertNotIn("idle", [st.state for st in self.states])
        self.assertEqual(s.state.state, "playing")
        self.assertAlmostEqual(s.state.position_s, 600, delta=1)

    def test_the_old_stream_ending_after_the_load_changes_nothing(self):
        s = self.seek_and_listen("after")
        self.assertNotIn("idle", [st.state for st in self.states])
        self.assertEqual(s.state.state, "playing")

    def test_the_end_of_what_is_on_is_still_heard(self):
        s = self.seek_and_listen("after")
        tv = self.tvs[-1]
        last = next(m for m in reversed(tv.seen) if m.payload.get("type") == "LOAD")
        tv.media_ended(last, tv.session)
        deadline = time.monotonic() + 3
        while s.state.state != "idle" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(s.state.state, "idle")


class StreamFailureTest(SessionCase):
    """What stops a stream is said in the state, not lost in a thread of the server."""

    def quietly(self, url):
        with contextlib.redirect_stderr(io.StringIO()) as said:
            with urllib.request.urlopen(url, timeout=5) as r:
                r.read()
        return said.getvalue()

    def test_a_live_stream_that_breaks_while_on_is_an_error_and_lets_the_source_go(self):
        def producer(_out, _job):
            raise transcode.TranscodeError("no more input formats")
        s = self.session()
        ended = threading.Event()
        s.show_live("screen", "The Screen", producer, ended.set)
        said = self.quietly(self.tvs[-1].loads[-1]["contentUrl"])
        deadline = time.monotonic() + 5
        while s.state.state != "error" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(s.state.state, "error")
        self.assertIn("no more input formats", s.state.error)
        self.assertIn("no more input formats", said)
        self.assertTrue(ended.is_set())

    def test_a_seek_the_tv_refuses_is_an_error_and_does_not_stay_loading(self):
        s = self.session()
        s.show([self.path("film.mkv")])
        self.assertEqual(s.state.way, "audio")
        self.tvs[-1].refuse_types.add("video/x-matroska")
        with self.assertRaises(SessionError):
            s.seek(100)
        self.assertEqual(s.state.state, "error")
        self.assertTrue(s.state.error)

    def test_an_old_stream_failing_after_a_new_one_started_says_nothing(self):
        s = self.session()
        s.show([self.path("film.mkv")])
        s._stream_failed(s._generation - 1, "old")
        self.assertEqual((s.state.state, s.state.error), ("playing", ""))


class ClockTest(unittest.TestCase):
    def test_a_position_reads_as_on_a_player(self):
        self.assertEqual(clock(0), "0:00")
        self.assertEqual(clock(125.9), "2:05")
        self.assertEqual(clock(3725), "1:02:05")
        self.assertEqual(clock(-3), "0:00")


def replace_end(st: State) -> State:
    from dataclasses import replace
    return replace(st, position_s=7190.0)


if __name__ == "__main__":
    unittest.main()

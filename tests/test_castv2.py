#!/usr/bin/env python3
"""Cast v2: messages framed and read back as the TV writes them, and the
conversation with a fake TV on the other end of a socketpair: launch,
load, control, the heartbeat answered, a TV that goes away noticed."""

import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import unittest

import _setup  # noqa: F401

from lanterna import castv2
from lanterna.castv2 import Channel, Message, Receiver
from _fake_tv import FakeTV


class FramingTest(unittest.TestCase):
    def test_a_message_reads_back_as_written(self):
        message = Message("sender-1", "receiver-0", castv2.NS_RECEIVER, {"type": "LAUNCH", "appId": "CC1AD845"})
        framed = castv2.encode(message)
        (length,) = struct.unpack(">I", framed[:4])
        self.assertEqual(length, len(framed) - 4)
        self.assertEqual(castv2.decode(framed[4:]), message)

    def test_unicode_and_long_payloads_survive(self):
        message = Message("s", "d", castv2.NS_MEDIA, {"title": "Città è più bella " * 40})
        self.assertEqual(castv2.decode(castv2.encode(message)[4:]), message)

    def test_unknown_fields_are_skipped(self):
        body = castv2.encode(Message("s", "d", "ns", {"a": 1}))[4:]
        extra = bytes([0x38]) + castv2._varint(300)        # field 7 as a varint, which we do not use
        self.assertEqual(castv2.decode(body + extra).payload, {"a": 1})

    def test_a_truncated_message_is_an_error_not_a_crash(self):
        body = castv2.encode(Message("s", "d", "ns", {"a": 1}))[4:]
        with self.assertRaises(ValueError):
            castv2.decode(body[:-3])

    def test_an_oversized_frame_is_refused(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.sendall(struct.pack(">I", castv2.MAX_MESSAGE + 1))
        with self.assertRaises(castv2.CastError):
            Channel(b).recv()


def wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)
    return cond()


class ReceiverTest(unittest.TestCase):
    def connect(self, **tv_options):
        ours, theirs = socket.socketpair()
        tv = FakeTV(theirs, **tv_options)
        tv.start()
        events = {"media": [], "volume": [], "closed": []}
        receiver = Receiver(Channel(ours), on_media=events["media"].append,
                            on_volume=lambda level, muted: events["volume"].append((level, muted)),
                            on_closed=events["closed"].append)
        receiver.start()
        self.addCleanup(receiver.close)
        self.addCleanup(theirs.close)
        return receiver, tv, events

    def test_launch_waits_for_the_app_after_user_allowed(self):
        receiver, tv, _ = self.connect()
        self.assertEqual(receiver.launch(), "web-7")
        kinds = [m.payload.get("type") for m in tv.seen]
        self.assertEqual(kinds[:2], ["CONNECT", "LAUNCH"])
        self.assertIn("GET_STATUS", kinds)
        connect = Message("sender-lanterna", "web-7", castv2.NS_CONNECTION, {"type": "CONNECT"})
        self.assertTrue(wait_for(lambda: connect in tv.seen))

    def test_load_play_pause_seek_follow_the_media_status(self):
        receiver, tv, events = self.connect()
        receiver.launch()
        status = receiver.load("http://192.168.1.10:1234/x/film.mp4", "video/mp4", title="Film")
        self.assertEqual((status.state, status.position_s, status.duration_s), ("PLAYING", 12.5, 5400.0))
        load = next(m for m in tv.seen if m.payload.get("type") == "LOAD")
        self.assertEqual(load.payload["media"]["streamType"], "BUFFERED")
        self.assertEqual(load.payload["media"]["metadata"]["title"], "Film")
        receiver.pause()
        self.assertEqual(receiver.media.state, "PAUSED")
        receiver.seek(600)
        seek = next(m for m in reversed(tv.seen) if m.payload.get("type") == "SEEK")
        self.assertEqual((seek.payload["currentTime"], seek.payload["mediaSessionId"]), (600, 1))
        receiver.play()
        self.assertEqual(events["media"][-1].state, "PLAYING")

    def test_a_live_stream_is_loaded_as_live(self):
        receiver, tv, _ = self.connect()
        receiver.launch()
        receiver.load("http://h/screen.mkv", "video/x-matroska", live=True)
        load = next(m for m in tv.seen if m.payload.get("type") == "LOAD")
        self.assertEqual(load.payload["media"]["streamType"], "LIVE")

    def test_a_refused_load_says_so(self):
        receiver, _, _ = self.connect(refuse_load=True)
        receiver.launch()
        with self.assertRaisesRegex(castv2.CastError, "refused"):
            receiver.load("http://h/x.avi", "video/x-msvideo")

    def test_commands_before_anything_plays_are_refused_here(self):
        receiver, _, _ = self.connect()
        receiver.launch()
        with self.assertRaisesRegex(castv2.CastError, "nothing is playing"):
            receiver.pause()

    def test_volume_is_clamped_and_reported(self):
        receiver, tv, events = self.connect()
        receiver.set_volume(1.7)
        self.assertEqual(tv.volume, 1.0)
        self.assertEqual(events["volume"][-1], (1.0, False))

    def test_the_tv_ping_is_answered(self):
        receiver, tv, _ = self.connect()
        tv.channel.send(Message("receiver-0", "sender-lanterna", castv2.NS_HEARTBEAT, {"type": "PING"}))
        self.assertTrue(wait_for(lambda: any(m.payload.get("type") == "PONG" for m in tv.seen)))

    def test_a_tv_that_does_not_answer_times_out_with_words(self):
        receiver, _, _ = self.connect(silent=True)
        with self.assertRaisesRegex(castv2.CastError, "did not answer"):
            receiver.request(castv2.PLATFORM, castv2.NS_RECEIVER, {"type": "GET_STATUS"}, timeout=0.3)

    def test_a_tv_that_goes_away_is_reported_once_and_wakes_the_waiters(self):
        receiver, tv, events = self.connect(silent=True)
        errors = []

        def ask():
            try:
                receiver.request(castv2.PLATFORM, castv2.NS_RECEIVER, {"type": "GET_STATUS"}, timeout=5)
            except castv2.CastError as e:
                errors.append(str(e))
        asker = threading.Thread(target=ask)
        asker.start()
        time.sleep(0.2)
        tv.channel.sock.shutdown(socket.SHUT_RDWR)     # close() alone does not wake a reader on Linux
        asker.join(5)
        self.assertEqual(len(errors), 1)
        self.assertIn("connection ended", errors[0])
        self.assertTrue(wait_for(lambda: events["closed"]))
        self.assertEqual(len(events["closed"]), 1)

    def test_close_does_not_report_itself_as_a_loss(self):
        receiver, _, events = self.connect()
        receiver.close()
        time.sleep(0.2)
        self.assertEqual(events["closed"], [])


@unittest.skipUnless(shutil.which("openssl"), "openssl makes the test's certificate")
class ConcurrentTlsTest(unittest.TestCase):
    """The reader waits on the socket while the heartbeat and the commands
    write: on one OpenSSL object at the same moment that broke the connection
    with RECORD_LAYER_FAILURE, on the real TV, during a pause [V 03/11/2026]."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.key, self.cert = f"{self.dir.name}/key.pem", f"{self.dir.name}/cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", self.key, "-out",
                        self.cert, "-days", "1", "-subj", "/CN=fake-tv"], check=True, capture_output=True)

    def pair(self):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        accepted = []

        def serve():
            raw, _addr = listener.accept()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.cert, self.key)
            accepted.append(Channel(context.wrap_socket(raw, server_side=True)))
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        client = Channel.connect("127.0.0.1", listener.getsockname()[1])
        thread.join(5)
        self.addCleanup(client.close)
        self.addCleanup(accepted[0].close)
        return client, accepted[0]

    def test_writing_from_many_threads_while_reading_keeps_the_connection(self):
        client, server = self.pair()
        count = 3000
        message = Message("a", "b", "urn:test", {"type": "PING", "pad": "x" * 200})
        problems = []

        def write(channel, times):
            try:
                for _ in range(times):
                    channel.send(message)
            except Exception as e:      # reported to the main thread
                problems.append(e)

        def read(channel, times, got):
            try:
                for _ in range(times):
                    channel.recv()
                    got.append(1)
            except Exception as e:      # reported to the main thread
                problems.append(e)
        got_client, got_server = [], []
        threads = [threading.Thread(target=read, args=(client, count, got_client), daemon=True),
                   threading.Thread(target=read, args=(server, 2 * count, got_server), daemon=True),
                   threading.Thread(target=write, args=(server, count), daemon=True),
                   threading.Thread(target=write, args=(client, count), daemon=True),
                   threading.Thread(target=write, args=(client, count), daemon=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(problems, [])
        self.assertEqual((len(got_client), len(got_server)), (count, 2 * count))

    def test_closing_wakes_a_reader_that_is_waiting(self):
        # a reader left waiting on a closed socket reads from the next socket that gets its number, TLS records
        # included: RECORD_LAYER_FAILURE on the real TV, during a pause, after another connection closed
        # [V 03/11/2026]
        client, _server = self.pair()
        failed = threading.Event()

        def read():
            try:
                client.recv()
            except (castv2.CastError, OSError, ValueError):
                failed.set()
        threading.Thread(target=read, daemon=True).start()
        time.sleep(0.1)
        client.close()
        self.assertTrue(failed.wait(3))


if __name__ == "__main__":
    unittest.main()

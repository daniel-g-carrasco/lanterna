#!/usr/bin/env python3
"""The media server: a file served whole and by ranges, only at its
token, only to the allowed addresses, a live stream produced per request,
and nothing once the item is withdrawn or the server stopped; a fixed port
when one is asked for, and a busy one said with its number."""

import http.client
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse
from pathlib import Path

import _setup  # noqa: F401

from lanterna import server
from lanterna.server import Item, MediaServer, parse_range


class RangeTest(unittest.TestCase):
    def test_ranges_as_players_send_them(self):
        self.assertEqual(parse_range("bytes=0-", 1000), (0, 999))
        self.assertEqual(parse_range("bytes=100-199", 1000), (100, 199))
        self.assertEqual(parse_range("bytes=900-5000", 1000), (900, 999))
        self.assertEqual(parse_range("bytes=-100", 1000), (900, 999))

    def test_no_header_or_an_odd_one_means_the_whole_file(self):
        self.assertIsNone(parse_range(None, 1000))
        self.assertIsNone(parse_range("bytes=0-10,20-30", 1000))
        self.assertIsNone(parse_range("items=0-1", 1000))

    def test_beyond_the_end_cannot_be_satisfied(self):
        self.assertIs(parse_range("bytes=1000-", 1000), False)
        self.assertIs(parse_range("bytes=50-10", 1000), False)
        self.assertIs(parse_range("bytes=-0", 1000), False)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.file = Path(self.dir.name) / "film.mp4"
        self.data = bytes(range(256)) * 4096           # 1 MiB
        self.file.write_bytes(self.data)
        self.server = MediaServer()
        self.server.start()
        self.addCleanup(self.server.stop)

    def get(self, url_or_path, headers=None, method="GET"):
        path = urllib.parse.urlsplit(url_or_path).path if url_or_path.startswith("http") else url_or_path
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        return response, response.read()

    def test_the_url_points_at_this_computer_with_a_token(self):
        url = self.server.publish(Item("film.mp4", "video/mp4", path=str(self.file)), "127.0.0.1")
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(parts.port, self.server.port)
        token, name = parts.path.lstrip("/").split("/")
        self.assertGreaterEqual(len(token), 16)
        self.assertEqual(name, "film.mp4")

    def test_a_whole_file_and_a_range(self):
        url = self.server.publish(Item("film.mp4", "video/mp4", path=str(self.file)), "127.0.0.1")
        response, body = self.get(url)
        self.assertEqual((response.status, response.getheader("Content-Type")), (200, "video/mp4"))
        self.assertEqual(response.getheader("Accept-Ranges"), "bytes")
        self.assertEqual(body, self.data)
        response, body = self.get(url, {"Range": "bytes=1000-1999"})
        self.assertEqual(response.status, 206)
        self.assertEqual(response.getheader("Content-Range"), f"bytes 1000-1999/{len(self.data)}")
        self.assertEqual(body, self.data[1000:2000])
        response, body = self.get(url, {"Range": "bytes=-10"})
        self.assertEqual(body, self.data[-10:])

    def test_head_tells_the_size_without_the_body(self):
        url = self.server.publish(Item("film.mp4", "video/mp4", path=str(self.file)), "127.0.0.1")
        response, body = self.get(url, method="HEAD")
        self.assertEqual((response.status, response.getheader("Content-Length"), body),
                         (200, str(len(self.data)), b""))

    def test_an_unsatisfiable_range_is_416(self):
        url = self.server.publish(Item("film.mp4", "video/mp4", path=str(self.file)), "127.0.0.1")
        response, _ = self.get(url, {"Range": f"bytes={len(self.data)}-"})
        self.assertEqual(response.status, 416)

    def test_nothing_but_the_published_item(self):
        url = self.server.publish(Item("film.mp4", "video/mp4", path=str(self.file)), "127.0.0.1")
        token = urllib.parse.urlsplit(url).path.split("/")[1]
        self.assertEqual(self.get("/")[0].status, 404)
        self.assertEqual(self.get("/wrongtoken/film.mp4")[0].status, 404)
        self.assertEqual(self.get(f"/{token}/../../etc/passwd")[0].status, 200)   # the name is ignored...
        self.assertEqual(self.get(f"/{token}/../../etc/passwd")[1], self.data)    # ...only the item is served

    def test_a_new_item_retires_the_old_url(self):
        old = self.server.publish(Item("film.mp4", "video/mp4", path=str(self.file)), "127.0.0.1")
        self.server.publish(Item("film.mp4", "video/mp4", path=str(self.file)), "127.0.0.1")
        self.assertEqual(self.get(old)[0].status, 404)

    def test_switching_receiver_retires_the_previous_address(self):
        item = Item("film.mp4", "video/mp4", path=str(self.file))
        with patch("lanterna.server.local_address_towards", return_value="127.0.0.1"):
            self.server.publish(item, "192.0.2.1")
            self.assertIn("192.0.2.1", self.server.allowed)
            self.server.publish(item, "192.0.2.2")
        self.assertIn("192.0.2.2", self.server.allowed)
        self.assertNotIn("192.0.2.1", self.server.allowed)
        self.server.withdraw()
        self.assertEqual(self.server.allowed, {"127.0.0.1", "::1"})

    def test_withdrawn_and_stopped_serve_nothing(self):
        url = self.server.publish(Item("film.mp4", "video/mp4", path=str(self.file)), "127.0.0.1")
        self.server.withdraw()
        self.assertEqual(self.get(url)[0].status, 404)
        port = self.server.port
        self.server.stop()
        with self.assertRaises(OSError):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/")
            conn.getresponse()

    def test_other_addresses_are_refused(self):
        refusing = MediaServer()
        refusing.allowed = {"192.0.2.1"}        # a documentation address: the tests' own is not in
        refusing.start()
        self.addCleanup(refusing.stop)
        with refusing._lock:
            refusing._item, refusing._token = Item("x", "video/mp4", path=str(self.file)), "t"
        conn = http.client.HTTPConnection("127.0.0.1", refusing.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/t/x")
        self.assertEqual(conn.getresponse().status, 403)

    def test_a_vanished_file_is_410(self):
        url = self.server.publish(Item("film.mp4", "video/mp4", path=str(self.file)), "127.0.0.1")
        self.file.unlink()
        self.assertEqual(self.get(url)[0].status, 410)

    def test_a_live_stream_is_produced_for_each_request(self):
        calls = []

        def produce(out):
            calls.append(threading.current_thread().name)
            for i in range(3):
                out.write(f"chunk{i};".encode())

        url = self.server.publish(Item("screen.mkv", "video/x-matroska", producer=produce), "127.0.0.1")
        response, body = self.get(url)
        self.assertEqual((response.status, body), (200, b"chunk0;chunk1;chunk2;"))
        self.get(url)
        self.assertEqual(len(calls), 2)

    def test_the_local_address_towards_loopback_is_loopback(self):
        self.assertEqual(server.local_address_towards("127.0.0.1"), "127.0.0.1")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def holding(port: int) -> socket.socket:
    """Another program on that port."""
    other = socket.socket()
    other.bind(("0.0.0.0", port))
    other.listen()
    return other


class FixedPortTest(unittest.TestCase):
    def test_a_fixed_port_is_the_one_the_tv_is_given(self):
        port = free_port()
        srv = MediaServer(port=port)
        srv.start()
        self.addCleanup(srv.stop)
        self.assertEqual(srv.port, port)
        url = srv.publish(Item("a.jpg", "image/jpeg", path=__file__), "127.0.0.1")
        self.assertEqual(urllib.parse.urlsplit(url).port, port)

    def test_a_busy_fixed_port_is_said_with_its_number(self):
        port = free_port()
        other = holding(port)
        self.addCleanup(other.close)
        srv = MediaServer(port=port)
        with self.assertRaises(server.PortBusy) as caught:
            srv.start()
        self.assertEqual(caught.exception.port, port)
        self.assertTrue(caught.exception.reason)
        self.assertEqual(srv.port, 0)                   # nothing half open

    def test_a_new_port_counts_from_the_next_start(self):
        srv = MediaServer()
        srv.start()
        self.addCleanup(srv.stop)
        self.assertFalse(srv.stale)
        port = free_port()
        srv.port_wanted = port
        self.assertTrue(srv.stale)
        srv.stop()
        srv.start()
        self.assertEqual((srv.port, srv.stale), (port, False))
        srv.port_wanted = 0                             # back to a free one each time
        self.assertTrue(srv.stale)


if __name__ == "__main__":
    unittest.main()

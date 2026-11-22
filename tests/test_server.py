#!/usr/bin/env python3
"""The media server: a file served whole and by ranges, only at its
token, only to the allowed addresses, a live stream produced per request,
and nothing once the item is withdrawn or the server stopped."""

import http.client
import tempfile
import threading
import unittest
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


if __name__ == "__main__":
    unittest.main()

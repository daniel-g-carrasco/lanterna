"""The small HTTP server the TV reads from while something is shown.

A Chromecast plays a URL: it does not take a file. So while Lanterna shows
a film, a photo or the screen, this server answers on the local network,
and on nothing else:

- one item at a time, at a path with a random token (`/<token>/<name>`):
  nothing else on the disk can be asked for, and a URL seen once is useless
  after the item changes;
- only to the TV's address (and to this computer, for the tests and for a
  look with a browser): any other client gets 403;
- only while the item is on: `stop()` closes the socket.

Files are served with byte ranges (`Range: bytes=a-b`), which the TV's
player uses to seek and to read the index at the end of an MP4; a live
stream (the screen, a transcoded film) is produced afresh for every GET by
a callback that writes into the response until the client goes away.
"""

from __future__ import annotations

import os
import re
import secrets
import socket
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK = 256 * 1024
RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


@dataclass(frozen=True)
class Item:
    """What the server hands out: a file on disk, or a live producer that
    writes the stream into a file-like object (it returns when the client
    has gone, raising BrokenPipeError or ConnectionResetError is fine)."""
    name: str
    content_type: str
    path: str | None = None
    producer: Callable | None = None


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | bool:
    """The byte range a Range header asks for, inclusive, clipped to the
    file: None when there is no (usable) header, False when it cannot be
    satisfied. Only single ranges: the media players ask for nothing else."""
    if not header:
        return None
    match = RANGE.match(header.strip())
    if not match:
        return None
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":                         # the last N bytes
        length = int(last)
        if length == 0:
            return False
        return max(0, size - length), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        return False
    return start, min(end, size - 1)


def local_address_towards(host: str) -> str:
    """The address of this computer that the TV reaches: the one the
    kernel would route a packet to the TV from (no packet is sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, 9))
        return probe.getsockname()[0]
    finally:
        probe.close()


class MediaServer:
    """One server, one item at a time. Thread-safe; start() binds a free
    port on every interface (the TV may be on Wi-Fi or Ethernet)."""

    def __init__(self, allowed: set[str] | None = None, port: int = 0):
        self.allowed = set(allowed or ()) | {"127.0.0.1", "::1"}
        self._port_wanted = port
        self._item: Item | None = None
        self._token = ""
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.served = 0                     # bytes, for the tests and the diagnostics

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else 0

    def start(self) -> None:
        if self._httpd:
            return
        handler = _handler_for(self)
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self._port_wanted), handler)
        self._httpd.daemon_threads = True
        httpd = self._httpd
        self._thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.1),
                                        name="media-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._item = None
            self._token = ""
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def publish(self, item: Item, host: str) -> str:
        """Put `item` on (in place of the previous one) and return the URL
        the TV at `host` uses for it."""
        with self._lock:
            self._item = item
            self._token = secrets.token_urlsafe(12)
            path = f"/{self._token}/{urllib.parse.quote(item.name)}"
        self.allowed.add(host)
        address = local_address_towards(host)
        return f"http://{address}:{self.port}{path}"

    def withdraw(self) -> None:
        with self._lock:
            self._item = None
            self._token = ""

    def lookup(self, path: str) -> Item | None:
        parts = urllib.parse.urlsplit(path).path.lstrip("/").split("/", 1)
        with self._lock:
            if self._item and self._token and parts[0] == self._token:
                return self._item
        return None


def _handler_for(server: MediaServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Lanterna"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass  # one line per range request of a film would flood the log; errors are reported below

        def _allowed(self) -> bool:
            if self.client_address[0] in server.allowed:
                return True
            self.send_error(403)
            return False

        def do_HEAD(self):
            self._serve(head=True)

        def do_GET(self):
            self._serve(head=False)

        def _serve(self, head: bool) -> None:
            if not self._allowed():
                return
            item = server.lookup(self.path)
            if item is None:
                self.send_error(404)
                return
            if item.producer is not None:
                self._live(item, head)
            else:
                self._file(item, head)

        def _common_headers(self, item: Item) -> None:
            self.send_header("Content-Type", item.content_type)
            self.send_header("Access-Control-Allow-Origin", "*")   # the receiver is a web page
            self.send_header("Cache-Control", "no-store")

        def _file(self, item: Item, head: bool) -> None:
            try:
                f = open(item.path, "rb")
            except OSError:
                self.send_error(410, "The file is no longer there")
                return
            with f:
                size = os.fstat(f.fileno()).st_size
                wanted = parse_range(self.headers.get("Range"), size)
                if wanted is False:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = wanted if wanted else (0, size - 1)
                self.send_response(206 if wanted else 200)
                self._common_headers(item)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(max(0, end - start + 1)))
                if wanted:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if head:
                    return
                f.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = f.read(min(CHUNK, left))
                        if not chunk:
                            break               # the file shrank while it was being read
                        self.wfile.write(chunk)
                        server.served += len(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the player closes a range it no longer needs: normal while seeking

        def _live(self, item: Item, head: bool) -> None:
            self.send_response(200)
            self._common_headers(item)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if head:
                return
            try:
                item.producer(self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the TV stopped reading: the producer's job ends here

    return Handler

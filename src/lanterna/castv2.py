"""Cast v2, the protocol a Chromecast speaks on TCP port 8009: just enough
of it to launch the Default Media Receiver, load a URL and drive playback.

The wire format is a 4-byte big-endian length and a protobuf CastMessage
(protocol version, source, destination, namespace, payload type, a JSON
payload). Six fields, so they are written and read by hand here rather than
with a protobuf library: one dependency less in the Flatpak and the bundle.
The messages and their order are the ones the Chromecast answered on the
Philips TV (cast_build_revision 3.72) during the proof of concept of
25/10/2026, and the ones GNOME Network Displays sends.

`Channel` frames messages over any connected socket (TLS in real use, a
socketpair in the tests). `Receiver` runs the conversation: a reader thread,
a heartbeat that notices a TV gone away, requests that wait for their own
answer, and callbacks for every status the TV pushes. Callbacks run on the
reader thread: the window hands them to the main loop.
"""

from __future__ import annotations

import json
import select
import socket
import ssl
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

PORT = 8009
DEFAULT_MEDIA_RECEIVER = "CC1AD845"

NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"

PLATFORM = "receiver-0"
HEARTBEAT_S = 5             # a PING this often...
DEAD_AFTER_S = 16           # ...and the TV is taken for gone after this long without a word
WAIT_S = 0.25               # select() wakes this often, so that a closed socket is noticed
SEND_TIMEOUT_S = 15
REQUEST_TIMEOUT_S = 15      # a LAUNCH on a TV that wakes from standby takes a few seconds
MAX_MESSAGE = 64 * 1024     # the spec caps a message at 64 KiB; more is a broken stream


class CastError(Exception):
    """The TV refused, did not answer, or the connection broke."""


# ---------------------------------------------------------------- framing

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _string(tag: int, text: str) -> bytes:
    data = text.encode()
    return bytes([tag]) + _varint(len(data)) + data


@dataclass(frozen=True)
class Message:
    source: str
    destination: str
    namespace: str
    payload: dict = field(default_factory=dict)


def encode(message: Message) -> bytes:
    """One framed CastMessage: protocol_version 0, payload_type STRING."""
    body = (b"\x08\x00"
            + _string(0x12, message.source)
            + _string(0x1A, message.destination)
            + _string(0x22, message.namespace)
            + b"\x28\x00"
            + _string(0x32, json.dumps(message.payload, separators=(",", ":"))))
    return struct.pack(">I", len(body)) + body


def decode(body: bytes) -> Message:
    """A CastMessage body (without its length) back to a Message. Unknown
    fields are skipped; a binary payload, which the media receiver never
    sends, decodes as an empty dict. ValueError on a truncated body."""
    fields: dict[int, bytes] = {}
    i = 0

    def varint() -> int:
        nonlocal i
        value = shift = 0
        while True:
            if i >= len(body):
                raise ValueError("truncated varint")
            byte = body[i]
            i += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value

    while i < len(body):
        key = varint()
        number, wire = key >> 3, key & 7
        if wire == 0:
            varint()
        elif wire == 2:
            length = varint()
            if i + length > len(body):
                raise ValueError("truncated field")
            fields[number] = body[i:i + length]
            i += length
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            raise ValueError(f"unknown wire type {wire}")
    raw = fields.get(6, b"")
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        raise ValueError(f"payload is not JSON: {e}") from None
    return Message(fields.get(2, b"").decode(), fields.get(3, b"").decode(),
                   fields.get(4, b"").decode(), payload if isinstance(payload, dict) else {})


class Channel:
    """Framed messages over a connected socket. send() may be called from
    any thread; recv() from one reader only.

    The socket is non-blocking and every call into it holds the lock: an
    OpenSSL connection cannot read and write at the same moment, and the
    reader spends its life waiting while the heartbeat and the commands
    write. Done without, it fails now and then with "A failure in the SSL
    library" (ConcurrentTlsTest) [V 03/11/2026]. The waiting is done with
    select(), outside the lock."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._lock = threading.Lock()
        sock.setblocking(False)

    @classmethod
    def connect(cls, host: str, port: int = PORT, timeout: float = 10.0) -> Channel:
        """TLS to the TV. The certificate is not checked: a Chromecast's is
        issued by Google's device CA for its own UUID, which no public trust
        store holds; what is on the other end is the device the user picked
        from the local network."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            tls = ctx.wrap_socket(raw)
        except (OSError, ssl.SSLError):
            raw.close()
            raise
        tls.settimeout(None)
        return cls(tls)

    def send(self, message: Message) -> None:
        data = memoryview(encode(message))
        deadline = time.monotonic() + SEND_TIMEOUT_S
        while data:
            readable, writable = (), ()
            with self._lock:
                try:
                    data = data[self.sock.send(data):]
                except ssl.SSLWantReadError:
                    readable = (self.sock,)
                except (ssl.SSLWantWriteError, BlockingIOError):
                    writable = (self.sock,)
            if not data:
                return
            if time.monotonic() > deadline:
                raise OSError("the TV does not read what is sent to it")
            if readable or writable:
                select.select(readable, writable, (), WAIT_S)

    def recv(self) -> Message:
        (length,) = struct.unpack(">I", self._exact(4))
        if length > MAX_MESSAGE:
            raise CastError(f"message of {length} bytes from the TV")
        return decode(self._exact(length))

    def _exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            readable, writable = (), ()
            chunk = None
            with self._lock:
                try:
                    chunk = self.sock.recv(n - len(buf))     # first: TLS may hold data the socket no longer shows
                except (ssl.SSLWantReadError, BlockingIOError):
                    readable = (self.sock,)
                except ssl.SSLWantWriteError:
                    writable = (self.sock,)
            if chunk == b"":
                raise CastError("the TV closed the connection")
            if chunk:
                buf += chunk
            else:
                select.select(readable, writable, (), WAIT_S)      # ValueError once closed: the reader ends there
        return bytes(buf)

    def close(self) -> None:
        """Shut down first: close() alone does not wake a reader waiting on
        the socket, and that reader would go on reading from the next socket
        that gets the same number, with TLS records that are not its own."""
        with self._lock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # not connected any more: there is no reader to wake, or the TV has woken it
            try:
                self.sock.close()
            except OSError:
                pass  # closing a socket the TV already dropped: nothing left to free


# ---------------------------------------------------------------- the conversation

@dataclass
class MediaStatus:
    """What the media receiver says about the item it plays."""
    session_id: int | None = None
    state: str = "IDLE"             # IDLE, BUFFERING, PLAYING, PAUSED
    idle_reason: str = ""           # FINISHED, CANCELLED, INTERRUPTED, ERROR
    position_s: float = 0.0
    duration_s: float | None = None
    rate: float = 1.0
    at: float = 0.0                 # time.monotonic() when position_s was true

    @classmethod
    def from_payload(cls, entry: dict) -> MediaStatus:
        media = entry.get("media") or {}
        duration = media.get("duration")
        return cls(session_id=entry.get("mediaSessionId"), state=entry.get("playerState", "IDLE"),
                   idle_reason=entry.get("idleReason", ""), position_s=float(entry.get("currentTime") or 0),
                   duration_s=float(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
                   rate=float(entry.get("playbackRate") or 1), at=time.monotonic())


class Receiver:
    """One TV: connect, launch the Default Media Receiver, load, control.

    `on_media` hears every MediaStatus, `on_volume` every (level, muted),
    `on_closed` once, with the reason, when the connection ends for any
    cause but close(). All three run on the reader thread."""

    def __init__(self, channel: Channel, sender_id: str = "sender-lanterna",
                 on_media: Callable[[MediaStatus], None] | None = None,
                 on_volume: Callable[[float, bool], None] | None = None,
                 on_closed: Callable[[str], None] | None = None):
        self.channel = channel
        self.sender = sender_id
        self.on_media = on_media
        self.on_volume = on_volume
        self.on_closed = on_closed
        self.transport: str | None = None       # the media receiver's id once launched
        self.app_session: str | None = None
        self.media = MediaStatus()
        self._request_id = 0
        self._lock = threading.Lock()
        self._waiters: dict[int, tuple[threading.Event, list]] = {}
        self._last_heard = time.monotonic()
        self._closing = threading.Event()
        self._reader: threading.Thread | None = None
        self._heart: threading.Thread | None = None

    # -- life

    def start(self) -> None:
        self._send(PLATFORM, NS_CONNECTION, {"type": "CONNECT"})
        self._reader = threading.Thread(target=self._read_loop, name="cast-reader", daemon=True)
        self._heart = threading.Thread(target=self._heartbeat, name="cast-heartbeat", daemon=True)
        self._reader.start()
        self._heart.start()

    def close(self) -> None:
        """Leave the TV as it is (the media keeps playing if it was) and drop the connection."""
        if self._closing.is_set():
            return
        self._closing.set()
        try:
            if self.transport:
                self._send(self.transport, NS_CONNECTION, {"type": "CLOSE"})
            self._send(PLATFORM, NS_CONNECTION, {"type": "CLOSE"})
        except OSError:
            pass  # the TV is already gone: closing is what we wanted anyway
        self.channel.close()
        self._fail_waiters("closed")
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(2)        # gone before its socket's number can be given to another connection

    # -- receiver

    def launch(self, app_id: str = DEFAULT_MEDIA_RECEIVER) -> str:
        """Start the app on the TV (or find it already running) and connect
        to it; returns its transport id."""
        status = self.request(PLATFORM, NS_RECEIVER, {"type": "LAUNCH", "appId": app_id})
        app = self._find_app(status, app_id)
        deadline = time.monotonic() + REQUEST_TIMEOUT_S
        while app is None:
            # LAUNCH_STATUS "USER_ALLOWED" comes first on some TVs; the app shows up in a later status
            if time.monotonic() > deadline:
                raise CastError("the TV did not start its media player")
            status = self.request(PLATFORM, NS_RECEIVER, {"type": "GET_STATUS"})
            app = self._find_app(status, app_id)
            if app is None:
                time.sleep(0.5)
        self.transport = app["transportId"]
        self.app_session = app.get("sessionId")
        self._send(self.transport, NS_CONNECTION, {"type": "CONNECT"})
        return self.transport

    def stop_app(self) -> None:
        """Close the media player on the TV (back to the TV's own screen)."""
        if self.app_session:
            self.request(PLATFORM, NS_RECEIVER, {"type": "STOP", "sessionId": self.app_session})
        self.transport = self.app_session = None

    def set_volume(self, level: float) -> None:
        self.request(PLATFORM, NS_RECEIVER, {"type": "SET_VOLUME", "volume": {"level": max(0.0, min(1.0, level))}})

    def set_muted(self, muted: bool) -> None:
        self.request(PLATFORM, NS_RECEIVER, {"type": "SET_VOLUME", "volume": {"muted": muted}})

    # -- media

    def load(self, url: str, content_type: str, title: str = "", live: bool = False,
             start_s: float = 0.0) -> MediaStatus:
        if not self.transport:
            raise CastError("load before launch")
        media = {"contentId": url, "contentUrl": url, "contentType": content_type,
                 "streamType": "LIVE" if live else "BUFFERED",
                 "metadata": {"metadataType": 0, "title": title}}
        reply = self.request(self.transport, NS_MEDIA, {"type": "LOAD", "media": media, "autoplay": True,
                                                        "currentTime": start_s})
        kind = reply.get("type")
        if kind in ("LOAD_FAILED", "LOAD_CANCELLED", "INVALID_REQUEST"):
            raise CastError(f"the TV refused the media ({kind.lower().replace('_', ' ')})")
        return self.media

    def play(self) -> None:
        self._media_command({"type": "PLAY"})

    def pause(self) -> None:
        self._media_command({"type": "PAUSE"})

    def seek(self, position_s: float) -> None:
        self._media_command({"type": "SEEK", "currentTime": max(0.0, position_s)})

    def stop_media(self) -> None:
        self._media_command({"type": "STOP"})

    def _media_command(self, payload: dict) -> None:
        if not self.transport or self.media.session_id is None:
            raise CastError("nothing is playing")
        payload["mediaSessionId"] = self.media.session_id
        self.request(self.transport, NS_MEDIA, payload)

    # -- plumbing

    def request(self, destination: str, namespace: str, payload: dict,
                timeout: float = REQUEST_TIMEOUT_S) -> dict:
        """Send and wait for the answer carrying the same requestId."""
        with self._lock:
            self._request_id += 1
            rid = self._request_id
            event, box = threading.Event(), []
            self._waiters[rid] = (event, box)
        payload = dict(payload, requestId=rid)
        try:
            self._send(destination, namespace, payload)
        except OSError as e:
            self._waiters.pop(rid, None)
            raise CastError(f"could not talk to the TV: {e}") from e
        if not event.wait(timeout):
            self._waiters.pop(rid, None)
            raise CastError(f"the TV did not answer {payload.get('type', 'a request')}")
        answer = box[0]
        if isinstance(answer, str):
            raise CastError(f"the connection ended: {answer}")
        return answer

    def _send(self, destination: str, namespace: str, payload: dict) -> None:
        self.channel.send(Message(self.sender, destination, namespace, payload))

    @staticmethod
    def _find_app(status: dict, app_id: str) -> dict | None:
        body = status.get("status")
        if not isinstance(body, dict):      # LAUNCH_STATUS carries "status": "USER_ALLOWED", a string
            return None
        for app in body.get("applications") or []:
            if app.get("appId") == app_id and app.get("transportId"):
                return app
        return None

    def _read_loop(self) -> None:
        reason = "the TV closed the connection"
        try:
            while not self._closing.is_set():
                message = self.channel.recv()
                self._last_heard = time.monotonic()
                self._dispatch(message)
        except (CastError, OSError, ValueError) as e:
            reason = str(e)
        if not self._closing.is_set():
            self._closing.set()
            self.channel.close()
            self._fail_waiters(reason)
            if self.on_closed:
                self.on_closed(reason)

    def _dispatch(self, message: Message) -> None:
        payload = message.payload
        kind = payload.get("type")
        if message.namespace == NS_HEARTBEAT:
            if kind == "PING":
                self._send(message.source, NS_HEARTBEAT, {"type": "PONG"})
            return
        if message.namespace == NS_CONNECTION and kind == "CLOSE" and message.source == self.transport:
            self.transport = None          # the media player on the TV went away (another sender, the remote)
            self._fail_waiters("the TV closed its player")      # what was waiting for it would wait for nothing
        if kind == "RECEIVER_STATUS":
            volume = (payload.get("status") or {}).get("volume") or {}
            if self.on_volume and "level" in volume:
                self.on_volume(float(volume["level"]), bool(volume.get("muted")))
        elif kind == "MEDIA_STATUS":
            for entry in payload.get("status") or []:
                self.media = MediaStatus.from_payload(entry)
                if self.on_media:
                    self.on_media(self.media)
        rid = payload.get("requestId")
        if rid:
            waiter = self._waiters.pop(rid, None)
            if waiter:
                waiter[1].append(payload)
                waiter[0].set()

    def _heartbeat(self) -> None:
        while not self._closing.wait(HEARTBEAT_S):
            if time.monotonic() - self._last_heard > DEAD_AFTER_S:
                self.channel.close()        # the reader wakes up with an error and reports it
                return
            try:
                self._send(PLATFORM, NS_HEARTBEAT, {"type": "PING"})
            except OSError:
                return

    def _fail_waiters(self, reason: str) -> None:
        for rid in list(self._waiters):
            waiter = self._waiters.pop(rid, None)
            if waiter:
                waiter[1].append(reason)
                waiter[0].set()

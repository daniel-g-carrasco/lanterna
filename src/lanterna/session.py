"""One TV, one thing shown on it: the part the window, MPRIS and the D-Bus
interface for the shell all drive.

A session holds the connection to a TV (castv2.Receiver), the media
server, and what is on: a single file or a list of them (a folder of
photos, with next and previous). For every file it follows the plan of
media.py: the first way, and when the TV refuses it (LOAD_FAILED, or an
IDLE with an ERROR reason soon after) the next one, saying why.

The position is the TV's own for a file served as it is. A live stream
(sound converted, everything converted) has no position on the TV's side:
the receiver reports 0. So the session counts it: where the stream started
plus the time spent playing. Seeking in a live stream starts a new stream
at that point.

The methods block on the network: the window runs them in a thread. The
state is published by `on_state(State)` from whatever thread changed it;
State is a frozen snapshot, safe to hand to the main loop.

The user does not wait for a file to arrive before changing their mind, so
every show, seek and stop starts a new generation, and whatever is still on
its way for an older one ends in Cancelled at its next step, without
touching the state or the TV any further.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from . import castv2, media, server, transcode
from .devices import Device
from .i18n import _
from .log import complain
from .media import Way

FAIL_WINDOW_S = 8.0         # an IDLE/ERROR this soon after a LOAD counts as the TV refusing the way
PLAY_TIMEOUT_S = 20.0       # a LOAD the TV takes but never starts playing


class SessionError(Exception):
    """What could not be done, in words for the user."""


class Cancelled(SessionError):
    """Something newer (a stop, another file) took over: nothing went wrong, nothing to say."""


@dataclass(frozen=True)
class State:
    state: str = "idle"             # idle, connecting, buffering, playing, paused, error
    kind: str = ""                  # file, photo, screen, window
    device_id: str = ""
    device_name: str = ""
    title: str = ""
    path: str = ""
    way: str = ""                   # direct, audio, transcode, screen
    position_s: float = 0.0
    at: float = 0.0                 # time.time() when position_s was true
    rate: float = 0.0               # 1.0 while playing, 0 otherwise: position now = position_s + rate * (now - at)
    duration_s: float | None = None
    seekable: bool = False
    index: int = 0
    count: int = 0
    volume: float | None = None
    error: str = ""
    note: str = ""                  # why not the file as it is ("the sound is converted"), or ""

    def position_now(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        value = self.position_s + self.rate * max(0.0, now - self.at)
        return min(value, self.duration_s) if self.duration_s else value


def clock(seconds: float) -> str:
    """A position or a duration as a player shows it: 2:05, 1:02:05."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def kind_of(info: media.MediaInfo) -> str:
    return "photo" if info.image else "file"


class Session:
    def __init__(self, device: Device, on_state: Callable[[State], None],
                 connect: Callable[[Device], castv2.Channel] | None = None,
                 media_server: server.MediaServer | None = None,
                 probe: Callable[[str], media.MediaInfo] = media.probe,
                 producer: Callable | None = None):
        self.device = device
        self.on_state = on_state
        self._connect = connect or (lambda d: castv2.Channel.connect(d.host, d.port))
        self._server = media_server or server.MediaServer()
        self._own_server = media_server is None
        self._probe = probe
        self._producer = producer or transcode.stream
        self._receiver: castv2.Receiver | None = None
        self._lock = threading.RLock()
        self._launching = threading.Lock()
        self._items: list[str] = []
        self._info: media.MediaInfo | None = None
        self._plan: media.Plan | None = None
        self._way: Way | None = None
        self._live_offset = 0.0
        self._job: transcode.Job | None = None
        self._loaded_at = 0.0
        self._state = State(device_id=device.id, device_name=device.label)
        self._refused = False           # set by the reader when the TV turns the last LOAD down
        self._generation = 0            # see the module's docstring
        self._ending: Callable[[], None] | None = None      # what show_live was given to end its source
        self._loading = False           # between a LOAD and its answer the old media may still announce its end
        self._media_session = None      # the TV's number for what is on now
        self._closed = False

    # -- what the others read

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    def _set(self, **changes) -> None:
        with self._lock:
            self._state = replace(self._state, **changes)
            snapshot = self._state
        self.on_state(snapshot)

    def _new_generation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def _set_for(self, generation: int, **changes) -> None:
        """_set for work that may have been overtaken in the meantime."""
        with self._lock:
            if generation != self._generation:
                raise Cancelled("something newer took over")
            self._state = replace(self._state, **changes)
            snapshot = self._state
        self.on_state(snapshot)

    # -- showing things

    def show(self, paths: list[str], index: int = 0) -> None:
        """Show paths[index] (the others are reached with next and previous)."""
        if not paths:
            raise SessionError(_("Nothing to show"))
        with self._lock:
            self._items = list(paths)
            ending, self._ending = self._ending, None
        if ending:                      # a file after the screen: the permission is not needed any more
            ending()
        self._show_index(max(0, min(index, len(paths) - 1)))

    def show_live(self, kind: str, title: str, producer: Callable, ending: Callable[[], None]) -> None:
        """Show something that has no file behind it (the screen, a window):
        `producer(out, job)` writes the stream for every GET, `ending()` is
        called once when the session lets it go."""
        generation = self._new_generation()
        with self._lock:
            self._items, self._info, self._plan = [], None, None
            previous, self._ending = self._ending, ending
        if previous:
            previous()

        def make_item(job):
            return server.Item(f"{kind}.mkv", media.LIVE_CONTENT_TYPE, producer=lambda out: producer(out, job))
        try:
            self._set_for(generation, state="connecting", kind=kind, path="", title=title, index=0, count=0, error="",
                          note="", position_s=0.0, rate=0.0, at=time.time(), duration_s=None, seekable=False)
            self._ensure_receiver(generation)
            self._load_item(Way.SCREEN, make_item, media.LIVE_CONTENT_TYPE, title, 0.0, generation)
        except Cancelled:
            raise                           # what overtook it has let the source go already
        except SessionError as e:
            self._let_go_of_live()
            self._set_for(generation, state="error", rate=0.0, error=str(e))
            raise

    def _let_go_of_live(self) -> None:
        with self._lock:
            ending, self._ending = self._ending, None
            job, self._job = self._job, None
        if job:
            job.stop()
        if ending:
            ending()

    def next(self) -> None:
        with self._lock:
            index, count = self._state.index, len(self._items)
        if index + 1 < count:
            self._show_index(index + 1)

    def previous(self) -> None:
        with self._lock:
            index = self._state.index
        if index > 0:
            self._show_index(index - 1)

    def _show_index(self, index: int) -> None:
        generation = self._new_generation()
        path = self._items[index]
        name = os.path.basename(path)
        self._set_for(generation, state="connecting", path=path, title=name, index=index, count=len(self._items),
                      error="", note="", position_s=0.0, rate=0.0, at=time.time())
        try:
            info = self._probe(path)
        except media.ProbeError as e:
            self._fail_for(generation, _("{name} cannot be read: {error}").format(name=name, error=e))
        plan = media.plan(info)
        if not plan.ways:
            self._fail_for(generation, plan.reason)
        with self._lock:
            self._info, self._plan = info, plan
        self._set_for(generation, kind=kind_of(info), duration_s=info.duration_s, note=plan.reason)
        self._ensure_receiver(generation)
        reasons = []
        for way in plan.ways:
            try:
                self._load(way, 0.0, generation)
                return
            except Cancelled:
                raise
            except SessionError as e:
                reasons.append(f"{way.value}: {e}")
        self._fail_for(generation, _("The TV refused this file ({reasons})").format(reasons="; ".join(reasons)))

    def _ensure_receiver(self, generation: int) -> None:
        # One at a time: a first LAUNCH takes the TV seconds, and the one that was overtaken decides what happens to
        # the player it opened before the next one asks for it.
        with self._launching:
            with self._lock:
                if self._receiver and self._receiver.transport:
                    return
                if generation != self._generation:
                    raise Cancelled("something newer took over")
            try:
                receiver = castv2.Receiver(self._connect(self.device), on_media=self._on_media,
                                           on_volume=self._on_volume, on_closed=self._on_closed)
                receiver.start()
                receiver.launch()
            except (castv2.CastError, OSError) as e:
                self._fail_for(generation, _("{tv} does not answer: {error}").format(tv=self.device.label, error=e))
            if self._own_server:
                self._server.start()
            with self._lock:
                current = generation == self._generation
                # Something newer is on its way to this same player: it gets the connection too. Closing it here
                # would close the player, since the receiver quits 0.3 s after its last sender leaves, whoever
                # has connected meanwhile [V 03/11/2026].
                keep = current or self._state.state != "idle"
                old = None
                if keep:
                    old, self._receiver = self._receiver, receiver
            if old:
                old.close()
            if not keep:
                self._close_on_tv(receiver)
            if not current:
                raise Cancelled("something newer took over")

    def _load(self, way: Way, start_s: float, generation: int) -> None:
        """Put the current file on the TV the given way, from start_s, and
        wait until it plays or the TV refuses it."""
        with self._lock:
            path, info, plan = self._state.path, self._info, self._plan
        name = os.path.basename(path)
        title = os.path.splitext(name)[0]
        if way is Way.DIRECT:
            self._load_item(way, lambda _job: server.Item(name, plan.content_type, path=path), plan.content_type, title,
                            start_s, generation, seekable=True)
            return

        def make_item(job):
            return server.Item(f"{title}.mkv", media.LIVE_CONTENT_TYPE,
                               producer=lambda out: self._producer(path, way, info, out, start_s=start_s, job=job))
        self._load_item(way, make_item, media.LIVE_CONTENT_TYPE, title, start_s, generation,
                        seekable=info.duration_s is not None)

    def _load_item(self, way: Way, make_item: Callable, content_type: str, title: str, start_s: float,
                   generation: int, seekable: bool = False) -> None:
        with self._lock:
            if generation != self._generation:
                raise Cancelled("something newer took over")
            receiver = self._receiver
            old_job, self._job = self._job, None
        if old_job:
            old_job.stop()
        if receiver is None:
            raise SessionError(_("The connection to the TV is gone"))
        live = way is not Way.DIRECT
        job = None
        if live:
            job = transcode.Job()
            with self._lock:
                self._job = job
        item = make_item(job)
        if item.producer is not None:
            item = replace(item, producer=self._guarded(item.producer, generation))
        url = self._server.publish(item, self.device.host)
        with self._lock:
            self._way, self._live_offset, self._loaded_at = way, start_s, time.monotonic()
            self._refused = False
            self._loading = True
        self._set_for(generation, way=way.value, seekable=seekable, state="buffering", error="",
                      position_s=start_s, rate=0.0, at=time.time())
        try:
            receiver.load(url, content_type, title=title, live=live, start_s=start_s if not live else 0.0)
            if not self._wait_playing(generation):
                raise SessionError(self.state.error or _("The TV did not start playing"))
        except castv2.CastError as e:
            raise SessionError(str(e)) from None
        finally:
            with self._lock:
                self._loading = False

    def _guarded(self, producer: Callable, generation: int) -> Callable:
        """The producer as the server's thread runs it: a stream that fails is said, where the user looks."""
        def run(out):
            try:
                producer(out)
            except (BrokenPipeError, ConnectionResetError):
                raise                       # the TV stopped reading: the server expects it
            except Exception as e:          # the edge of a thread nobody else listens to
                complain("producing the stream", e)
                self._stream_failed(generation, _("The stream stopped: {error}").format(error=e))
        return run

    def _stream_failed(self, generation: int, message: str) -> None:
        with self._lock:
            if generation != self._generation:
                return                      # something newer took over: that stream was going away anyway
            waiting = self._state.state in ("connecting", "buffering")
            self._refused = waiting
        if waiting:                         # _load_item is waiting: it tries the next way, with this reason
            self._set(error=message)
            return
        self._let_go_of_live()
        self._set(state="error", rate=0.0, error=message)

    def _wait_playing(self, generation: int) -> bool:
        deadline = time.monotonic() + PLAY_TIMEOUT_S
        while time.monotonic() < deadline:
            with self._lock:
                if generation != self._generation:
                    raise Cancelled("something newer took over")
                st = self._state.state
                refused = self._refused
            if refused:
                with self._lock:
                    self._refused = False
                return False
            if st in ("playing", "paused"):
                return True
            time.sleep(0.05)
        return False

    # -- playback

    def pause(self) -> None:
        self._command(lambda r: r.pause())

    def resume(self) -> None:
        self._command(lambda r: r.play())

    def seek(self, position_s: float) -> None:
        with self._lock:
            way, info = self._way, self._info
        if way is None:
            raise SessionError(_("Nothing is playing"))
        if way is Way.SCREEN:
            raise SessionError(_("The screen has no other point to go to"))
        position_s = max(0.0, min(position_s, (info.duration_s or position_s + 1) - 1))
        if way is Way.DIRECT:
            self._command(lambda r: r.seek(position_s))
        else:
            generation = self._new_generation()
            try:
                self._load(way, position_s, generation)     # a live stream cannot seek: a new one there
            except Cancelled:
                raise
            except SessionError as e:
                self._set_for(generation, state="error", rate=0.0, error=str(e))
                raise

    def set_volume(self, level: float) -> None:
        self._command(lambda r: r.set_volume(level))

    def stop(self) -> None:
        """Close the player on the TV and stop serving."""
        with self._lock:
            self._generation += 1
            receiver, job, ending = self._receiver, self._job, self._ending
            self._receiver, self._job, self._way, self._ending = None, None, None, None
            self._state = replace(self._state, state="idle", rate=0.0, way="", error="", note="")
            snapshot = self._state
        self.on_state(snapshot)         # at once: the TV may take seconds to answer what follows
        if job:
            job.stop()
        if ending:
            ending()
        self._server.withdraw()
        if receiver:
            self._close_on_tv(receiver)

    @staticmethod
    def _close_on_tv(receiver: castv2.Receiver) -> None:
        try:
            receiver.stop_app()
        except castv2.CastError:
            pass  # the TV is already gone or already closed it: stopping is what we wanted
        receiver.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop()
        if self._own_server:
            self._server.stop()

    def _command(self, action) -> None:
        with self._lock:
            receiver = self._receiver
        if receiver is None:
            raise SessionError(_("Nothing is playing"))
        try:
            action(receiver)
        except castv2.CastError as e:
            raise SessionError(str(e)) from None

    # -- what the TV says

    def _on_media(self, status: castv2.MediaStatus) -> None:
        with self._lock:
            way, offset, loaded_at, kind = self._way, self._live_offset, self._loaded_at, self._state.kind
            current = self._state
        if way is None:
            return
        if status.state == "IDLE":
            if status.idle_reason == "ERROR" and time.monotonic() - loaded_at < FAIL_WINDOW_S:
                with self._lock:
                    self._refused = True
                    said = self._state.error
                self._set(error=said or _("The TV could not play it this way"))
                return
            if status.idle_reason in ("FINISHED", "CANCELLED", "INTERRUPTED", "ERROR"):
                # Going to another point of a live stream is a new stream: the TV announces the end of the old one
                # during the load, or just after. That is not the end of what is shown: the window went back to its
                # first page, with the film loading on the TV (seen by hand, 03/11/2026).
                with self._lock:
                    old = self._loading or (self._media_session is not None and status.session_id is not None
                                            and status.session_id != self._media_session)
                if old:
                    return
                if kind == "photo" and status.idle_reason == "FINISHED":
                    return                          # a photo stays on screen
                if self._ending:                # the TV closed it: the screen is not being shown any more
                    self._let_go_of_live()
                self._set(state="error" if status.idle_reason == "ERROR" else "idle", rate=0.0,
                          error=_("The TV stopped playing") if status.idle_reason == "ERROR" else "")
            return
        with self._lock:
            self._media_session = status.session_id
        state = {"PLAYING": "playing", "PAUSED": "paused", "BUFFERING": "buffering"}.get(status.state, "buffering")
        if kind == "photo" and state == "paused":
            state = "playing"               # a photo on screen is reported PAUSED [V Philips, 25/10/2026]
        now = time.time()
        if way is Way.DIRECT:
            position = status.position_s
        else:
            # the receiver reports 0 for a live stream: count from where it started
            position = current.position_now(now) if current.state == "playing" else current.position_s
            position = max(position, offset)
        moving = state == "playing" and kind != "photo"
        self._set(state=state, position_s=position, at=now, rate=status.rate if moving else 0.0)

    def _on_volume(self, level: float, muted: bool) -> None:
        self._set(volume=0.0 if muted else level)

    def _on_closed(self, reason: str) -> None:
        with self._lock:
            self._receiver, self._way = None, None
            job, self._job = self._job, None
            ending, self._ending = self._ending, None
        if job:
            job.stop()
        if ending:
            ending()
        self._server.withdraw()
        self._set(state="error", rate=0.0,
                  error=_("Lost the connection to {tv}: {reason}").format(tv=self.device.label, reason=reason))

    def _fail_for(self, generation: int, reason: str) -> None:
        self._set_for(generation, state="error", rate=0.0, error=reason)
        raise SessionError(reason)

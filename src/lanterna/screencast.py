"""The screen or a window, from the system's ScreenCast portal to a live stream.

What the proof of concept found on niri [V 25/10/2026], which this code is
shaped by:

- niri hands the screen over only as DMA-BUF, so a chain through system memory
  fails with "no more input formats": the frames stay on the GPU (vapostproc,
  vacompositor, vah264enc).
- niri sends a frame only when the screen changes, and a Matroska stream with
  gaps stalls the TV: a live black background through vacompositor is the
  metronome, and the last frame of the screen stays on top of it.
- pipewiresrc's `target-object` takes the node's object.serial, while the
  portal hands out its id.
- The connection the portal session lives on has to stay alive: when it goes,
  the portal closes the session and the compositor stops 10-40 ms after it
  started. It is the shared session bus here, and Grant keeps a reference.

The sound is the monitor of the default output, mixed with live silence: if
the output is suspended the monitor may have nothing to give, and the muxer
would wait for it and freeze the picture too.

The chooser asks every time: a remembered choice (the portal's restore token)
would start showing a screen without a word, and what the user is sharing is
theirs to say each time.

A Grant runs on a thread of its own, with its own main context, because the
portal answers with signals while the user takes their time in its dialog.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

from . import transcode
from .i18n import _

DESKTOP = "org.freedesktop.portal.Desktop"
DESKTOP_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
SOURCE_MONITOR, SOURCE_WINDOW = 1, 2
CURSOR_EMBEDDED = 2
BITRATE_KBPS = 8000
FPS = 30
SOUND_RATE = 48000
STALL_S = 10            # with the metronome running, this long without data means the chain is stuck


class ScreenError(transcode.TranscodeError):
    """The screen could not be had, in words for the user."""


class Declined(ScreenError):
    """The user closed the portal's dialog: nothing went wrong, nothing to say."""


@dataclass(frozen=True)
class Source:
    kind: str           # "screen" or "window"
    node: int
    serial: str         # "" when it could not be read: pipewiresrc is then left to find it
    fd: int
    width: int
    height: int


class Grant:
    """One permission from the portal, from the dialog to close()."""

    def __init__(self, on_closed: Callable[[], None] | None = None, bus=None):
        self.on_closed = on_closed
        self.source: Source | None = None
        self._ready = threading.Event()
        self._error: ScreenError | None = None
        self._loop = None
        self._closing = threading.Event()
        self._bus = bus
        self._session = ""
        self._counter = 0
        self._thread: threading.Thread | None = None

    def request(self) -> Source:
        """Show the portal's dialog and wait for the answer: blocks, for as long as the user likes."""
        self._thread = threading.Thread(target=self._run, name="screen-portal", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            raise self._error
        return self.source

    def close(self) -> None:
        """End the permission, from any thread; also while the dialog is up."""
        self._closing.set()
        if self._loop is None:
            return
        # through the loop's own context: a quit() that comes just before run() is lost, and run() never returns
        from gi.repository import GLib
        source = GLib.idle_source_new()
        source.set_callback(lambda *_args: self._loop.quit() or False)
        source.attach(self._loop.get_context())

    # -- the portal's thread

    def _run(self) -> None:
        from gi.repository import Gio, GLib
        context = GLib.MainContext.new()
        context.push_thread_default()
        self._loop = GLib.MainLoop.new(context, False)
        subscription = 0
        try:
            self._bus = self._bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.source = self._open()
            subscription = self._bus.signal_subscribe(
                DESKTOP, "org.freedesktop.portal.Session", "Closed", self._session, None,
                Gio.DBusSignalFlags.NONE, lambda *_args: self._closed_by_portal())
        except ScreenError as e:
            self._error = e
        except GLib.Error as e:
            self._error = ScreenError(_("The screen cannot be shared here: {error}").format(error=e.message))
        self._ready.set()
        if self._error is None and not self._closing.is_set():
            self._loop.run()
        if subscription:
            self._bus.signal_unsubscribe(subscription)
        self._end_session()
        context.pop_thread_default()

    def _closed_by_portal(self) -> None:
        self._session = ""              # already gone: nothing to close
        self._loop.quit()
        if self.on_closed:
            self.on_closed()

    def _end_session(self) -> None:
        from gi.repository import Gio, GLib
        if self.source is not None:
            try:
                os.close(self.source.fd)
            except OSError:
                pass  # closed already: this is the only reference that matters here
        if self._session and self._bus is not None:
            try:
                self._bus.call_sync(DESKTOP, self._session, "org.freedesktop.portal.Session", "Close", None, None,
                                    Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error:
                pass  # the portal has gone with the session: closing is what we wanted

    def _portal(self, method: str, build) -> dict:
        """A portal call: the answer comes as a Response signal on a path made from a token of ours."""
        from gi.repository import Gio
        self._counter += 1
        token = f"lanterna{os.getpid()}_{self._counter}"
        sender = self._bus.get_unique_name()[1:].replace(".", "_")
        path = f"{DESKTOP_PATH}/request/{sender}/{token}"
        answer = {}

        def response(_conn, _sender, _path, _iface, _signal, params):
            answer["code"], answer["results"] = params.unpack()
            self._loop.quit()
        subscription = self._bus.signal_subscribe(DESKTOP, "org.freedesktop.portal.Request", "Response", path, None,
                                                  Gio.DBusSignalFlags.NONE, response)
        try:
            self._bus.call_sync(DESKTOP, DESKTOP_PATH, SCREENCAST, method, build(token), None,
                                Gio.DBusCallFlags.NONE, 10000, None)
            if not self._closing.is_set():
                self._loop.run()
        finally:
            self._bus.signal_unsubscribe(subscription)
        if not answer:                  # close() while the dialog was up
            self._bus.call(DESKTOP, path, "org.freedesktop.portal.Request", "Close", None, None,
                           Gio.DBusCallFlags.NONE, 2000, None, None)
            raise Declined("closed")
        if answer["code"] == 1:
            raise Declined("the user said no")
        if answer["code"] != 0:
            raise ScreenError(_("The system refused to share the screen"))
        return answer["results"]

    def _open(self) -> Source:
        from gi.repository import Gio, GLib
        self._session = self._portal("CreateSession", lambda t: GLib.Variant("(a{sv})", ({
            "handle_token": GLib.Variant("s", t),
            "session_handle_token": GLib.Variant("s", t + "s")},)))["session_handle"]
        self._portal("SelectSources", lambda t: GLib.Variant("(oa{sv})", (self._session, {
            "handle_token": GLib.Variant("s", t),
            "types": GLib.Variant("u", SOURCE_MONITOR | SOURCE_WINDOW),
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", CURSOR_EMBEDDED)})))
        results = self._portal("Start", lambda t: GLib.Variant("(osa{sv})", (self._session, "", {
            "handle_token": GLib.Variant("s", t)})))
        streams = results.get("streams") or []
        if not streams:
            raise ScreenError(_("The system shared nothing"))
        node, props = streams[0]
        reply, fds = self._bus.call_with_unix_fd_list_sync(
            DESKTOP, DESKTOP_PATH, SCREENCAST, "OpenPipeWireRemote", GLib.Variant("(oa{sv})", (self._session, {})),
            None, Gio.DBusCallFlags.NONE, 10000, None, None)
        fd = fds.get(reply.unpack()[0])
        width, height = props.get("size") or (0, 0)
        kind = "window" if props.get("source_type") == SOURCE_WINDOW else "screen"
        return Source(kind, node, node_serial(node, fd), fd, width, height)


def node_serial(node: int, fd: int) -> str:
    """The object.serial of the portal's node. Through the portal's own
    connection, which is all there is inside the Flatpak: pw-dump cannot reach
    PipeWire from the sandbox, and without a target pipewiresrc fails with
    "target not found" [V 03/11/2026]. pw-dump, on the host, if that gave nothing;
    "" when nothing knows."""
    return _serial_from_provider(node, fd) or serial_from_dump(_pw_dump(), node)


def _serial_from_provider(node: int, fd: int) -> str:
    Gst = transcode._gst()
    factory = Gst.DeviceProviderFactory.find("pipewiredeviceprovider")
    provider = factory.get() if factory else None
    if provider is None or provider.find_property("fd") is None:
        return ""
    provider.set_property("fd", os.dup(fd))
    if not provider.start():
        return ""
    try:
        return serial_from_devices(provider.get_devices(), node)
    finally:
        provider.stop()


def serial_from_devices(devices, node: int) -> str:
    """A GstPipeWireDevice has the node's id and serial as properties of its own (its `properties` hold them as text)."""
    for device in devices:
        if device.find_property("id") and device.find_property("serial") and device.get_property("id") == node:
            return str(device.get_property("serial"))
    return ""


def _pw_dump() -> str:
    try:
        return subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def serial_from_dump(dump: str, node: int) -> str:
    try:
        objects = json.loads(dump)
    except ValueError:
        return ""
    for entry in objects if isinstance(objects, list) else []:
        if entry.get("id") == node:
            serial = ((entry.get("info") or {}).get("props") or {}).get("object.serial")
            return "" if serial is None else str(serial)
    return ""


def source_chain(source: Source) -> str:
    """Where the frames come from. A copy of the descriptor per pipeline: the element closes what it is given."""
    target = f" target-object={source.serial}" if source.serial else ""
    return f"pipewiresrc fd={os.dup(source.fd)}{target} do-timestamp=true"


def output_size(source: Source) -> tuple[int, int]:
    """What the TV receives. A window goes out at the TV's whole size, with
    borders where it does not fill it (keep_shape): the size the portal gives
    for one is not usable on niri (it fitted to 0x0 [V 03/11/2026]), and a
    window can change it at any moment anyway."""
    if source.kind == "window":
        return transcode.MAX_W, transcode.MAX_H
    return transcode.fit(source.width, source.height)


def letterbox(width: int, height: int, out_width: int, out_height: int) -> tuple[int, int, int, int]:
    """Where a picture goes in the output, whole and undistorted, centred:
    (x, y, width, height), in even numbers, as NV12 wants."""
    if width <= 0 or height <= 0:
        return 0, 0, out_width, out_height
    scale = min(out_width / width, out_height / height)
    w, h = max(2, int(width * scale) & ~1), max(2, int(height * scale) & ~1)
    return (out_width - w) // 2 & ~1, (out_height - h) // 2 & ~1, w, h


def keep_shape(pipeline, width: int, height: int) -> None:
    """vapostproc's add-borders does nothing on the Radeon 760M: an 800x800
    picture came out stretched to 16:9 [V 03/11/2026]. So the compositor's pad
    places and scales the picture. The size is read where the frames enter
    vapostproc: when a window changes size vapostproc goes on giving out its
    first size, with the new picture stretched into it, so what reaches the
    compositor never says it changed. Scaling that box to the new shape puts
    the proportions back."""
    mix, shape = pipeline.get_by_name("mix"), pipeline.get_by_name("shape")
    if mix is None or shape is None:
        return
    pad = mix.get_static_pad("sink_1")

    def caps_changed(entry, _param):
        caps = entry.get_current_caps()
        if caps is None:
            return
        structure = caps.get_structure(0)
        x, y, w, h = letterbox(structure.get_int("width")[1], structure.get_int("height")[1], width, height)
        for name, value in (("xpos", x), ("ypos", y), ("width", w), ("height", h)):
            pad.set_property(name, value)
    shape.get_static_pad("sink").connect("notify::caps", caps_changed)


def has_gpu_chain() -> bool:
    Gst = transcode._gst()
    return all(Gst.ElementFactory.find(name) for name in ("vacompositor", "vapostproc", "vah264enc"))


def describe(source: str, width: int, height: int, sound: str, gpu: bool) -> str:
    """The pipeline, ending in an appsink named "out". `sound` is the
    PulseAudio device to capture: "" for no sound."""
    tail = "h264parse config-interval=-1 ! video/x-h264,profile=high ! mux.video_0"
    if gpu:
        memory = f"video/x-raw(memory:VAMemory),format=NV12,width={width},height={height}"
        picture = (f"vacompositor name=mix latency=0 ! {memory},framerate={FPS}/1 ! "
                   f"vah264enc rate-control=cbr bitrate={BITRATE_KBPS} key-int-max={FPS} b-frames=0 ! {tail} "
                   f"videotestsrc is-live=true pattern=black ! "
                   f"video/x-raw,width={width},height={height},framerate={FPS}/1 ! vapostproc ! mix.sink_0 "
                   f"{source} ! vapostproc name=shape ! video/x-raw(memory:VAMemory),format=NV12 ! mix.sink_1")
    else:
        encoder = transcode.h264_encoder()
        if encoder is None:
            raise ScreenError(_("No H.264 encoder is installed"))
        picture = (f"{source} ! videoconvert ! videoscale add-borders=true ! videorate ! "
                   f"video/x-raw,format=NV12,width={width},height={height},framerate={FPS}/1 ! "
                   f"{encoder[0]} {encoder[1]} ! {tail}")
    out = f"matroskamux name=mux streamable=true ! appsink name=out sync=false max-buffers=200 {picture}"
    if not sound:
        return out
    raw = f"audio/x-raw,rate={SOUND_RATE},channels=2"
    return (f"{out} audiomixer name=sound latency=100000000 ! {raw} ! opusenc bitrate=128000 ! mux.audio_0 "
            f"audiotestsrc is-live=true wave=silence ! {raw} ! sound. "
            f"pulsesrc device={sound} do-timestamp=true ! audioconvert ! audioresample ! {raw} ! sound.")


def stream(description: str, out, job: transcode.Job, size: tuple[int, int] = (0, 0)) -> None:
    """Write the live stream of `size` into `out` until job.stop(), the TV
    stops reading (BrokenPipeError goes up to the server, which expects it) or
    the chain fails."""
    Gst = transcode._gst()
    try:
        pipeline = Gst.parse_launch(description)
    except Exception as e:      # GLib.Error, or a missing element: said in words, GStreamer's own in the log
        raise ScreenError(_("The screen cannot be converted")) from e
    job.pipeline, job.stopped = pipeline, job.stopped or threading.Event()
    keep_shape(pipeline, *size)
    sink = pipeline.get_by_name("out")
    bus = pipeline.get_bus()
    silent = 0.0
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise ScreenError(transcode._bus_error(bus) or _("The screen cannot be converted"))
        while not job.stopped.is_set():
            sample = sink.emit("try-pull-sample", Gst.SECOND // 2)
            if sample is None:
                error = transcode._bus_error(bus)
                silent += 0.5
                if error or silent >= STALL_S:
                    raise ScreenError(error or _("The screen stopped arriving"))
                continue
            silent = 0.0
            buffer = sample.get_buffer()
            ok, mapped = buffer.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                out.write(mapped.data)
            finally:
                buffer.unmap(mapped)
    finally:
        pipeline.set_state(Gst.State.NULL)


def show_on(session, grant: Grant, sound: str = "") -> None:
    """Ask for the screen and put it on the session's TV: blocks while the
    user answers the portal's dialog. Declined when they say no."""
    source = grant.request()
    width, height = output_size(source)
    gpu = has_gpu_chain()

    def producer(out, job):
        stream(describe(source_chain(source), width, height, sound, gpu), out, job, (width, height))
    title = _("A Window") if source.kind == "window" else _("The Screen")
    session.show_live(source.kind, title, producer, grant.close)

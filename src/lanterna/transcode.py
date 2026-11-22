"""The live ways of media.py: a film repacked or re-encoded on the fly into
Matroska, written straight into the TV's HTTP response.

- AUDIO: `parsebin` splits the file into its streams without decoding them;
  the picture goes into the new file as it is, the first sound track is
  decoded and encoded again. A film keeps its full picture quality and the
  computer does little.
- TRANSCODE: `decodebin` decodes everything; the picture is encoded again
  in H.264, on the GPU through VAAPI when it can (`vah264enc`), otherwise
  with a software encoder if one is installed; larger than 1920x1080 is
  scaled down, the most the TV's H.264 decoder takes at level 4.2.

The sound is encoded in Opus: its encoder is in every GStreamer, the
Flatpak runtime included, while AAC encoders depend on extra codecs; the
Philips receiver played H.264 with Opus in Matroska streamed live
[V 25/10/2026]. Subtitles and further sound tracks are dropped.

The stream is live (no index, no seeking): the session seeks by starting a
new stream at an offset (`start_s`), which these pipelines honour with a
seek before they play. The pipeline is pushed by the TV's reading: the
appsink blocks when the TV's buffer is full, so a film is not encoded
faster than it is watched, beyond a few seconds of lead.
"""

from __future__ import annotations

import threading
import time
from itertools import pairwise
from collections.abc import Callable
from dataclasses import dataclass

from .media import MediaInfo, Way

MAX_W, MAX_H = 1920, 1080
VIDEO_BITRATE_KBPS = 10000
AUDIO_BITRATE = 192000
START_TIMEOUT_S = 20                # nothing out of the pipeline in this long: it is not going to start
PARSERS ={"video/x-h264": "h264parse", "video/x-h265": "h265parse", "video/x-vp8": "", "video/x-vp9": "vp9parse"}


class TranscodeError(Exception):
    """The pipeline could not be built or stopped with an error."""


def _gst():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    return Gst


def h264_encoder() -> tuple[str, str] | None:
    """The best H.264 encoder here, as (element, properties): the GPU first."""
    return next(iter(h264_encoders()), None)


def h264_encoders() -> list[tuple[str, str]]:
    """The H.264 encoders installed here, as (element, properties), the GPU's first."""
    Gst = _gst()
    choices = (
        ("vah264enc", f"bitrate={VIDEO_BITRATE_KBPS} key-int-max=60 b-frames=0 rate-control=vbr"),
        ("vah264lpenc", f"bitrate={VIDEO_BITRATE_KBPS} key-int-max=60 rate-control=vbr"),
        ("x264enc", f"bitrate={VIDEO_BITRATE_KBPS} key-int-max=60 speed-preset=veryfast tune=zerolatency"),
        ("openh264enc", f"bitrate={VIDEO_BITRATE_KBPS * 1000} gop-size=60 complexity=low"),
    )
    return [choice for choice in choices if Gst.ElementFactory.find(choice[0])]


def software_h264_encoder() -> tuple[str, str] | None:
    return next((choice for choice in h264_encoders() if not choice[0].startswith("va")), None)


def gpu_takes(caps) -> bool:
    """Frames vapostproc is known to take: already on the GPU, or plain 8-bit
    4:2:0 from a software decoder (an MPEG-4 film, shown on the TV [V 03/11/2026])."""
    features = caps.get_features(0)
    if features.contains("memory:VAMemory") or features.contains("memory:DMABuf"):
        return True
    return caps.get_structure(0).get_string("format") in ("I420", "YV12")


def fit(width: int, height: int, max_w: int = MAX_W, max_h: int = MAX_H) -> tuple[int, int]:
    """The picture's size scaled down to fit max_w x max_h, aspect kept,
    even numbers (the encoders want them); unchanged when it already fits."""
    if width <= 0 or height <= 0:
        return max_w, max_h
    scale = min(1.0, max_w / width, max_h / height)
    w, h = int(width * scale), int(height * scale)
    w, h = w - w % 2, h - h % 2
    return (w, h) if w and h else (max_w, max_h)       # 1x1 is a placeholder, and 0x0 builds no pipeline


@dataclass
class Job:
    """One stream being produced; stop() ends it from any thread."""
    pipeline: object = None
    stopped: threading.Event = None
    error: str = ""

    def stop(self) -> None:
        if self.stopped is not None:
            self.stopped.set()


def build(path: str, way: Way, info: MediaInfo, start_s: float = 0.0):
    """The pipeline for `way`, ending in an appsink named "out". Returns
    (pipeline, appsink). The dynamic part (which stream goes where) is
    wired when the demuxer announces its pads."""
    Gst = _gst()
    if way not in (Way.AUDIO, Way.TRANSCODE):
        raise TranscodeError(f"{way.value} is not a live way")
    pipeline = Gst.Pipeline.new("lanterna-live")

    def make(factory: str, props: str = ""):
        element = Gst.ElementFactory.make(factory, None)
        if element is None:
            raise TranscodeError(f"GStreamer element {factory} is missing")
        for prop in props.split():
            key, value = prop.split("=", 1)
            Gst.util_set_object_arg(element, key, value)
        pipeline.add(element)
        return element

    src = make("filesrc")
    src.set_property("location", path)
    demux = make("parsebin" if way is Way.AUDIO else "decodebin")
    mux = make("matroskamux", "streamable=true")
    sink = make("appsink", "sync=false max-buffers=64 emit-signals=false")
    src.link(demux)
    mux.link(sink)
    encoder = h264_encoder() if way is Way.TRANSCODE else None
    if way is Way.TRANSCODE and encoder is None:
        raise TranscodeError("no H.264 encoder is installed")
    # The muxer's pads are asked for before any data flows: matroskamux writes
    # its header with the tracks it has when the first buffer arrives, so a
    # sound track that shows up later (out of its own decoder) would be left
    # out [V 25/10/2026]. A track the file turns out not to have is released
    # when the demuxer says it has no more.
    targets = {"video": mux.request_pad_simple("video_%u") if info.video else None,
               "audio": mux.request_pad_simple("audio_%u") if info.audio else None}
    wired = {"video": False, "audio": False}
    stream_pads = []                    # the demuxer's own outputs: where a seek has to enter
    holds = {}                          # pad name: (pad, probe id) dropping data until the seek
    all_pads = threading.Event()        # every stream the probe announced is wired
    flowing = threading.Event()         # the demuxer is past the file's headers: it can take a seek

    def drop_until_seek(pad, info):
        if info.type & (Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST):
            flowing.set()
            return Gst.PadProbeReturn.DROP
        event = info.get_event()
        if event is not None and event.type == Gst.EventType.EOS:
            flowing.set()
            return Gst.PadProbeReturn.DROP
        if event is not None and event.type == Gst.EventType.FLUSH_STOP:
            holds.pop(pad.get_name(), None)
            return Gst.PadProbeReturn.REMOVE    # what comes next is from the new point: its first key frame included
        return Gst.PadProbeReturn.OK

    def check_all_wired():
        # parsebin never emits no-more-pads (decodebin does) [V GStreamer 1.28]: count the streams instead
        if all(wired[kind] for kind, target in targets.items() if target is not None):
            all_pads.set()

    def link_chain(kind, pad, steps):
        """pad -> steps... -> queue -> the muxer's `kind` pad."""
        elements = [make(f, p) for f, p in steps] + [make("queue", "max-size-time=4000000000 max-size-bytes=0")]
        for element in elements:
            element.sync_state_with_parent()
        if pad.link(elements[0].get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            raise TranscodeError(f"could not link the {kind} stream")
        for a, b in pairwise(elements):
            a.link(b)
        elements[-1].get_static_pad("src").link(targets[kind])

    def drop(pad):
        # Not into a fakesink: a sink holds its first buffer until the pipeline plays, whatever its async says, and
        # the pipeline plays once the appsink has something. That something needs the sound, which comes through
        # the demuxer's one thread, stopped in the fakesink of the second sound track [V 04/11/2026].
        pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, lambda _p, _i: Gst.PadProbeReturn.DROP)

    def report(error: TranscodeError):
        pipeline.get_bus().post(Gst.Message.new_application(pipeline, Gst.Structure.new_from_string(
            f'lanterna-error, text=(string)"{error}"')))

    def software_steps(chosen):
        w, h = fit(info.width, info.height)
        return [("videoconvertscale", ""), ("capsfilter", f"caps=video/x-raw,format=I420,width={w},height={h}"),
                chosen, ("h264parse", "config-interval=-1")]

    def video_steps(name, caps):
        if way is Way.AUDIO:
            parser = PARSERS.get(name)
            if parser == "h264parse":
                return [(parser, "config-interval=-1")]
            return [(parser, "")] if parser else []
        w, h = fit(info.width, info.height)
        if encoder[0].startswith("va"):
            if not gpu_takes(caps):
                # 10-bit and 4:4:4 H.264, which the TV refuses, come out of the software decoder in formats
                # vapostproc does not negotiate. Converted to NV12 first, they made the VCN encoder fault on
                # the Radeon 760M: "ring vcn_unified_0 timeout", a ring reset, the process aborted
                # [V 03/11/2026]. So they stay away from the GPU altogether.
                software = software_h264_encoder()
                if software is None:
                    raise TranscodeError("this video needs a software H.264 encoder, and none is installed")
                return software_steps(software)
            return [("vapostproc", ""),
                    ("capsfilter", f"caps=video/x-raw(memory:VAMemory),format=NV12,width={w},height={h}"),
                    encoder, ("h264parse", "config-interval=-1")]
        return software_steps(encoder)

    audio_steps = [("audioconvert", ""), ("audioresample", ""), ("opusenc", f"bitrate={AUDIO_BITRATE}")]

    def on_pad(_element, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        name = caps.get_structure(0).get_name()
        stream_pads.append(pad)
        if start_s > 0:
            holds[pad.get_name()] = (pad, pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST
                                                        | Gst.PadProbeType.EVENT_DOWNSTREAM
                                                        | Gst.PadProbeType.EVENT_FLUSH, drop_until_seek))
        try:
            if name.startswith("video/") and targets["video"] is not None and not wired["video"]:
                wired["video"] = True
                link_chain("video", pad, video_steps(name, caps))
                check_all_wired()
            elif name.startswith("audio/") and targets["audio"] is not None and not wired["audio"]:
                wired["audio"] = True
                if way is Way.AUDIO:
                    decode_then_chain(pad)      # parsebin hands the sound still encoded
                else:
                    link_chain("audio", pad, audio_steps)
                check_all_wired()
            else:
                drop(pad)                       # subtitles, further sound tracks, a cover picture
        except TranscodeError as e:
            report(e)

    def decode_then_chain(pad):
        decoder = make("decodebin")
        decoder.sync_state_with_parent()
        pad.link(decoder.get_static_pad("sink"))

        def decoded(_d, raw_pad):
            if not raw_pad.query_caps(None).get_structure(0).get_name().startswith("audio/"):
                drop(raw_pad)
                return
            try:
                link_chain("audio", raw_pad, audio_steps)
            except TranscodeError as e:
                report(e)
        decoder.connect("pad-added", decoded)

    def no_more_pads(_element):
        for kind, target in targets.items():
            if target is not None and not wired[kind]:
                mux.release_request_pad(target)
                targets[kind] = None
        all_pads.set()

    def seek(start_s: float) -> bool:
        """A seek sent into the demuxer through one of its outputs: sent to
        the pipeline it would go up from the appsink and stop at the muxer,
        which does not seek. To a key frame at or before start_s: the AUDIO
        way copies the picture and can only start on one."""
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT | Gst.SeekFlags.SNAP_BEFORE
        event = Gst.Event.new_seek(1.0, Gst.Format.TIME, flags, Gst.SeekType.SET, int(start_s * Gst.SECOND),
                                   Gst.SeekType.NONE, -1)
        sent = any(pad.send_event(event) for pad in stream_pads[:1])
        for pad, probe in list(holds.values()):     # those the seek's flush has not reached
            pad.remove_probe(probe)
        holds.clear()
        return sent

    demux.connect("no-more-pads", no_more_pads)
    demux.connect("pad-added", on_pad)
    return pipeline, sink, seek, all_pads, flowing


def stream(path: str, way: Way, info: MediaInfo, out, start_s: float = 0.0,
           job: Job | None = None, on_error: Callable[[str], None] | None = None) -> None:
    """Produce the live stream into `out` (a file-like object) until the
    film ends, the TV stops reading (BrokenPipeError goes up to the server,
    which expects it) or job.stop()."""
    Gst = _gst()
    pipeline, sink, seek, all_pads, flowing = build(path, way, info, start_s)
    job = job or Job()
    job.pipeline, job.stopped = pipeline, job.stopped or threading.Event()
    bus = pipeline.get_bus()
    try:
        if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            raise TranscodeError(_bus_error(bus) or "the pipeline did not start")
        if start_s > 0:
            if not all_pads.wait(15):
                raise TranscodeError(_bus_error(bus) or "the film's streams did not show up")
            # parsebin shows the streams while matroskademux is still reading the headers. A seek that comes then
            # waits for the first cluster and is carried out in the middle of reading it: the bytes of that cluster's
            # header are skipped at the new place too. Where the header there is of another length, and FFmpeg writes
            # them as short as the cluster allows, the demuxer goes on off by a byte and stops: "Could not demultiplex
            # stream", or "Stream doesn't contain enough data" [V 04/11/2026, one seek in ten of a real film].
            if not flowing.wait(15):
                raise TranscodeError(_bus_error(bus) or "the film's data did not arrive")
            if not seek(start_s):
                raise TranscodeError("the film cannot be started from that point")
        pipeline.set_state(Gst.State.PLAYING)
        deadline = time.monotonic() + START_TIMEOUT_S
        while not job.stopped.is_set():
            sample = sink.emit("try-pull-sample", Gst.SECOND // 2)
            if sample is None:
                if sink.get_property("eos"):
                    return
                error = _bus_error(bus)
                if error:
                    raise TranscodeError(error)
                if deadline is not None and time.monotonic() > deadline:
                    raise TranscodeError("the film's stream did not start")
                continue
            deadline = None
            buffer = sample.get_buffer()
            ok, mapped = buffer.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                out.write(mapped.data)
            finally:
                buffer.unmap(mapped)
    except TranscodeError as e:
        job.error = str(e)
        if on_error:
            on_error(job.error)
        raise
    finally:
        pipeline.set_state(Gst.State.NULL)


def _bus_error(bus) -> str:
    Gst = _gst()
    while True:
        message = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.APPLICATION)
        if message is None:
            return ""
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            return error.message
        structure = message.get_structure()
        if structure and structure.get_name() == "lanterna-error":
            return structure.get_string("text") or "the pipeline failed"

"""What a file is, and how to get it onto the TV.

The Default Media Receiver plays what a Chromecast's browser engine plays
(Google, "Supported media for Google Cast"): images (JPEG, PNG, GIF, WebP,
BMP), H.264 up to High profile, VP8 and VP9, and on newer receivers HEVC;
AAC, MP3, Opus, Vorbis, FLAC and PCM audio; MP4, WebM and Matroska files.
Matroska is not on Google's list, but the Philips' receiver played H.264 in
Matroska during the proof of concept [V 25/10/2026].

So every file gets a plan: an ordered list of ways to send it, tried one
after the other when the TV refuses the previous one.

- DIRECT: the file as it is, served with byte ranges: full quality, seeking,
  nothing for this computer to do.
- AUDIO: the picture copied as it is, only the sound re-encoded (the usual
  case of a film in Matroska with AC-3, DTS or TrueHD, which a receiver
  without audio passthrough refuses).
- TRANSCODE: picture and sound re-encoded, on the GPU when it can.

The last two are produced live: the TV cannot seek in them. `plan()` is pure
and tested on its own; `probe()` asks GStreamer's discoverer what a file
holds.
"""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass, field

from .i18n import _


class Way(enum.Enum):
    DIRECT = "direct"
    AUDIO = "audio"
    TRANSCODE = "transcode"
    SCREEN = "screen"


class ProbeError(Exception):
    """The file could not be read as media."""


@dataclass(frozen=True)
class MediaInfo:
    container: str = ""             # short names, see CONTAINERS
    video: str = ""                 # "" when there is no picture
    video_profile: str = ""
    audio: tuple[str, ...] = ()
    image: bool = False
    duration_s: float | None = None
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class Plan:
    ways: tuple[Way, ...]
    content_type: str               # for DIRECT; the live ways send Matroska
    reason: str = ""                # why not DIRECT, in words
    notes: tuple[str, ...] = field(default_factory=tuple)


LIVE_CONTENT_TYPE = "video/x-matroska"

# GStreamer caps name -> short name. Containers first, then streams.
CONTAINERS = {
    "video/quicktime": "mp4", "video/webm": "webm", "video/x-matroska": "matroska",
    "video/x-msvideo": "avi", "video/mpegts": "mpegts", "video/x-ms-asf": "asf", "application/ogg": "ogg",
    "video/x-flv": "flv", "audio/x-wav": "wav", "audio/x-flac": "flac", "application/x-id3": "id3",
    "audio/mpeg": "mpeg-audio", "audio/x-m4a": "m4a",
    "image/jpeg": "jpeg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp",
}
VIDEO = {
    "video/x-h264": "h264", "video/x-h265": "hevc", "video/x-vp8": "vp8", "video/x-vp9": "vp9",
    "video/x-av1": "av1", "video/x-divx": "mpeg4", "video/x-xvid": "mpeg4", "video/x-wmv": "wmv",
    "video/x-theora": "theora", "video/x-msmpeg": "msmpeg",
}
AUDIO = {
    "audio/x-opus": "opus", "audio/x-vorbis": "vorbis", "audio/x-flac": "flac", "audio/x-ac3": "ac3",
    "audio/x-eac3": "eac3", "audio/x-dts": "dts", "audio/x-true-hd": "truehd", "audio/x-raw": "pcm",
    "audio/x-wav": "pcm",           # the discoverer names a WAV's stream after its container [V GStreamer 1.28]
    "audio/x-wma": "wma", "audio/x-alac": "alac",
}

IMAGES = {"jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}
VIDEO_CONTAINERS = {"mp4": "video/mp4", "webm": "video/webm", "matroska": "video/x-matroska"}
AUDIO_FILES = {"mpeg-audio": "audio/mpeg", "id3": "audio/mpeg", "m4a": "audio/mp4", "mp4": "audio/mp4",
               "flac": "audio/flac", "wav": "audio/wav", "ogg": "audio/ogg", "webm": "audio/webm"}
GOOD_VIDEO = {"h264", "vp8", "vp9"}
MAYBE_VIDEO = {"hevc"}              # newer receivers only: tried as it is, re-encoded if refused
GOOD_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac", "pcm"}
BAD_H264_PROFILES = {"high-10", "high-10-intra", "high-4:2:2", "high-4:2:2-intra", "high-4:4:4",
                     "high-4:4:4-intra"}


def parse_caps(text: str) -> tuple[str, dict[str, str]]:
    """'video/x-h264, profile=(string)high, level=(string)4.1' -> name and
    fields, without GStreamer (the tests and the CLI run without it)."""
    head, *rest = [p.strip() for p in text.split(",")]
    fields = {}
    for part in rest:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if value.startswith("(") and ")" in value:
            value = value[value.index(")") + 1:]
        fields[key.strip()] = value.strip().strip('"')
    return head, fields


def short_codec(caps: str) -> tuple[str, str]:
    """A stream's caps as a short codec name and its profile."""
    name, fields = parse_caps(caps)
    if name == "audio/mpeg":
        version = fields.get("mpegversion", "1")
        if version in ("2", "4"):
            return "aac", fields.get("profile", "")
        return ("mp3" if fields.get("layer", "3") == "3" else "mp2"), ""
    if name == "video/mpeg":
        version = fields.get("mpegversion", "1")
        return {"1": "mpeg1", "2": "mpeg2", "4": "mpeg4"}.get(version, "mpeg"), ""
    short = VIDEO.get(name) or AUDIO.get(name) or name
    return short, fields.get("profile", "")


def plan(info: MediaInfo) -> Plan:
    """The ordered ways to show `info` on a Default Media Receiver."""
    if info.image:
        kind = IMAGES.get(info.container)
        if kind:
            return Plan((Way.DIRECT,), kind)
        return Plan((), "", _("The TV does not show this kind of image"))

    audio_ok = all(a in GOOD_AUDIO for a in info.audio)

    if not info.video:
        kind = AUDIO_FILES.get(info.container)
        if kind and info.audio and audio_ok:
            return Plan((Way.DIRECT,), kind)
        if info.audio:
            return Plan((Way.TRANSCODE,), LIVE_CONTENT_TYPE, _("The TV does not play this sound format"))
        return Plan((), "", _("Nothing to show in this file"))

    container_type = VIDEO_CONTAINERS.get(info.container)
    video_ok = info.video in GOOD_VIDEO and not (info.video == "h264" and info.video_profile in BAD_H264_PROFILES)
    video_maybe = info.video in MAYBE_VIDEO
    notes = []
    if info.video == "h264" and info.video_profile in BAD_H264_PROFILES:
        notes.append(f"H.264 {info.video_profile} is beyond what the TV decodes")

    if container_type and (video_ok or video_maybe):
        if audio_ok:
            ways = (Way.DIRECT, Way.TRANSCODE) if video_maybe else (Way.DIRECT, Way.AUDIO)
            return Plan(ways, container_type, "", tuple(notes))
        return Plan((Way.AUDIO, Way.TRANSCODE), LIVE_CONTENT_TYPE,
                    _("The TV does not play this film's sound, so the sound is converted"), tuple(notes))
    if video_ok or video_maybe:
        # a good picture in a container the TV does not open (AVI, MPEG-TS, FLV): repacked live
        return Plan((Way.AUDIO, Way.TRANSCODE), LIVE_CONTENT_TYPE,
                    _("The TV does not open this kind of file, so the film is repacked"),
                    tuple(notes))
    return Plan((Way.TRANSCODE,), LIVE_CONTENT_TYPE,
                _("The TV does not play this kind of video, so the film is converted"), tuple(notes))


def probe(path: str, timeout_s: int = 15) -> MediaInfo:
    """What the file holds, from GStreamer's discoverer (which reads the
    headers, not the whole file)."""
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstPbutils", "1.0")
    from gi.repository import GLib, Gst, GstPbutils
    Gst.init(None)
    try:
        discoverer = GstPbutils.Discoverer.new(timeout_s * Gst.SECOND)
        result = discoverer.discover_uri(Gst.filename_to_uri(os.path.abspath(path)))
    except GLib.Error as e:
        raise ProbeError(e.message) from None
    top = result.get_stream_info()
    caps = top.get_caps() if top else None
    container = CONTAINERS.get(caps.get_structure(0).get_name(), "") if caps and caps.get_size() else ""
    video, profile, width, height, image = "", "", 0, 0, False
    for stream in result.get_video_streams():
        video, profile = short_codec(stream.get_caps().to_string())
        width, height = stream.get_width(), stream.get_height()
        image = stream.is_image()
        break
    if image and not container:
        container = video.split("/")[-1] if "/" in video else video
    audio = tuple(short_codec(s.get_caps().to_string())[0] for s in result.get_audio_streams())
    duration = result.get_duration()
    return MediaInfo(container=container, video="" if image else video, video_profile=profile, audio=audio,
                     image=image, duration_s=duration / Gst.SECOND if duration and duration > 0 else None,
                     width=width, height=height)

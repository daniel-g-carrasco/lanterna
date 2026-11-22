#!/usr/bin/env python3
"""What a file is and how it goes to the TV: the caps read without
GStreamer, the plan for the usual cases (a phone video, a film with AC-3,
an AVI, HEVC, a photo, a song), and GStreamer's own reading of real files
made on the spot."""

import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
import zlib
from pathlib import Path

import _setup  # noqa: F401

from lanterna import media
from lanterna.media import MediaInfo, Way


class CapsTest(unittest.TestCase):
    def test_caps_text_without_gstreamer(self):
        name, fields = media.parse_caps("video/x-h264, profile=(string)high, level=(string)4.1, width=(int)1920")
        self.assertEqual(name, "video/x-h264")
        self.assertEqual(fields, {"profile": "high", "level": "4.1", "width": "1920"})

    def test_codec_names(self):
        self.assertEqual(media.short_codec("audio/mpeg, mpegversion=(int)4, stream-format=(string)raw"), ("aac", ""))
        self.assertEqual(media.short_codec("audio/mpeg, mpegversion=(int)1, layer=(int)3"), ("mp3", ""))
        self.assertEqual(media.short_codec("video/x-h265, profile=(string)main-10"), ("hevc", "main-10"))
        self.assertEqual(media.short_codec("video/mpeg, mpegversion=(int)2"), ("mpeg2", ""))
        self.assertEqual(media.short_codec("audio/x-ac3, rate=(int)48000"), ("ac3", ""))


class PlanTest(unittest.TestCase):
    def test_a_phone_video_goes_as_it_is(self):
        plan = media.plan(MediaInfo("mp4", "h264", "high", ("aac",)))
        self.assertEqual((plan.ways[0], plan.content_type), (Way.DIRECT, "video/mp4"))

    def test_a_film_with_ac3_keeps_the_picture_and_converts_the_sound(self):
        plan = media.plan(MediaInfo("matroska", "h264", "high", ("ac3", "aac")))
        self.assertEqual(plan.ways, (Way.AUDIO, Way.TRANSCODE))
        self.assertEqual(plan.content_type, media.LIVE_CONTENT_TYPE)
        self.assertIn("sound", plan.reason)

    def test_hevc_is_tried_as_it_is_then_converted(self):
        plan = media.plan(MediaInfo("matroska", "hevc", "main", ("aac",)))
        self.assertEqual(plan.ways, (Way.DIRECT, Way.TRANSCODE))

    def test_an_avi_with_xvid_is_converted(self):
        plan = media.plan(MediaInfo("avi", "mpeg4", "", ("mp3",)))
        self.assertEqual(plan.ways, (Way.TRANSCODE,))

    def test_a_good_picture_in_an_avi_is_repacked_not_converted(self):
        plan = media.plan(MediaInfo("avi", "h264", "main", ("mp3",)))
        self.assertEqual(plan.ways[0], Way.AUDIO)

    def test_ten_bit_h264_is_converted(self):
        plan = media.plan(MediaInfo("matroska", "h264", "high-10", ("aac",)))
        self.assertEqual(plan.ways, (Way.TRANSCODE,))
        self.assertTrue(plan.notes)

    def test_photos_and_songs(self):
        self.assertEqual(media.plan(MediaInfo("jpeg", image=True)).content_type, "image/jpeg")
        self.assertEqual(media.plan(MediaInfo("id3", audio=("mp3",))).content_type, "audio/mpeg")
        self.assertEqual(media.plan(MediaInfo("flac", audio=("flac",))).ways, (Way.DIRECT,))
        self.assertEqual(media.plan(MediaInfo("asf", audio=("wma",))).ways, (Way.TRANSCODE,))

    def test_what_cannot_be_shown_says_why(self):
        empty = media.plan(MediaInfo("matroska"))
        self.assertEqual(empty.ways, ())
        self.assertTrue(empty.reason)
        tiff = media.plan(MediaInfo("tiff", image=True))
        self.assertEqual(tiff.ways, ())


def gst_or_skip(*elements):
    try:
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstPbutils", "1.0")
        from gi.repository import Gst, GstPbutils  # noqa: F401
    except (ImportError, ValueError):
        raise unittest.SkipTest("GStreamer with pbutils") from None
    Gst.init(None)
    for element in elements:
        if Gst.ElementFactory.find(element) is None:
            raise unittest.SkipTest(f"GStreamer element {element}")


def png(path: Path, width=8, height=6):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + b"\x80\x40\x20" * width for _ in range(height))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_a_png_is_an_image(self):
        gst_or_skip("pngdec")
        path = self.dir / "photo.png"
        png(path)
        info = media.probe(str(path))
        self.assertTrue(info.image)
        self.assertEqual((info.container, info.width, info.height), ("png", 8, 6))
        self.assertEqual(media.plan(info).content_type, "image/png")

    def test_a_wav_is_sound_only(self):
        gst_or_skip("wavparse")
        path = self.dir / "beep.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x10" * 8000)
        info = media.probe(str(path))
        self.assertEqual((info.container, info.video, info.audio), ("wav", "", ("pcm",)))
        self.assertAlmostEqual(info.duration_s, 1.0, places=1)
        self.assertEqual(media.plan(info).ways, (Way.DIRECT,))

    def test_a_webm_video_goes_as_it_is(self):
        gst_or_skip("vp8enc", "webmmux", "videotestsrc")
        path = self.dir / "clip.webm"
        subprocess.run(["gst-launch-1.0", "-q", "videotestsrc", "num-buffers=15", "!",
                        "video/x-raw,width=64,height=48,framerate=15/1", "!", "vp8enc", "!", "webmmux", "!",
                        "filesink", f"location={path}"], check=True, timeout=60)
        info = media.probe(str(path))
        self.assertEqual((info.container, info.video, info.width), ("webm", "vp8", 64))
        self.assertEqual(media.plan(info).content_type, "video/webm")

    def test_a_file_that_is_not_media_is_an_error_with_words(self):
        gst_or_skip()
        path = self.dir / "notes.txt"
        path.write_text("just words")
        with self.assertRaises(media.ProbeError):
            media.probe(str(path))


if __name__ == "__main__":
    unittest.main()

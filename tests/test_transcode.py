#!/usr/bin/env python3
"""The live ways: a film with AC-3 sound keeps its picture and gets Opus
sound, an AVI with MPEG-4 and MP3 becomes H.264 and Opus, a stream started
at an offset is shorter, and a size larger than the TV takes is fitted.
The inputs are made on the spot with GStreamer; the outputs are read back
with the same discoverer the app uses."""

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _setup  # noqa: F401

from lanterna import media, transcode
from lanterna.media import Way

FILM = Path(__file__).parent / "data" / "two-cluster-sizes-two-sound-tracks.mkv"


class FitTest(unittest.TestCase):
    def test_sizes_that_fit_stay(self):
        self.assertEqual(transcode.fit(1280, 720), (1280, 720))
        self.assertEqual(transcode.fit(1920, 1080), (1920, 1080))

    def test_larger_pictures_shrink_with_their_aspect(self):
        self.assertEqual(transcode.fit(3840, 2160), (1920, 1080))
        self.assertEqual(transcode.fit(2880, 1920), (1620, 1080))
        self.assertEqual(transcode.fit(4096, 1716), (1920, 804))

    def test_odd_results_are_made_even(self):
        w, h = transcode.fit(3001, 2001)
        self.assertEqual((w % 2, h % 2), (0, 0))


def need(*elements):
    try:
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstPbutils", "1.0")
        from gi.repository import Gst, GstPbutils  # noqa: F401
    except (ImportError, ValueError):
        raise unittest.SkipTest("GStreamer") from None
    Gst.init(None)
    for element in elements:
        if Gst.ElementFactory.find(element) is None:
            raise unittest.SkipTest(f"GStreamer element {element}")


def make(path: Path, video: list[str], audio: list[str], mux: str, seconds: int = 3):
    frames = seconds * 15
    samples = seconds * 48000 // 1024 + 1
    cmd = (["gst-launch-1.0", "-q", "-e", "videotestsrc", f"num-buffers={frames}", "!",
            "video/x-raw,width=320,height=240,framerate=15/1", "!", "videoconvert", "!"] + video
           + ["!", mux, "name=m", "!", "filesink", f"location={path}",
              "audiotestsrc", f"num-buffers={samples}", "samplesperbuffer=1024", "!", "audioconvert", "!",
              "audioresample", "!", "audio/x-raw,rate=48000,channels=2", "!"] + audio + ["!", "m."])
    subprocess.run(cmd, check=True, timeout=120, capture_output=True)


class TooSmallToBuildTest(unittest.TestCase):
    def test_a_placeholder_size_never_fits_to_nothing(self):
        self.assertEqual(transcode.fit(1, 1), (transcode.MAX_W, transcode.MAX_H))
        self.assertEqual(transcode.fit(0, 0), (transcode.MAX_W, transcode.MAX_H))
        self.assertEqual(transcode.fit(3000, 1), (transcode.MAX_W, transcode.MAX_H))


class GpuTakesTest(unittest.TestCase):
    """Formats sent to the GPU's encoder by mistake make it fault: the ring reset of 03/11/2026."""

    def caps(self, text):
        need()
        from gi.repository import Gst
        return Gst.Caps.from_string(text)

    def test_what_stays_on_the_gpu_and_plain_420_go_through_it(self):
        self.assertTrue(transcode.gpu_takes(self.caps("video/x-raw(memory:VAMemory),format=NV12")))
        self.assertTrue(transcode.gpu_takes(self.caps("video/x-raw,format=I420")))

    def test_high_bit_depth_and_444_from_software_stay_away_from_it(self):
        for pixel_format in ("I420_10LE", "Y444", "Y42B", "NV12"):
            self.assertFalse(transcode.gpu_takes(self.caps(f"video/x-raw,format={pixel_format}")), pixel_format)


class LiveWaysTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def run_way(self, source: Path, way: Way, start_s: float = 0.0) -> tuple[media.MediaInfo, int]:
        info = media.probe(str(source))
        out = io.BytesIO()
        transcode.stream(str(source), way, info, out, start_s=start_s)
        result = self.dir / f"out-{way.value}-{start_s}.mkv"
        result.write_bytes(out.getvalue())
        return media.probe(str(result)), len(out.getvalue())

    def test_a_film_with_ac3_keeps_its_picture_and_gets_opus(self):
        need("vp8enc", "avenc_ac3", "matroskamux", "parsebin", "opusenc", "avdec_ac3")
        source = self.dir / "film.mkv"
        make(source, ["vp8enc", "deadline=1"], ["avenc_ac3"], "matroskamux")
        self.assertEqual(media.probe(str(source)).audio, ("ac3",))
        self.assertEqual(media.plan(media.probe(str(source))).ways[0], Way.AUDIO)
        out, _ = self.run_way(source, Way.AUDIO)
        self.assertEqual((out.container, out.video, out.audio), ("matroska", "vp8", ("opus",)))
        self.assertEqual((out.width, out.height), (320, 240))

    def test_an_avi_with_mpeg4_and_mp3_becomes_h264_and_opus(self):
        need("avenc_mpeg4", "lamemp3enc", "avimux", "decodebin", "opusenc")
        if transcode.h264_encoder() is None:
            self.skipTest("no H.264 encoder")
        source = self.dir / "old.avi"
        make(source, ["avenc_mpeg4"], ["lamemp3enc"], "avimux")
        info = media.probe(str(source))
        self.assertEqual((info.container, info.video), ("avi", "mpeg4"))
        out, _ = self.run_way(source, Way.TRANSCODE)
        self.assertEqual((out.container, out.video, out.audio), ("matroska", "h264", ("opus",)))

    def test_h264_the_tv_refuses_is_converted_whatever_decodes_it(self):
        need("x264enc", "h264parse", "matroskamux", "avdec_h264", "videoconvertscale")
        for pixel_format, profile in (("I420_10LE", "high-10"), ("Y444", "high-4:4:4")):
            path = self.dir / f"{pixel_format}.mkv"
            subprocess.run(
                ["gst-launch-1.0", "-q", "videotestsrc", "num-buffers=30", "!",
                 f"video/x-raw,format={pixel_format},width=320,height=240,framerate=30/1", "!", "x264enc", "!",
                 "h264parse", "!", "matroskamux", "!", "filesink", f"location={path}"], check=True)
            info = media.probe(str(path))
            self.assertEqual(info.video_profile, profile)
            self.assertEqual(media.plan(info).ways, (Way.TRANSCODE,))
            out = io.BytesIO()
            transcode.stream(str(path), Way.TRANSCODE, info, out)
            self.assertEqual(out.getvalue()[:4], b"\x1a\x45\xdf\xa3", pixel_format)

    def test_starting_at_an_offset_gives_a_shorter_stream(self):
        need("vp8enc", "matroskamux", "parsebin", "opusenc", "vorbisenc")
        source = self.dir / "long.mkv"
        make(source, ["vp8enc", "deadline=1", "keyframe-max-dist=15"], ["vorbisenc"], "matroskamux", seconds=6)
        _, whole = self.run_way(source, Way.AUDIO)
        _, rest = self.run_way(source, Way.AUDIO, start_s=4.0)
        self.assertLess(rest, whole * 0.6)

    def test_a_direct_way_is_not_a_live_way(self):
        need()
        with self.assertRaises(transcode.TranscodeError):
            transcode.build("/nonexistent", Way.DIRECT, media.MediaInfo())  # noqa: B018

    def test_a_job_stopped_from_outside_ends_the_stream(self):
        need("vp8enc", "matroskamux", "parsebin", "opusenc", "vorbisenc")
        source = self.dir / "stop.mkv"
        make(source, ["vp8enc", "deadline=1"], ["vorbisenc"], "matroskamux", seconds=4)
        job = transcode.Job()

        class StopAfterFirstWrite(io.BytesIO):
            def write(self, data):
                job.stop()
                return super().write(data)

        out = StopAfterFirstWrite()
        transcode.stream(str(source), Way.AUDIO, media.probe(str(source)), out, job=job)
        self.assertGreater(len(out.getvalue()), 0)
        self.assertLess(len(out.getvalue()), source.stat().st_size)

    def test_a_stream_that_never_starts_is_said_and_does_not_wait_for_ever(self):
        need("appsrc", "appsink")
        Gst = transcode._gst()
        pipeline = Gst.parse_launch("appsrc ! appsink name=out")
        built = (pipeline, pipeline.get_by_name("out"), None, None, None)
        with (mock.patch.object(transcode, "build", return_value=built),
              mock.patch.object(transcode, "START_TIMEOUT_S", 1)):
            with self.assertRaises(transcode.TranscodeError):
                transcode.stream("/nothing", Way.AUDIO, media.MediaInfo(), io.BytesIO())


class SeekingInARealFilmTest(unittest.TestCase):
    """A film as FFmpeg writes it, the way the real ones failed on the TV: two sound tracks, and clusters whose headers
    are not all of one length. The small file is made with
    ffmpeg -f lavfi -i testsrc2=size=320x240:rate=10:duration=2 -f lavfi -i color=black:size=320x240:rate=10:duration=7
           -f lavfi -i anullsrc=r=48000:cl=mono -f lavfi -i anullsrc=r=48000:cl=mono
           -filter_complex "[0:v][1:v]concat=n=2:v=1[v]" -map "[v]" -map 2:a -map 3:a -t 9 -c:v libx264 -preset ultrafast
           -crf 32 -pix_fmt yuv420p -g 10 -c:a ac3 -b:a 32k -cluster_time_limit 1000 out.mkv
    The first two clusters have a header of 7 bytes, the others of 6."""

    def setUp(self):
        need("parsebin", "matroskademux", "h264parse", "matroskamux", "opusenc", "avdec_ac3")
        self.info = media.probe(str(FILM))

    def stream_from(self, start_s: float) -> bytes:
        out = io.BytesIO()
        with mock.patch.object(transcode, "START_TIMEOUT_S", 5):
            transcode.stream(str(FILM), Way.AUDIO, self.info, out, start_s=start_s)
        return out.getvalue()

    def test_the_rest_of_the_film_comes_out_from_every_point(self):
        for start in (0.0, 1.5, 3.5, 5.5, 7.5):
            with self.subTest(start=start):
                for _ in range(3):          # what went wrong depended on the threads as well as on the point
                    # the sound alone is 24 kB/s: an end that comes too soon is not the end of the film
                    self.assertGreater(len(self.stream_from(start)), (self.info.duration_s - start) * 20_000)

    def test_the_new_point_starts_on_a_key_frame(self):
        Gst = transcode._gst()
        for start in (1.5, 3.5, 5.5, 7.5):
            with self.subTest(start=start):
                path = Path(tempfile.mkdtemp()) / "out.mkv"
                self.addCleanup(shutil.rmtree, path.parent)
                path.write_bytes(self.stream_from(start))
                pipeline = Gst.parse_launch(f"filesrc location={path} ! matroskademux ! video/x-h264 ! appsink name=out")
                pipeline.set_state(Gst.State.PAUSED)
                sample = pipeline.get_by_name("out").emit("try-pull-preroll", 5 * Gst.SECOND)
                try:
                    self.assertIsNotNone(sample)
                    self.assertFalse(sample.get_buffer().has_flags(Gst.BufferFlags.DELTA_UNIT))
                finally:
                    pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    unittest.main()

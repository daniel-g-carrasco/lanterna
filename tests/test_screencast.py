#!/usr/bin/env python3
"""The screen: the chain as a description, a real run of it where the encoders
are, and the conversation with the portal against a fake one on a private bus
(Gio.TestDBus), never the real portal and its dialog."""

import io
import json
import os
import shutil
import threading
import time
import unittest
from unittest import mock

import _setup  # noqa: F401

from lanterna import screencast, transcode

SCREENCAST_XML = """
<node>
  <interface name="org.freedesktop.portal.ScreenCast">
    <method name="CreateSession"><arg type="a{sv}" direction="in"/><arg type="o" direction="out"/></method>
    <method name="SelectSources"><arg type="o" direction="in"/><arg type="a{sv}" direction="in"/>
      <arg type="o" direction="out"/></method>
    <method name="Start"><arg type="o" direction="in"/><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/>
      <arg type="o" direction="out"/></method>
    <method name="OpenPipeWireRemote"><arg type="o" direction="in"/><arg type="a{sv}" direction="in"/>
      <arg type="h" direction="out"/></method>
  </interface>
</node>"""
SESSION_XML = """
<node><interface name="org.freedesktop.portal.Session"><method name="Close"/></interface></node>"""
REQUEST_XML = """
<node><interface name="org.freedesktop.portal.Request"><method name="Close"/></interface></node>"""
SESSION = "/org/freedesktop/portal/desktop/session/test/s1"


class DescribeTest(unittest.TestCase):
    def test_the_gpu_chain_keeps_a_metronome_under_the_screen(self):
        text = screencast.describe("pipewiresrc fd=5", 1620, 1080, "", gpu=True)
        self.assertIn("vacompositor", text)
        self.assertIn("videotestsrc is-live=true pattern=black", text)
        self.assertIn("pipewiresrc fd=5 ! vapostproc name=shape ! video/x-raw(memory:VAMemory),format=NV12 ! mix.sink_1",
                      text)
        self.assertIn("width=1620,height=1080", text)
        self.assertIn('appsink name=out', text)
        self.assertNotIn("videoconvert", text)              # niri gives DMA-BUF only

    def test_without_a_device_there_is_no_sound_at_all(self):
        self.assertNotIn("audio", screencast.describe("s", 1920, 1080, "", gpu=True))

    def test_the_sound_is_mixed_with_silence_so_that_it_never_stops_the_picture(self):
        text = screencast.describe("s", 1920, 1080, "@DEFAULT_MONITOR@", gpu=True)
        self.assertIn("pulsesrc device=@DEFAULT_MONITOR@", text)
        self.assertIn("audiotestsrc is-live=true wave=silence", text)
        self.assertIn("mux.audio_0", text)

    def test_the_serial_of_the_node_is_read_from_the_dump(self):
        dump = json.dumps([{"id": 7, "info": {"props": {"object.serial": 1234}}}, {"id": 8}])
        self.assertEqual(screencast.serial_from_dump(dump, 7), "1234")
        self.assertEqual(screencast.serial_from_dump(dump, 8), "")
        self.assertEqual(screencast.serial_from_dump(dump, 9), "")
        self.assertEqual(screencast.serial_from_dump("not json", 7), "")

    def test_a_screen_keeps_its_shape_and_a_window_takes_the_whole_tv(self):
        def size(kind, width, height):
            return screencast.output_size(screencast.Source(kind, 1, "", -1, width, height))
        self.assertEqual(size("screen", 2880, 1920), (1620, 1080))
        for width, height in ((0, 0), (1, 1), (640, 480), (3000, 200)):
            self.assertEqual(size("window", width, height), (1920, 1080))

    def test_a_picture_is_placed_whole_and_undistorted(self):
        self.assertEqual(screencast.letterbox(1920, 1080, 1920, 1080), (0, 0, 1920, 1080))
        self.assertEqual(screencast.letterbox(864, 1792, 1920, 1080), (700, 0, 520, 1080))      # a tall window
        self.assertEqual(screencast.letterbox(3000, 500, 1920, 1080), (0, 380, 1920, 320))      # a wide one
        self.assertEqual(screencast.letterbox(2880, 1920, 1620, 1080), (0, 0, 1620, 1080))      # the screen
        self.assertEqual(screencast.letterbox(320, 240, 1920, 1080), (240, 0, 1440, 1080))      # a small one grows
        for size in ((1510, 1808), (1001, 777), (1, 1)):
            x, y, w, h = screencast.letterbox(*size, 1920, 1080)
            self.assertEqual([v % 2 for v in (x, y, w, h)], [0, 0, 0, 0], size)
            self.assertLessEqual(x + w, 1920)
            self.assertLessEqual(y + h, 1080)
        self.assertEqual(screencast.letterbox(0, 0, 1920, 1080), (0, 0, 1920, 1080))

    def test_the_serial_is_read_from_the_device_of_the_portals_node(self):
        class Device:
            def __init__(self, **props):
                self.props = props

            def find_property(self, name):
                return name in self.props

            def get_property(self, name):
                return self.props[name]
        devices = [Device(), Device(id=116, serial=1537), Device(id=117, serial=1540)]
        self.assertEqual(screencast.serial_from_devices(devices, 117), "1540")
        self.assertEqual(screencast.serial_from_devices(devices, 9), "")

    def test_the_source_gets_a_descriptor_of_its_own_and_the_serial(self):
        fd = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        chain = screencast.source_chain(screencast.Source("screen", 1, "99", fd, 0, 0))
        given = int(chain.split("fd=")[1].split()[0])
        self.addCleanup(os.close, given)
        self.assertNotEqual(given, fd)
        self.assertIn("target-object=99", chain)
        self.assertNotIn("target-object", screencast.source_chain(screencast.Source("screen", 1, "", fd, 0, 0))
                         .replace(f"fd={given}", ""))


class Stopper(io.BytesIO):
    """A TV that reads a few chunks and stops the job."""

    def __init__(self, job, enough):
        super().__init__()
        self.job, self.enough = job, enough

    def write(self, data):
        written = super().write(data)
        if self.tell() >= self.enough:
            self.job.stop()
        return written


def gst_or_skip(*elements):
    try:
        Gst = transcode._gst()
    except (ImportError, ValueError):
        raise unittest.SkipTest("GStreamer") from None
    for name in elements:
        if Gst.ElementFactory.find(name) is None:
            raise unittest.SkipTest(f"GStreamer element {name}")


class StreamTest(unittest.TestCase):
    TEST_SOURCE = "videotestsrc is-live=true ! video/x-raw,width=640,height=360,framerate=30/1"

    def run_chain(self, description):
        job = transcode.Job()
        job.stopped = threading.Event()
        out = Stopper(job, 2048)
        screencast.stream(description, out, job)
        return out.getvalue()

    def test_a_real_run_writes_matroska_until_stopped(self):
        gst_or_skip("videotestsrc", "matroskamux", "videoconvert", "videoscale", "videorate")
        if transcode.h264_encoder() is None:
            self.skipTest("no H.264 encoder")
        data = self.run_chain(screencast.describe(self.TEST_SOURCE, 640, 360, "", gpu=False))
        self.assertEqual(data[:4], b"\x1a\x45\xdf\xa3")     # the EBML header

    def test_the_compositor_is_told_where_the_picture_goes(self):
        gst_or_skip("vacompositor", "vapostproc", "vah264enc", "videotestsrc", "matroskamux")
        source = "videotestsrc is-live=true ! video/x-raw,format=I420,width=800,height=800,framerate=30/1"
        job = transcode.Job()
        job.stopped = threading.Event()
        out = Stopper(job, 2048)
        screencast.stream(screencast.describe(source, 1920, 1080, "", gpu=True), out, job, (1920, 1080))
        self.assertEqual(out.getvalue()[:4], b"\x1a\x45\xdf\xa3")
        pad = job.pipeline.get_by_name("mix").get_static_pad("sink_1")
        self.assertEqual([pad.get_property(n) for n in ("xpos", "ypos", "width", "height")], [420, 0, 1080, 1080])

    def test_a_window_that_changes_shape_keeps_its_proportions(self):
        # what reaches the compositor keeps the first size, with the picture stretched into it: the window deformed
        # on the TV until the size was read at the entrance of vapostproc [V 03/11/2026]
        gst_or_skip("vacompositor", "vapostproc", "videotestsrc", "fakesink")
        Gst = transcode._gst()
        memory = "video/x-raw(memory:VAMemory),format=NV12"
        pipeline = Gst.parse_launch(
            f"vacompositor name=mix latency=0 ! {memory},width=1920,height=1080,framerate=30/1 ! fakesink "
            "videotestsrc is-live=true pattern=black ! video/x-raw,width=1920,height=1080,framerate=30/1 ! "
            "vapostproc ! mix.sink_0 "
            "videotestsrc is-live=true ! capsfilter name=size caps=video/x-raw,format=I420,width=800,height=800,"
            f"framerate=30/1 ! vapostproc name=shape ! {memory} ! mix.sink_1")
        self.addCleanup(pipeline.set_state, Gst.State.NULL)
        screencast.keep_shape(pipeline, 1920, 1080)
        pad = pipeline.get_by_name("mix").get_static_pad("sink_1")
        placed = lambda: [pad.get_property(n) for n in ("xpos", "ypos", "width", "height")]  # noqa: E731

        def wait_for(expected):
            deadline = time.monotonic() + 5
            while placed() != expected and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(placed(), expected)
        pipeline.set_state(Gst.State.PLAYING)
        wait_for([420, 0, 1080, 1080])
        pipeline.get_by_name("size").set_property(
            "caps", Gst.Caps.from_string("video/x-raw,format=I420,width=1400,height=500,framerate=30/1"))
        wait_for([0, 198, 1920, 684])

    def test_a_chain_that_cannot_be_built_is_said(self):
        gst_or_skip("matroskamux")
        with self.assertRaises(screencast.ScreenError):
            self.run_chain("no-such-element ! appsink name=out")


class FakePortal(threading.Thread):
    """org.freedesktop.portal.Desktop on a private bus, answering as the real one does: with a signal."""

    def __init__(self, address, answer_start=0, hang_on_start=False):
        super().__init__(daemon=True)
        self.address = address
        self.answer_start, self.hang_on_start = answer_start, hang_on_start
        self.ready = threading.Event()
        self.closed_session = threading.Event()
        self.closed_request = threading.Event()
        self.waiting_in_start = threading.Event()
        self.loop = None
        self.conn = None

    def run(self):
        from gi.repository import Gio, GLib
        context = GLib.MainContext.new()
        context.push_thread_default()
        self.loop = GLib.MainLoop.new(context, False)
        self.conn = Gio.DBusConnection.new_for_address_sync(
            self.address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        cast = Gio.DBusNodeInfo.new_for_xml(SCREENCAST_XML).interfaces[0]
        self.conn.register_object(screencast.DESKTOP_PATH, cast, self.on_call, None, None)
        session = Gio.DBusNodeInfo.new_for_xml(SESSION_XML).interfaces[0]
        self.conn.register_object(SESSION, session, self.on_session, None, None)
        self.request_info = Gio.DBusNodeInfo.new_for_xml(REQUEST_XML).interfaces[0]
        self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                            GLib.Variant("(su)", (screencast.DESKTOP, 0)), None, Gio.DBusCallFlags.NONE, 5000, None)
        self.ready.set()
        self.loop.run()

    def respond(self, sender, token, code, results):
        from gi.repository import GLib
        path = f"{screencast.DESKTOP_PATH}/request/{sender[1:].replace('.', '_')}/{token}"
        self.conn.emit_signal(sender, path, "org.freedesktop.portal.Request", "Response",
                              GLib.Variant("(ua{sv})", (code, results)))

    def on_session(self, _conn, _sender, _path, _iface, _method, _params, invocation):
        self.closed_session.set()
        invocation.return_value(None)

    def on_request(self, _conn, _sender, _path, _iface, _method, _params, invocation):
        self.closed_request.set()
        invocation.return_value(None)

    def on_call(self, _conn, sender, _path, _iface, method, params, invocation):
        from gi.repository import Gio, GLib
        if method == "OpenPipeWireRemote":
            fds = Gio.UnixFDList.new()
            fds.append(os.open(os.devnull, os.O_RDONLY))
            invocation.return_value_with_unix_fd_list(GLib.Variant("(h)", (0,)), fds)
            return
        options = params.unpack()[-1]
        token = options["handle_token"]
        path = f"{screencast.DESKTOP_PATH}/request/{sender[1:].replace('.', '_')}/{token}"
        invocation.return_value(GLib.Variant("(o)", (path,)))
        if method == "CreateSession":
            self.respond(sender, token, 0, {"session_handle": GLib.Variant("s", SESSION)})
        elif method == "SelectSources":
            self.options = options
            self.respond(sender, token, 0, {})
        elif self.hang_on_start:
            self.conn.register_object(path, self.request_info, self.on_request, None, None)
            self.waiting_in_start.set()
        elif self.answer_start:
            self.respond(sender, token, self.answer_start, {})
        else:
            results = GLib.Variant("a(ua{sv})", [(42, {"size": GLib.Variant("(ii)", (2880, 1920)),
                                                       "source_type": GLib.Variant("u", screencast.SOURCE_WINDOW)})])
            self.respond(sender, token, 0, {"streams": results})

    def close_from_outside(self):
        def emit():
            self.conn.emit_signal(None, SESSION, "org.freedesktop.portal.Session", "Closed", None)
            return False
        from gi.repository import GLib
        self.loop.get_context().invoke_full(GLib.PRIORITY_DEFAULT, emit)


@unittest.skipUnless(shutil.which("dbus-daemon"), "no dbus-daemon for a private bus")
class GrantTest(unittest.TestCase):
    def setUp(self):
        from gi.repository import Gio
        self.Gio = Gio
        self.bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
        before = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        self.bus.up()
        self.addCleanup(self.bus.down)
        # the private bus only for the connections made here: nothing else in the process follows it
        if before is not None:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = before
        self.address = self.bus.get_bus_address()
        patch = mock.patch.object(screencast, "node_serial", return_value="1234")
        patch.start()
        self.addCleanup(patch.stop)

    def client(self):
        return self.Gio.DBusConnection.new_for_address_sync(
            self.address, self.Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | self.Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)

    def portal(self, **options):
        portal = FakePortal(self.address, **options)
        portal.start()
        self.assertTrue(portal.ready.wait(5))
        self.addCleanup(portal.loop.quit)
        return portal

    def test_a_yes_gives_the_source_and_always_asks_again(self):
        portal = self.portal()
        grant = screencast.Grant(bus=self.client())
        source = grant.request()
        self.assertEqual((source.kind, source.node, source.serial, source.width, source.height),
                         ("window", 42, "1234", 2880, 1920))
        self.assertGreaterEqual(source.fd, 0)
        self.assertNotIn("persist_mode", portal.options)     # the chooser every time
        self.assertEqual(portal.options["cursor_mode"], screencast.CURSOR_EMBEDDED)
        grant.close()
        self.assertTrue(portal.closed_session.wait(5))

    def test_a_no_is_declined(self):
        self.portal(answer_start=1)
        with self.assertRaises(screencast.Declined):
            screencast.Grant(bus=self.client()).request()

    def test_a_failure_of_the_portal_is_said(self):
        self.portal(answer_start=2)
        with self.assertRaises(screencast.ScreenError) as caught:
            screencast.Grant(bus=self.client()).request()
        self.assertNotIsInstance(caught.exception, screencast.Declined)

    def test_no_portal_is_said_and_does_not_hang(self):
        with self.assertRaises(screencast.ScreenError):
            screencast.Grant(bus=self.client()).request()

    def test_closing_while_the_dialog_is_up_closes_the_dialog(self):
        portal = self.portal(hang_on_start=True)
        grant = screencast.Grant(bus=self.client())
        outcome = []

        def run():
            try:
                grant.request()
            except screencast.ScreenError as e:
                outcome.append(e)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(portal.waiting_in_start.wait(5))
        grant.close()
        thread.join(5)
        self.assertIsInstance(outcome[0], screencast.Declined)
        self.assertTrue(portal.closed_request.wait(5))

    def test_the_end_of_the_session_from_outside_is_heard(self):
        portal = self.portal()
        ended = threading.Event()
        grant = screencast.Grant(on_closed=ended.set, bus=self.client())
        grant.request()
        portal.close_from_outside()
        self.assertTrue(ended.wait(5))
        self.assertFalse(portal.closed_session.wait(0.2))     # gone already: nothing to close


if __name__ == "__main__":
    unittest.main()

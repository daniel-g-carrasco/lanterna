#!/usr/bin/env python3
"""The window, where there is a display: it lists the TVs and remembers the
one used, shows what is chosen on the fake TV with the real session, works
the controls, says what goes wrong, waits for a TV when there is none yet,
remembers its size; the preferences apply the theme; the About window has the
troubleshooting text; the update dialog focuses its action and starts nothing
after "Later"."""

import os
import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import _setup

Gtk, Adw = _setup.gtk_or_skip()

from _fake_tv import FakeTV  # noqa: E402

from lanterna import APP_NAME, app as app_mod, keepawake, screencast, server, updates  # noqa: E402
from lanterna.castv2 import Channel  # noqa: E402
from lanterna.config import Config  # noqa: E402
from lanterna.devices import Device  # noqa: E402
from lanterna.media import MediaInfo, ProbeError  # noqa: E402
from lanterna.paths import Paths  # noqa: E402
from lanterna.session import Session  # noqa: E402
from lanterna.update_dialog import UpdateDialog  # noqa: E402
from lanterna.window import MainWindow, PreferencesDialog, about_dialog  # noqa: E402


class TestApp(app_mod.App):
    """The application, never run: its folders in a temporary place, its
    saves counted, so that the window has the application a GTK window
    needs without a main loop of its own."""

    def __init__(self, tmp: str):
        super().__init__()
        self.paths = Paths(Path(tmp) / "config", Path(tmp) / "state", Path(tmp) / "data")
        self.cfg = Config()
        self.saved = 0

    def save_config(self):
        self.saved += 1


class FakeBrowser:
    """No Avahi in the tests: they hand the window its TVs."""

    def __init__(self, on_change, on_error=None):
        self.on_change, self.on_error = on_change, on_error
        self.rescans = 0

    def start(self):
        pass

    def stop(self):
        pass

    def rescan(self):
        self.rescans += 1


class FakeSleep:
    def __init__(self, on_error=None):
        self.what = ""

    def hold(self, what, _reason):
        self.what = what

    def release(self):
        self.what = ""


class FakeSilence:
    """The real output is the user's: never touched by the tests."""

    def __init__(self):
        self.muted = ""
        self.restored = 0

    def silence(self):
        self.muted = "speakers"
        return self.muted

    def restore(self):
        self.restored += 1
        self.muted = ""


class FakeGrant:
    """The portal's dialog, answered at once."""
    answer = "screen"

    def __init__(self, on_closed=None):
        self.on_closed = on_closed
        self.closed = threading.Event()
        FakeGrant.last = self

    def request(self):
        if self.answer == "no":
            raise screencast.Declined("the user said no")
        if self.answer == "fail":
            raise screencast.ScreenError("The system refused to share the screen")
        return screencast.Source(self.answer, 42, "1234", os.open(os.devnull, os.O_RDONLY), 2880, 1920)

    def close(self):
        self.closed.set()


TV = Device("tv1", "Living Room TV", "Philips", "127.0.0.1", 8009)
OTHER = Device("tv2", "Kitchen", "Chromecast", "127.0.0.2", 8009)
INFOS = {
    "film.mp4": MediaInfo("mp4", "h264", "high", ("aac",), duration_s=5400.0, width=1920, height=1080),
    "film.mkv": MediaInfo("matroska", "h264", "high", ("ac3",), duration_s=7200.0, width=1920, height=800),
    "a.jpg": MediaInfo("jpeg", image=True, width=4000, height=3000),
    "b.jpg": MediaInfo("jpeg", image=True, width=4000, height=3000),
}


def fake_probe(path):
    info = INFOS.get(Path(path).name)
    if info is None:
        raise ProbeError("Could not determine type of stream.")
    return info


class WindowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.app = TestApp(self.tmp)
        self.tvs: list[FakeTV] = []
        self.tv_options = {}
        self.media_server = server.MediaServer()
        self.media_server.start()
        self.addCleanup(self.media_server.stop)
        self.made: list[Session] = []
        for name in (*INFOS, "notes.txt"):
            (Path(self.tmp) / name).write_bytes(b"x" * 100)

    def connect(self, _device):
        ours, theirs = socket.socketpair()
        tv = FakeTV(theirs, **self.tv_options)
        tv.start()
        self.tvs.append(tv)
        self.addCleanup(theirs.close)
        return Channel(ours)

    def make_session(self, device, on_state):
        session = Session(device, on_state, connect=self.connect, media_server=self.media_server, probe=fake_probe,
                          producer=lambda path, way, info, out, start_s=0.0, job=None: out.write(b"live"))
        self.made.append(session)
        self.addCleanup(session.close)
        return session

    def window(self) -> MainWindow:
        window = MainWindow(self.app, browser_factory=FakeBrowser, session_factory=self.make_session,
                            grant_factory=FakeGrant, sleep_factory=FakeSleep, silence_factory=FakeSilence)
        self.addCleanup(window.destroy)
        self.addCleanup(window.shut_down)
        return window

    def path(self, name):
        return str(Path(self.tmp) / name)

    def showing(self, *names, tvs=(TV,)) -> MainWindow:
        window = self.window()
        window.present()
        window._tvs_changed(list(tvs))
        window.open_paths([self.path(n) for n in names])
        self.assertTrue(_setup.spin(lambda: window.state.state == "playing"), window.state)
        return window

    def sharing(self, answer="screen") -> MainWindow:
        FakeGrant.answer = answer
        self.addCleanup(setattr, FakeGrant, "answer", "screen")
        window = self.window()
        window.present()
        window._tvs_changed([TV])
        window.show_screen()
        return window

    def test_the_screen_goes_on_the_tv_with_only_stop_to_press(self):
        window = self.sharing()
        self.assertTrue(_setup.spin(lambda: window.state.state == "playing"), window.state)
        self.assertEqual(window.state.way, "screen")
        self.assertEqual(self.tvs[-1].loads[-1]["streamType"], "LIVE")
        self.assertIn("screen", window.status_label.get_label())
        self.assertFalse(window.play_button.get_visible())
        self.assertFalse(window.bar.get_visible())
        self.assertEqual(window.sleep.what, keepawake.SCREEN)
        window.stop()
        self.assertTrue(_setup.spin(lambda: window.state.state == "idle"))
        self.assertTrue(FakeGrant.last.closed.wait(2))
        self.assertTrue(_setup.spin(lambda: window.sleep.what == ""))

    def test_this_computer_stays_quiet_only_while_the_screen_is_on_and_remembers_it(self):
        window = self.sharing()
        self.assertTrue(_setup.spin(lambda: window.silence.muted == "speakers"))
        self.assertTrue(_setup.spin(lambda: self.app.cfg.muted_output == "speakers"))     # survives a crash
        window.stop()
        self.assertTrue(_setup.spin(lambda: window.silence.muted == "" and self.app.cfg.muted_output == ""))

    def test_without_the_sound_on_the_tv_this_computer_keeps_it(self):
        self.app.cfg.screen_sound = False
        window = self.sharing()
        self.assertTrue(_setup.spin(lambda: window.state.state == "playing"))
        _setup.spin(lambda: False, 0.1)
        self.assertEqual(window.silence.muted, "")

    def test_the_preference_leaves_this_computer_alone(self):
        self.app.cfg.screen_quiet_here = False
        window = self.sharing()
        self.assertTrue(_setup.spin(lambda: window.state.state == "playing"))
        _setup.spin(lambda: False, 0.1)
        self.assertEqual(window.silence.muted, "")

    def test_a_file_leaves_this_computer_alone(self):
        window = self.showing("film.mp4")
        _setup.spin(lambda: False, 0.1)
        self.assertEqual(window.silence.muted, "")

    def test_closing_the_window_gives_the_sound_back(self):
        window = self.sharing()
        self.assertTrue(_setup.spin(lambda: window.silence.muted == "speakers"))
        window.shut_down()
        self.assertTrue(_setup.spin(lambda: window.silence.muted == "" and self.app.cfg.muted_output == ""))

    def test_an_output_left_mute_by_a_crash_is_given_back_at_the_start(self):
        self.app.cfg.muted_output = "speakers"
        window = self.window()
        self.assertTrue(_setup.spin(lambda: window.silence.restored == 1))
        self.assertTrue(_setup.spin(lambda: self.app.cfg.muted_output == ""))

    def test_a_window_is_called_a_window(self):
        window = self.sharing("window")
        self.assertTrue(_setup.spin(lambda: window.state.state == "playing"))
        self.assertIn("window", window.status_label.get_label())

    def test_saying_no_to_the_dialog_changes_nothing_and_says_nothing(self):
        window = self.sharing("no")
        _setup.spin(lambda: False, 0.2)
        self.assertEqual(window.stack.get_visible_child_name(), "pick")
        self.assertEqual(self.tvs, [])

    def test_a_refusal_of_the_system_is_said(self):
        window = self.sharing("fail")
        with mock.patch.object(window, "toast") as toast:
            self.assertTrue(_setup.spin(lambda: toast.called))
        self.assertIn("refused", toast.call_args.args[0])

    def test_the_end_of_the_sharing_from_outside_stops_the_tv(self):
        window = self.sharing()
        self.assertTrue(_setup.spin(lambda: window.state.state == "playing"))
        FakeGrant.last.on_closed()
        self.assertTrue(_setup.spin(lambda: window.state.state == "idle"))

    def test_a_file_keeps_the_computer_awake_but_not_the_screen(self):
        window = self.showing("film.mp4")
        self.assertEqual(window.sleep.what, keepawake.FILE)

    def test_the_screen_without_a_tv_is_said(self):
        window = self.window()
        with mock.patch.object(window, "toast") as toast:
            window.show_screen()
        toast.assert_called_once()

    def test_the_sound_preference_is_kept(self):
        window = self.window()
        PreferencesDialog(window, self.app)._set("screen_sound", False)
        self.assertFalse(self.app.cfg.screen_sound)

    def test_the_tvs_are_listed_and_the_one_used_last_is_selected(self):
        self.app.cfg.last_device = "tv2"
        window = self.window()
        self.assertFalse(window.tv_choice.get_sensitive())      # "Looking for TVs…"
        window._tvs_changed([OTHER, TV])
        self.assertIs(window.selected_tv(), OTHER)
        self.assertTrue(window.tv_choice.get_sensitive())
        self.assertEqual(window.tv_choice.get_model().get_string(1), "Living Room TV")
        self.assertEqual(self.app.saved, 0)                     # a list arriving is not the user choosing

    def test_choosing_a_tv_is_remembered(self):
        window = self.window()
        window._tvs_changed([TV, OTHER])
        window.tv_choice.set_selected(1)
        self.assertIs(window.selected_tv(), OTHER)
        self.assertEqual((self.app.cfg.last_device, self.app.saved), ("tv2", 1))

    def test_a_film_is_shown_with_its_controls(self):
        window = self.showing("film.mp4")
        self.assertEqual(window.stack.get_visible_child_name(), "show")
        self.assertEqual(window.title_label.get_label(), "film")
        self.assertIn("Living Room TV", window.status_label.get_label())
        self.assertEqual(window.duration_label.get_label(), "1:30:00")
        self.assertTrue(window.bar.get_visible())
        self.assertTrue(window.play_button.get_visible())
        self.assertFalse(window.previous_button.get_visible())
        self.assertFalse(window.tv_choice.get_sensitive())      # not while something is on
        self.assertEqual(self.app.cfg.last_folder, self.tmp)
        self.assertEqual(self.made[0].device, TV)

    def test_pause_play_and_stop_reach_the_tv(self):
        window = self.showing("film.mp4")
        window.toggle_pause()
        self.assertTrue(_setup.spin(lambda: window.state.state == "paused"))
        self.assertEqual(window.play_button.get_icon_name(), "media-playback-start-symbolic")
        window.toggle_pause()
        self.assertTrue(_setup.spin(lambda: window.state.state == "playing"))
        window.stop()
        self.assertTrue(_setup.spin(lambda: window.stack.get_visible_child_name() == "pick"))
        self.assertTrue(window.tv_choice.get_sensitive())

    def test_dragging_the_bar_sends_one_seek_where_it_stops(self):
        window = self.showing("film.mp4")
        for value in (100.0, 200.0, 300.0):
            window._on_seek_dragged(window.scale, None, value)
        self.assertEqual(window.position_label.get_label(), "5:00")
        seeks = lambda: [m.payload["currentTime"] for m in self.tvs[-1].seen if m.payload.get("type") == "SEEK"]  # noqa: E731, E501
        self.assertTrue(_setup.spin(lambda: seeks() == [300.0]), seeks())

    def test_with_the_bar_in_hand_nothing_goes_to_the_tv_until_it_is_let_go(self):
        window = self.showing("film.mp4")
        seeks = lambda: [m.payload["currentTime"] for m in self.tvs[-1].seen if m.payload.get("type") == "SEEK"]  # noqa: E731, E501
        window._bar_pressed()
        window._on_seek_dragged(window.scale, None, 100.0)
        _setup.spin(lambda: False, timeout=0.5)     # longer than the pause that sends a seek without a hand on the bar
        self.assertEqual(seeks(), [])
        window._on_seek_dragged(window.scale, None, 200.0)
        self.assertEqual(window.position_label.get_label(), "3:20")
        window._tick()
        self.assertEqual(window.position_label.get_label(), "3:20")     # the time going on does not take it back
        window._bar_released()
        self.assertTrue(_setup.spin(lambda: seeks() == [200.0]), seeks())
        window._bar_pressed()
        window._bar_released()                                          # a click without a move: nothing to ask
        _setup.spin(lambda: False, timeout=0.3)
        self.assertEqual(seeks(), [200.0])

    def test_the_bar_stays_usable_while_the_tv_loads_the_new_point(self):
        window = self.showing("film.mp4")
        self.assertTrue(window.scale.get_sensitive())
        window._state_changed(replace(window.state, state="buffering"))
        self.assertTrue(window.scale.get_sensitive())
        window._state_changed(replace(window.state, state="connecting"))
        self.assertFalse(window.scale.get_sensitive())

    def test_a_folder_of_photos_goes_forward_and_back(self):
        window = self.showing("a.jpg")
        self.assertFalse(window.bar.get_visible())
        self.assertFalse(window.play_button.get_visible())
        self.assertTrue(window.next_button.get_sensitive())
        self.assertFalse(window.previous_button.get_sensitive())
        self.assertIn("1 of 2", window.status_label.get_label())
        window.next()
        self.assertTrue(_setup.spin(lambda: window.state.title == "b.jpg" and window.state.state == "playing"))
        self.assertTrue(_setup.spin(lambda: "2 of 2" in window.status_label.get_label()))
        self.assertFalse(window.next_button.get_sensitive())

    def test_what_cannot_be_shown_is_said_and_the_choice_comes_back(self):
        window = self.window()
        window.present()
        window._tvs_changed([TV])
        window.open_paths([self.path("notes.txt")])
        self.assertTrue(_setup.spin(lambda: window.state.state == "error"))
        self.assertIn("notes.txt", window.state.error)
        self.assertEqual(window.stack.get_visible_child_name(), "pick")

    def test_a_folder_with_nothing_to_show_starts_nothing(self):
        empty = Path(self.tmp) / "empty"
        empty.mkdir()
        window = self.window()
        window._tvs_changed([TV])
        window.open_paths([str(empty)])
        self.assertTrue(_setup.spin(lambda: self.app.saved >= 1))
        _setup.spin(lambda: False, 0.2)
        self.assertEqual(self.made, [])

    def test_what_is_chosen_before_a_tv_is_found_waits_for_it(self):
        window = self.window()
        window.present()
        window.open_paths([self.path("film.mp4")])
        self.assertTrue(_setup.spin(lambda: window._pending is not None))
        self.assertEqual(self.made, [])
        window._tvs_changed([TV])
        self.assertTrue(_setup.spin(lambda: window.state.state == "playing"))

    def test_another_tv_gets_a_session_of_its_own(self):
        window = self.showing("film.mp4", tvs=(TV, OTHER))
        window.stop()
        self.assertTrue(_setup.spin(lambda: window.state.state == "idle"))
        window.tv_choice.set_selected(1)
        window.open_paths([self.path("film.mp4")])
        self.assertTrue(_setup.spin(lambda: len(self.made) == 2 and window.state.state == "playing"))
        self.assertEqual(self.made[1].device, OTHER)

    def test_no_tv_after_a_while_is_said_with_a_way_to_look_again(self):
        window = self.window()
        window._no_tv_yet()
        self.assertTrue(window.banner.get_revealed())
        window.look_again()
        self.assertFalse(window.banner.get_revealed())
        self.assertEqual(window.browser.rescans, 1)
        window._tvs_failed("Avahi is not running")
        self.assertIn("Avahi", window.banner.get_title())

    def test_closing_the_window_closes_the_player_on_the_tv(self):
        window = self.showing("film.mp4")
        tv = self.tvs[-1]
        window.shut_down()
        stops = lambda: [m for m in tv.seen if m.payload.get("type") == "STOP"]  # noqa: E731
        self.assertTrue(_setup.spin(lambda: bool(stops())))

    def test_the_size_is_remembered(self):
        window = self.window()
        window.set_default_size(640, 700)
        window.save_state()
        self.assertTrue(self.app.paths.window_state_file.exists())
        again = self.window()
        self.assertEqual(again.get_default_size(), (640, 700))

    def test_the_preferences_apply_the_theme_and_save(self):
        window = self.window()
        dialog = PreferencesDialog(window, self.app)
        style = Adw.StyleManager.get_default()
        self.addCleanup(style.set_color_scheme, Adw.ColorScheme.DEFAULT)
        dialog._set_theme("dark")
        self.assertEqual(style.get_color_scheme(), Adw.ColorScheme.FORCE_DARK)
        self.assertEqual((self.app.cfg.theme, self.app.saved), ("dark", 1))

    def test_the_about_window_has_the_troubleshooting_text(self):
        dialog = about_dialog(self.window(), self.app.cfg, self.app.paths, present=False)
        self.assertIn(APP_NAME, dialog.get_debug_info())
        self.assertTrue(_setup.spin(lambda: "python:" in dialog.get_debug_info()))
        self.assertNotIn("password", dialog.get_debug_info().lower())

    def test_the_update_dialog(self):
        dialog = UpdateDialog(updates.Release("99.0.0", "https://example/release", "Notes\n\n- one line", []))
        self.assertIs(dialog.get_focus(), dialog.action)
        self.assertIs(dialog.get_default_widget(), dialog.action)
        dialog._cancel.set()            # "Later" (the dialog closed) once the last chunk was in
        with mock.patch("lanterna.update_dialog.subprocess.Popen") as popen:
            dialog._downloaded(Path("setup.exe"))
        popen.assert_not_called()


@unittest.skipUnless(_setup.WINDOWS, "Windows only")
class FontTest(unittest.TestCase):
    def test_a_size_larger_and_following_windows(self):
        settings = Gtk.Settings.get_default()
        before = settings.props.gtk_font_name
        self.addCleanup(settings.reset_property, "gtk-font-name")
        settings.props.gtk_font_name = "Segoe UI 9"
        self.addCleanup(settings.disconnect, app_mod.larger_system_font())
        self.assertEqual(settings.props.gtk_font_name, "Segoe UI 10.5")
        settings.props.gtk_font_name = "Segoe UI 11"            # a larger text size chosen in Windows
        self.assertEqual(settings.props.gtk_font_name, "Segoe UI 13")
        settings.props.gtk_font_name = "Segoe UI Bold 9"
        self.assertEqual(settings.props.gtk_font_name, "Segoe UI Bold 10.5")
        self.assertTrue(before)


class ScreenshotTest(unittest.TestCase):
    """The developer aid the CI's smoke test relies on: the variables are
    named after the package."""

    def test_the_variables_are_the_apps_own(self):
        from lanterna import ENV_PREFIX
        self.assertIn(f'"{ENV_PREFIX}_SCREENSHOT"', Path(app_mod.__file__).read_text(encoding="utf-8")
                      .replace("f\"{ENV_PREFIX}_SCREENSHOT\"", f'"{ENV_PREFIX}_SCREENSHOT"'))
        self.assertEqual(ENV_PREFIX, "LANTERNA")
        self.assertTrue(os.environ.get("LANGUAGE"))


if __name__ == "__main__":
    unittest.main()

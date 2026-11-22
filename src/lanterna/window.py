"""The main window: which TV, what to show on it, and the controls while it
is on. Everything is decided elsewhere (playlist.py, session.py, devices.py):
here it is displayed and triggered, and what talks to the TV or reads the disk
runs in a thread.

On Windows the main window is a plain GTK window, so that it keeps the
system title bar (an AdwApplicationWindow always draws its own, and with
it Windows does not snap the window to the screen edges, nor on
Win+arrows); its content is libadwaita's all the same, and the dialogs
then open as windows of their own, with the system's title bar too
(app.native_windows gives them the content's colour and the app's icon).
"""

from __future__ import annotations

import json
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, WEBSITE, WINDOWS, __version__, devices, diagnostics, keepawake  # noqa: E402
from . import localsound, playlist, screencast  # noqa: E402
from .i18n import _  # noqa: E402
from .log import complain  # noqa: E402
from .session import Cancelled, Session, SessionError, State, clock  # noqa: E402
from .ui import escape, run_in_thread  # noqa: E402

STATE_VERSION = 1       # window.json of another layout is ignored
DEFAULT_SOUND = "@DEFAULT_MONITOR@"     # PulseAudio's name for what the default output is playing
NO_TV_AFTER_S = 6       # an mDNS answer takes a second or two: this long without one is worth saying
TICK_MS = 500
SEEK_SETTLE_MS = 250    # dragging the bar sends one seek, where it stops
SEEK_STEP_S = 10
VOLUME_SETTLE_MS = 150


def header_bar() -> Adw.HeaderBar:
    """A page's header bar. On Windows a toolbar under the system title bar,
    which has the window buttons."""
    if WINDOWS:
        return Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
    return Adw.HeaderBar()


_WindowBase = Gtk.ApplicationWindow if WINDOWS else Adw.ApplicationWindow


class MainWindow(_WindowBase):
    def __init__(self, app, browser_factory=devices.Browser, session_factory=Session,
                 grant_factory=screencast.Grant, sleep_factory=keepawake.SleepBlock,
                 silence_factory=localsound.Silence):
        super().__init__(application=app, title=APP_NAME, default_width=560, default_height=480,
                         width_request=420, height_request=400)
        self.app = app
        self._state_file = app.paths.window_state_file
        self._restore_state()
        self.connect("close-request", self._on_close)
        self._session_factory = session_factory
        self._grant_factory = grant_factory
        self.sleep = sleep_factory(on_error=lambda why: complain("keeping the computer awake", why))
        self.silence = silence_factory()
        self._quiet_wanted = False
        self._give_back_after_crash()
        self.session: Session | None = None
        self.state = State()
        self.tvs: list[devices.Device] = []
        self._selected_id = ""
        self._filling_choice = False    # the list being rebuilt is not the user choosing
        self._pending: playlist.Playlist | None = None      # chosen before any TV was found
        self._tick_source = 0
        self._seek_source = 0
        self._bar_held = False          # a button or a finger is on the bar
        self._bar_value: float | None = None        # where it was dragged to, not yet asked of the TV
        self._volume_source = 0
        self._setting_volume = False
        self._no_tv_source = 0
        self._last_error = ""

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(self._header())
        self.banner = Adw.Banner(button_label=_("Look Again"))
        self.banner.connect("button-clicked", lambda *_: self.look_again())
        view.add_top_bar(self.banner)
        view.set_content(self._content())
        self.toasts.set_child(view)
        if WINDOWS:
            self.set_child(self.toasts)     # the title bar is Windows' own
        else:
            self.set_content(self.toasts)

        # files or a folder dropped on the window are shown, as if chosen
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.connect("drop", self._on_drop)
        self.add_controller(target)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        self.browser = browser_factory(self._tvs_changed, self._tvs_failed)
        self._fill_choice()
        self.browser.start()
        self._no_tv_source = GLib.timeout_add_seconds(NO_TV_AFTER_S, self._no_tv_yet)

    # -- the widgets ----------------------------------------------------------------

    def _header(self) -> Gtk.Widget:
        header = header_bar()
        if WINDOWS:
            header.set_title_widget(Gtk.Box())      # the name is in the system title bar already
        self.tv_choice = Gtk.DropDown(tooltip_text=_("The TV to show things on"))
        self.tv_choice.connect("notify::selected", self._tv_chosen)
        header.pack_start(self.tv_choice)
        look = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Look for TVs Again"))
        look.connect("clicked", lambda *_: self.look_again())
        header.pack_start(look)
        menu = Gio.Menu()
        section = Gio.Menu()
        if WINDOWS:
            section.append(_("Check for Updates…"), "app.check-updates")
        section.append(_("Preferences"), "app.preferences")
        section.append(_("About {app}").format(app=APP_NAME), "app.about")
        menu.append_section(None, section)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text=_("Main Menu")))
        return header

    def _content(self) -> Gtk.Widget:
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._pick_page(), "pick")
        self.stack.add_named(self._show_page(), "show")
        return self.stack

    def _pick_page(self) -> Gtk.Widget:
        page = Adw.StatusPage(icon_name=APP_ID, title=_("Show It on the TV"),
                              description=_("Choose photos, a video or a film, or drop them here."))
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        files = Gtk.Button(label=_("Choose Files…"), css_classes=["pill", "suggested-action"])
        files.connect("clicked", lambda *_: self.choose_files())
        buttons.append(files)
        folder = Gtk.Button(label=_("Choose a Folder…"), css_classes=["pill"])
        folder.connect("clicked", lambda *_: self.choose_folder())
        buttons.append(folder)
        screen = Gtk.Button(label=_("Show the Screen…"), css_classes=["pill"])
        screen.connect("clicked", lambda *_: self.show_screen())
        buttons.append(screen)
        page.set_child(buttons)
        return page

    def _show_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.CENTER,
                      margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.title_label = Gtk.Label(css_classes=["title-2"], wrap=True, justify=Gtk.Justification.CENTER)
        titles.append(self.title_label)
        self.status_label = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        titles.append(self.status_label)
        self.note_label = Gtk.Label(css_classes=["dim-label", "caption"], wrap=True,
                                    justify=Gtk.Justification.CENTER, visible=False)
        titles.append(self.note_label)
        box.append(titles)

        self.bar = Gtk.Box(spacing=12)
        self.position_label = Gtk.Label(css_classes=["numeric", "caption"], label="0:00")
        self.bar.append(self.position_label)
        self.scale = Gtk.Scale(hexpand=True, draw_value=False, tooltip_text=_("Go to another point of the film"))
        self.scale.set_range(0, 1)
        self.scale.connect("change-value", self._on_seek_dragged)
        in_hand = Gtk.EventControllerLegacy(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        in_hand.connect("event", self._on_bar_event)
        self.scale.add_controller(in_hand)
        self.bar.append(self.scale)
        self.duration_label = Gtk.Label(css_classes=["numeric", "caption"], label="0:00")
        self.bar.append(self.duration_label)
        box.append(self.bar)

        controls = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.previous_button = Gtk.Button(icon_name="media-skip-backward-symbolic", tooltip_text=_("Previous"),
                                          valign=Gtk.Align.CENTER, css_classes=["circular", "flat"])
        self.previous_button.connect("clicked", lambda *_: self.previous())
        controls.append(self.previous_button)
        self.play_button = Gtk.Button(icon_name="media-playback-pause-symbolic", tooltip_text=_("Pause"),
                                      valign=Gtk.Align.CENTER, css_classes=["circular", "suggested-action"])
        self.play_button.connect("clicked", lambda *_: self.toggle_pause())
        controls.append(self.play_button)
        self.next_button = Gtk.Button(icon_name="media-skip-forward-symbolic", tooltip_text=_("Next"),
                                      valign=Gtk.Align.CENTER, css_classes=["circular", "flat"])
        self.next_button.connect("clicked", lambda *_: self.next())
        controls.append(self.next_button)
        self.volume = Gtk.ScaleButton(icons=["audio-volume-muted-symbolic", "audio-volume-high-symbolic",
                                             "audio-volume-low-symbolic", "audio-volume-medium-symbolic"],
                                      valign=Gtk.Align.CENTER, tooltip_text=_("The TV's Volume"))
        self.volume.set_adjustment(Gtk.Adjustment(lower=0, upper=1, step_increment=0.05, page_increment=0.1))
        self.volume.connect("value-changed", self._on_volume_changed)
        controls.append(self.volume)
        box.append(controls)

        self.stop_button = Gtk.Button(label=_("Stop"), halign=Gtk.Align.CENTER, css_classes=["pill"])
        self.stop_button.connect("clicked", lambda *_: self.stop())
        box.append(self.stop_button)
        return Adw.Clamp(maximum_size=520, child=box)

    def toast(self, text: str, timeout: int = 5) -> None:
        self.toasts.add_toast(Adw.Toast(title=escape(text), timeout=timeout))     # (a toast reads markup)

    # -- the TVs --------------------------------------------------------------------

    def selected_tv(self) -> devices.Device | None:
        return next((d for d in self.tvs if d.id == self._selected_id), None)

    def look_again(self) -> None:
        self.banner.set_revealed(False)
        self._arm_no_tv()
        self.browser.rescan()

    def _arm_no_tv(self) -> None:
        if self._no_tv_source:
            GLib.source_remove(self._no_tv_source)
        self._no_tv_source = GLib.timeout_add_seconds(NO_TV_AFTER_S, self._no_tv_yet)

    def _no_tv_yet(self) -> bool:
        self._no_tv_source = 0
        if not self.tvs:
            self.banner.set_title(_("No TV found. Is it on, and on this network?"))
            self.banner.set_revealed(True)
        return False

    def _tvs_failed(self, reason: str) -> None:
        self.banner.set_title(escape(_("TVs cannot be looked for: {reason}").format(reason=reason)))
        self.banner.set_revealed(True)

    def _tvs_changed(self, tvs: list[devices.Device]) -> None:
        self.tvs = tvs
        if tvs:
            self.banner.set_revealed(False)
        chosen = devices.pick(tvs, self._selected_id, self.app.cfg.last_device)
        self._selected_id = chosen.id if chosen else ""
        self._fill_choice()
        self._show_pending()

    def _fill_choice(self) -> None:
        self._filling_choice = True
        labels = [d.label for d in self.tvs] or [_("Looking for TVs…")]
        self.tv_choice.set_model(Gtk.StringList.new(labels))
        self.tv_choice.set_selected(next((i for i, d in enumerate(self.tvs) if d.id == self._selected_id), 0))
        self._filling_choice = False
        self._update_choice_sensitivity()

    def _update_choice_sensitivity(self) -> None:
        self.tv_choice.set_sensitive(bool(self.tvs) and self.state.state in ("idle", "error"))

    def _tv_chosen(self, *_args) -> None:
        if self._filling_choice or not self.tvs:
            return
        self._selected_id = self.tvs[self.tv_choice.get_selected()].id
        self.app.cfg.last_device = self._selected_id
        self.app.save_config()
        self._show_pending()

    # -- choosing what to show ------------------------------------------------------

    def _dialog_folder(self, dialog: Gtk.FileDialog) -> None:
        last = self.app.cfg.last_folder
        if last and os.path.isdir(last):
            dialog.set_initial_folder(Gio.File.new_for_path(last))

    def choose_files(self) -> None:
        media = Gtk.FileFilter(name=_("Photos and Videos"))
        media.add_mime_type("image/*")
        media.add_mime_type("video/*")
        everything = Gtk.FileFilter(name=_("All Files"))
        everything.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(media)
        filters.append(everything)
        dialog = Gtk.FileDialog(title=_("Choose What to Show"), modal=True, filters=filters, default_filter=media)
        self._dialog_folder(dialog)

        def chosen(dialog, result):
            try:
                files = dialog.open_multiple_finish(result)
            except GLib.Error:      # dismissed
                return
            self.open_paths([f.get_path() for f in files if f.get_path()])
        dialog.open_multiple(self, None, chosen)

    def choose_folder(self) -> None:
        dialog = Gtk.FileDialog(title=_("Choose a Folder to Show"), modal=True)
        self._dialog_folder(dialog)

        def chosen(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:      # dismissed
                return
            if folder is not None and folder.get_path():
                self.open_paths([folder.get_path()])
        dialog.select_folder(self, None, chosen)

    def _on_drop(self, _target, files, _x, _y) -> bool:
        paths = [f.get_path() for f in files.get_files() if f.get_path()]
        if paths:
            self.open_paths(paths)
        return bool(paths)

    def open_paths(self, paths: list[str]) -> None:
        """Files and folders, from the chooser, a drop or the command line."""
        first = paths[0]
        self.app.cfg.last_folder = first if os.path.isdir(first) else os.path.dirname(first)
        self.app.save_config()

        def done(result, error):
            if error is not None:
                self.toast(_("{name} cannot be read: {error}").format(name=os.path.basename(first), error=error))
            elif not result.paths:
                self.toast(_("No photos or videos in “{name}”").format(name=os.path.basename(first) or first))
            else:
                self._pending = result
                self._show_pending()
        run_in_thread(lambda: playlist.expand(paths), done)

    def _show_pending(self) -> None:
        """What was chosen, on the selected TV; said if there is none yet, and shown when one turns up."""
        if self._pending is None:
            return
        device = self.selected_tv()
        if device is None:
            self.toast(_("Waiting for a TV to show it on"))
            return
        chosen, self._pending = self._pending, None
        session = self._session_on(device)
        run_in_thread(lambda: session.show(list(chosen.paths), chosen.index), lambda _r, e: self._report(e))

    def _session_on(self, device: devices.Device) -> Session:
        """The session with that TV: a new one if the TV is another, or its address changed."""
        if self.session is not None and self.session.device != device:
            run_in_thread(self.session.close, lambda _r, e: self._report(e))
            self.session = None
        if self.session is None:
            self.session = self._session_factory(device, self._session_state)
        return self.session

    def show_screen(self) -> None:
        """The system's dialog asks what to share; the window stays usable meanwhile."""
        device = self.selected_tv()
        if device is None:
            self.toast(_("No TV to show it on yet"))
            return
        session = self._session_on(device)
        grant = self._grant_factory(on_closed=lambda: GLib.idle_add(lambda: self._screen_ended(session) and False))
        sound = DEFAULT_SOUND if self.app.cfg.screen_sound else ""
        run_in_thread(lambda: screencast.show_on(session, grant, sound), lambda _r, e: self._report(e))

    def _screen_ended(self, session: Session) -> None:
        """Stopped from outside: the shared window closed, or the system ended it."""
        if session is self.session and self.state.kind in ("screen", "window") and self.state.state != "idle":
            self.toast(_("The sharing has ended"))
            self.stop()

    def _report(self, error: Exception | None) -> None:
        """What came back from a thread that talked to the TV."""
        if error is None or isinstance(error, (Cancelled, screencast.Declined)):
            return
        if isinstance(error, screencast.ScreenError):
            self.toast(str(error))
            return
        if isinstance(error, SessionError):
            if self.state.state != "error":     # otherwise the state has said it already
                self.toast(str(error))
            return
        complain("talking to the TV", error)
        self.toast(_("Something went wrong: {error}").format(error=error))

    def _command(self, work) -> None:
        if self.session is not None:
            run_in_thread(work, lambda _r, e: self._report(e))

    # -- what is on -----------------------------------------------------------------

    def _session_state(self, state: State) -> None:
        """From whatever thread the session is on."""
        GLib.idle_add(lambda: self._state_changed(state) and False)

    def _state_changed(self, state: State) -> None:
        self.state = state
        active = state.state not in ("idle", "error")
        self.stack.set_visible_child_name("show" if active else "pick")
        self._update_choice_sensitivity()
        if state.state == "error" and state.error != self._last_error:
            self.toast(state.error, timeout=0)      # it stays: the user may be looking at the TV
        self._last_error = state.error if state.state == "error" else ""
        live_screen = state.kind in ("screen", "window")
        cfg = self.app.cfg
        self._quiet_here(active and live_screen and state.state == "playing" and cfg.screen_sound
                         and cfg.screen_quiet_here or self._quiet_wanted and active and live_screen)
        if not active:
            self.sleep.release()
            self._stop_ticking()
            self._bar_held, self._bar_value = False, None
            return
        self.sleep.hold(keepawake.SCREEN if live_screen else keepawake.FILE, _("Showing something on the TV"))
        self.title_label.set_label(os.path.splitext(state.title)[0] or state.title)
        self.status_label.set_label(self._status_text(state))
        self.note_label.set_label(state.note)
        self.note_label.set_visible(bool(state.note))
        photo = state.kind == "photo"
        settled = state.state in ("playing", "paused")
        self.bar.set_visible(not photo and state.kind != "" and bool(state.duration_s))
        # Also while the TV loads: a widget that stops being sensitive drops the drag in the user's hand, and every
        # seek in a live stream passes through here.
        self.scale.set_sensitive(state.seekable and state.state != "connecting")
        self.play_button.set_visible(not photo and not live_screen)
        self.play_button.set_sensitive(settled)
        paused = state.state == "paused"
        self.play_button.set_icon_name("media-playback-start-symbolic" if paused else "media-playback-pause-symbolic")
        self.play_button.set_tooltip_text(_("Play") if paused else _("Pause"))
        several = state.count > 1
        self.previous_button.set_visible(several)
        self.next_button.set_visible(several)
        self.previous_button.set_sensitive(settled and state.index > 0)
        self.next_button.set_sensitive(settled and state.index + 1 < state.count)
        if state.duration_s:
            self.scale.set_range(0, state.duration_s)
            self.duration_label.set_label(clock(state.duration_s))
        if state.volume is not None and not self._volume_source:
            self._setting_volume = True
            self.volume.set_value(state.volume)
            self._setting_volume = False
        self._tick()
        if state.state == "playing" and not photo:
            self._start_ticking()
        else:
            self._stop_ticking()

    def _quiet_here(self, wanted: bool) -> None:
        """Only the sound that also goes to the TV: the rest of the time this computer is the user's."""
        if wanted == self._quiet_wanted:
            return
        self._quiet_wanted = wanted
        run_in_thread(self.silence.silence if wanted else self.silence.restore, self._quiet_done)

    def _quiet_done(self, _result, error) -> None:
        if error is not None:           # the sound still works, twice: not worth more than the log and a word
            complain("muting this computer", error)
            self.toast(_("This computer could not be silenced"))
            return
        muted = self.silence.muted
        if muted != self.app.cfg.muted_output:
            self.app.cfg.muted_output = muted
            self.app.save_config()

    def _give_back_after_crash(self) -> None:
        output = self.app.cfg.muted_output
        if not output:
            return
        self.silence.muted = output
        run_in_thread(self.silence.restore, self._quiet_done)

    def _status_text(self, state: State) -> str:
        tv = state.device_name
        if state.state == "connecting":
            return _("Connecting to {tv}…").format(tv=tv)
        if state.state == "buffering":
            return _("Loading on {tv}…").format(tv=tv)
        if state.kind == "screen":
            return _("Showing the screen on {tv}").format(tv=tv)
        if state.kind == "window":
            return _("Showing a window on {tv}").format(tv=tv)
        if state.kind == "photo":
            return _("On {tv}, {n} of {count}").format(tv=tv, n=state.index + 1, count=state.count)
        if state.state == "paused":
            return _("Paused on {tv}").format(tv=tv)
        return _("Playing on {tv}").format(tv=tv)

    def _start_ticking(self) -> None:
        if not self._tick_source:
            self._tick_source = GLib.timeout_add(TICK_MS, self._on_tick)

    def _stop_ticking(self) -> None:
        if self._tick_source:
            GLib.source_remove(self._tick_source)
            self._tick_source = 0

    def _on_tick(self) -> bool:
        self._tick()
        return True

    def _tick(self) -> None:
        if self._seek_source or self._bar_held:     # the bar is the user's while they drag it
            return
        position = self.state.position_now()
        self.scale.set_value(position)
        self.position_label.set_label(clock(position))

    # -- the controls ---------------------------------------------------------------

    def toggle_pause(self) -> None:
        session = self.session
        if self.state.state == "paused":
            self._command(lambda: session.resume())
        elif self.state.state == "playing":
            self._command(lambda: session.pause())

    def stop(self) -> None:
        session = self.session
        self._command(lambda: session.stop())

    def next(self) -> None:
        session = self.session
        self._command(lambda: session.next())

    def previous(self) -> None:
        session = self.session
        self._command(lambda: session.previous())

    def seek_to(self, position: float) -> None:
        session = self.session
        self._command(lambda: session.seek(position))

    def _on_bar_event(self, controller, _event) -> bool:
        event = controller.get_current_event()      # the signal's own argument reaches Python as None
        kind = event.get_event_type() if event is not None else None
        if kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.TOUCH_BEGIN):
            self._bar_pressed()
        elif kind in (Gdk.EventType.BUTTON_RELEASE, Gdk.EventType.TOUCH_END, Gdk.EventType.TOUCH_CANCEL,
                      Gdk.EventType.GRAB_BROKEN):
            self._bar_released()
        return False

    def _bar_pressed(self) -> None:
        self._bar_held = True
        self._cancel_settling()

    def _bar_released(self) -> None:
        if not self._bar_held:
            return
        self._bar_held = False
        value, self._bar_value = self._bar_value, None
        if value is not None:
            self.seek_to(value)

    def _on_seek_dragged(self, _scale, _scroll, value: float) -> bool:
        """In a live stream a seek is a new stream: nothing until the hand lets go. The keyboard and the
        wheel have no release, for them the seek waits for the moves to stop."""
        self.position_label.set_label(clock(value))
        self._cancel_settling()
        if self._bar_held:
            self._bar_value = value
        else:
            self._seek_source = GLib.timeout_add(SEEK_SETTLE_MS, self._seek_settled, value)
        return False

    def _cancel_settling(self) -> None:
        if self._seek_source:
            GLib.source_remove(self._seek_source)
            self._seek_source = 0

    def _seek_settled(self, value: float) -> bool:
        self._seek_source = 0
        self.seek_to(value)
        return False

    def _on_volume_changed(self, _button, value: float) -> None:
        if self._setting_volume:
            return
        if self._volume_source:
            GLib.source_remove(self._volume_source)
        self._volume_source = GLib.timeout_add(VOLUME_SETTLE_MS, self._volume_settled, value)

    def _volume_settled(self, value: float) -> bool:
        self._volume_source = 0
        session = self.session
        self._command(lambda: session.set_volume(value))
        return False

    def _on_key(self, _controller, keyval, _keycode, state) -> bool:
        """Space and the arrows work the player from the laptop, as in any
        player; left and right go through the photos when photos are on."""
        if self.stack.get_visible_child_name() != "show" or state & Gdk.ModifierType.CONTROL_MASK:
            return False
        current = self.state
        if current.kind in ("screen", "window"):
            return False                # the keys belong to what is being shown
        if keyval == Gdk.KEY_space and current.kind != "photo":
            self.toggle_pause()
            return True
        if keyval not in (Gdk.KEY_Left, Gdk.KEY_Right):
            return False
        forward = keyval == Gdk.KEY_Right
        if current.kind == "photo" or not current.seekable:
            self.next() if forward else self.previous()
        else:
            self.seek_to(current.position_now() + (SEEK_STEP_S if forward else -SEEK_STEP_S))
        return True

    # -- closing and the remembered size ----------------------------------------------

    def _on_close(self, *_args) -> bool:
        self.save_state()
        self.shut_down()
        return False

    def shut_down(self) -> None:
        """The stream lives in this process, so closing the window ends what
        is on. The TV may take seconds to answer: the application waits for
        it without keeping the window up."""
        self._stop_ticking()
        for source in (self._seek_source, self._volume_source, self._no_tv_source):
            if source:
                GLib.source_remove(source)
        self._seek_source = self._volume_source = self._no_tv_source = 0
        self.browser.stop()
        self.sleep.release()
        session, self.session = self.session, None
        self._quiet_wanted = False
        if session is None and not self.silence.muted:
            return
        self.app.hold()

        def work():
            try:
                if session is not None:
                    session.close()
            finally:
                self.silence.restore()

        def closed(_result, error):
            if error is not None:
                complain("closing the session", error)
            else:
                self._quiet_done(None, None)
            self.app.release()
        run_in_thread(work, closed)

    def _restore_state(self) -> None:
        try:
            with open(self._state_file, encoding="utf-8") as f:
                state = json.load(f)
            if state.get("version") != STATE_VERSION:
                return
            self.set_default_size(int(state["width"]), int(state["height"]))
            if state.get("maximized"):
                self.maximize()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def save_state(self) -> None:
        width, height = self.get_default_size()
        state = {"version": STATE_VERSION, "width": width, "height": height, "maximized": self.is_maximized()}
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, window: MainWindow, app):
        super().__init__(title=_("Preferences"))
        self.window, self.app = window, app
        cfg = app.cfg
        page = Adw.PreferencesPage()

        group = Adw.PreferencesGroup(title=_("Appearance"))
        themes = [("system", _("Follow the System")), ("light", _("Light")), ("dark", _("Dark"))]
        theme = Adw.ComboRow(title=_("Theme"))
        theme.set_model(Gtk.StringList.new([label for _code, label in themes]))
        theme.set_selected(next((i for i, (code, _l) in enumerate(themes) if code == cfg.theme), 0))
        theme.connect("notify::selected", lambda r, _p: self._set_theme(themes[r.get_selected()][0]))
        group.add(theme)
        languages = [("", _("Desktop language")), ("en", "English"), ("it", "Italiano")]
        language = Adw.ComboRow(title=_("Language"), subtitle=_("Applied at the next start"))
        language.set_model(Gtk.StringList.new([label for _code, label in languages]))
        language.set_selected(next((i for i, (code, _l) in enumerate(languages) if code == cfg.language), 0))
        language.connect("notify::selected", lambda r, _p: self._set("language", languages[r.get_selected()][0]))
        group.add(language)
        page.add(group)

        group = Adw.PreferencesGroup(title=_("Showing the Screen"))
        sound = Adw.SwitchRow(title=_("Send the sound too"), active=cfg.screen_sound)
        sound.connect("notify::active", lambda r, _p: self._set("screen_sound", r.get_active()))
        group.add(sound)
        quiet = Adw.SwitchRow(title=_("Keep this computer quiet meanwhile"),
                              subtitle=_("The TV plays the sound about three seconds later, so the two would echo. "
                                         "It comes back when you stop."),
                              active=cfg.screen_quiet_here)
        quiet.connect("notify::active", lambda r, _p: self._set("screen_quiet_here", r.get_active()))
        group.add(quiet)
        page.add(group)

        if WINDOWS:
            group = Adw.PreferencesGroup(title=_("Updates"))
            updates = Adw.SwitchRow(title=_("Check for updates at start-up"),
                                    subtitle=_("Once a day, from the project's releases on GitHub; "
                                               "nothing about you or your files is sent"),
                                    active=cfg.check_updates)
            updates.connect("notify::active", lambda r, _p: self._set("check_updates", r.get_active()))
            group.add(updates)
            page.add(group)
        self.add(page)

    def _set(self, name: str, value) -> None:
        setattr(self.app.cfg, name, value)
        self.app.save_config()

    def _set_theme(self, theme: str) -> None:
        from .app import apply_theme
        self._set("theme", theme)
        apply_theme(theme)


def about_dialog(parent, cfg, paths, present: bool = True) -> Adw.AboutDialog:
    dialog = Adw.AboutDialog(
        application_name=APP_NAME, application_icon=APP_ID, version=__version__,
        developer_name="Daniel Grasso", license_type=Gtk.License.MIT_X11, website=WEBSITE,
        issue_url=WEBSITE + "/issues",
        comments=_("Show photos, slides, videos and films on your TV with Chromecast."))
    # Troubleshooting: what a report needs, with its Save button; the slow
    # part (the room the app's folders take) follows, from a thread
    head = diagnostics.summary(cfg, paths)
    dialog.set_debug_info_filename(f"{APP_ID}-debug-info.txt")
    dialog.set_debug_info(head + "\n" + _("Measuring the space taken by the app's files…"))
    run_in_thread(lambda: diagnostics.storage(paths),
                  lambda text, error: dialog.set_debug_info(head + "\n" + (text if error is None else str(error))))
    if present:
        dialog.present(parent)
    return dialog

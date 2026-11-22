#!/usr/bin/env python3
"""Lanterna's window driven through a few scenes with the tests' fake TV, for the README's GIFs and gallery.
Run under Broadway (GDK_BACKEND=broadway), never in the user's session. Writes frames/<scene>-<n>.png and
frames/<scene>.txt (one line per frame: the png, the file on the TV, how long the frame stays)."""
import math
import os
import random
import socket
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = Path(sys.argv[1]).resolve()
SCHEME = sys.argv[2] if len(sys.argv) > 2 else "light"
SCALE = float(os.environ.get("DEMO_SCALE", "1"))
sys.path.insert(0, str(REPO / "tests"))
os.environ["LANGUAGE"] = "en"

import _setup  # noqa: E402

Gtk, Adw = _setup.gtk_or_skip()
Gtk.Settings.get_default().props.gtk_enable_animations = False   # no frame clock to drive them without a browser
from gi.repository import Graphene  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

from _fake_tv import FakeTV  # noqa: E402
from test_window import FakeBrowser, FakeGrant, FakeSilence, FakeSleep, TestApp  # noqa: E402

from lanterna import server  # noqa: E402
from lanterna.castv2 import Channel  # noqa: E402
from lanterna.devices import Device  # noqa: E402
from lanterna.media import MediaInfo  # noqa: E402
from lanterna.session import Session  # noqa: E402
from lanterna.window import MainWindow, PreferencesDialog  # noqa: E402

OUT.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp())
media = tmp / "Holidays"
media.mkdir()


def landscape(path: Path, seed: int, sky, ground, sun) -> None:
    """A made-up landscape: a sky in two tones, a sun, three ridges of hills, a little haze."""
    rnd = random.Random(seed)
    w, h = 1600, 1000
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(sky[0], sky[1], strict=True)))
    sx, sy, sr = sun
    d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(255, 236, 190))
    for k, base in enumerate((560, 680, 800)):
        colour = tuple(round(c * (0.55 + 0.18 * k)) for c in ground)
        pts = [(0, h)]
        phase, amp = rnd.random() * 6, 60 - 12 * k
        for x in range(0, w + 40, 40):
            pts.append((x, base + amp * math.sin(x / (180 + 60 * k) + phase) + rnd.uniform(-8, 8)))
        pts.append((w, h))
        d.polygon(pts, fill=colour)
    im.filter(ImageFilter.GaussianBlur(1.2)).save(path, quality=90)


PHOTOS = [("Lake at dawn.jpg", ((244, 178, 140), (120, 150, 190)), (60, 80, 90), (1180, 420, 70)),
          ("Dunes.jpg", ((250, 214, 150), (240, 160, 100)), (200, 130, 80), (400, 300, 90)),
          ("Harbour at dusk.jpg", ((60, 70, 120), (230, 120, 90)), (40, 50, 70), (800, 520, 60)),
          ("Pine forest.jpg", ((180, 210, 230), (230, 236, 220)), (50, 90, 60), (1300, 200, 55)),
          ("Snowy ridge.jpg", ((140, 180, 230), (225, 235, 245)), (180, 190, 210), (300, 220, 50))]
for i, (name, sky, ground, sun) in enumerate(PHOTOS):
    landscape(media / name, i, sky, ground, sun)
FILM = media / "Summer by the sea.mkv"
FILM.write_bytes(b"x" * 100)
INFOS = {name: MediaInfo("jpeg", image=True, width=1600, height=1000) for name, *_ in PHOTOS}
INFOS[FILM.name] = MediaInfo("matroska", "h264", "high", ("ac3",), duration_s=5537.0, width=1920, height=1080)

LIVING = Device("tv1", "Living Room TV", "Android TV", "127.0.0.1", 8009)
KITCHEN = Device("tv2", "Kitchen", "Chromecast", "127.0.0.2", 8009)

app = TestApp(str(tmp))
app.cfg.theme = SCHEME
Adw.StyleManager.get_default().set_color_scheme(
    Adw.ColorScheme.FORCE_DARK if SCHEME == "dark" else Adw.ColorScheme.FORCE_LIGHT)
media_server = server.MediaServer()
media_server.start()
tvs = []


def connect(_device):
    ours, theirs = socket.socketpair()
    tv = FakeTV(theirs)
    tv.start()
    tvs.append((tv, theirs))
    return Channel(ours)


def make_session(device, on_state):
    return Session(device, on_state, connect=connect, media_server=media_server,
                   probe=lambda path: INFOS[Path(path).name],
                   producer=lambda path, way, info, out, start_s=0.0, job=None: out.write(b"live"))


window = MainWindow(app, browser_factory=FakeBrowser, session_factory=make_session, grant_factory=FakeGrant,
                    sleep_factory=FakeSleep, silence_factory=FakeSilence)
window.set_default_size(560, 600)
window.stack.set_transition_type(Gtk.StackTransitionType.NONE)   # the pictures without a fade half way
window.present()


def settle(seconds: float = 0.4) -> None:
    end = time.monotonic() + seconds
    _setup.spin(lambda: time.monotonic() > end, seconds + 1)


def shot(path: Path) -> None:
    """The window as PNG right after a frame is painted (as the app's own LANTERNA_SCREENSHOT does)."""
    done = []
    clock = window.get_frame_clock()

    def painted(*_a):
        clock.disconnect(handler)
        w, h = window.get_width(), window.get_height()
        snap = Gtk.Snapshot()
        snap.scale(SCALE, SCALE)
        Gtk.WidgetPaintable.new(window).snapshot(snap, w, h)
        area = Graphene.Rect().init(0, 0, w * SCALE, h * SCALE)
        window.get_renderer().render_texture(snap.to_node(), area).save_to_png(str(path))
        done.append(1)
    handler = clock.connect("after-paint", painted)
    window.queue_draw()
    _setup.spin(lambda: done, 5)


scenes: dict[str, list[str]] = {}


def frame(scene: str, hold: float, on_tv: str = "") -> None:
    n = len(scenes.setdefault(scene, []))
    path = OUT / f"{scene}-{n:02}.png"
    shot(path)
    scenes[scene].append(f"{path.name}\t{on_tv}\t{hold}")


def on_tv() -> str:
    return Path(window.state.path).name if window.state.state in ("playing", "paused") else ""


# -- scene 1: the TVs are found, photos chosen, the TV shows them one by one
settle(0.5)
frame("photos", 1.2)
window._tvs_changed([LIVING, KITCHEN])
settle(0.6)
frame("photos", 1.4)
window.open_paths([str(media / name) for name, *_ in PHOTOS])
_setup.spin(lambda: window.state.state == "playing", 10)
settle(0.5)
frame("photos", 2.2, on_tv())
for _ in range(3):
    window.next()
    _setup.spin(lambda: window.state.state == "playing" and window.state.index > 0, 10)
    settle(0.6)
    frame("photos", 2.0, on_tv())
window.stop()
_setup.spin(lambda: window.state.state == "idle", 10)
settle(0.8)

# -- scene 2: a film, played, paused, the volume of the TV
window.open_paths([str(FILM)])
_setup.spin(lambda: window.state.state == "playing", 10)
settle(1.0)
frame("film", 2.0, FILM.name)
settle(2.0)
frame("film", 1.5, FILM.name)
window.toggle_pause()
_setup.spin(lambda: window.state.state == "paused", 10)
settle(0.6)
frame("film", 2.0, FILM.name)
window.toggle_pause()
_setup.spin(lambda: window.state.state == "playing", 10)
settle(0.6)
frame("film", 1.5, FILM.name)

# -- the gallery's still pictures
window.stop()
_setup.spin(lambda: window.state.state == "idle", 10)
settle(0.6)
shot(OUT / "still-start.png")
window.open_paths([str(media / name) for name, *_ in PHOTOS])
_setup.spin(lambda: window.state.state == "playing", 10)
settle(0.8)
shot(OUT / "still-photos.png")
window.stop()
_setup.spin(lambda: window.state.state == "idle", 10)
window.open_paths([str(FILM)])
_setup.spin(lambda: window.state.state == "playing", 10)
settle(1.5)
shot(OUT / "still-film.png")
window.stop()
_setup.spin(lambda: window.state.state == "idle", 10)
settle(0.5)
window.show_screen()
_setup.spin(lambda: window.state.state == "playing", 10)
settle(0.8)
shot(OUT / "still-screen.png")
window.stop()
_setup.spin(lambda: window.state.state == "idle", 10)
settle(0.5)
prefs = PreferencesDialog(window, app)
prefs.present(window)
settle(1.0)
shot(OUT / "still-preferences.png")

for scene, lines in scenes.items():
    (OUT / f"{scene}.txt").write_text("\n".join(lines) + "\n")
(OUT / "media").mkdir(exist_ok=True)
for name, *_ in PHOTOS:
    (OUT / "media" / name).write_bytes((media / name).read_bytes())
print("ok", {k: len(v) for k, v in scenes.items()}, flush=True)
os._exit(0)

"""The Chromecasts on the local network, found through Avahi.

A Cast device announces `_googlecast._tcp` over mDNS, with TXT records that
carry what the window shows: `fn` the name the user gave it ("Living Room TV"),
`md` the model ("2021/22 Philips UHD Android TV"), `id` a stable identifier
(the setting that remembers "the last TV" keeps it). Avahi, the system's
mDNS daemon, browses and resolves; asking it over D-Bus avoids opening a
multicast socket of our own (and works the same from the Flatpak, with
`--system-talk-name=org.freedesktop.Avahi`). The announcement is looked for on
both protocols and always resolved to an IPv4 address, the one the media server
listens on: Avahi may hold a TV under IPv6 only for a while, the TV still
answering on IPv4 [V 03/11/2026], and a browser limited to IPv4 lost it.

When Avahi has given nothing after a moment the question is also asked directly:
one mDNS query, answers wanted on our own socket (the QU bit). On 03/11/2026
Avahi timed out on the TV's IPv4 address for minutes while the TV answered this
at once, with everything in one packet [V].

`parse_txt`, `parse_answer` and `Device` are pure; `Browser` needs the system bus and the
GLib main loop, and calls its callback there.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

SERVICE = "_googlecast._tcp"
AVAHI = "org.freedesktop.Avahi"
PROTO_UNSPEC = -1
PROTO_INET = 0
IF_UNSPEC = -1
MDNS_GROUP = ("224.0.0.251", 5353)
ASK_AFTER_MS = 2000
ASK_FOR_S = 1.5
TYPE_A, TYPE_PTR, TYPE_TXT, TYPE_SRV = 1, 12, 16, 33
CLASS_IN_UNICAST_RESPONSE = 0x8001


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    model: str
    host: str
    port: int

    @property
    def label(self) -> str:
        return self.name or self.model or self.host


def parse_txt(records) -> dict[str, str]:
    """Avahi's TXT records (arrays of bytes, `key=value`) as a dict; a
    record without `=` is a flag, kept with an empty value."""
    out = {}
    for record in records:
        raw = bytes(record).decode("utf-8", "replace")
        key, _, value = raw.partition("=")
        if key:
            out[key.lower()] = value
    return out


def device_from(name: str, host: str, port: int, txt: dict[str, str]) -> Device:
    """A resolved service as a Device; the mDNS instance name (a long
    model-and-hash string) stands in when the TXT has no id or no name."""
    return Device(id=txt.get("id") or name, name=txt.get("fn") or "", model=txt.get("md") or "",
                  host=host, port=port)


def _encode_name(name: str) -> bytes:
    return b"".join(bytes([len(part)]) + part.encode() for part in name.split(".")) + b"\0"


def query_packet() -> bytes:
    return (struct.pack(">6H", 0, 0, 1, 0, 0, 0) + _encode_name(f"{SERVICE}.local")
            + struct.pack(">HH", TYPE_PTR, CLASS_IN_UNICAST_RESPONSE))


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    """A DNS name, following compression, and the offset after it. It comes from the network: the jumps are limited."""
    parts, end, jumps = [], None, 0
    while True:
        length = data[offset]
        if length == 0:
            return ".".join(parts), end if end is not None else offset + 1
        if length & 0xC0 == 0xC0:
            jumps += 1
            if jumps > 16:
                raise ValueError("a name that points at itself")
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            continue
        parts.append(data[offset + 1:offset + 1 + length].decode("utf-8", "replace"))
        offset += 1 + length


def parse_answer(data: bytes) -> list[Device]:
    """The Cast devices described by one mDNS answer; [] for anything else, or anything broken."""
    try:
        questions, *counts = struct.unpack(">4H", data[4:12])
        offset = 12
        for _ in range(questions):
            _name, offset = _read_name(data, offset)
            offset += 4
        instances, ports, txts, addresses = [], {}, {}, {}
        for _ in range(sum(counts)):
            name, offset = _read_name(data, offset)
            kind, _class, _ttl, length = struct.unpack(">HHIH", data[offset:offset + 10])
            offset += 10
            body = data[offset:offset + length]
            if kind == TYPE_PTR and name == f"{SERVICE}.local":
                instances.append(_read_name(data, offset)[0])
            elif kind == TYPE_SRV:
                ports[name] = (struct.unpack(">H", body[4:6])[0], _read_name(data, offset + 6)[0])
            elif kind == TYPE_TXT:
                records, at = [], 0
                while at < len(body):
                    records.append(body[at + 1:at + 1 + body[at]])
                    at += 1 + body[at]
                txts[name] = parse_txt(records)
            elif kind == TYPE_A and length == 4:
                addresses[name] = socket.inet_ntoa(body)
            offset += length
    except (struct.error, IndexError, ValueError, OSError):
        return []
    found = []
    for instance in instances:
        port, host = ports.get(instance, (0, ""))
        if port and host in addresses:
            found.append(device_from(instance.split(f".{SERVICE}")[0], addresses[host], port, txts.get(instance, {})))
    return found


def ask_directly(wait_s: float = ASK_FOR_S) -> list[Device]:
    """Blocks for wait_s. The Cast devices that answer on the local network; [] if the network says no."""
    found: dict[str, Device] = {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(query_packet(), MDNS_GROUP)
            deadline = time.monotonic() + wait_s
            while (left := deadline - time.monotonic()) > 0:
                sock.settimeout(left)
                try:
                    data, _sender = sock.recvfrom(9000)
                except TimeoutError:
                    break
                found.update((d.id, d) for d in parse_answer(data))
    except OSError:
        return []                       # no network to ask on: Avahi's answer, or its absence, is what is said
    return list(found.values())


def pick(devices: list[Device], current_id: str, last_id: str) -> Device | None:
    """The TV to have selected in a new list: the one selected if it is
    still there, else the one used last, else the first."""
    by_id = {d.id: d for d in devices}
    return by_id.get(current_id) or by_id.get(last_id) or (devices[0] if devices else None)


class Browser:
    """Keeps `devices` up to date and calls `on_change(devices)` on the main
    loop whenever one appears, changes or goes."""

    def __init__(self, on_change: Callable[[list[Device]], None],
                 on_error: Callable[[str], None] | None = None):
        self.on_change = on_change
        self.on_error = on_error
        self.devices: dict[str, Device] = {}
        self._by_instance: dict[tuple, str] = {}    # (interface, protocol, mDNS instance name) -> device id
        self._bus = None
        self._browser_path = ""
        self._subs: list[int] = []
        self._ask_source = 0
        self._round = 0                 # an answer to a question from before a rescan is not wanted

    def start(self) -> None:
        from gi.repository import Gio, GLib
        self._ask_source = GLib.timeout_add(ASK_AFTER_MS, self._ask)
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as e:
            self._fail(f"no system bus: {e.message}")
            return
        for signal in ("ItemNew", "ItemRemove", "Failure"):
            self._subs.append(self._bus.signal_subscribe(
                AVAHI, f"{AVAHI}.ServiceBrowser", signal, None, None, Gio.DBusSignalFlags.NONE,
                self._on_signal))
        self._bus.call(AVAHI, "/", f"{AVAHI}.Server", "ServiceBrowserNew",
                       GLib.Variant("(iissu)", (IF_UNSPEC, PROTO_UNSPEC, SERVICE, "local", 0)),
                       GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 5000, None, self._browser_made)

    def rescan(self) -> None:
        self.stop()
        self.devices.clear()
        self._by_instance.clear()
        self._emit()
        self.start()

    def stop(self) -> None:
        self._round += 1
        if self._ask_source:
            from gi.repository import GLib
            GLib.source_remove(self._ask_source)
            self._ask_source = 0
        if not self._bus:
            return
        for sub in self._subs:
            self._bus.signal_unsubscribe(sub)
        self._subs.clear()
        if self._browser_path:
            from gi.repository import Gio
            self._bus.call(AVAHI, self._browser_path, f"{AVAHI}.ServiceBrowser", "Free", None, None,
                           Gio.DBusCallFlags.NONE, 2000, None, None)
            self._browser_path = ""

    def _ask(self) -> bool:
        from gi.repository import GLib
        self._ask_source = 0
        if self.devices:
            return False
        asked_in = self._round

        def work():
            found = ask_directly()
            GLib.idle_add(lambda: self._asked(found, asked_in) and False)
        threading.Thread(target=work, name="mdns-question", daemon=True).start()
        return False

    def _asked(self, found: list[Device], asked_in: int) -> None:
        if asked_in != self._round:
            return
        new = [d for d in found if d.id not in self.devices]
        for device in new:
            self.devices[device.id] = device
        if new:
            self._emit()

    def _browser_made(self, bus, result) -> None:
        from gi.repository import GLib
        try:
            (self._browser_path,) = bus.call_finish(result).unpack()
        except GLib.Error as e:
            self._fail("Avahi is not running" if "ServiceUnknown" in e.message else e.message)

    def _on_signal(self, bus, _sender, path, _iface, signal, params) -> None:
        if self._browser_path and path != self._browser_path:
            return
        if signal == "Failure":
            self._fail(params.unpack()[0])
            return
        interface, protocol, name, stype, domain, _flags = params.unpack()
        instance = (interface, protocol, name)
        if signal == "ItemRemove":
            device_id = self._by_instance.pop(instance, None)
            if device_id and device_id not in self._by_instance.values():
                self.devices.pop(device_id, None)
                self._emit()
            return
        from gi.repository import Gio, GLib
        bus.call(AVAHI, "/", f"{AVAHI}.Server", "ResolveService",
                 GLib.Variant("(iisssiu)", (interface, protocol, name, stype, domain, PROTO_INET, 0)),
                 None, Gio.DBusCallFlags.NONE, 5000, None, self._resolved, instance)

    def _resolved(self, bus, result, instance) -> None:
        from gi.repository import GLib
        try:
            values = bus.call_finish(result).unpack()
        except GLib.Error:
            return  # a TV that left between the announce and the resolve: the next announce brings it back
        _if, _proto, name, _type, _domain, _host, _aproto, address, port, txt, _flags = values
        if ":" in address:
            return  # no IPv4 address known for it yet: its announcement on the other protocol brings it
        device = device_from(name, address, port, parse_txt(txt))
        self._by_instance[instance] = device.id
        if self.devices.get(device.id) != device:
            self.devices[device.id] = device
            self._emit()

    def _emit(self) -> None:
        self.on_change(sorted(self.devices.values(), key=lambda d: d.label.lower()))

    def _fail(self, reason: str) -> None:
        if self.on_error:
            self.on_error(reason)

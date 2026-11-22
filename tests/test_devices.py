#!/usr/bin/env python3
"""The devices: TXT records read as Avahi hands them, a device named by
what the user called it, and the instance name when the TV says nothing."""

import socket
import struct
import unittest

import _setup  # noqa: F401

from lanterna import devices


def txt(*records: str):
    return [list(r.encode()) for r in records]       # Avahi gives aay: arrays of byte values


class PickTest(unittest.TestCase):
    def setUp(self):
        self.a = devices.Device("a", "Kitchen", "", "10.0.0.1", 8009)
        self.b = devices.Device("b", "Living Room", "", "10.0.0.2", 8009)

    def test_the_selected_one_stays_if_it_is_still_there(self):
        self.assertIs(devices.pick([self.a, self.b], "b", "a"), self.b)

    def test_else_the_one_used_last(self):
        self.assertIs(devices.pick([self.a, self.b], "gone", "b"), self.b)

    def test_else_the_first_and_none_when_there_are_none(self):
        self.assertIs(devices.pick([self.a, self.b], "", "gone"), self.a)
        self.assertIsNone(devices.pick([], "a", "b"))


def answer_packet(instance="2021/22-Philips-UHD--a1b2", host="a1b2.local", address="192.168.1.20", port=8009):
    """An mDNS answer as the Philips sends it: the question repeated, then PTR, TXT, SRV and A, with compression."""
    def name(text):
        return b"".join(bytes([len(part)]) + part.encode() for part in text.split(".")) + b"\0"

    def record(owner, kind, body):
        return owner + struct.pack(">HHIH", kind, 1, 120, len(body)) + body
    service = name("_googlecast._tcp.local")
    head = struct.pack(">6H", 0, 0x8400, 1, 1, 0, 3) + service + struct.pack(">HH", 12, 0x8001)
    pointer = struct.pack(">H", 0xC000 | 12)
    target = bytes([len(instance)]) + instance.encode() + pointer
    txt = b"".join(bytes([len(item)]) + item for item in (b"id=tv1", b"fn=Living Room TV"))
    full = bytes([len(instance)]) + instance.encode() + service
    return (head + record(pointer, 12, target) + record(full, 16, txt)
            + record(full, 33, struct.pack(">HHH", 0, 0, port) + name(host))
            + record(name(host), 1, socket.inet_aton(address)))


class DirectQuestionTest(unittest.TestCase):
    def test_an_answer_gives_the_whole_device(self):
        found = devices.parse_answer(answer_packet())
        self.assertEqual(found, [devices.Device("tv1", "Living Room TV", "", "192.168.1.20", 8009)])

    def test_the_question_asks_for_the_answer_on_our_own_socket(self):
        packet = devices.query_packet()
        self.assertEqual(struct.unpack(">HH", packet[-4:]), (12, 0x8001))
        self.assertIn(b"_googlecast", packet)

    def test_anything_broken_is_nothing_and_never_an_exception(self):
        whole = answer_packet()
        for cut in range(len(whole)):
            devices.parse_answer(whole[:cut])         # never raises
        self.assertEqual(devices.parse_answer(b""), [])
        self.assertEqual(devices.parse_answer(b"\xff" * 64), [])
        loop = struct.pack(">6H", 0, 0, 1, 0, 0, 0) + struct.pack(">H", 0xC000 | 12)      # a name pointing at itself
        self.assertEqual(devices.parse_answer(loop), [])

    def test_an_answer_without_an_address_gives_nothing(self):
        whole = answer_packet()
        no_address = whole[:6] + struct.pack(">H", 1) + whole[8:10] + struct.pack(">H", 1) + whole[12:]
        self.assertEqual(devices.parse_answer(no_address), [])

    def test_a_late_answer_from_before_a_rescan_is_dropped(self):
        changes = []
        browser = devices.Browser(changes.append)
        tv = devices.Device("tv1", "Living Room TV", "", "192.168.1.20", 8009)
        browser._asked([tv], browser._round - 1)
        self.assertEqual(changes, [])
        browser._asked([tv], browser._round)
        browser._asked([tv], browser._round)
        self.assertEqual(changes, [[tv]])             # the same TV again is no change


class FakeParams:
    def __init__(self, *values):
        self.values = values

    def unpack(self):
        return self.values


class FakeBus:
    """Avahi's answers, handed straight back; a resolve answers with what `addresses` holds for that protocol."""

    def __init__(self, addresses):
        self.addresses = addresses

    def call(self, _name, _path, _iface, method, params, _type, _flags, _timeout, _cancel, callback, instance):
        _if, protocol, name, _stype, _domain, _aproto, _flags2 = params.unpack()
        self.answer = (_if, protocol, name, "_googlecast._tcp", "local", "tv.local", 0, self.addresses[protocol], 8009,
                       [list(b"id=tv1"), list(b"fn=Living Room TV")], 0)
        callback(self, None, instance)

    def call_finish(self, _result):
        return FakeParams(*self.answer)


class BrowserTest(unittest.TestCase):
    def browser(self):
        changes = []
        browser = devices.Browser(changes.append)
        return browser, changes

    def item(self, browser, bus, signal, protocol):
        browser._on_signal(bus, None, "", "", signal, FakeParams(2, protocol, "Philips-a1b2", devices.SERVICE, "local", 0))

    def test_a_tv_announced_only_on_ipv6_is_found_with_its_ipv4_address(self):
        browser, changes = self.browser()
        self.item(browser, FakeBus({1: "192.168.1.20"}), "ItemNew", 1)
        self.assertEqual([d.host for d in changes[-1]], ["192.168.1.20"])

    def test_an_answer_with_no_ipv4_address_is_not_a_tv_yet(self):
        browser, changes = self.browser()
        self.item(browser, FakeBus({1: "fe80::4c36"}), "ItemNew", 1)
        self.assertEqual(changes, [])

    def test_the_tv_stays_until_its_last_announcement_goes(self):
        browser, changes = self.browser()
        bus = FakeBus({0: "192.168.1.20", 1: "192.168.1.20"})
        self.item(browser, bus, "ItemNew", 0)
        self.item(browser, bus, "ItemNew", 1)
        self.assertEqual(len(changes), 1)           # the same TV twice is no change
        self.item(browser, bus, "ItemRemove", 0)
        self.assertEqual(len(browser.devices), 1)
        self.item(browser, bus, "ItemRemove", 1)
        self.assertEqual(changes[-1], [])


class TxtTest(unittest.TestCase):
    def test_records_as_the_philips_sends_them(self):
        parsed = devices.parse_txt(txt("id=a1b2c3d4e5f60718293a4b5c6d7e8f90", "fn=Living Room TV",
                                       "md=2021/22 Philips UHD Android TV", "ca=463365", "rs="))
        self.assertEqual(parsed["fn"], "Living Room TV")
        self.assertEqual(parsed["md"], "2021/22 Philips UHD Android TV")
        self.assertEqual(parsed["rs"], "")

    def test_odd_records_do_not_break_the_rest(self):
        parsed = devices.parse_txt(txt("FN=Upper", "flag", "=novalue", "x=a=b"))
        self.assertEqual(parsed, {"fn": "Upper", "flag": "", "x": "a=b"})

    def test_bytes_that_are_not_utf8_are_replaced(self):
        self.assertIn("fn", devices.parse_txt([[ord("f"), ord("n"), ord("="), 0xFF]]))


class DeviceTest(unittest.TestCase):
    def test_a_device_is_named_by_its_friendly_name(self):
        device = devices.device_from("2021-22-Philips-UHD--a1b2", "192.168.1.20", 8009,
                                     {"id": "a1b2", "fn": "Living Room TV", "md": "Philips"})
        self.assertEqual((device.id, device.label, device.host, device.port),
                         ("a1b2", "Living Room TV", "192.168.1.20", 8009))

    def test_without_txt_the_instance_name_stands_in(self):
        device = devices.device_from("Chromecast-abc", "192.168.1.7", 8009, {})
        self.assertEqual(device.id, "Chromecast-abc")
        self.assertEqual(device.label, "192.168.1.7")


if __name__ == "__main__":
    unittest.main()

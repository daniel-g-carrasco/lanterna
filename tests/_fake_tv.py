"""A fake Chromecast for the tests: the TV's side of the Cast v2
conversation over a socketpair, scripted after what the Philips TV
answered on 25/10/2026 (LAUNCH_STATUS first, the app in a later
RECEIVER_STATUS, MEDIA_STATUS after LOAD and every command)."""

import threading
import time

import _setup  # noqa: F401

from lanterna import castv2
from lanterna.castv2 import Channel, Message


class FakeTV(threading.Thread):
    """The TV's side of the conversation, scripted after what the Philips
    answered on 25/10/2026: LAUNCH_STATUS first, the app in a later
    RECEIVER_STATUS, MEDIA_STATUS after LOAD and every command."""

    def __init__(self, sock, refuse_load=False, silent=False, refuse_types=(), stalls=False, silent_on_stop=False,
                 launch_delay=0.0, closes_on_load=False, old_media_ends=""):
        super().__init__(daemon=True)
        self.channel = Channel(sock)
        self.refuse_load = refuse_load
        self.silent = silent
        self.refuse_types = set(refuse_types)    # content types answered with LOAD_FAILED
        self.stalls = stalls                     # a LOAD accepted, and buffering for ever
        self.silent_on_stop = silent_on_stop     # the request to close the player is not answered
        self.launch_delay = launch_delay         # the player takes this long to start
        self.closes_on_load = closes_on_load     # the player goes away instead of answering a LOAD
        self.old_media_ends = old_media_ends     # "before" or "after" the answer to the next LOAD, as the Philips does
        self.session = 0
        self.loads: list[dict] = []
        self.seen: list[Message] = []
        self.state = "IDLE"
        self.volume = 0.35

    def reply(self, to: Message, payload: dict, source=None):
        self.channel.send(Message(source or to.destination, to.source, to.namespace, payload))

    def status(self, to: Message, rid: int, apps: bool):
        applications = [{"appId": "CC1AD845", "transportId": "web-7", "sessionId": "sess-1"}] if apps else []
        self.reply(to, {"type": "RECEIVER_STATUS", "requestId": rid,
                        "status": {"applications": applications, "volume": {"level": self.volume, "muted": False}}},
                   source="receiver-0")

    def media_status(self, to: Message, rid: int):
        self.reply(to, {"type": "MEDIA_STATUS", "requestId": rid, "status": [{
            "mediaSessionId": self.session or 1, "playerState": self.state, "currentTime": 12.5, "playbackRate": 1,
            "media": {"duration": 5400.0}}]})

    def media_ended(self, to: Message, session: int):
        self.reply(to, {"type": "MEDIA_STATUS", "requestId": 0, "status": [{
            "mediaSessionId": session, "playerState": "IDLE", "idleReason": "FINISHED", "currentTime": 0,
            "playbackRate": 1, "media": {"duration": 5400.0}}]})

    def run(self):
        launched = False
        try:
            while True:
                m = self.channel.recv()
                self.seen.append(m)
                if self.silent:
                    continue
                kind, rid = m.payload.get("type"), m.payload.get("requestId", 0)
                if m.namespace == castv2.NS_HEARTBEAT and kind == "PING":
                    self.reply(m, {"type": "PONG"})
                elif kind == "LAUNCH":
                    self.reply(m, {"type": "LAUNCH_STATUS", "status": "USER_ALLOWED", "requestId": rid},
                               source="receiver-0")
                    time.sleep(self.launch_delay)
                    launched = True
                elif kind == "GET_STATUS":
                    self.status(m, rid, apps=launched)
                elif kind == "SET_VOLUME":
                    self.volume = m.payload["volume"].get("level", self.volume)
                    self.status(m, rid, apps=launched)
                elif kind == "LOAD":
                    self.loads.append(m.payload["media"])
                    old_session, self.session = self.session, self.session + 1
                    if old_session and self.old_media_ends == "before":
                        self.media_ended(m, old_session)
                    if self.closes_on_load:
                        self.channel.send(Message(m.destination, m.source, castv2.NS_CONNECTION, {"type": "CLOSE"}))
                        continue
                    if self.refuse_load or m.payload["media"]["contentType"] in self.refuse_types:
                        self.reply(m, {"type": "LOAD_FAILED", "requestId": rid})
                    else:
                        # a photo is reported PAUSED once on screen, as the Philips does
                        photo = m.payload["media"]["contentType"].startswith("image/")
                        self.state = "BUFFERING" if self.stalls else "PAUSED" if photo else "PLAYING"
                        self.media_status(m, rid)
                        if old_session and self.old_media_ends == "after":
                            self.media_ended(m, old_session)
                elif kind == "STOP" and self.silent_on_stop:
                    pass
                elif kind in ("PAUSE", "PLAY", "SEEK", "STOP"):
                    self.state = {"PAUSE": "PAUSED", "PLAY": "PLAYING", "SEEK": self.state, "STOP": "IDLE"}[kind]
                    self.media_status(m, rid)
                elif kind == "STOP_APP":
                    pass
        except (castv2.CastError, OSError, ValueError):
            return

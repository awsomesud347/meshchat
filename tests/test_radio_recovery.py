"""Crash recovery of the Meshtastic adapter, against simulated boards that crash the ways real ones do.

The fake board stands in for meshtastic.SerialInterface and behaves like the library does:
  * drop:   port disappears, library publishes connection.lost, stream=None, later writes are silently discarded
  * reboot: firmware restarts behind the USB-serial chip; port stays open, raw boot-log lines arrive, packets stop
  * freeze: board stops doing anything, no signal at all (only /reconnect helps)
"""
import argparse
import io
import itertools
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "codec"))
import runtime  # noqa: E402

runtime.prepare_imports()
pubsub = pytest.importorskip("pubsub")
from pubsub import pub  # noqa: E402

import link  # noqa: E402
import transports  # noqa: E402

_ids = itertools.count(1)


class Air:
    def __init__(self):
        self.boards = {}

    def board(self, num, port):
        b = Board(self, num, port)
        self.boards[port] = b
        return b

    def by_num(self, num):
        return next((b for b in self.boards.values() if b.num == num), None)

    def transmit(self, iface, data, dest, want_ack, on_resp):
        src = iface.board
        alive = iface is src.iface and src.api_alive and iface.stream is not None and src.present
        if not alive:
            return                                   # the real library silently drops writes here
        dst = self.by_num(dest)
        ok = dst is not None and dst.present and dst.api_alive and dst.iface is not None

        def deliver():
            time.sleep(0.02)
            if ok:
                pub.sendMessage("meshtastic.receive", interface=dst.iface, packet={
                    "from": src.num, "to": dest, "id": next(_ids), "rxSnr": 5.0, "rxRssi": -60,
                    "decoded": {"portnum": "PRIVATE_APP", "payload": bytes(data)}})
            if want_ack and on_resp:
                on_resp({"decoded": {"routing": {"errorReason": "NONE" if ok else "MAX_RETRANSMIT"}}})
        threading.Thread(target=deliver, daemon=True).start()

    def factory(self, port, owner):
        b = self.boards[port]
        if not b.present:
            raise RuntimeError(f"could not open port {port}")
        return FakeIface(b, owner)


class Board:
    def __init__(self, air, num, port):
        self.air, self.num, self.port = air, num, port
        self.present, self.api_alive, self.iface, self.opens = True, True, None, 0

    def drop(self, down_s):
        self.present = False
        it = self.iface
        it.stream = None
        it.isConnected.clear()
        pub.sendMessage("meshtastic.connection.lost", interface=it)
        threading.Timer(down_s, lambda: setattr(self, "present", True)).start()

    def reboot(self):
        self.api_alive = False
        for _ in range(6):                      # boot banner lines arriving as raw text
            self.iface._owner._raw_line(self.iface)

    def freeze(self):
        self.api_alive = False


class FakeIface:
    def __init__(self, board, owner):
        self.board, self._owner = board, owner
        self.isConnected = threading.Event()
        self.isConnected.set()
        self.stream = object()
        self.nodesByNum = {}
        self.myInfo = SimpleNamespace(my_node_num=board.num)
        board.iface, board.api_alive = self, True
        board.opens += 1

    def getMyNodeInfo(self):
        return {"num": self.board.num, "user": {"longName": f"Board {self.board.num:x}"}}

    def sendData(self, data, destinationId, portNum, wantAck, onResponse, onResponseAckPermitted):
        assert portNum == 256
        self.board.air.transmit(self, data, destinationId, wantAck, onResponse)
        return SimpleNamespace(id=next(_ids))

    def close(self):
        self.stream = None


@pytest.fixture
def air(monkeypatch):
    a = Air()
    monkeypatch.setattr(transports, "_port_present", lambda p: a.boards[p].present if p in a.boards else False)
    monkeypatch.setattr(transports, "list_radio_ports", lambda: [])
    return a


def radio(air, num, port, events):
    air.board(num, port)
    return transports.MeshtasticRadio(port, status=events.append, iface_factory=air.factory,
                                      send_wait=10, grace_s=0.1, stuck_s=1.0, retry_s=0.1)


def pair(air, ack_timeout=2.5):
    ev_a, ev_b = [], []
    ra, rb = radio(air, 0xA, "COM4", ev_a), radio(air, 0xB, "COM5", ev_b)
    la = link.Link(ra, None, ev_a.append, ack_timeout=ack_timeout)
    lb = link.Link(rb, None, ev_b.append, ack_timeout=ack_timeout)
    return ra, rb, la, lb, ev_a, ev_b


def wait_for(cond, s=5.0):
    end = time.time() + s
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def kinds(events, k):
    return [e for e in events if e.get("event") == k]


def test_normal_two_way(air):
    ra, rb, la, lb, *_ = pair(air)
    assert la.send("Water rising at the school gym.", rb.node_id).state == "VERIFIED"
    assert lb.send("Team en route, 20 min.", ra.node_id).state == "VERIFIED"
    assert [r["text"] for r in lb.received] == ["Water rising at the school gym."]
    assert [r["text"] for r in la.received] == ["Team en route, 20 min."]


def test_receiver_board_drops_usb_and_comes_back(air):
    ra, rb, la, lb, ev_a, ev_b = pair(air)
    air.boards["COM5"].drop(down_s=0.8)
    out = la.send("Two families on the roof.", rb.node_id)       # attempt 1 lost, raw retry after reconnect
    assert out.state == "VERIFIED", out
    assert wait_for(lambda: kinds(ev_b, "radio_up"))
    assert kinds(ev_b, "radio_down") and air.boards["COM5"].opens == 2
    assert [r["text"] for r in lb.received] == ["Two families on the roof."]
    assert lb.send("Copy.", ra.node_id).state == "VERIFIED"       # and B can talk again


def test_sender_board_drops_send_waits_instead_of_vanishing(air):
    ra, rb, la, lb, ev_a, _ = pair(air)
    air.boards["COM4"].drop(down_s=0.8)
    assert wait_for(lambda: kinds(ev_a, "radio_down"))
    out = la.send("Insulin needed at 12 Elm.", rb.node_id)        # waits for the radio, then sends
    assert out.state == "VERIFIED" and len(out.attempts) == 1
    assert kinds(ev_a, "radio_up")


def test_firmware_reboot_detected_from_boot_log(air):
    ra, rb, la, lb, ev_a, _ = pair(air)
    time.sleep(0.2)                  # past the post-open grace window
    air.boards["COM4"].reboot()
    assert wait_for(lambda: kinds(ev_a, "radio_up"))
    assert "rebooted" in kinds(ev_a, "radio_down")[0]["why"]
    assert la.send("After reboot.", rb.node_id).state == "VERIFIED"


def test_raw_lines_right_after_open_are_ignored(air):
    ev = []
    r = radio(air, 0xC, "COM6", ev)
    r.grace_s = 5.0
    air.boards["COM6"].reboot()      # within grace: boot chatter while the port opens is normal
    time.sleep(0.3)
    assert not kinds(ev, "radio_down")


def test_frozen_board_manual_reconnect(air):
    ra, rb, la, lb, ev_a, _ = pair(air, ack_timeout=0.4)
    air.boards["COM4"].freeze()
    out = la.send("Frozen?", rb.node_id)
    assert out.state == "NOT_DELIVERED"                           # nothing went out, and we say so
    ra.reconnect()
    assert wait_for(lambda: kinds(ev_a, "radio_up"))
    assert la.send("Frozen?", rb.node_id, msg_id=out.msg_id).state == "VERIFIED"
    assert [r["text"] for r in lb.received] == ["Frozen?"]


def test_library_stuck_disconnected_triggers_reopen(air):
    ra, rb, la, lb, ev_a, _ = pair(air)
    air.boards["COM4"].iface.isConnected.clear()                  # e.g. 'rebooted' handled but config never finished
    assert wait_for(lambda: kinds(ev_a, "radio_up"), s=4)
    assert "not reconnected" in kinds(ev_a, "radio_down")[0]["why"]


def test_chat_auto_resends_after_peer_crash(air, monkeypatch):
    """End to end through `meshchat chat`: the hub's board is gone longer than both attempts; the field side
    keeps the message, resends it with the same id when the hub is back, and the hub shows it exactly once."""
    sys.path.insert(0, str(ROOT))
    import meshchat
    ev_b = []
    rb = radio(air, 0xB, "COM5", ev_b)
    lb = link.Link(rb, None, ev_b.append, ack_timeout=0.5)
    ra_holder = {}

    def fake_open(args, status=None):
        ra_holder["r"] = radio(air, 0xA, "COM4", [])
        ra_holder["r"].status = status
        return ra_holder["r"]
    monkeypatch.setattr(meshchat, "open_transport", fake_open)
    air.boards["COM5"].drop(down_s=1.5)
    args = argparse.Namespace(model=None, no_model=True, port="COM4", udp=None, peer=None, name=None,
                              dest=rb.node_id, ack_timeout=0.3, send=None, probes=False, batch=True, linger=4.0,
                              verbose=False, no_auto_resend=False, max_resends=5, resend_every=0.5)
    monkeypatch.setattr(sys, "stdin", io.StringIO("Bridge on Route 9 is out, use the north road.\n"))
    meshchat.cmd_chat(args)
    assert [r["text"] for r in lb.received] == ["Bridge on Route 9 is out, use the north road."]

"""Link-layer tests: two endpoints, both directions, every failure path. No radios and no model weights needed.

FakeLLM stands in for SharedModel: both ends hold a "shared table" and send a 1-byte index. A mismatched table is the
exact failure mode of a drifted model: the receiver decodes *plausible but wrong* text, and only the CRC catches it.

    python -m pytest tests -q
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "codec"))
import runtime  # noqa: E402

runtime.prepare_imports()
import envelope as env  # noqa: E402
import link  # noqa: E402
import mesh_codec as mc  # noqa: E402
import transports  # noqa: E402

TEXTS = ["Water is rising into the school gym on 5th Street, two families on the roof.",
         "Team en route, 20 min. Keep everyone upstairs.",
         "Elderly man at 12 Elm Road needs insulin today.",
         "Café crème — 避難所 🚑 needs water"]


class FakeLLM:
    model_id = 2

    def __init__(self, table):
        self.table = list(table)

    def compress(self, text):
        return bytes([self.table.index(text)]) if text in self.table else None

    def decompress(self, data, max_tokens=None):
        i = data[0] if data else 255
        return self.table[i] if i < len(self.table) else None


def pair(model_a=None, model_b=None, timeout=1.0, udp=False):
    events = []
    if udp:
        ta = transports.UdpLink("127.0.0.1:0", None, "test-A")
        tb = transports.UdpLink("127.0.0.1:0", None, "test-B")
        ta.peer = ("127.0.0.1", tb.sock.getsockname()[1])
        tb.peer = ("127.0.0.1", ta.sock.getsockname()[1])
        air = None
    else:
        air = transports.LoopbackAir()
        ta, tb = air.radio("!0000000a"), air.radio("!0000000b")
    a = link.Link(ta, model_a, events.append, ack_timeout=timeout)
    b = link.Link(tb, model_b, events.append, ack_timeout=timeout)
    return a, b, ta, tb, air, events


def settle(cond, s=2.0):
    end = time.time() + s
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_two_way_without_model():
    a, b, ta, tb, *_ = pair()
    assert a.send(TEXTS[0], tb.node_id).state == "VERIFIED"
    assert b.send(TEXTS[1], ta.node_id).state == "VERIFIED"
    assert [r["text"] for r in b.received] == [TEXTS[0]]
    assert [r["text"] for r in a.received] == [TEXTS[1]]


def test_two_way_llm_shared_model():
    m = FakeLLM(TEXTS)
    a, b, ta, tb, air, _ = pair(m, FakeLLM(TEXTS))
    for i, (s, r, dst) in enumerate([(a, b, tb), (b, a, ta), (a, b, tb), (b, a, ta)]):
        out = s.send(TEXTS[i], dst.node_id)
        assert out.state == "VERIFIED" and out.attempts[0].method == "llm" and len(out.attempts) == 1
    assert [r["text"] for r in b.received] == [TEXTS[0], TEXTS[2]]
    assert [r["text"] for r in a.received] == [TEXTS[1], TEXTS[3]]
    assert all(r["method"] == "llm" for r in a.received + b.received)


def test_model_drift_is_caught_by_crc_and_raw_retry_delivers():
    a, b, ta, tb, _, events = pair(FakeLLM(TEXTS), FakeLLM(list(reversed(TEXTS))))
    out = a.send(TEXTS[0], tb.node_id)
    assert out.state == "VERIFIED"
    assert [x.method for x in out.attempts] == ["llm", "raw"]
    assert out.attempts[0].decode_status == "DECODE_FAILED"
    assert [r["text"] for r in b.received] == [TEXTS[0]]          # never the wrong text
    assert any(e["event"] == "decode_failed" and e["reason"] == "checksum failed" for e in events)


def test_receiver_without_model_rejects_then_raw_delivers():
    a, b, ta, tb, *_ = pair(FakeLLM(TEXTS), None)
    out = a.send(TEXTS[1], tb.node_id)
    assert out.state == "VERIFIED" and [x.method for x in out.attempts] == ["llm", "raw"]


def test_corrupt_first_forces_raw_retry():
    a, b, ta, tb, *_ = pair()
    out = a.send(TEXTS[2], tb.node_id, corrupt_first=True)
    assert out.state == "VERIFIED" and len(out.attempts) == 2 and out.attempts[1].method == "raw"
    assert [r["text"] for r in b.received] == [TEXTS[2]]


def test_lost_first_attempt_times_out_then_raw():
    a, b, ta, tb, air, _ = pair(timeout=0.3)
    n = {"c": 0}

    def drop_first(frame, src, dst):
        n["c"] += 1
        return None if n["c"] == 1 else frame
    air.tamper = drop_first
    out = a.send(TEXTS[0], tb.node_id)
    assert out.state == "VERIFIED" and len(out.attempts) == 2
    assert out.attempts[0].radio_ack is False


def test_lost_decode_ack_retry_is_duplicate_not_shown_twice():
    a, b, ta, tb, air, events = pair(timeout=0.3)

    def drop_first_ack(frame, src, dst):
        if env.decode(frame).__class__ is env.DecodeAck and not getattr(drop_first_ack, "done", False):
            drop_first_ack.done = True
            return None
        return frame
    air.tamper = drop_first_ack
    out = a.send(TEXTS[0], tb.node_id, msg_id=0xC0FFEE)
    assert out.state == "VERIFIED"
    assert [r["text"] for r in b.received] == [TEXTS[0]]   # delivered once even though it arrived twice


def test_same_id_different_text_is_conflict():
    a, b, ta, tb, *_ = pair()
    assert a.send(TEXTS[0], tb.node_id, msg_id=7).state == "VERIFIED"
    assert a.send(TEXTS[1], tb.node_id, msg_id=7).state == "CONFLICT"


def test_oversized_is_rejected_before_air():
    a, b, ta, tb, air, _ = pair()
    import random
    rnd = random.Random(1)
    out = a.send("".join(rnd.choice("qzxjkvbpgw0123456789#%&") for _ in range(400)), tb.node_id)
    assert out.state == "REJECTED" and not out.attempts and air.log == []


def test_newline_text_goes_exact_without_llm():
    a, b, ta, tb, *_ = pair(FakeLLM(TEXTS), FakeLLM(TEXTS))
    out = a.send("line one\nline two", tb.node_id)
    assert out.state == "VERIFIED" and out.attempts[0].method != "llm"
    assert b.received[0]["text"] == "line one\nline two"


def test_frames_for_other_nodes_are_ignored():
    a, b, ta, tb, air, events = pair(timeout=0.2)
    c = air.radio("!0000000c")
    out = a.send(TEXTS[0], "!0000000c")   # c has no Link, nobody decodes
    assert out.state in ("RADIO_ACK_ONLY", "NOT_DELIVERED") and b.received == []


def test_garbage_frame_never_crashes_receiver():
    a, b, ta, tb, air, events = pair()
    tb._cb(b"\xff\x00garbage", {"from": "!0000000a", "to": "!0000000b"})
    tb._cb(bytes([0x11]) + b"\x00" * 9 + b"\x22\x00", {"from": "!0000000a", "to": "!0000000b"})
    assert settle(lambda: sum(e["event"] in ("rejected", "decode_failed") for e in events) == 2)
    assert a.send(TEXTS[0], tb.node_id).state == "VERIFIED"


def test_udp_two_way_with_llm():
    a, b, ta, tb, *_ = pair(FakeLLM(TEXTS), FakeLLM(TEXTS), timeout=2.0, udp=True)
    try:
        assert a.send(TEXTS[0], "!00000000").state == "VERIFIED"       # "whoever is on the other end"
        assert settle(lambda: b.last_sender == ta.node_id)
        assert b.send(TEXTS[1], b.last_sender).state == "VERIFIED"     # reply by learned id
        assert [r["text"] for r in b.received] == [TEXTS[0]]
        assert [r["text"] for r in a.received] == [TEXTS[1]]
        assert b.received[0]["method"] == "llm"
    finally:
        ta.close(); tb.close()


def test_concurrent_both_directions():
    import threading
    a, b, ta, tb, *_ = pair(FakeLLM(TEXTS), FakeLLM(TEXTS), timeout=2.0)
    res = []
    th = [threading.Thread(target=lambda s=s, d=d, t=t: res.append(s.send(t, d).state))
          for s, d, t in [(a, tb.node_id, TEXTS[0]), (b, ta.node_id, TEXTS[1]),
                          (a, tb.node_id, TEXTS[2]), (b, ta.node_id, TEXTS[3])]]
    [x.start() for x in th]
    [x.join() for x in th]
    assert sorted(res) == ["VERIFIED"] * 4
    assert sorted(r["text"] for r in a.received) == sorted([TEXTS[1], TEXTS[3]])


def test_pinned_runtime_matches_hashes():
    if runtime.platform_dir().name not in ("linux-x64", "win-x64"):
        pytest.skip("no pinned runtime for this platform")
    runtime.verify_runtime()
    assert runtime.sha256(runtime.context_path()) == runtime.CONTEXT_SHA256

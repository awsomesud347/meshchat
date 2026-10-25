"""The browser front end, end to end over loopback radios (no model, no hardware): HTTP endpoints, the live event
stream with its ms timestamps and airtime enrichment, the simulated peer, and the corruption demo."""
import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "codec"))
import runtime  # noqa: E402

runtime.prepare_imports()
pytest.importorskip("pubsub")
import transports  # noqa: E402
import webui  # noqa: E402
from meshchat import ChatSession  # noqa: E402


@pytest.fixture
def gui():
    args = argparse.Namespace(dest="!0000000b", ack_timeout=3.0, no_auto_resend=True, max_resends=0,
                              resend_every=0.0, verbose=False)
    hub = webui.Hub()
    air = transports.LoopbackAir()
    mine, theirs = air.radio("!0000000a"), air.radio("!0000000b")
    session = ChatSession(args, None, hub.publish, lambda status_cb: mine)
    webui.start_loopback_peer(session, theirs)
    srv = webui.serve(session, hub, "disabled (test)", 0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    events, stop = [], threading.Event()

    def listen():
        with urllib.request.urlopen(base + "/events", timeout=30) as r:
            for raw in r:
                if stop.is_set():
                    return
                line = raw.decode("utf-8").strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

    threading.Thread(target=listen, daemon=True).start()
    yield base, events, session
    stop.set()
    srv.shutdown()
    session.close()


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read()


def post(base, line):
    req = urllib.request.Request(base + "/send", data=json.dumps({"line": line}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_for(events, pred, s=8.0):
    end = time.time() + s
    while time.time() < end:
        hit = [e for e in list(events) if pred(e)]
        if hit:
            return hit
        time.sleep(0.02)
    raise AssertionError(f"timed out; saw {[e.get('event') for e in events]}")


def test_page_and_state(gui):
    base, _, _ = gui
    code, body = get(base + "/")
    assert code == 200 and b"<title>meshchat</title>" in body
    st = json.loads(get(base + "/state")[1])
    assert st["me"]["id"] == "!0000000a" and st["peer"]["id"] == "!0000000b"
    assert st["codec"]["ok"] is False and st["radio"]["up"] is True and st["pending"] == []


def test_send_streams_enriched_events_and_peer_answers(gui):
    base, events, _ = gui
    assert post(base, "Water rising at the school gym.") == (200, {"ok": True})
    out = wait_for(events, lambda e: e["event"] == "outcome")[0]
    assert out["state"] == "VERIFIED"
    send = wait_for(events, lambda e: e["event"] == "send")[0]
    assert send["text"] == "Water rising at the school gym." and isinstance(send["ts"], int)
    assert send["air_ms_plain"] > 0 and send["air_ms_frame"] > 0
    assert send["air_ms_frame"] == round(webui.air_ms(send["frame_bytes"]))
    assert wait_for(events, lambda e: e["event"] == "radio_ack")[0]["ok"] is True
    rx = wait_for(events, lambda e: e["event"] == "decoded")[0]   # the simulated peer answers once
    assert rx["text"] == "Copy that: Water rising at the school gym." and rx["air_ms_frame"] > 0


def test_corruption_demo_falls_back_to_raw(gui):
    base, events, _ = gui
    post(base, "/corrupt Shelter at the library needs blankets.")
    out = wait_for(events, lambda e: e["event"] == "outcome")[0]
    assert out["state"] == "VERIFIED"
    assert [a["decode_status"] for a in out["attempts"]] == ["DECODE_FAILED", "VERIFIED"]
    assert out["attempts"][1]["method"] == "raw"
    assert wait_for(events, lambda e: e["event"] == "retry")


def test_commands_reply_as_info_events(gui):
    base, events, _ = gui
    post(base, "/status")
    info = wait_for(events, lambda e: e["event"] == "info")[0]
    assert "radio up: True" in info["text"] and "undelivered: none" in info["text"]
    assert post(base, "/quit")[0] == 400                    # the page cannot stop meshchat
    assert post(base, "two\nlines")[0] == 400


def test_preview_sizes(gui):
    base, _, _ = gui
    p = json.loads(get(base + "/preview?text=" + urllib.request.quote("Bridge on Route 9 is out."))[1])
    assert p["text_bytes"] == 25 and p["envelope"] == 10 and p["limit"] == 216
    assert set(p["methods"]) == {"raw", "unishox2"}       # no model in this test: no LLM row
    assert p["methods"]["raw"]["frame"] == 10 + 2 + 25 + 2
    assert p["best"] in ("raw", "unishox2") and p["methods"][p["best"]]["fits"]
    assert 0 < p["survival"]["ours_3hop"] <= 1
    empty = json.loads(get(base + "/preview?text=")[1])
    assert empty["methods"] == {} and empty["best"] is None
    long = json.loads(get(base + "/preview?text=" + "x" * 400)[1])
    assert "error" in long["methods"]["raw"]
    typed = json.loads(get(base + "/preview?typing=1&text=" + urllib.request.quote("Bridge on Route 9 is out."))[1])
    assert typed == p                                     # served from the cache


def test_stale_typing_preview_gives_up(gui):
    base, _, session = gui
    results = {}
    with session.link.mlock:                              # model busy: both typing previews have to wait
        t1 = threading.Thread(target=lambda: results.setdefault("old", json.loads(get(base + "/preview?typing=1&text=Brid")[1])))
        t1.start()
        time.sleep(0.3)
        t2 = threading.Thread(target=lambda: results.setdefault("new", json.loads(get(base + "/preview?typing=1&text=Bridge")[1])))
        t2.start()
        time.sleep(0.3)
    t1.join(5); t2.join(5)
    assert results["old"] == {"stale": True}
    assert results["new"]["text_bytes"] == 6


def test_history_replays_to_a_new_page(gui):
    base, events, _ = gui
    post(base, "first")
    wait_for(events, lambda e: e["event"] == "outcome")
    seen = []
    with urllib.request.urlopen(base + "/events", timeout=10) as r:
        for raw in r:
            line = raw.decode().strip()
            if line.startswith("data: "):
                seen.append(json.loads(line[6:])["event"])
                if "outcome" in seen:
                    break
    assert seen[0] == "send" and "outcome" in seen

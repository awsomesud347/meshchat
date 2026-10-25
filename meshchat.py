"""meshchat: two-way LLM-compressed messaging between two laptops, over Meshtastic radios or plain UDP.

    python meshchat.py check                      prove this laptop computes the shared codec bit-for-bit
    python meshchat.py selftest [--udp]           two endpoints inside one process (no radios, no network)
    python meshchat.py chat --udp 47000 --peer 192.168.1.20:47000      two laptops over Wi-Fi/LAN
    python meshchat.py chat --port COM4 --dest !f99260d4               two laptops over Meshtastic radios
    python meshchat.py explain "text"             per-token bit cost (what the model found surprising)

In chat, type a line and press Enter to send it. Incoming messages print as they arrive. Commands:
    /to !nodeid      set who plain lines go to (default: --dest, else whoever messaged you last)
    /nodes           list nodes the radio (or UDP link) has heard
    /raw TEXT        send uncompressed            /unishox TEXT   send with Unishox2 only
    /corrupt TEXT    corrupt attempt 1 on purpose: the peer rejects it, the raw retry delivers
    /probes          send the 8 reference probe texts
    /explain TEXT    show per-token bits locally   /stats   totals so far
    /status          radio state, reconnects, undelivered messages
    /retry           resend undelivered messages now (same ids: the receiver never shows a message twice)
    /reconnect       force the radio to reopen (use if the board froze without rebooting)
    /quit

If the board crashes, reboots or is unplugged, meshchat reopens it by itself. Messages typed meanwhile wait, and
messages that did not get a decode receipt are resent automatically once the radio is back.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "codec"))
import runtime  # noqa: E402  (must come before anything imports llama_cpp)

runtime.prepare_imports()
import mesh_codec as mc  # noqa: E402
import lora_math  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REF = json.loads((ROOT / "data" / "endpoint_ref.json").read_text(encoding="utf-8"))
PROBES = [p["text"] for p in REF["probes"]]
_print_lock = threading.Lock()


def say(*a, **k):
    with _print_lock:
        print(*a, **k, flush=True)


def air_ms(nbytes: int) -> float:
    return lora_math.airtime_ms(nbytes, 11, 250.0, 5)   # Meshtastic LongFast (default preset)


# ------------------------------------------------------------------------------------------------ model
def load_model(args, quiet=False):
    """Returns (model or None, status string). Never exits: without a usable model we still talk raw/Unishox2."""
    if getattr(args, "no_model", False):
        return None, "disabled (--no-model): raw/Unishox2 only"
    try:
        path = runtime.find_model(getattr(args, "model", None))
    except RuntimeError as e:
        return None, f"UNAVAILABLE ({e}): raw/Unishox2 only"
    if path is None:
        return None, (f"UNAVAILABLE: {runtime.MODEL_FILE} not found (pass --model or put it in "
                      f"{ROOT / 'models'}): raw/Unishox2 only")
    if not quiet:
        say(f"loading model {path} (hash check + shared context, a few seconds)...")
    t0 = time.time()
    try:
        m = runtime.load(path)
    except Exception as e:
        return None, f"UNAVAILABLE ({e}): raw/Unishox2 only"
    fp = runtime.fingerprint(m)
    if fp != runtime.REFERENCE_FINGERPRINT:
        return None, (f"FINGERPRINT MISMATCH {fp} != {runtime.REFERENCE_FINGERPRINT}: this machine computes "
                      f"different probabilities, LLM disabled (raw/Unishox2 only)")
    return m, f"SmolLM2-360M OK, fingerprint {fp}, loaded in {time.time() - t0:.1f}s"


# ------------------------------------------------------------------------------------------------ display
def make_emit(role: str, verbose: bool):
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    path = logs / f"meshchat-{role}-{socket.gethostname()}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    f = open(path, "a", encoding="utf-8")
    lock = threading.Lock()
    say(f"log: {path}")

    def emit(ev: dict):
        ev = {"t": time.strftime("%H:%M:%S"), **ev}
        with lock:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            f.flush()
        show(ev, verbose)
    return emit


def show(e: dict, verbose: bool):
    k, t = e["event"], e["t"]
    if k == "send":
        say(f"{t}  >> TX {e['msg_id']} try {e['attempt']} {e['method'].upper()}  "
            f"{e['text_bytes']} B text -> {e['codec_bytes']} B codec packet ({e['frame_bytes']} B on air)  "
            f"airtime ~{air_ms(e['text_bytes']):.0f} ms raw -> {air_ms(e['frame_bytes']):.0f} ms")
        if verbose:
            say(f"           frame {e['frame']}")
    elif k == "radio_ack":
        say(f"{t}     radio ACK try {e['attempt']}: {'yes' if e['ok'] else 'NO (' + str(e['detail']) + ')'}")
    elif k == "decode_ack":
        say(f"{t}     decode receipt {e['status']} for {e['msg_id']} try {e['attempt']}"
            + ("  (late)" if e.get("late") else ""))
    elif k == "retry":
        say(f"{t}     attempt 1 not verified ({e['why']}): one raw retry, same message id")
    elif k == "outcome":
        st = e["state"]
        mark = "OK " if st.startswith("VERIFIED") else "!! "
        rtt = [a["rtt_s"] for a in e["attempts"] if a.get("rtt_s") is not None]
        say(f"{t}  {mark}{st}  {e['msg_id']}  {e['text_bytes']} B text, {e['air_bytes']} B on air"
            + (f", receipt in {rtt[-1]}s" if rtt else "") + (f"  ({e['reason']})" if e.get("reason") else ""))
    elif k == "decoded":
        tag = {"new": "", "duplicate": "  [duplicate, already shown]", "conflict": "  [CONFLICT: same id, new text]"}
        if e["kind"] == "duplicate":
            say(f"{t}  << RX {e['msg_id']} again from {e['from']}{tag['duplicate']}")
            return
        sig = f" snr={e['snr']} rssi={e['rssi']}" if e.get("rssi") is not None else ""
        say(f"{t}  << RX from {e['from']}  {e['method'].upper()}  {e['frame_bytes']} B on air -> "
            f"{e['text_bytes']} B text, CRC OK, decoded in {e['decode_s']}s{sig}{tag[e['kind']]}")
        say(f"           \"{e['text']}\"")
    elif k == "decode_failed":
        say(f"{t}  << RX REJECTED {e['msg_id']} try {e['attempt']} from {e['from']}: {e['reason']} "
            f"(asked sender to retry raw)")
    elif k in ("rejected", "rx_error", "send_error"):
        say(f"{t}  !! {k}: {e.get('reason') or e.get('error')}")
    elif k == "radio_down":
        say(f"{t}  !! RADIO DOWN: {e['why']}\n"
            f"           reconnecting automatically; anything you type waits and goes out when it is back")
    elif k == "radio_retrying":
        say(f"{t}     radio: still trying to reopen {e['port']} (attempt {e['attempt']}): {e['error']}")
    elif k == "radio_up":
        say(f"{t}  OK RADIO BACK on {e['port']} ({e['node']}) after {e['after_s']}s")
    elif k == "radio_warning":
        say(f"{t}  !! radio: {e['why']}")
    elif k == "resend":
        say(f"{t}     resending {e['count']} undelivered message(s) with their original ids ({e['why']})")
    elif k == "ignored" and verbose:
        say(f"{t}     ignored frame from {e['from']} to {e['to']}")
    elif k == "decode_ack_sent" and verbose:
        say(f"{t}     sent decode receipt {e['status']} to {e['to']}")


# ------------------------------------------------------------------------------------------------ transport
def open_transport(args, status=None):
    import transports
    if args.udp is not None:
        return transports.UdpLink(f"0.0.0.0:{args.udp}" if str(args.udp).isdigit() else args.udp,
                                  args.peer, args.name)
    port = args.port or os.environ.get("RADIO_SERIAL_PORT")
    if not port:
        from serial.tools import list_ports
        found = [p for p in list_ports.comports() if p.vid in (0x1A86, 0x10C4, 0x303A, 0x0403)]
        if len(found) == 1:
            port = found[0].device
            say(f"auto-detected radio on {port} ({found[0].description})")
        else:
            listing = "\n".join(f"  {p.device}  {p.description}" for p in list_ports.comports()) or "  (none)"
            sys.exit(f"Pass --port COMx (radio) or --udp PORT (network). Serial ports seen:\n{listing}")
    say(f"opening radio on {port} (this takes a few seconds; the board may reset when the port opens)...")
    return transports.MeshtasticRadio(port, status=status)


# ------------------------------------------------------------------------------------------------ commands
def cmd_check(args) -> int:
    say(f"host {socket.gethostname()} | {platform.platform()} | {platform.machine()} | Python "
        f"{platform.python_version()}")
    try:
        d = runtime.verify_runtime()
        say(f"runtime {d.name}: all pinned binaries match SHA256SUMS")
    except RuntimeError as e:
        say(f"FAIL runtime: {e}")
        return 1
    path = runtime.find_model(args.model)
    if path is None:
        say(f"FAIL: {runtime.MODEL_FILE} not found. Pass --model PATH, set CODEC_MODEL_PATH, or copy it to "
            f"{ROOT / 'models'}")
        return 2
    got = runtime.sha256(path)
    say(f"model   {path}\n        sha256 {got[:16]}... {'OK' if got == runtime.MODEL_SHA256 else 'WRONG FILE'}")
    ctx = runtime.sha256(runtime.context_path())
    say(f"context sha256 {ctx[:16]}... {'OK' if ctx == runtime.CONTEXT_SHA256 else 'WRONG FILE'}")
    if got != runtime.MODEL_SHA256 or ctx != runtime.CONTEXT_SHA256:
        return 1
    t0 = time.time()
    model = runtime.load(path)
    say(f"model loaded in {time.time() - t0:.1f}s")
    fp = runtime.fingerprint(model)
    fp_ok = fp == runtime.REFERENCE_FINGERPRINT
    say(f"fingerprint {fp}  {'OK' if fp_ok else 'MISMATCH (expected ' + runtime.REFERENCE_FINGERPRINT + ')'}")

    rows = [json.loads(l) for l in (ROOT / "data" / "kit_packets.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()][: args.n]
    bad, t0 = [], time.time()
    for r in rows:
        res = mc.parse_packet(bytes.fromhex(r["packet"]), model)
        if not (res.get("ok") and res["text"] == r["text"]):
            bad.append((r["id"], res.get("reason")))
    say(f"decode {len(rows)} packets encoded on another machine: {len(rows) - len(bad)}/{len(rows)} exact "
        f"({(time.time() - t0) / max(1, len(rows)):.2f}s each)")
    for i, why in bad[:5]:
        say(f"   FAIL id {i}: {why}")

    enc_bad, t0 = [], time.time()
    for want in REF["probes"]:
        pkt, method = mc.build_packet(want["text"], model)
        back = mc.parse_packet(pkt, model)
        if not (pkt.hex() == want["packet"] and back.get("ok") and back["text"] == want["text"]):
            enc_bad.append(want["text"])
    say(f"encode {len(REF['probes'])} probes byte-identical to the reference: "
        f"{len(REF['probes']) - len(enc_bad)}/{len(REF['probes'])}")
    for tx in enc_bad:
        say(f"   FAIL {tx!r}")
    ok = fp_ok and not bad and not enc_bad
    say(f"RESULT {'PASS: this laptop can talk LLM-compressed to any other PASS laptop' if ok else 'FAIL'}")
    return 0 if ok else 1


def cmd_explain(args) -> int:
    model, status = load_model(args)
    if model is None:
        say(status)
        return 2
    text = " ".join(args.text)
    pieces = model.explain(text)
    if pieces is None:
        say("the model coder cannot carry this text exactly (newline?)")
        return 1
    pkt, method = mc.build_packet(text, model)
    total = sum(p["bits"] for p in pieces)
    say(f"{len(text.encode())} B text -> {len(pkt)} B packet ({['raw', 'unishox2', 'llm'][method]}), "
        f"{total:.1f} bits of model cost")
    for p in pieces:
        bar = "#" * min(40, int(round(p["bits"] * 2)))
        say(f"  {p['piece']!r:>18}  {p['bits']:6.2f}  {bar}")
    return 0


def cmd_selftest(args) -> int:
    import link
    import transports
    model, status = load_model(args)
    say(f"model: {status}")
    events = []

    def emit(ev):
        ev = {"t": time.strftime("%H:%M:%S"), **ev}
        events.append(ev)
        show(ev, False)

    if args.udp:
        a_t = transports.UdpLink("127.0.0.1:0", None, "selftest-A")
        b_t = transports.UdpLink("127.0.0.1:0", None, "selftest-B")
        a_t.peer = ("127.0.0.1", b_t.sock.getsockname()[1])
        b_t.peer = ("127.0.0.1", a_t.sock.getsockname()[1])
    else:
        air = transports.LoopbackAir()
        a_t, b_t = air.radio("!0000000a"), air.radio("!0000000b")
    # one model shared by both ends is fine: Link serializes model access across links via the same lock
    a = link.Link(a_t, model, emit, ack_timeout=5)
    b = link.Link(b_t, model, emit, ack_timeout=5)
    b.mlock = a.mlock
    fails = 0
    cases = [(a, b_t.node_id, PROBES[0], {}), (b, a_t.node_id, "Team en route, 20 min. Keep everyone upstairs.", {}),
             (a, b_t.node_id, PROBES[1], {"corrupt_first": True}), (b, a_t.node_id, PROBES[7], {})]
    for sender, dest, text, kw in cases:
        out = sender.send(text, dest, **kw)
        if out.state != "VERIFIED":
            fails += 1
    got_b = [r["text"] for r in b.received]
    got_a = [r["text"] for r in a.received]
    exact = got_b == [PROBES[0], PROBES[1]] and got_a == ["Team en route, 20 min. Keep everyone upstairs.", PROBES[7]]
    say(f"\nRESULT {'PASS' if (fails == 0 and exact) else 'FAIL'}: {4 - fails}/4 verified, texts exact both "
        f"ways: {exact}, compression: {'LLM' if model else 'raw/Unishox2 only (no model)'}")
    a_t.close(); b_t.close()
    return 0 if (fails == 0 and exact) else 1


def cmd_chat(args) -> int:
    import link
    model, status = load_model(args)
    log_emit = make_emit("chat", args.verbose)
    pending: dict[int, tuple] = {}        # msg_id -> (text, kwargs, rounds) for messages without a decode receipt
    pend_lock = threading.Lock()
    jobs: queue.Queue = queue.Queue()

    def resend_pending(why):
        with pend_lock:
            items = [(mid, v) for mid, v in pending.items() if v[2] < args.max_resends]
            for mid, (text, kw, rounds) in items:
                pending[mid] = (text, kw, rounds + 1)
        if items:
            log_emit({"event": "resend", "count": len(items), "why": why})
            for mid, (text, kw, rounds) in items:
                jobs.put((text, {**kw, "msg_id": mid}))

    def emit(ev):
        log_emit(ev)
        k = ev.get("event")
        if k == "late_success":
            with pend_lock:
                pending.pop(int(ev["msg_id"], 16), None)
        elif k == "radio_up" and not args.no_auto_resend:
            resend_pending("radio is back")

    t = open_transport(args, status=emit)
    timeout = args.ack_timeout or (8.0 if t.kind == "udp" else 30.0)
    L = link.Link(t, model, emit, ack_timeout=timeout)
    say("=" * 100)
    say(f"me:    {t.describe()}")
    say(f"codec: {status}")
    dest = {"v": args.dest}
    say(f"to:    {dest['v'] or '(nobody yet: use /to !nodeid, or reply after someone messages you)'}")
    say("type a message and press Enter. /help for commands. Ctrl+C or /quit to stop.")
    say("=" * 100)

    stats = {"sent": 0, "verified": 0, "text": 0, "air": 0}

    def worker():
        while True:
            job = jobs.get()
            if job is None:
                return
            text, kw = job
            to = dest["v"] or L.last_sender or ("!00000000" if t.kind == "udp" else None)
            if not to:
                say("   no destination: /to !nodeid first (see /nodes)")
                jobs.task_done()
                continue
            try:
                out = L.send(text, to, **kw)
            except Exception as e:   # never let one message kill the sender thread
                say(f"   !! send failed: {e!r}")
                jobs.task_done()
                continue
            keep = {k: v for k, v in kw.items() if k not in ("corrupt_first", "msg_id")}
            with pend_lock:
                if out.state in ("VERIFIED", "VERIFIED_LATE", "CONFLICT", "REJECTED"):
                    pending.pop(out.msg_id, None)
                else:
                    rounds = pending.get(out.msg_id, (None, None, 0))[2]
                    pending[out.msg_id] = (text, keep, rounds)
                    if rounds >= args.max_resends:
                        say(f"   !! {out.msg_id:08x} still undelivered after {rounds} resends; /retry to try again")
            stats["sent"] += 1
            if out.state.startswith("VERIFIED"):
                stats["verified"] += 1
                stats["text"] += out.text_bytes
                stats["air"] += out.air_bytes
            jobs.task_done()

    threading.Thread(target=worker, daemon=True, name="sender").start()

    def periodic_resend():   # covers the OTHER board crashing: our radio never goes down, so radio_up never fires
        while args.resend_every > 0 and not args.no_auto_resend:
            time.sleep(args.resend_every)
            if jobs.unfinished_tasks == 0 and getattr(t, "is_up", True):
                resend_pending(f"no receipt yet, periodic retry every {args.resend_every:g}s")

    threading.Thread(target=periodic_resend, daemon=True, name="resender").start()

    def handle(line: str) -> bool:
        if not line.strip():
            return True
        if not line.startswith("/"):
            jobs.put((line, {}))
            return True
        cmd, _, rest = line.partition(" ")
        cmd = cmd.lower()
        if cmd in ("/quit", "/exit", "/q"):
            return False
        if cmd == "/help":
            say(__doc__.split("Commands:")[1])
        elif cmd == "/to":
            dest["v"] = rest.strip() or None
            say(f"   now sending to {dest['v']}")
        elif cmd == "/nodes":
            ns = t.nodes() if hasattr(t, "nodes") else []
            say("\n".join("   " + n for n in ns) or "   (none heard yet)")
        elif cmd == "/raw":
            jobs.put((rest, {"force_method": mc.RAW}))
        elif cmd == "/unishox":
            jobs.put((rest, {"force_method": mc.UNISHOX}))
        elif cmd == "/corrupt":
            jobs.put((rest, {"corrupt_first": True}))
        elif cmd == "/probes":
            for p in PROBES:
                jobs.put((p, {}))
        elif cmd == "/explain":
            if model is None:
                say("   no model loaded")
            else:
                with L.mlock:
                    pieces = model.explain(rest)
                say("   " + " ".join(f"{p['piece'].strip() or '_'}:{p['bits']:.1f}" for p in (pieces or [])))
        elif cmd == "/retry":
            with pend_lock:
                for mid in pending:
                    pending[mid] = (pending[mid][0], pending[mid][1], 0)
            if pending:
                resend_pending("/retry")
            else:
                say("   nothing undelivered")
        elif cmd == "/reconnect":
            if hasattr(t, "reconnect"):
                t.reconnect()
            else:
                say("   not a radio link")
        elif cmd == "/status":
            up = getattr(t, "is_up", True)
            say(f"   link: {t.describe()}\n   radio up: {up}, reconnects: {getattr(t, 'reconnects', 0)}, "
                f"to: {dest['v'] or L.last_sender or '(nobody)'}")
            with pend_lock:
                for mid, (text, kw, rounds) in pending.items():
                    say(f"   undelivered {mid:08x} (resent {rounds}x): {text[:70]!r}")
                if not pending:
                    say("   undelivered: none")
        elif cmd == "/stats":
            r = stats["text"] / stats["air"] if stats["air"] else 0
            say(f"   sent {stats['sent']}, verified {stats['verified']}, {stats['text']} B text in "
                f"{stats['air']} B on air (incl. 10 B envelope + retries), received {len(L.received)}")
        else:
            say(f"   unknown command {cmd}; /help")
        return True

    for s in args.send or []:
        handle(s)
    if args.probes:
        handle("/probes")
    try:
        if args.batch or args.send or args.probes:
            if args.batch:
                for line in sys.stdin:
                    if not handle(line.rstrip("\r\n")):
                        break
            jobs.join()
            time.sleep(args.linger)
        else:
            while True:
                try:
                    line = input()
                except EOFError:
                    jobs.join()
                    time.sleep(args.linger)
                    break
                if not handle(line):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        r = stats["text"] / stats["air"] if stats["air"] else 0
        say(f"\nbye: sent {stats['sent']} (verified {stats['verified']}), received {len(L.received)}"
            + (f", {stats['text']} B of text took {stats['air']} B on air" if r else ""))
        t.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def model_opts(p):
        p.add_argument("--model", help=f"path to {runtime.MODEL_FILE} (else CODEC_MODEL_PATH, else ./models/)")
        p.add_argument("--no-model", action="store_true", help="no LLM: raw/Unishox2 only")

    c = sub.add_parser("check", help="prove this laptop is a compatible codec endpoint")
    c.add_argument("--model")
    c.add_argument("-n", type=int, default=20, help="foreign packets to decode (max 40)")
    s = sub.add_parser("selftest", help="two endpoints in one process")
    model_opts(s)
    s.add_argument("--udp", action="store_true", help="go through real UDP sockets on 127.0.0.1")
    e = sub.add_parser("explain", help="per-token bit cost of a text")
    model_opts(e)
    e.add_argument("text", nargs="+")
    ch = sub.add_parser("chat", help="two-way chat over a radio (--port) or the network (--udp)")
    model_opts(ch)
    ch.add_argument("--port", help="radio serial port, e.g. COM4 (auto-detected if exactly one)")
    ch.add_argument("--udp", help="use UDP instead of a radio: local port (e.g. 47000) or IP:PORT to bind")
    ch.add_argument("--peer", help="UDP: the other laptop, IP:PORT (needed on at least one side)")
    ch.add_argument("--name", help="UDP: this endpoint's name (node id is derived from it; default hostname)")
    ch.add_argument("--dest", default=os.environ.get("RADIO_DESTINATION_ID"),
                    help="who to send to, e.g. !f99260d4 (UDP: optional)")
    ch.add_argument("--ack-timeout", type=float, help="seconds to wait for the decode receipt (30 radio, 8 udp)")
    ch.add_argument("--send", action="append", help="send this text on start (repeatable)")
    ch.add_argument("--probes", action="store_true", help="send the 8 reference probes on start")
    ch.add_argument("--batch", action="store_true", help="read lines from stdin (scripts), then exit")
    ch.add_argument("--linger", type=float, default=3.0, help="seconds to keep listening before exiting")
    ch.add_argument("-v", "--verbose", action="store_true", help="show frames in hex and every receipt")
    ch.add_argument("--no-auto-resend", action="store_true",
                    help="do not resend undelivered messages automatically when the radio comes back")
    ch.add_argument("--max-resends", type=int, default=3, help="automatic resend rounds per message (default 3)")
    ch.add_argument("--resend-every", type=float, default=20.0,
                    help="seconds between automatic resends of undelivered messages (0 = only when our radio "
                         "reconnects or on /retry)")
    a = ap.parse_args()
    sys.exit({"check": cmd_check, "selftest": cmd_selftest, "explain": cmd_explain, "chat": cmd_chat}[a.cmd](a))


if __name__ == "__main__":
    main()

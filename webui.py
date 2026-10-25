"""Browser front end for meshchat: the same ChatSession as `meshchat.py chat`, shown as a chat page with the codec,
airtime and delivery numbers next to every message. Standard library only; the page is one offline HTML file.

    python meshchat.py gui --port COM5 [--dest !06fca5ec]      radio
    python meshchat.py gui --loopback                          no radio: a simulated peer in this process

GET  /            webui/index.html
GET  /events      Server-Sent Events: every engine event (replayed from the start of the session, then live)
GET  /state       this node, peer, codec, radio state, undelivered messages
GET  /preview?text=...[&typing=1]   what each method would put on air for this text (compose hint, details)
POST /send        {"line": "..."}: a message or any chat slash command
"""
from __future__ import annotations

import collections
import http.server
import json
import queue
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

from meshchat import ChatSession, air_ms, load_model, make_emit, open_transport, say

import envelope as env
import lora_math
import mesh_codec as mc
import runtime

PAGE = Path(__file__).resolve().parent / "webui" / "index.html"
METHODS = ((mc.RAW, "raw"), (mc.UNISHOX, "unishox2"), (mc.LLM, "llm"))


class Hub:
    """Fans every engine event out to the browser pages: stamps ms time, adds airtime, keeps the last 500."""

    def __init__(self, keep: int = 500):
        self.lock = threading.Lock()
        self.history: collections.deque = collections.deque(maxlen=keep)
        self.subs: list[queue.Queue] = []

    def publish(self, ev: dict):
        ev = {**ev, "ts": round(time.time() * 1000)}
        if ev.get("event") in ("send", "decoded") and "frame_bytes" in ev:
            ev["air_ms_plain"] = round(air_ms(ev["text_bytes"]))
            ev["air_ms_frame"] = round(air_ms(ev["frame_bytes"]))
        with self.lock:
            self.history.append(ev)
            for q in self.subs:
                q.put(ev)

    def subscribe(self) -> tuple[queue.Queue, list]:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subs.append(q)
            return q, list(self.history)

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)


class _AirDelay:
    """A loopback radio that takes LongFast airtime to deliver, so the no-radio backup shows realistic timings."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def send(self, frame, dest, want_ack, on_radio_ack=None) -> int:
        threading.Timer(air_ms(len(frame)) / 1000.0, self._inner.send,
                        args=(frame, dest, want_ack, on_radio_ack)).start()
        return 0


def start_loopback_peer(session: ChatSession, peer_t):
    """A simulated second laptop: decodes, sends receipts, and answers each new message once."""
    import link
    peer = link.Link(peer_t, session.model, lambda ev: None, ack_timeout=10)
    peer.mlock = session.link.mlock          # one llama.cpp context shared by both ends

    def on_frame(frame, meta):
        n = len(peer.received)
        peer._rx.put((frame, meta))

        def maybe_answer():
            time.sleep(1.5)
            if len(peer.received) > n and peer.received[-1]["kind"] == "new":
                text = peer.received[-1]["text"]
                peer.send(f"Copy that: {text}" if len(text) < 120 else "Copy that, received in full.",
                          session.t.node_id)
        threading.Thread(target=maybe_answer, daemon=True).start()

    peer_t.on_frame(on_frame)
    return peer


def peer_name(t, node_id: str | None) -> str | None:
    if not node_id:
        return None
    names = getattr(t, "peer_names", None)
    if names:
        return names.get(node_id)
    try:
        import transports
        n = (t.iface.nodesByNum or {}).get(transports.parse_node(node_id)) or {}
        return (n.get("user") or {}).get("longName")
    except Exception:
        return None


class Previews:
    """preview() with a cache, and typing previews that give up once a newer one is waiting: the model runs one
    text at a time (~0.5-2 s each), so stale compose-box requests must not queue up in front of the current one."""

    def __init__(self, session: ChatSession, keep: int = 256):
        self.session, self.keep = session, keep
        self.cache: collections.OrderedDict = collections.OrderedDict()
        self.lock = threading.Lock()
        self.typing_gen = 0

    def get(self, text: str, typing: bool = False) -> dict:
        with self.lock:
            if text in self.cache:
                self.cache.move_to_end(text)
                return self.cache[text]
            if typing:
                self.typing_gen += 1
            gen = self.typing_gen
        if typing:
            with self.session.link.mlock:   # wait our turn, then check whether the user has typed on since
                pass
            if gen != self.typing_gen:
                return {"stale": True}
        out = preview(self.session, text)
        with self.lock:
            self.cache[text] = out
            while len(self.cache) > self.keep:
                self.cache.popitem(last=False)
        return out


def preview(session: ChatSession, text: str) -> dict:
    """What every method would put on air for `text` (frame = 10 B envelope + codec packet)."""
    tb = len(text.encode("utf-8"))
    out = {"text_bytes": tb, "plain_air_ms": round(air_ms(tb)) if tb else 0, "methods": {}, "best": None,
           "limit": session.link.max_payload, "envelope": env.REPORT_HEADER}
    if not text.strip():
        return out
    best = None
    for m, name in METHODS:
        if m == mc.LLM and session.model is None:
            continue
        try:
            with session.link.mlock:
                pkt, _ = mc.build_packet(text, session.model, force_method=m)
        except ValueError as e:
            out["methods"][name] = {"error": str(e)}
            continue
        frame = env.REPORT_HEADER + len(pkt)
        out["methods"][name] = {"packet": len(pkt), "frame": frame, "air_ms": round(air_ms(frame)),
                                "fits": frame <= session.link.max_payload}
        if best is None or frame < out["methods"][best]["frame"]:
            best = name
    out["best"] = best
    if best:   # what a 1-hop / 3-hop delivery survives on a channel that is already 25% busy
        sf, bw, cr = lora_math.PRESETS["LongFast (default)"]
        tbar = lora_math.airtime_ms(tb, sf, bw, cr)
        p_plain = lora_math.p_hop(lora_math.airtime_ms(tb, sf, bw, cr), tbar, 0.25)
        p_ours = lora_math.p_hop(lora_math.airtime_ms(out["methods"][best]["frame"], sf, bw, cr), tbar, 0.25)
        out["survival"] = {"plain_1hop": round(p_plain, 3), "ours_1hop": round(p_ours, 3),
                           "plain_3hop": round(p_plain ** 3, 3), "ours_3hop": round(p_ours ** 3, 3)}
    return out


def state(session: ChatSession, codec_status: str) -> dict:
    t, L = session.t, session.link
    peer = session.dest or L.last_sender
    with session.pend_lock:
        pend = [{"msg_id": f"{mid:08x}", "text": v[0], "resent": v[2]} for mid, v in session.pending.items()]
    return {"me": {"name": getattr(t, "name", None), "id": t.node_id, "port": getattr(t, "port", None),
                   "kind": t.kind},
            "peer": {"id": peer, "name": peer_name(t, peer)},
            "codec": {"ok": session.model is not None, "status": codec_status,
                      "fingerprint": runtime.REFERENCE_FINGERPRINT if session.model is not None else None},
            "radio": {"up": getattr(t, "is_up", True), "reconnects": getattr(t, "reconnects", 0)},
            "pending": pend, "stats": dict(session.stats)}


def make_handler(session: ChatSession, hub: Hub, codec_status: str):
    previews = Previews(session)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path in ("/", "/index.html"):
                body = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/state":
                self._json(state(session, codec_status))
            elif url.path == "/preview":
                q = urllib.parse.parse_qs(url.query)
                self._json(previews.get(q.get("text", [""])[0], typing=q.get("typing") == ["1"]))
            elif url.path == "/events":
                self._events()
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/send":
                return self._json({"error": "not found"}, 404)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                line = json.loads(self.rfile.read(n) or b"{}").get("line", "")
            except (ValueError, AttributeError):
                return self._json({"error": "body must be JSON {\"line\": ...}"}, 400)
            if not isinstance(line, str) or "\n" in line:
                return self._json({"error": "line must be one line of text"}, 400)
            if line.strip().lower() in ("/quit", "/exit", "/q"):
                return self._json({"error": "close the terminal window to stop meshchat"}, 400)
            session.handle(line)
            self._json({"ok": True})

        def _events(self):
            q, backlog = hub.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                for ev in backlog:
                    self._sse(ev)
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")   # keeps the connection (and proxies) awake
                        self.wfile.flush()
                        continue
                    self._sse(ev)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                hub.unsubscribe(q)
                self.close_connection = True

        def _sse(self, ev):
            self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n\n")

    return Handler


def serve(session: ChatSession, hub: Hub, codec_status: str, port: int):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(session, hub, codec_status))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="webui").start()
    return srv


def run(args) -> int:
    model, status = load_model(args)
    hub = Hub()
    log_emit = make_emit("gui", args.verbose)

    def emit(ev):
        log_emit(ev)
        hub.publish(ev)

    if args.loopback:
        import transports
        air = transports.LoopbackAir()
        mine, theirs = _AirDelay(air.radio("!0000000a")), _AirDelay(air.radio("!0000000b"))
        mine._inner.name, theirs._inner.name = "This laptop (simulated radio)", "Simulated peer"
        mine.peer_names = {theirs.node_id: "Simulated peer"}
        args.dest = args.dest or theirs.node_id
        session = ChatSession(args, model, emit, lambda status_cb: mine)
        start_loopback_peer(session, theirs)
    else:
        session = ChatSession(args, model, emit, lambda status_cb: open_transport(args, status=status_cb))
    srv = serve(session, hub, status, args.http_port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    say("=" * 100)
    say(f"me:    {session.t.describe()}")
    say(f"codec: {status}")
    say(f"GUI:   {url}   (Ctrl+C here to stop)")
    say("=" * 100)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        say("\n" + session.bye())
        srv.shutdown()
        session.close()
    return 0

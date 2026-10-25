"""Two-way codec link. One Link per endpoint; every endpoint both sends and receives.

Sending (send):
  1. build_packet picks the smallest exact method (LLM, Unishox2 or raw). Invalid or oversized text fails visibly
     before anything goes on air: no truncation, no fragmentation.
  2. Attempt 1 goes out as a direct message with want_ack, then the sender waits for the peer's DECODE_ACK.
  3. DECODE_FAILED, or no DECODE_ACK within ack_timeout -> exactly one more attempt as raw text, same message id.
  4. Radio ACK (Meshtastic routing) and decode ACK (peer decoded + CRC verified) are reported separately.

Receiving (_on_frame), runs on its own thread:
  * not addressed to us / from us / malformed -> logged and dropped, never crashes
  * REPORT -> mesh_codec.parse_packet -> DECODE_ACK (VERIFIED / DECODE_FAILED / CONFLICT)
  * the same (sender, message id) seen again with the same text -> "duplicate", ACKed again, shown once
  * DECODE_ACKs go out without want_ack and are never answered, so ACKs cannot loop
"""
from __future__ import annotations

import queue
import secrets
import threading
import time
from dataclasses import dataclass, field

import envelope as env
import mesh_codec as mc

MAX_DM_PAYLOAD = 216   # largest direct message measured on T3 V1.6.1 / fw 2.7.26
METHOD_NAMES = {mc.RAW: "raw", mc.UNISHOX: "unishox2", mc.LLM: "llm"}


@dataclass
class Attempt:
    number: int
    method: str
    frame_bytes: int
    codec_bytes: int
    radio_ack: bool | None = None
    radio_detail: str | None = None
    decode_status: str | None = None
    rtt_s: float | None = None


@dataclass
class Outcome:
    msg_id: int
    text: str
    text_bytes: int
    state: str = "REJECTED"   # VERIFIED, VERIFIED_LATE, CONFLICT, RADIO_ACK_ONLY, DECODE_FAILED, NOT_DELIVERED, REJECTED
    reason: str | None = None
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def air_bytes(self) -> int:
        return sum(a.frame_bytes for a in self.attempts)


class Link:
    def __init__(self, transport, model, emit, max_payload: int = MAX_DM_PAYLOAD, ack_timeout: float = 30.0,
                 ack_delay: float = 0.0, ack_repeat_s: float = 5.0):
        """emit(event: dict) receives every event (log + display)."""
        self.t, self.model, self.emit = transport, model, emit
        self.max_payload, self.ack_timeout = max_payload, ack_timeout
        self.ack_delay, self.ack_repeat_s = ack_delay, ack_repeat_s
        self.mlock = threading.Lock()            # llama.cpp contexts are not thread-safe
        self._lock = threading.Lock()
        self._waiting: dict[int, tuple[threading.Event, dict]] = {}
        self._finished: dict[int, Outcome] = {}
        self._acks_sent: dict[tuple, float] = {}
        self._seen: dict[tuple, str] = {}         # (src, msg_id) -> text
        self.last_sender: str | None = None
        self.received: list[dict] = []
        self._rx: queue.Queue = queue.Queue()
        threading.Thread(target=self._rx_loop, daemon=True, name="link-rx").start()
        transport.on_frame(lambda frame, meta: self._rx.put((frame, meta)))

    # ------------------------------------------------------------------ sending
    def send(self, text: str, dest: str, msg_id: int | None = None, force_method: int | None = None,
             corrupt_first: bool = False, mtype: str = "info", priority: int = 0) -> Outcome:
        msg_id = secrets.randbits(32) if msg_id is None else msg_id
        sent_at = int(time.time())
        out = Outcome(msg_id, text, len(text.encode("utf-8")) if isinstance(text, str) else 0)
        try:
            with self.mlock:
                pkt, method = mc.build_packet(text, self.model, mtype=mtype, priority=priority,
                                              force_method=force_method)
        except ValueError as e:
            out.reason = f"not sent: {e}"
            return self._finish(out)
        frame = env.encode_report(msg_id, 1, sent_at, pkt)
        if len(frame) > self.max_payload:
            out.reason = f"not sent: {len(frame)} B frame exceeds the {self.max_payload} B radio limit"
            return self._finish(out)
        if corrupt_first:   # demo/test: flip one bit in the codec body so the peer's checksum fails
            b = bytearray(frame)
            b[env.REPORT_HEADER + 2] ^= 0x01
            frame = bytes(b)

        status = self._attempt(out, 1, method, frame, len(pkt), dest)
        if status in ("VERIFIED", "CONFLICT"):
            out.state = status
            return self._finish(out)

        try:   # exactly one bounded raw retry, same message id
            raw_pkt, raw_method = mc.build_packet(text, None, mtype=mtype, priority=priority, force_method=mc.RAW)
            raw_frame = env.encode_report(msg_id, 2, sent_at, raw_pkt)
            if len(raw_frame) > self.max_payload:
                raise ValueError(f"{len(raw_frame)} B raw frame exceeds the {self.max_payload} B radio limit")
        except ValueError as e:
            out.state, out.reason = self._failure_state(out), f"raw fallback impossible: {e}"
            return self._finish(out)
        self.emit({"event": "retry", "msg_id": f"{msg_id:08x}",
                   "why": out.attempts[-1].decode_status or "no decode ACK in time"})
        status = self._attempt(out, 2, raw_method, raw_frame, len(raw_pkt), dest)
        out.state = status if status in ("VERIFIED", "CONFLICT") else self._failure_state(out)
        if out.state not in ("VERIFIED", "CONFLICT"):
            out.reason = "no verified decode after the raw retry"
        return self._finish(out)

    @staticmethod
    def _failure_state(out: Outcome) -> str:
        if any(a.decode_status == "DECODE_FAILED" for a in out.attempts):
            return "DECODE_FAILED"
        if any(a.radio_ack for a in out.attempts):
            return "RADIO_ACK_ONLY"
        return "NOT_DELIVERED"

    def _attempt(self, out: Outcome, number: int, method: int, frame: bytes, codec_bytes: int, dest: str):
        att = Attempt(number, METHOD_NAMES[method], len(frame), codec_bytes)
        out.attempts.append(att)
        ev, box = threading.Event(), {}
        with self._lock:
            self._waiting[out.msg_id] = (ev, box)

        def on_radio_ack(ok, detail):
            att.radio_ack, att.radio_detail = ok, detail
            self.emit({"event": "radio_ack", "msg_id": f"{out.msg_id:08x}", "attempt": number, "ok": ok,
                       "detail": detail})

        t0 = time.time()
        self.emit({"event": "send", "msg_id": f"{out.msg_id:08x}", "attempt": number, "method": att.method,
                   "text_bytes": out.text_bytes, "codec_bytes": codec_bytes, "frame_bytes": len(frame),
                   "dest": dest, "frame": frame.hex()})
        try:
            self.t.send(frame, dest, want_ack=True, on_radio_ack=on_radio_ack)
        except Exception as e:
            self.emit({"event": "send_error", "msg_id": f"{out.msg_id:08x}", "error": repr(e)})
            with self._lock:
                self._waiting.pop(out.msg_id, None)
            return None
        deadline = t0 + self.ack_timeout
        result = None
        while True:
            if not ev.wait(max(0.0, deadline - time.time())):
                break
            ev.clear()
            ack = box.pop("ack", None)
            if ack is None:
                continue
            if ack.attempt == number:
                att.decode_status = env.STATUS_NAMES[ack.status]
                att.rtt_s = round(time.time() - t0, 2)
                result = att.decode_status
                break
            if ack.status == env.VERIFIED and 1 <= ack.attempt <= len(out.attempts):
                out.attempts[ack.attempt - 1].decode_status = "VERIFIED"   # an earlier attempt landed late
                result = "VERIFIED"
                break
        with self._lock:
            self._waiting.pop(out.msg_id, None)
        return result

    def _finish(self, out: Outcome) -> Outcome:
        with self._lock:
            self._finished[out.msg_id] = out
        self.emit({"event": "outcome", "msg_id": f"{out.msg_id:08x}", "state": out.state, "reason": out.reason,
                   "text": out.text, "text_bytes": out.text_bytes, "air_bytes": out.air_bytes,
                   "attempts": [a.__dict__ for a in out.attempts]})
        return out

    # ------------------------------------------------------------------ receiving
    def _rx_loop(self):
        while True:
            frame, meta = self._rx.get()
            try:
                self._on_frame(frame, meta)
            except Exception as e:   # radio input must never stop the receiver
                self.emit({"event": "rx_error", "error": repr(e), "from": meta.get("from")})

    def _on_frame(self, frame: bytes, meta: dict):
        src = meta.get("from")
        if src == self.t.node_id or meta.get("to") != self.t.node_id:
            self.emit({"event": "ignored", "reason": "not a direct message to this node", "from": src,
                       "to": meta.get("to"), "bytes": len(frame)})
            return
        try:
            f = env.decode(frame)
        except env.EnvelopeError as e:
            self.emit({"event": "rejected", "reason": f"envelope: {e}", "from": src, "bytes": len(frame),
                       "frame": frame.hex()})
            return
        if isinstance(f, env.DecodeAck):
            self._on_decode_ack(f, src)
            return
        t0 = time.time()
        with self.mlock:
            res = mc.parse_packet(f.packet, self.model)
        base = {"from": src, "msg_id": f"{f.msg_id:08x}", "attempt": f.attempt, "frame_bytes": len(frame),
                "snr": meta.get("snr"), "rssi": meta.get("rssi"), "hops": meta.get("hops"),
                "decode_s": round(time.time() - t0, 2), "frame": frame.hex()}
        if not res.get("ok"):
            self.emit({"event": "decode_failed", **base, "reason": res.get("reason"),
                       "method": METHOD_NAMES.get(res.get("method"), res.get("method"))})
            self._ack(src, f, env.DECODE_FAILED)
            return
        text = res["text"]
        key = (src, f.msg_id)
        with self._lock:
            prev = self._seen.get(key)
            kind = "new" if prev is None else ("duplicate" if prev == text else "conflict")
            if prev is None:
                self._seen[key] = text
        self.last_sender = src
        rec = {"event": "decoded", **base, "kind": kind, "method": METHOD_NAMES[res["method"]], "text": text,
               "text_bytes": len(text.encode("utf-8")), "type": res.get("type"), "priority": res.get("priority")}
        if kind != "duplicate":
            self.received.append(rec)
        self.emit(rec)
        self._ack(src, f, env.CONFLICT if kind == "conflict" else env.VERIFIED)

    def _ack(self, dest: str, f: env.Report, status: int):
        key = (dest, f.msg_id, f.attempt, status)
        now = time.time()
        with self._lock:
            if now - self._acks_sent.get(key, -1e9) < self.ack_repeat_s:
                return
            self._acks_sent[key] = now
        if self.ack_delay:
            time.sleep(self.ack_delay)
        frame = env.encode_ack(f.msg_id, f.attempt, status)
        try:
            self.t.send(frame, dest, want_ack=False)
        except Exception as e:
            self.emit({"event": "send_error", "msg_id": f"{f.msg_id:08x}", "error": repr(e)})
            return
        self.emit({"event": "decode_ack_sent", "to": dest, "msg_id": f"{f.msg_id:08x}", "attempt": f.attempt,
                   "status": env.STATUS_NAMES[status], "bytes": len(frame)})

    def _on_decode_ack(self, ack: env.DecodeAck, src: str):
        with self._lock:
            waiting = self._waiting.get(ack.msg_id)
            finished = self._finished.get(ack.msg_id)
        self.emit({"event": "decode_ack", "from": src, "msg_id": f"{ack.msg_id:08x}", "attempt": ack.attempt,
                   "status": env.STATUS_NAMES[ack.status], "late": waiting is None and finished is not None})
        if waiting:
            ev, box = waiting
            box["ack"] = ack
            ev.set()
        elif finished is not None and ack.status == env.VERIFIED and finished.state not in ("VERIFIED",):
            finished.state = "VERIFIED_LATE"
            self.emit({"event": "late_success", "msg_id": f"{ack.msg_id:08x}", "attempt": ack.attempt})

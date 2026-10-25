"""mesh_codec: compress short emergency reports into Meshtastic-sized packets.

Packet layout (big-endian):
  byte 0  : model id (high 4 bits) | method (low 4 bits: 0 raw, 1 unishox2, 2 language model)
  byte 1  : type (high 4 bits) | priority (2 bits) | has_location (1 bit) | has_people (1 bit)
  [1 byte : people count]                       if has_people
  [4 bytes: two uint16 offsets from area origin] if has_location
  body    : compressed text
  2 bytes : CRC-16/CCITT over header bytes + original UTF-8 text (computed before compression)

Determinism: every step after the model's logits is integer arithmetic. Logits are snapped
to a 1/16-nat grid and mapped through a weight table computed with exact decimal math, so two
machines that produce bit-identical logits produce bit-identical frequency tables.
"""
import struct
from decimal import Decimal, getcontext

import numpy as np

TYPES = ["status_ok", "trapped", "medical", "need", "hazard", "damage", "info"]
RAW, UNISHOX, LLM = 0, 1, 2
MAX_PAYLOAD = 233

# ---------------------------------------------------------------- CRC-16/CCITT-FALSE
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc

# ---------------------------------------------------------------- integer probability tables
GRID = 16            # logits snapped to 1/16 nat
DMAX = 16 * 32       # weights below e^-32 of the top token are treated as 0 (still get freq 1)
WSCALE = 1 << 30
TOTAL = 1 << 24

def _weight_table():
    getcontext().prec = 50
    return np.array([int((Decimal(WSCALE) * (Decimal(-d) / Decimal(GRID)).exp()).to_integral_value(rounding="ROUND_FLOOR"))
                     for d in range(DMAX + 1)], dtype=np.int64)

WTABLE = _weight_table()

def freq_table(logits) -> np.ndarray:
    """float32 logits -> integer cumulative frequency table summing to TOTAL (deterministic)."""
    q = np.rint(np.asarray(logits, dtype=np.float32).astype(np.float64) * GRID).astype(np.int64)
    d = np.minimum(q.max() - q, DMAX)
    w = WTABLE[d]
    v = w.shape[0]
    s = int(w.sum())
    f = (w * (TOTAL - v)) // s + 1
    f[int(np.argmax(f))] += TOTAL - int(f.sum())
    cum = np.zeros(v + 1, dtype=np.int64)
    np.cumsum(f, out=cum[1:])
    return cum

# ---------------------------------------------------------------- arithmetic coder (32-bit, bit level)
PREC = 32
FULL, HALF, QUARTER = (1 << PREC) - 1, 1 << (PREC - 1), 1 << (PREC - 2)

class Encoder:
    def __init__(self):
        self.low, self.high, self.pending, self.bits = 0, FULL, 0, []
    def _emit(self, b):
        self.bits.append(b); self.bits.extend([1 - b] * self.pending); self.pending = 0
    def encode(self, cum, s):
        r = self.high - self.low + 1
        self.high = self.low + r * int(cum[s + 1]) // TOTAL - 1
        self.low = self.low + r * int(cum[s]) // TOTAL
        while True:
            if self.high < HALF:
                self._emit(0)
            elif self.low >= HALF:
                self._emit(1); self.low -= HALF; self.high -= HALF
            elif self.low >= QUARTER and self.high < 3 * QUARTER:
                self.pending += 1; self.low -= QUARTER; self.high -= QUARTER
            else:
                break
            self.low, self.high = 2 * self.low, 2 * self.high + 1
    def finish(self) -> bytes:
        self.pending += 1
        self._emit(0 if self.low < QUARTER else 1)
        bits = self.bits + [0] * (-len(self.bits) % 8)
        return bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))

class Decoder:
    def __init__(self, data: bytes):
        self.bits = [int(b) for byte in data for b in f"{byte:08b}"]
        self.pos, self.low, self.high, self.value = 0, 0, FULL, 0
        for _ in range(PREC):
            self.value = 2 * self.value + self._next()
    def _next(self):
        b = self.bits[self.pos] if self.pos < len(self.bits) else 0
        self.pos += 1
        return b
    def decode(self, cum):
        r = self.high - self.low + 1
        target = ((self.value - self.low + 1) * TOTAL - 1) // r
        s = int(np.searchsorted(cum, target, side="right")) - 1
        self.high = self.low + r * int(cum[s + 1]) // TOTAL - 1
        self.low = self.low + r * int(cum[s]) // TOTAL
        while True:
            if self.high < HALF:
                pass
            elif self.low >= HALF:
                self.value -= HALF; self.low -= HALF; self.high -= HALF
            elif self.low >= QUARTER and self.high < 3 * QUARTER:
                self.value -= QUARTER; self.low -= QUARTER; self.high -= QUARTER
            else:
                break
            self.low, self.high = 2 * self.low, 2 * self.high + 1
            self.value = 2 * self.value + self._next()
        return s

# ---------------------------------------------------------------- language model wrapper
MAX_LLM_TOKENS = 300   # decoder bound on body tokens (incl. the newline terminator); compress() must respect it


class SharedModel:
    """Evaluates the shared context once (fixed batch size), then one token at a time.
    Encoder and decoder use exactly the same calls, which keeps their logits identical."""
    def __init__(self, model_path, context_text, model_id, threads=2):
        from llama_cpp import Llama
        self.llm = Llama(model_path=model_path, n_ctx=1024, n_threads=threads, n_batch=256,
                         logits_all=True, verbose=False, seed=0)
        self.ctx_text, self.model_id = context_text, model_id
        self.pre = self.llm.tokenize(context_text.encode(), add_bos=True)
        self.llm.reset(); self.llm.eval(self.pre)
        self.state = self.llm.save_state()
        self.first = np.array(self.llm.scores[len(self.pre) - 1], dtype=np.float32, copy=True)

    def _start(self):
        self.llm.load_state(self.state)
        return self.first

    def _step(self, tok):
        self.llm.eval([tok])
        return np.array(self.llm.scores[self.llm.n_tokens - 1], dtype=np.float32, copy=True)

    def body_tokens(self, text):
        full = self.llm.tokenize((self.ctx_text + " " + text + "\n").encode(), add_bos=True)
        if full[:len(self.pre)] != self.pre:
            return None
        body = full[len(self.pre):]
        if self.llm.detokenize(body) != (" " + text + "\n").encode():
            return None
        return body

    def compress(self, text):
        """Arithmetic-code `text` exactly, or return None when the model coder cannot carry it unchanged.
        A newline is the coder's end-of-message token, so text containing one goes raw/Unishox2 instead."""
        if "\n" in text:
            return None
        body = self.body_tokens(text)
        if body is None or len(body) > MAX_LLM_TOKENS:   # the decoder stops at MAX_LLM_TOKENS: never send what it can't read
            return None
        enc, lg = Encoder(), self._start()
        for t in body:
            enc.encode(freq_table(lg), t)
            lg = self._step(t)
        return enc.finish()

    def explain(self, text):
        """Per-token cost for visualization: [{'piece': str, 'bits': float}], same tables as compress().
        Predictable pieces cost a fraction of a bit; surprising ones (names, numbers) cost many bits."""
        import math
        if "\n" in text:
            return None
        body = self.body_tokens(text)
        if body is None:
            return None
        lg, out = self._start(), []
        for t in body:
            cum = freq_table(lg)
            p = (int(cum[t + 1]) - int(cum[t])) / TOTAL
            out.append({"piece": self.llm.detokenize([t]).decode("utf-8", "replace"), "bits": round(-math.log2(p), 2)})
            lg = self._step(t)
        return out

    def decompress(self, data, max_tokens=None):
        max_tokens = MAX_LLM_TOKENS if max_tokens is None else max_tokens
        dec, lg, out = Decoder(data), self._start(), []
        for _ in range(max_tokens):
            t = dec.decode(freq_table(lg))
            out.append(t)
            if b"\n" in self.llm.detokenize(out):
                break
            lg = self._step(t)
        raw = self.llm.detokenize(out)
        if not (raw.startswith(b" ") and raw.endswith(b"\n")):
            return None
        try:
            return raw[1:-1].decode("utf-8")
        except UnicodeDecodeError:
            return None

# ---------------------------------------------------------------- packet build / parse
METHODS = (RAW, UNISHOX, LLM)
MAX_TEXT_BYTES = 4096  # decoded-text bound at the receiver; far above what one Meshtastic packet can carry


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _header(model_id, method, mtype, priority, people, location):
    """Header bytes. Every field is validated; nothing is wrapped, clipped or coerced."""
    if not _is_int(model_id) or not 0 <= model_id <= 15:
        raise ValueError(f"model id must be an integer 0..15, got {model_id!r}")
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}")
    if mtype not in TYPES:
        raise ValueError(f"unknown message type {mtype!r}; expected one of {TYPES}")
    if not _is_int(priority) or not 0 <= priority <= 3:
        raise ValueError(f"priority must be an integer 0..3, got {priority!r}")
    if people is not None and (not _is_int(people) or not 0 <= people <= 255):
        raise ValueError(f"people must be an integer 0..255 or None, got {people!r}")
    if location is not None:
        if (not isinstance(location, (tuple, list)) or len(location) != 2
                or not all(_is_int(v) and 0 <= v <= 0xFFFF for v in location)):
            raise ValueError(f"location must be two integers 0..65535 or None, got {location!r}")
    h = bytes([model_id << 4 | method,
               TYPES.index(mtype) << 4 | priority << 2 | (location is not None) << 1 | (people is not None)])
    if people is not None:
        h += bytes([people])
    if location is not None:
        h += struct.pack(">HH", *location)
    return h


def _unishox_exact(text):
    """Unishox2 body for `text`, or None if Unishox2 would not give back exactly the same string."""
    import unishox2
    try:
        body = unishox2.compress(text)[0]
        back = unishox2.decompress(body, MAX_TEXT_BYTES)
    except (ValueError, UnicodeError):
        return None
    return body if back == text else None


def build_packet(text, model=None, mtype="info", priority=0, people=None, location=None, force_method=None):
    """Return (packet_bytes, method) carrying `text` exactly. Picks the smallest method that reproduces the text
    byte for byte; raises ValueError for empty text, invalid metadata, an unusable forced method, or a packet that
    would exceed MAX_PAYLOAD. Text is never normalized, trimmed or truncated."""
    if not isinstance(text, str):
        raise ValueError("text must be a str")
    if not text.strip():
        raise ValueError("empty report text")
    if force_method is not None and force_method not in METHODS:
        raise ValueError(f"unknown method {force_method!r}")
    raw = text.encode("utf-8")
    bodies = {RAW: raw}
    if force_method in (None, UNISHOX):
        u = _unishox_exact(text)
        if u is not None:
            bodies[UNISHOX] = u
    if model is not None and force_method in (None, LLM):
        c = model.compress(text)
        if c is not None:
            bodies[LLM] = c
    if force_method is not None:
        if force_method not in bodies:
            raise ValueError(f"method {force_method} cannot carry this text exactly")
        method = force_method
    else:
        method = min(bodies, key=lambda m: len(bodies[m]))
    mid = model.model_id if model is not None else 0
    head = _header(mid, method, mtype, priority, people, location)
    crc = crc16(head + raw)
    pkt = head + bodies[method] + struct.pack(">H", crc)
    if len(pkt) > MAX_PAYLOAD:
        raise ValueError(f"packet {len(pkt)} bytes exceeds {MAX_PAYLOAD}")
    return pkt, method


def _reject(reason, method=None):
    r = {"ok": False, "reason": reason}
    if method is not None:
        r["method"] = method
    return r


def parse_packet(pkt, model=None):
    """Return dict with fields and text, or {'ok': False, 'reason': ...}. Never raises on radio input."""
    try:
        return _parse(bytes(pkt), model)
    except Exception as e:  # last line of defence: malformed radio input must not crash the receiver
        return _reject(f"malformed packet ({type(e).__name__})")


def _parse(pkt, model):
    # smallest valid packet: 2 header bytes + 1 body byte + 2 CRC bytes (empty text is never sent)
    if len(pkt) < 5:
        return _reject("too short")
    mid, method = pkt[0] >> 4, pkt[0] & 0xF
    if method not in METHODS:
        return _reject(f"invalid method {method}")
    flags = pkt[1]
    type_idx = flags >> 4
    if type_idx >= len(TYPES):
        return _reject(f"invalid type {type_idx}", method)
    mtype, priority = TYPES[type_idx], (flags >> 2) & 3
    i = 2
    people = location = None
    if flags & 1:
        if len(pkt) < i + 1 + 3:
            return _reject("truncated optional fields", method)
        people = pkt[i]; i += 1
    if flags & 2:
        if len(pkt) < i + 4 + 3:
            return _reject("truncated optional fields", method)
        location = struct.unpack(">HH", pkt[i:i + 4]); i += 4
    head, body, crc = pkt[:i], pkt[i:-2], struct.unpack(">H", pkt[-2:])[0]
    if not body:
        return _reject("empty body", method)
    text = None
    if method == RAW:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return _reject("invalid utf-8 in raw body", method)
    elif method == UNISHOX:
        import unishox2
        try:
            text = unishox2.decompress(body, MAX_TEXT_BYTES)
        except (ValueError, UnicodeError) as e:
            return _reject(f"unishox2 decode failed ({type(e).__name__})", method)
    elif method == LLM:
        if model is None or model.model_id != mid:
            return _reject("model mismatch", method)
        text = model.decompress(body)
    if text is None or crc16(head + text.encode("utf-8")) != crc:
        return _reject("checksum failed", method)
    if not text.strip():
        return _reject("empty text", method)
    return {"ok": True, "text": text, "method": method, "type": mtype, "priority": priority,
            "people": people, "location": location}

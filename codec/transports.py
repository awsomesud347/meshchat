"""Ways to move a frame between two endpoints. All three expose the same small interface:

    t.node_id                                    "!06fca5ec" (radio) or "!xxxxxxxx" derived from the name (udp)
    t.on_frame(cb)                               cb(frame: bytes, meta: dict) with keys from, to, packet_id, snr, rssi
    t.send(frame, dest, want_ack, on_radio_ack=None) -> packet id
        on_radio_ack(ok, detail) fires when the Meshtastic routing ACK/NAK arrives (radio + want_ack only)
    t.describe(), t.close()

MeshtasticRadio  a Meshtastic board over USB serial; frames ride PRIVATE_APP (port 256) as direct messages.
UdpLink          plain UDP between two laptops on the same network. Same frames, same size limit, no radio.
                 Use it to prove the two machines decode each other before involving radios.
Loopback         in-process, for tests and `meshchat.py selftest`.
"""
from __future__ import annotations

import hashlib
import re
import socket
import struct
import threading
import time
from typing import Callable, Optional

PRIVATE_APP = 256

# Firmware log lines ("INFO  | ??:??:?? 181 [DeviceTelemetry] ...") are printed as text even while the API session is
# up (2.7.x does this for the per-minute telemetry), so they are NOT a sign of a reboot. Only uptime going backwards is.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_FW_LOG = re.compile(r"^\s*(?:TRACE|DEBUG|INFO|WARN|ERROR|CRIT)\s*\|\s*\S+\s+(\d+)\s")


def node_hex(num: int) -> str:
    return f"!{num & 0xFFFFFFFF:08x}"


def parse_node(node) -> int:
    if isinstance(node, int):
        return node
    s = str(node).strip()
    return int(s[1:], 16) if s.startswith("!") else int(s, 0)


# ---------------------------------------------------------------------------------------------- Meshtastic
RADIO_VIDS = (0x1A86, 0x10C4, 0x303A, 0x0403)   # CH9102/CH340, CP210x, Espressif native USB, FTDI


def list_radio_ports() -> list[str]:
    from serial.tools import list_ports
    return [p.device for p in list_ports.comports() if p.vid in RADIO_VIDS]


def _port_present(port: str) -> bool:
    from serial.tools import list_ports
    return any(p.device == port for p in list_ports.comports())


_watched_cls = None


def _default_factory(port: str, owner: "MeshtasticRadio"):
    """A SerialInterface that reports raw (non-protobuf) text lines to `owner`. Once the API session is up the
    firmware only speaks protobuf, so a burst of raw text lines means the board rebooted into console mode."""
    global _watched_cls
    if _watched_cls is None:
        import meshtastic.serial_interface as si

        class _Watched(si.SerialInterface):
            _owner = None

            def _handleLogByte(self, b):
                if b == b"\n" and self._owner is not None:
                    self._owner._raw_line(self)
                super()._handleLogByte(b)
        _watched_cls = _Watched
    iface = _watched_cls.__new__(_watched_cls)
    iface._owner = owner
    _watched_cls.__init__(iface, devPath=port)
    return iface


class MeshtasticRadio:
    """A Meshtastic board over USB serial, supervised so a crashing board does not take the link down.

    Failure modes handled (the T3 boards are known to crash):
      * USB/serial drop (board unplugged, brown-out, native-USB re-enumeration): the meshtastic library publishes
        connection.lost and silently DISCARDS later writes. We reopen the port (or the one radio still plugged in).
      * firmware reboot behind the USB-serial chip (COM port stays open): the board falls back to printing boot logs
        as raw text and stops streaming packets. A burst of raw lines triggers a reopen + API handshake.
      * stuck: the library marks itself disconnected and never recovers within stuck_s -> reopen.
      * manual: reconnect() (chat command /reconnect) for a board that froze silently.
    While the radio is down, send() waits up to send_wait seconds for it to come back instead of dropping.
    status(event_dict) reports radio_down / radio_retrying / radio_up.
    """

    kind = "radio"

    def __init__(self, port: str, status: Optional[Callable[[dict], None]] = None, iface_factory=None,
                 send_wait: float = 90.0, grace_s: float = 8.0, stuck_s: float = 30.0, retry_s: float = 2.0):
        from pubsub import pub
        self.port = port
        self.status = status or (lambda ev: None)
        self._factory = iface_factory or _default_factory
        self.send_wait, self.grace_s, self.stuck_s, self.retry_s = send_wait, grace_s, stuck_s, retry_s
        self._cb: Optional[Callable] = None
        self._lock = threading.RLock()
        self._up = threading.Event()
        self._reconnecting = False
        self._closed = False
        self._raw: list[float] = []
        self._raw_lines: list[str] = []   # the text that tripped the reboot detector, logged with radio_down
        self._fw_uptime: Optional[int] = None
        self._down_since: Optional[float] = None
        self._opened_at = 0.0
        self.iface = None
        self.num: Optional[int] = None
        self.node_id = "!????????"
        self.name = port
        self.reconnects = 0
        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        pub.subscribe(self._on_established, "meshtastic.connection.established")
        self._open_until_up(first=True)
        threading.Thread(target=self._watchdog, daemon=True, name="radio-watchdog").start()

    # ---------------------------------------------------------------- connection management
    def _open(self):
        port = self.port
        if not _port_present(port):
            found = list_radio_ports()
            if len(found) == 1:
                port = found[0]
            else:
                raise RuntimeError(f"{self.port} not present (radio ports seen: {found or 'none'})")
        iface = self._factory(port, self)
        info = iface.getMyNodeInfo() or {}
        num = info.get("num") or iface.myInfo.my_node_num
        with self._lock:
            if self.num is not None and num != self.num:
                self.status({"event": "radio_warning", "why": f"a different radio ({node_hex(num)}) is now on {port}, "
                             f"was {node_hex(self.num)}; continuing with the new one"})
            self.iface, self.port, self.num = iface, port, num
            self.node_id = node_hex(num)
            self.name = (info.get("user") or {}).get("longName") or port
            self._raw.clear()
            self._raw_lines.clear()
            self._fw_uptime = None
            self._opened_at = time.time()
            self._down_since = None
            self._up.set()

    def _open_until_up(self, first=False):
        n = 0
        while not self._closed:
            n += 1
            try:
                self._open()
                return True
            except Exception as e:
                if n == 1 or n % 5 == 0:
                    self.status({"event": "radio_retrying", "attempt": n, "port": self.port, "error": _short(e)})
                if first and n == 1:
                    print(f"[radio] cannot open {self.port} yet ({_short(e)}); retrying every {self.retry_s:.0f}s. "
                          f"Ctrl+C to stop.", flush=True)
                time.sleep(self.retry_s)
        return False

    def _trigger(self, why: str, lines: Optional[list] = None):
        with self._lock:
            if self._reconnecting or self._closed:
                return
            self._reconnecting = True
            self._up.clear()
            old = self.iface
        threading.Thread(target=self._reconnect, args=(why, old, lines), daemon=True, name="radio-reconnect").start()

    def _reconnect(self, why, old, lines=None):
        t0 = time.time()
        ev = {"event": "radio_down", "why": why, "port": self.port}
        if lines:
            ev["raw_lines"] = lines
        self.status(ev)
        _close_quietly(old)
        time.sleep(1.0)   # let a rebooting board finish enumerating
        ok = self._open_until_up()
        with self._lock:
            self._reconnecting = False
        if ok:
            self.reconnects += 1
            self.status({"event": "radio_up", "port": self.port, "node": self.node_id,
                         "after_s": round(time.time() - t0, 1)})

    def reconnect(self):
        self._trigger("manual reconnect")

    @property
    def is_up(self) -> bool:
        return self._up.is_set()

    def _on_lost(self, interface=None):
        if interface is not None and interface is self.iface:
            self._trigger("serial connection lost (board crashed, rebooted or was unplugged)")

    def _on_established(self, interface=None):
        if interface is not None and interface is self.iface and not self._reconnecting:
            self._down_since = None
            self._up.set()

    def _raw_line(self, iface):
        now = time.time()
        if iface is not self.iface or self._reconnecting or now - self._opened_at < self.grace_s:
            return
        text = _ANSI.sub("", getattr(iface, "cur_log_line", "") or "")
        m = _FW_LOG.match(text)
        if m:
            up, prev = int(m.group(1)), self._fw_uptime
            self._fw_uptime = up
            if prev is not None and up < prev:
                self._trigger(f"board rebooted (uptime went {prev}s -> {up}s)", [repr(text)[:200]])
            return
        line = repr(text)[:200]   # not a firmware log line: ROM boot banner, crash dump, ...
        keep = [i for i, t in enumerate(self._raw) if now - t < 5.0]
        self._raw = [self._raw[i] for i in keep] + [now]
        self._raw_lines = [self._raw_lines[i] for i in keep if i < len(self._raw_lines)] + [line]
        if len(self._raw) >= 3:
            self._trigger("board rebooted (boot log on serial, API session gone)", list(self._raw_lines))

    def _watchdog(self):
        while not self._closed:
            time.sleep(1.0)
            iface = self.iface
            if iface is None or self._reconnecting:
                continue
            connected = getattr(iface, "isConnected", None)
            stream_gone = hasattr(iface, "stream") and iface.stream is None
            if stream_gone:
                self._trigger("serial port closed underneath us")
                continue
            if connected is not None and not connected.is_set():
                self._down_since = self._down_since or time.time()
                if time.time() - self._down_since > self.stuck_s:
                    self._trigger(f"radio not reconnected by itself after {self.stuck_s:.0f}s")
            else:
                self._down_since = None

    # ---------------------------------------------------------------- the transport interface
    def describe(self) -> str:
        try:
            node = self.iface.localNode
            lora = node.localConfig.lora
            ch = node.channels[0].settings.name if node.channels else "?"
            meta = getattr(self.iface, "metadata", None)
            fw = getattr(meta, "firmware_version", "?") if meta else "?"
            return (f"{self.name} {self.node_id} on {self.port} | fw {fw} | channel {ch or '(default)'} "
                    f"| preset {lora.modem_preset} | region {lora.region}")
        except Exception as e:  # describe() is cosmetic; never fail startup over it
            return f"{self.name} {self.node_id} on {self.port} ({_short(e)})"

    def nodes(self) -> list[str]:
        out = []
        for num, n in ((self.iface and self.iface.nodesByNum) or {}).items():
            u = n.get("user") or {}
            if num == self.num:
                continue
            out.append(f"{node_hex(num)}  {u.get('longName', '?'):24s} snr={n.get('snr')} "
                       f"hops={n.get('hopsAway')} last={n.get('lastHeard')}")
        return out

    def on_frame(self, cb: Callable[[bytes, dict], None]):
        self._cb = cb

    def _on_receive(self, packet, interface):
        if interface is not self.iface or self._cb is None:
            return
        dec = packet.get("decoded") or {}
        if dec.get("portnum") not in ("PRIVATE_APP", PRIVATE_APP) or "payload" not in dec:
            return
        meta = {"from": node_hex(packet.get("from", 0)), "to": node_hex(packet.get("to", 0)),
                "packet_id": packet.get("id"), "snr": packet.get("rxSnr"), "rssi": packet.get("rxRssi"),
                "hops": (packet.get("hopStart") or 0) - (packet.get("hopLimit") or 0)}
        try:
            self._cb(bytes(dec["payload"]), meta)
        except Exception as e:  # an app bug must never kill the meshtastic reader thread
            print(f"[link] frame handler error: {e!r}")

    def send(self, frame: bytes, dest, want_ack: bool, on_radio_ack=None) -> int:
        def on_ack_nak(p):  # meshtastic only delivers plain ACKs to handlers named onAckNak
            err = (p.get("decoded") or {}).get("routing", {}).get("errorReason", "NONE")
            if on_radio_ack:
                on_radio_ack(err == "NONE", err)
        on_ack_nak.__name__ = "onAckNak"
        deadline = time.time() + self.send_wait
        while True:
            if not self._up.wait(max(0.0, deadline - time.time())):
                raise RuntimeError(f"radio offline for {self.send_wait:.0f}s (crashed or unplugged); not sent")
            iface = self.iface
            if getattr(iface, "stream", True) is None:   # writes to a closed stream are silently dropped
                self._trigger("serial port closed underneath us")
                continue
            mp = iface.sendData(bytes(frame), destinationId=parse_node(dest), portNum=PRIVATE_APP,
                                wantAck=want_ack, onResponse=on_ack_nak if (want_ack and on_radio_ack) else None,
                                onResponseAckPermitted=True)
            return mp.id

    def close(self):
        self._closed = True
        self._up.set()
        _close_quietly(self.iface)


def _short(e: Exception) -> str:
    s = f"{type(e).__name__}: {e}"
    return s if len(s) < 160 else s[:157] + "..."


def _close_quietly(iface, timeout: float = 5.0):
    if iface is None:
        return
    t = threading.Thread(target=lambda: _try(iface.close), daemon=True)
    t.start()
    t.join(timeout)


def _try(fn):
    try:
        fn()
    except Exception:
        pass


# ---------------------------------------------------------------------------------------------- UDP
_MAGIC = b"MC1"
_HDR = struct.Struct(">3sII")   # magic, from node num, to node num


class UdpLink:
    """Point-to-point UDP. Each datagram = MC1 | from u32 | to u32 | frame.

    Node id = hash of --name (default: hostname), so it is stable across restarts. The receiver learns the
    sender's address from the first datagram, so only one side strictly needs --peer."""

    kind = "udp"

    def __init__(self, listen: str, peer: str | None, name: str | None = None):
        self.name = name or socket.gethostname()
        self.num = int.from_bytes(hashlib.sha256(self.name.encode()).digest()[:4], "big")
        self.node_id = node_hex(self.num)
        host, port = _hostport(listen, "0.0.0.0")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.listen = (host, port)
        self.peer = _hostport(peer, None) if peer else None
        self.book: dict[str, tuple] = {}       # node id -> last address heard from
        self._cb = None
        self._ids = 0
        self._closed = False
        threading.Thread(target=self._loop, daemon=True, name="udp-rx").start()

    def describe(self) -> str:
        return (f"UDP {self.name} {self.node_id} listening on {self.listen[0]}:{self.listen[1]}"
                + (f" | peer {self.peer[0]}:{self.peer[1]}" if self.peer else " | peer: learned from first packet"))

    def nodes(self) -> list[str]:
        return [f"{k}  {v[0]}:{v[1]}" for k, v in self.book.items()]

    def on_frame(self, cb):
        self._cb = cb

    def _loop(self):
        while not self._closed:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                if self._closed:
                    return
                continue
            if len(data) < _HDR.size or data[:3] != _MAGIC:
                continue
            _, src, dst = _HDR.unpack_from(data)
            src_id = node_hex(src)
            self.book[src_id] = addr
            if self.peer is None:
                self.peer = addr
            if dst == 0:   # "whoever is on the other end" (sender did not know our id yet)
                dst = self.num
            if self._cb:
                try:
                    self._cb(data[_HDR.size:], {"from": src_id, "to": node_hex(dst), "packet_id": None,
                                                "snr": None, "rssi": None, "hops": 0})
                except Exception as e:
                    print(f"[link] frame handler error: {e!r}")

    def send(self, frame: bytes, dest, want_ack: bool, on_radio_ack=None) -> int:
        dest_id = dest if isinstance(dest, str) and dest.startswith("!") else node_hex(parse_node(dest))
        addr = self.book.get(dest_id) or self.peer
        if addr is None:
            raise RuntimeError("no UDP peer yet: pass --peer IP:PORT on at least one side")
        self.sock.sendto(_HDR.pack(_MAGIC, self.num, parse_node(dest_id)) + bytes(frame), addr)
        self._ids += 1
        return self._ids   # no routing ACK over UDP; on_radio_ack is never called

    def close(self):
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass


def _hostport(s: str, default_host):
    s = str(s)
    if ":" in s:
        h, p = s.rsplit(":", 1)
        return (h or default_host or "127.0.0.1", int(p))
    return (default_host or "127.0.0.1", int(s))


# ---------------------------------------------------------------------------------------------- loopback
class LoopbackAir:
    """In-process 'air'. tamper(frame, src, dst) may return modified bytes, or None to drop the frame."""

    def __init__(self):
        self.nodes: dict[str, "Loopback"] = {}
        self.tamper: Optional[Callable] = None
        self.log: list[tuple[str, str, bytes]] = []
        self._ids = 0
        self.lock = threading.Lock()

    def radio(self, node_id: str) -> "Loopback":
        r = Loopback(self, node_id)
        self.nodes[node_id] = r
        return r

    def deliver(self, frame, src, dst, want_ack, on_radio_ack) -> int:
        with self.lock:
            self._ids += 1
            pid = self._ids
        self.log.append((src, dst, bytes(frame)))
        out = self.tamper(frame, src, dst) if self.tamper else frame
        target = self.nodes.get(dst)
        if out is None or target is None:
            if want_ack and on_radio_ack:
                on_radio_ack(False, "MAX_RETRANSMIT")
            return pid
        if want_ack and on_radio_ack:
            on_radio_ack(True, "NONE")
        if target._cb:
            target._cb(bytes(out), {"from": src, "to": dst, "packet_id": pid, "snr": 0.0, "rssi": -40, "hops": 0})
        return pid


class Loopback:
    kind = "loopback"

    def __init__(self, air: LoopbackAir, node_id: str):
        self.air, self.node_id, self.name = air, node_id, node_id
        self._cb = None

    def describe(self) -> str:
        return f"loopback {self.node_id}"

    def nodes(self) -> list[str]:
        return [n for n in self.air.nodes if n != self.node_id]

    def on_frame(self, cb):
        self._cb = cb

    def send(self, frame, dest, want_ack, on_radio_ack=None) -> int:
        return self.air.deliver(frame, self.node_id, dest, want_ack, on_radio_ack)

    def close(self):
        pass

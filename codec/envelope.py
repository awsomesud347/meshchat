"""Application envelope around a codec packet: stable message identity and the decode ACK (§3, §17, G2).

Every frame travels as one Meshtastic direct message on PRIVATE_APP (256). Byte 0 = version (high nibble) and kind
(low nibble). All integers are big-endian.

REPORT (10 bytes + codec packet):
    0      version|kind=1
    1..4   message id (u32, chosen by the sender, identical on every attempt and on the raw fallback)
    5      attempt (u8: 1 = first send, 2 = the one bounded raw retry)
    6..9   sent time (u32 Unix seconds, sender clock, unverified; 0 = unknown)
    10..   codec packet (mesh_codec.build_packet), carries its own CRC over the exact text

DECODE_ACK (7 bytes), receiver -> sender, sent without want_ack and never answered (no ACK loops):
    0      version|kind=2
    1..4   message id
    5      attempt it answers
    6      status: 0 VERIFIED (decoded, CRC ok), 1 DECODE_FAILED, 2 CONFLICT (same id, different text)

A radio ACK (Meshtastic routing) means "the radio got it". A DECODE_ACK VERIFIED means "decoded and checksum
verified". Neither means a human read it; that is shown only on the incident desk.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

VERSION = 1
REPORT, DECODE_ACK = 1, 2
VERIFIED, DECODE_FAILED, CONFLICT = 0, 1, 2
STATUS_NAMES = {VERIFIED: "VERIFIED", DECODE_FAILED: "DECODE_FAILED", CONFLICT: "CONFLICT"}

REPORT_HEADER = 10
ACK_SIZE = 7
_REPORT = struct.Struct(">BIBI")
_ACK = struct.Struct(">BIBB")


class EnvelopeError(ValueError):
    pass


@dataclass(frozen=True)
class Report:
    msg_id: int
    attempt: int
    sent_at: int          # 0 = unknown
    packet: bytes         # codec packet


@dataclass(frozen=True)
class DecodeAck:
    msg_id: int
    attempt: int
    status: int


def encode_report(msg_id: int, attempt: int, sent_at: int, packet: bytes) -> bytes:
    if not (0 <= msg_id <= 0xFFFFFFFF and 1 <= attempt <= 255 and 0 <= sent_at <= 0xFFFFFFFF):
        raise EnvelopeError("message id, attempt or sent time out of range")
    if not packet:
        raise EnvelopeError("empty codec packet")
    return _REPORT.pack((VERSION << 4) | REPORT, msg_id, attempt, sent_at) + bytes(packet)


def encode_ack(msg_id: int, attempt: int, status: int) -> bytes:
    if status not in STATUS_NAMES:
        raise EnvelopeError(f"unknown status {status}")
    return _ACK.pack((VERSION << 4) | DECODE_ACK, msg_id, attempt, status)


def decode(frame: bytes) -> Report | DecodeAck:
    """Parse a received frame. Raises EnvelopeError (never anything else) for malformed input."""
    frame = bytes(frame)
    if not frame:
        raise EnvelopeError("empty frame")
    version, kind = frame[0] >> 4, frame[0] & 0xF
    if version != VERSION:
        raise EnvelopeError(f"unsupported envelope version {version}")
    if kind == REPORT:
        if len(frame) <= REPORT_HEADER:
            raise EnvelopeError("report frame too short")
        _, msg_id, attempt, sent_at = _REPORT.unpack_from(frame)
        if attempt < 1:
            raise EnvelopeError("attempt 0")
        return Report(msg_id, attempt, sent_at, frame[REPORT_HEADER:])
    if kind == DECODE_ACK:
        if len(frame) != ACK_SIZE:
            raise EnvelopeError("decode ack has the wrong length")
        _, msg_id, attempt, status = _ACK.unpack(frame)
        if status not in STATUS_NAMES:
            raise EnvelopeError(f"unknown ack status {status}")
        return DecodeAck(msg_id, attempt, status)
    raise EnvelopeError(f"unknown frame kind {kind}")

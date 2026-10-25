"""LoRa airtime, collision-limited delivery, and range math for Meshtastic presets.

Airtime: Semtech SX127x/SX126x time-on-air formula (AN1200.13), explicit header, CRC on.
Meshtastic specifics taken from firmware source: 16-symbol preamble, 16-byte radio header,
presets (SF/BW/CR) from MeshRadio.h. Protobuf Data overhead for a text message ~6 bytes.

Delivery model (per hop): unslotted-ALOHA style collision survival
    P(success) = exp(-U * (1 + T / Tbar))
where U is the fraction of time the channel is already busy with other traffic and Tbar
is the mean airtime of that other traffic. Meshtastic does listen-before-talk, which
removes many collisions between nodes that hear each other, so this is a pessimistic
model; hidden-node collisions (the common case in a multi-hop mesh) still follow it.
"""
import json, math, sys

PREAMBLE = 16
MESH_HDR = 16
PB_OVERHEAD = 6

PRESETS = {  # name: (SF, BW kHz, CR denominator 5..8)
    "LongFast (default)": (11, 250.0, 5),
    "MediumFast": (9, 250.0, 5),
    "ShortFast": (7, 250.0, 5),
    "LongSlow": (12, 125.0, 8),
}

def airtime_ms(text_bytes, sf, bw_khz, cr_den):
    pl = MESH_HDR + PB_OVERHEAD + text_bytes
    tsym = (2 ** sf) / (bw_khz * 1000.0) * 1000.0  # ms
    de = 1 if tsym > 16.0 else 0
    cr = cr_den - 4
    n = 8 + max(math.ceil((8 * pl - 4 * sf + 28 + 16) / (4 * (sf - 2 * de))) * (cr + 4), 0)
    return (PREAMBLE + 4.25) * tsym + n * tsym

def p_hop(t, tbar, u):
    return math.exp(-u * (1 + t / tbar))

if __name__ == "__main__":
    sizes = json.loads(sys.argv[1])  # {"label": bytes, ...}
    out = {"airtime_ms": {}, "delivery": {}}
    for name, (sf, bw, cr) in PRESETS.items():
        out["airtime_ms"][name] = {k: round(airtime_ms(v, sf, bw, cr), 1) for k, v in sizes.items()}
    # delivery on default preset, other traffic averaging the raw-median message
    sf, bw, cr = PRESETS["LongFast (default)"]
    tbar = airtime_ms(sizes["raw_median"], sf, bw, cr)
    for u in (0.10, 0.25, 0.40):
        row = {}
        for k, v in sizes.items():
            p1 = p_hop(airtime_ms(v, sf, bw, cr), tbar, u)
            row[k] = {"1hop": round(p1, 3), "3hop": round(p1 ** 3, 3)}
        out["delivery"][f"channel_busy_{int(u*100)}pct"] = row
    # range: SX1262 sensitivity (datasheet, BW125): SF7..SF12
    sens = {7: -124, 8: -127, 9: -130, 10: -133, 11: -135.5, 12: -137}  # dBm, approx
    gains = [sens[s] - sens[s + 1] for s in range(7, 12)]
    out["sf_step_gain_db"] = gains
    out["range_factor_per_sf_step"] = {f"path_loss_exp_{n}": round(10 ** ((sum(gains) / len(gains)) / (10 * n)), 2)
                                       for n in (2.0, 2.7, 3.5)}
    # MeshXT claim check: does halving payload let you go SF+1 at the same airtime?
    chk = {}
    for b in (60, 120, 200):
        chk[b] = {"SF11 full": round(airtime_ms(b, 11, 250, 5), 1),
                  "SF12 half": round(airtime_ms(b // 2, 12, 250, 5), 1)}
    out["meshxt_sf_step_check_ms"] = chk
    print(json.dumps(out, indent=1))

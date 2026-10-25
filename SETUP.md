# meshchat: setup for both laptops (radio only, works offline)

Two laptops, two roles. The software is identical on both; only the commands you run differ.

| Role | What it does | Example radio |
|---|---|---|
| **H**: hub / command post | receives field reports, replies | "MeshSOS A" on COM5 |
| **F**: field | sends reports, receives replies | "MeshSOS B" on COM4 |

Nothing here needs Wi-Fi or internet. Packages and the codec runtime ship in the zip. The model (386 MB) goes over by USB stick.

Each stage proves one layer, so when something breaks you know which layer it is:

1. Install (both laptops)
2. Codec check (both). This proves the two laptops compute identical model math.
3. Radio prep (both radios)
4. Two-way over the radios
5. When a board crashes
6. Pre-demo health check

---

## 0. What you need

**Each laptop:**
- [ ] Windows 10 or 11 on an **x86-64 (Intel or AMD) CPU**. In PowerShell, `$env:PROCESSOR_ARCHITECTURE` must print `AMD64`. A Snapdragon/ARM laptop cannot decode.
- [ ] Python **3.11, 3.12 or 3.13**. Check with `py --list`. The bundled offline packages cover exactly these. If Python is missing, install it from a python.org installer you carry on the USB stick, then open a **new** PowerShell.
- [ ] `meshchat.zip`

**USB stick:** `meshchat.zip` plus `SmolLM2-360M.Q8_0.gguf`. Take the model from `C:\Users\aweso\Downloads\` on Sud's laptop, or from `meshchat\models\` on any laptop that has finished setup.

**Radios:**
- [ ] 2 LoRa boards on the same Meshtastic firmware version, 915 MHz
- [ ] 2 antennas. **Screw the antenna on before plugging in USB.** Transmitting without one can damage the radio.
- [ ] 2 USB **data** cables. Charge-only cables look identical; if no COM port appears, swap the cable first.

---

## 1. Install (H and F)

**1.1 Unzip.** Right-click `meshchat.zip` → Properties → if there's an **Unblock** checkbox, tick it → OK. Then Extract All to `C:\meshchat`, so that `C:\meshchat\meshchat.py` exists. On Sud's laptop the folder is already unpacked at `C:\Users\aweso\Downloads\meshchat\`, with the model in place.

**1.2 Put the model in place (F, and any laptop without it).** Copy `SmolLM2-360M.Q8_0.gguf` from the USB stick into `C:\meshchat\models\`. If you skip this, setup also looks in `Downloads\` and in the root of every drive, including the USB stick, and copies it from there.

**1.3 Run setup.** Open PowerShell (not as admin):

```powershell
cd C:\meshchat
powershell -ExecutionPolicy Bypass -File setup.ps1
```

Setup does the following:
1. Checks the CPU is x86-64.
2. Finds Python and creates `.venv\` (a private Python for this folder only).
3. Installs packages **offline** from `wheels\`. It only uses the internet if that fails.
4. Finds the model and checks its SHA-256.
5. Runs the tests (27, with simulated radios including crash cases).
6. Runs the codec check (stage 2).
7. Lists the serial ports.

**Expected output (the parts that matter):**

```
Using py -3.12
packages OK
model SHA-256 OK
--- tests (simulated radios, no hardware needed) ---
27 passed
--- codec check (must say fingerprint 422ac23fca10 and PASS) ---
fingerprint 422ac23fca10  OK
...
RESULT PASS: this laptop can talk LLM-compressed to any other PASS laptop
```

**If setup fails:**

| Message | Fix |
|---|---|
| `This laptop is ARM64` | use a different laptop |
| `Need Python 3.11, 3.12 or 3.13` | install one, open a new PowerShell, rerun. If you only have 3.14, install 3.12 alongside it; setup prefers 3.12 |
| `running scripts is disabled on this system` | use the exact `powershell -ExecutionPolicy Bypass -File setup.ps1` line |
| `Offline install failed; trying the internet...` then `pip install failed` | the Python version isn't 3.11–3.13; check `.venv\Scripts\python --version`. Delete `.venv` and rerun after installing 3.12 |
| `NO MODEL` | copy the model into `models\` (step 1.2), rerun |
| `model SHA-256 ... is WRONG` | a different file with the same name (QuantFactory's, for example). Delete the `.bad` file and copy the right one from Sud's laptop |
| tests fail on a DLL load error | step 1.1 Unblock was skipped: `Get-ChildItem -Recurse C:\meshchat \| Unblock-File`, rerun |

You can rerun `setup.ps1` any time; it skips what's already done.

---

## 2. Codec check (H and F)

Setup already ran it. To rerun: `.\mc check`

**Must show on both laptops:**

```
runtime win-x64: all pinned binaries match SHA256SUMS
        sha256 8e0b159d81bce479... OK
context sha256 55d73dd75f06c70a... OK
fingerprint 422ac23fca10  OK
decode 20 packets encoded on another machine: 20/20 exact
encode 8 probes byte-identical to the reference: 8/8
RESULT PASS
```

**Why it matters:** the receiver can only rebuild a message if its model computes exactly the same probabilities as the sender's. Two laptops that both print fingerprint `422ac23fca10` and `PASS` decode each other. This was verified with the real model on a different machine and OS, against packets encoded on Sud's Windows laptop.

| Result | Meaning | Fix |
|---|---|---|
| `FAIL runtime: ... missing or modified` | antivirus removed a DLL, or the unzip broke | re-extract; allow the folder in Defender if it quarantined something |
| `fingerprint xxxx MISMATCH`, or `decode 17/20`, `encode 6/8` | different math on this machine | run it as `.\mc check` (uses `.venv`); never `pip install llama-cpp-python` into it. If it persists, this laptop can still chat, but uncompressed (raw/Unishox2) only |

**Don't continue until both laptops print `RESULT PASS`.**

---

## 3. Radio prep (both radios, once)

**3.1 Firmware.** Both boards need the **same** Meshtastic firmware version. Skip this if they were flashed together. Otherwise, before the event, flash at <https://flasher.meshtastic.org> in Chrome or Edge (target "T-LoRa V2.1-1.6" for the LilyGO T3 V1.6.1).

**3.2 Find the COM port.** Antenna on, then USB. Then:

```powershell
cd C:\meshchat
.venv\Scripts\python -m serial.tools.list_ports -v
```

You're looking for a line like `COM4  USB-Enhanced-SERIAL CH9102 (COM4)`. Write it down. No new port appears?
- Try another cable.
- Install the WCH CH9102/CH343 driver ("CH343SER", from wch-ic.com; bring it on the USB stick), then replug.
- Check Device Manager → **Ports (COM & LPT)**.

**3.3 Region, preset, name.** On each laptop, with its own radio's port:

```powershell
.venv\Scripts\meshtastic --port COM4 --set lora.region US --set lora.modem_preset LONG_FAST
.venv\Scripts\meshtastic --port COM4 --set-owner "MeshSOS B"
```

The radio reboots after a config change. Wait about 15 seconds between commands. Use `"MeshSOS A"` on H's radio.

**3.4 One private channel on both radios.** Recommended: the venue will be full of other radios. On **one** radio:

```powershell
.venv\Scripts\meshtastic --port COM4 --ch-set psk random --ch-index 0
.venv\Scripts\meshtastic --port COM4 --info
```

Copy the `Primary channel URL: https://meshtastic.org/e/#...` line, for example into a text file on the USB stick. On the **other** laptop:

```powershell
.venv\Scripts\meshtastic --port COM5 --seturl "https://meshtastic.org/e/#...paste..."
```

The URL carries the channel key and radio settings, so the two radios now match exactly.

**3.5 Get H's node ID.** On H:

```powershell
.\mc chat --port COM5
#   me:    MeshSOS A !f99260d4 on COM5 | ...      <- !f99260d4 is H's node ID
/quit
```

Write it down. It never changes for that radio.

**3.6 Confirm the radios hear each other.** Leave both powered for about a minute, then on either laptop:

```powershell
.venv\Scripts\meshtastic --port COM4 --nodes
```

The other radio should be in the table. If it isn't after 2–3 minutes, the region, preset or channel differ; redo 3.3 and 3.4.

**Close every `meshtastic` command before stage 4.** Only one program can hold a COM port.

---

## 4. Two-way over the radios

**4.1 H first:**

```powershell
cd C:\meshchat
.\mc chat --port COM5
```

```
opening radio on COM5 (this takes a few seconds; the board may reset when the port opens)...
me:    MeshSOS A !f99260d4 on COM5 | fw 2.7.x | channel ... | preset 0 | region 1
codec: SmolLM2-360M OK, fingerprint 422ac23fca10, loaded in 3.1s
```

Here `preset 0` = LONG_FAST and `region 1` = US. If `codec:` says anything except `OK, fingerprint 422ac23fca10`, go back to stage 2.

**4.2 F, sending to H's ID:**

```powershell
cd C:\meshchat
.\mc chat --port COM4 --dest !f99260d4
```

If only one radio is plugged in, you can leave out `--port`; it's auto-detected. Lost the ID? On F, `/nodes` lists every radio heard; then `/to !xxxxxxxx`.

**4.3 Send from F.** Type a report and press Enter. The sizes below are real codec output; the receipt time and signal values are illustrative and will differ on your boards:

```
>> TX a6c83886 try 1 LLM  86 B text -> 18 B codec packet (28 B on air)  airtime ~1051 ms raw -> 641 ms
   radio ACK try 1: yes                        <- H's radio received the packet
   decode receipt VERIFIED for a6c83886 try 1  <- H's laptop decoded it, checksum matched
OK VERIFIED  a6c83886  86 B text, 28 B on air, receipt in 3.2s
```

On H:

```
<< RX from !06fca5ec  LLM  28 B on air -> 86 B text, CRC OK, decoded in 0.4s snr=9.5 rssi=-41
   "Please I am trapped in a Restaurant in Rue Du Centre, Port-au-Prince. Please send Help"
```

**4.4 Reply from H.** Just type. H replies to whoever messaged it last; the reply travels back compressed, and F decodes it and sends its own receipt.

**4.5 Safety-net drill.** On F:

```
/corrupt Shelter at the library needs blankets and drinking water for 40 people tonight.
```

H shows `RX REJECTED ... checksum failed`, then the same message arriving `RAW`. F shows `DECODE_FAILED` → raw retry → `OK VERIFIED`. A wrong text is never shown.

**If stage 4 fails:**

| You see | Meaning | Fix |
|---|---|---|
| stuck at `cannot open COM4 yet (... Access is denied)` | another program holds the port | close the meshtastic CLI, client.meshtastic.org and any serial monitor. meshchat keeps retrying and continues on its own once the port is free |
| `radio ACK try 1: NO (MAX_RETRANSMIT)` | the radio link failed | antennas? same channel (3.4)? move the radios closer; on the demo table they can sit a meter apart |
| radio ACK `yes`, but no decode receipt, then a retry | H isn't running meshchat, or F has the wrong ID | compare F's `--dest` with H's `me:` line |
| `NO_CHANNEL` / `PKI_FAILED` | the radios haven't swapped node info yet | wait until `/nodes` shows the other radio; check 3.4 |
| `not sent: ... exceeds the 216 B radio limit` | too long even compressed | shorten; there's deliberately no splitting |

---

## 5. When a board crashes

The boards are known to crash. **Don't restart meshchat.** It recovers by itself:

| What the board does | What meshchat does | What you see |
|---|---|---|
| drops off USB (crash, brown-out, cable bump) | reopens the port every 2 s; if the COM number changed and only one radio is plugged in, it uses that one | `!! RADIO DOWN: serial connection lost` … `OK RADIO BACK on COM4 after 6.2s` |
| reboots while USB stays connected | notices boot text on the serial line, or the firmware's uptime going backwards, then reopens and redoes the Meshtastic handshake. Normal firmware log lines (like the per-minute telemetry) are ignored | `!! RADIO DOWN: board rebooted (boot log on serial ...)` … `OK RADIO BACK` |
| library stuck "disconnected" | reopens after 30 s | `!! RADIO DOWN: radio not reconnected by itself after 30s` |
| freezes silently (no reboot, nothing) | can't be detected automatically | sends end `NOT_DELIVERED`. Type `/reconnect`, or unplug and replug the USB; meshchat picks the board back up |

**Your messages aren't lost:**
- Anything you type while the radio is down waits, for up to 90 s, and goes out when it's back.
- A message that didn't get a decode receipt (yours, or because the *other* board crashed) is kept. It's resent **with its original message ID** when your radio comes back, and every 20 s otherwise, up to 3 times. `/retry` resends now; `/status` lists what's still undelivered.
- The receiver remembers message IDs, so a resend **never shows twice**. You'll see `[duplicate, already shown]` on H instead.

If a board crashes over and over, unplug it, wait 5 s, plug it back in. meshchat reconnects; nothing to retype.

---

## 6. Pre-demo health check (2 minutes, before each judge)

1. Antennas on. Both radios on USB.
2. If either laptop was restarted: `.\mc check` → `PASS`, fingerprint `422ac23fca10`.
3. H: `.\mc chat --port COM5`. F: `.\mc chat --port COM4 --dest !<H id>`.
4. Send one line each way. Both should end `OK VERIFIED`.
5. `/status` on both: `radio up: True`, undelivered: none.

**Demo moves:**

| Type | Shows |
|---|---|
| a full report on F | `86 B text -> 18 B codec packet`, airtime ~1051 → 641 ms |
| `/explain Water is rising into the school gym on 5th Street` | per-word bit cost: predictable words ≈0.1 bit, names and numbers cost the most |
| `/corrupt <text>` | rejected by the checksum, delivered raw: a wrong text is never shown |
| `/raw <same text>` | the uncompressed size, for comparison |
| unplug H's radio mid-demo, plug it back | `RADIO DOWN` → `RADIO BACK`; the pending message is resent and shown once |
| `/stats` | session totals |

Keep a recorded video of a good run as a backup.

---

## Command reference

```
.\mc check                       codec check: hashes, fingerprint, cross-machine decode/encode
.\mc chat --port COMx [--dest !id]
      --ack-timeout 30           seconds to wait for a decode receipt before the raw retry
      --resend-every 20          seconds between automatic resends of undelivered messages (0 = off)
      --max-resends 3            automatic resend rounds per message
      --no-auto-resend           never resend automatically (use /retry)
      -v                         show every frame in hex
.\mc explain "text"              per-token bit cost
.\mc selftest                    two endpoints in one process, no radio
```

Inside chat:

```
text             send to the current destination
/to !id          change destination             /nodes      radios heard
/status          radio state + undelivered      /retry      resend undelivered now
/reconnect       reopen a frozen radio          /stats      totals
/corrupt TEXT    force a checksum failure + raw retry
/raw TEXT        uncompressed                    /unishox TEXT   Unishox2 only
/explain TEXT    per-token bits                  /probes     8 reference texts
/quit
```

Logs: `logs\meshchat-chat-<host>-<time>.jsonl`, one JSON object per event: frames in hex, receipts, outcomes, radio down/up.

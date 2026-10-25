# meshchat

**Setup, step by step, for both laptops and both radios: [SETUP.md](SETUP.md).**

Two-way LLM-compressed messaging between two Windows laptops over Meshtastic radios. It's standalone and needs no internet.

On each laptop, every message is:
1. compressed by the shared SmolLM2-360M model, or Unishox2/raw, whichever is smallest and reproduces the text exactly;
2. sent with a message ID as a Meshtastic direct message on port 256;
3. decoded on the other laptop and checked with a CRC over the original text;
4. answered with a **decode receipt**.

If the receipt reports a failed decode, or none arrives, the sender retries once as raw text with the same ID. A wrong text is never shown, and a repeated message is shown once.

Built for crashing boards: if a board crashes, reboots or drops off USB, meshchat reopens it by itself. Messages typed meanwhile wait, and undelivered messages are resent with their original IDs (SETUP.md, stage 5).

```
mc.cmd            launcher:  .\mc check | chat | explain | selftest
meshchat.py       the program
codec\            mesh_codec.py (the proven arithmetic coder, unchanged), runtime.py (pinned llama.cpp + hash
                  checks), envelope.py (message id + receipts), link.py (two-way engine), transports.py (supervised
                  Meshtastic radio; a UDP link for bench tests)
runtime\win-x64\  pinned llama.cpp + Unishox2 DLLs, SHA-256 checked on every start
wheels\           all Python packages for Windows (Python 3.11-3.13), so setup works offline
data\             shared context (hash pinned) and cross-machine reference packets
models\           SmolLM2-360M.Q8_0.gguf (386 MB, not in the zip; SHA-256 8e0b159d...995eb4)
tests\            25 tests: two-way, every failure path, simulated board crashes (drop / reboot / freeze)
logs\             one JSONL per chat session
```

Verified so far with the real model: `check` PASS (fingerprint `422ac23fca10`, 40/40 packets encoded on Sud's Windows laptop decode exactly elsewhere, 8/8 probes byte-identical), and two independent processes exchanging LLM-compressed messages both ways, e.g. 86 B of text sent as 28 B (18 B codec packet + 10 B envelope). The real radio path has not been run on hardware from this build. The radio adapter is the one the team ran on the bench, plus the crash supervisor, which is tested against simulated boards.

"""Load the codec on the pinned llama.cpp runtime, and prove two endpoints compute the same thing.

Encoder and decoder must produce bit-identical logits. That holds only when both endpoints run the same model file,
the same shared context and the same llama.cpp binary, on the CPU. This module enforces all three:

* it points the vendored llama_cpp bindings (third_party/llama_cpp, pure Python) at the pinned binaries in
  runtime/<platform>/, never at a pip-installed build;
* it checks the binaries, the model and the context against pinned SHA-256 hashes;
* fingerprint() hashes the integer probability tables for a fixed probe text. Two endpoints that print the same
  fingerprint decode each other's packets.
"""
from __future__ import annotations

import hashlib
import os
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODEC_DIR = ROOT / "codec"

MODEL_FILE = "SmolLM2-360M.Q8_0.gguf"
MODEL_SHA256 = "8e0b159d81bce47993580827a26ec01e64092edd55165b6076e6fbfa37995eb4"
CONTEXT_SHA256 = "55d73dd75f06c70a4ec84c7d0c9cad2ae71601e4e1956cba9d322719576d5e43"
MODEL_ID = 2  # high nibble of packet byte 0 for this model
THREADS = max(1, min(4, (os.cpu_count() or 2) // 2))  # thread count never changes the logits, only speed

PROBE = "Water is rising into the school gym on 5th Street, two families on the roof."
# Identical on the Linux build (Intel Xeon) and the win-x64 DLLs (AMD Ryzen, native Windows 11).
REFERENCE_FINGERPRINT = "422ac23fca10"

# Where to look for the model when CODEC_MODEL_PATH / --model is not given.
MODEL_SEARCH = [
    ROOT / "models" / MODEL_FILE,
    ROOT / MODEL_FILE,
    Path(r"D:\Projects\calhacks-mesh\models") / MODEL_FILE,
    Path(r"C:\calhacks\models") / MODEL_FILE,
    Path.home() / "Downloads" / MODEL_FILE,
]


def platform_dir() -> Path:
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    osname = {"win32": "win", "darwin": "macos"}.get(sys.platform, "linux")
    return ROOT / "runtime" / f"{osname}-{arch}"


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_runtime() -> Path:
    """Check runtime/<platform>/ against its SHA256SUMS. Raises if anything is missing or modified."""
    d = platform_dir()
    sums = d / "SHA256SUMS"
    if not sums.exists():
        raise RuntimeError(f"no pinned codec runtime for {d.name}. Endpoints must be x86-64 Windows or Linux "
                           f"with AVX2; Apple Silicon / ARM compute different logits and cannot be an endpoint.")
    bad = []
    for line in sums.read_text().splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            p = d / name.strip().lstrip("*")
            if not p.exists() or sha256(p) != digest:
                bad.append(p.name)
    if bad:
        raise RuntimeError(f"pinned runtime files missing or modified in {d}: {bad}")
    return d


def prepare_imports():
    """Make `import llama_cpp` load the vendored bindings on the pinned binaries, and `import mesh_codec` work."""
    os.environ["LLAMA_CPP_LIB_PATH"] = str(platform_dir())
    for p in (str(ROOT / "third_party"), str(CODEC_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)
    mod = sys.modules.get("llama_cpp")
    if mod is not None and not str(getattr(mod, "__file__", "")).startswith(str(ROOT / "third_party")):
        raise RuntimeError("a different llama_cpp was imported before runtime; import runtime first")


def find_model(explicit: str | None = None) -> Path | None:
    for cand in [explicit, os.environ.get("CODEC_MODEL_PATH")]:
        if cand:
            p = Path(cand)
            if not p.is_file():
                raise RuntimeError(f"model not found at {p}")
            return p
    for p in MODEL_SEARCH:
        if p.is_file():
            return p
    return None


def context_path() -> Path:
    return Path(os.environ.get("CODEC_CONTEXT_PATH") or ROOT / "data" / "context_primed.txt")


def load(model_path: Path, check_hashes: bool = True, threads: int = THREADS):
    """A mesh_codec.SharedModel on the pinned runtime, after checking every pinned hash."""
    if check_hashes:
        verify_runtime()
        for path, want in ((model_path, MODEL_SHA256), (context_path(), CONTEXT_SHA256)):
            got = sha256(path)
            if got != want:
                raise RuntimeError(f"{path} has SHA-256 {got[:12]}..., expected {want[:12]}...: both endpoints "
                                   f"must use the identical file")
    prepare_imports()
    import mesh_codec
    return mesh_codec.SharedModel(str(model_path), context_path().read_text(encoding="utf-8"), MODEL_ID,
                                  threads=threads)


def fingerprint(model) -> str:
    """12 hex chars over the integer frequency tables the coder uses for PROBE. Equal => endpoints interoperate."""
    import numpy as np
    import mesh_codec
    h = hashlib.sha256()
    lg = model._start()
    for t in model.body_tokens(PROBE):
        h.update(np.ascontiguousarray(mesh_codec.freq_table(lg)).tobytes())
        lg = model._step(t)
    return h.hexdigest()[:12]

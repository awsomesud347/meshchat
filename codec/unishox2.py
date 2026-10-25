"""Bounded Unishox2 for the codec, with the same call shape as the `unishox2-py3` package.

    compress(text: str) -> (bytes, int)                 # compressed body, original UTF-8 length
    decompress(data: bytes, max_bytes: int) -> str      # raises ValueError if the output would exceed max_bytes
                                                        # or is not valid UTF-8 (UnicodeDecodeError is a ValueError)

It loads the library built from third_party/unishox2 (same C source as unishox2-py3 1.0.0) through ctypes, so
Windows needs no C compiler. Unlike unishox2-py3, decompression is bounded: a corrupt or hostile packet can't write
past the output buffer (G1). Compression output is byte-identical to unishox2-py3.

Because this file sits next to mesh_codec.py, `import unishox2` there resolves to it, not to the pip package.
"""
import ctypes
import platform
import shutil
import subprocess
import sys
from pathlib import Path

_SERVICE = Path(__file__).resolve().parent.parent
_SRC = _SERVICE / "third_party" / "unishox2"


def _platform_dir():
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    osname = {"win32": "win", "darwin": "macos"}.get(sys.platform, "linux")
    return _SERVICE / "runtime" / f"{osname}-{arch}"


def _library_path():
    d = _platform_dir()
    name = {"win32": "unishox2.dll", "darwin": "libunishox2.dylib"}.get(sys.platform, "libunishox2.so")
    path = d / name
    if path.exists():
        return path
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None or sys.platform == "win32":
        raise ImportError(f"Unishox2 library missing at {path}; see {_SRC / 'README.md'}")
    d.mkdir(parents=True, exist_ok=True)   # macOS / other Unix: build it once from the vendored source
    subprocess.run([cc, "-O2", "-shared", "-fPIC", "-DUNISHOX_API_WITH_OUTPUT_LEN=1", "-o", str(path),
                    str(_SRC / "unishox2.c"), str(_SRC / "usx_bounded.c")], check=True)
    return path


def _load():
    lib = ctypes.CDLL(str(_library_path()))
    for fn in (lib.usx_compress, lib.usx_decompress):
        fn.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        fn.restype = ctypes.c_int
    return lib


try:
    _lib = _load()
except (OSError, subprocess.CalledProcessError) as e:
    raise ImportError(f"cannot load the Unishox2 library: {e}") from e


def compress(text):
    raw = text.encode("utf-8")
    cap = 2 * len(raw) + 64
    out = ctypes.create_string_buffer(cap)
    n = _lib.usx_compress(raw, len(raw), out, cap)
    if n < 0 or n > cap:
        raise ValueError("unishox2 compression exceeded its buffer")
    return out.raw[:n], len(raw)


def decompress(data, max_bytes):
    max_bytes = int(max_bytes)
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    data = bytes(data)
    out = ctypes.create_string_buffer(max_bytes + 1)
    n = _lib.usx_decompress(data, len(data), out, max_bytes)
    if n < 0 or n > max_bytes:
        raise ValueError(f"unishox2 output exceeds {max_bytes} bytes")
    return out.raw[:n].decode("utf-8")

# Unishox2 (vendored)

`unishox2.c` / `unishox2.h` are Unishox2 by Siara Logics, exactly as shipped in `unishox2-py3` 1.0.0 (Apache-2.0, `LICENSE`).
`usx_bounded.c` is ours: two exported wrappers that call the library's own `unishox2_compress` /
`unishox2_decompress` with the default preset **and an output-length bound**. The `unishox2-py3` wrapper calls the
unbounded `_simple` API with a guessed buffer, which overflows on compressible text (G1).

Built into `../../runtime/<platform>/` (`unishox2.dll`, `libunishox2.so`) and loaded by `codec/unishox2.py`:

    gcc -O2 -shared -fPIC -DUNISHOX_API_WITH_OUTPUT_LEN=1 -o runtime/linux-x64/libunishox2.so third_party/unishox2/unishox2.c third_party/unishox2/usx_bounded.c
    x86_64-w64-mingw32-gcc -O2 -shared -DUNISHOX_API_WITH_OUTPUT_LEN=1 -o runtime/win-x64/unishox2.dll third_party/unishox2/unishox2.c third_party/unishox2/usx_bounded.c -static-libgcc

On macOS, `codec/unishox2.py` builds the library itself with the system C compiler on first use.

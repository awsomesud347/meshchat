/* Bounded wrappers over Unishox2 (default preset), so a corrupt radio packet can never overrun the output buffer. */
#define UNISHOX_API_WITH_OUTPUT_LEN 1
#include "unishox2.h"
#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif
EXPORT int usx_compress(const char *in, int len, char *out, int olen) { return unishox2_compress(in, len, out, olen, USX_PSET_DFLT); }
EXPORT int usx_decompress(const char *in, int len, char *out, int olen) { return unishox2_decompress(in, len, out, olen, USX_PSET_DFLT); }

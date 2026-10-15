import ctypes
import sys

if sys.platform == "linux":
    # Tests share a process; production keeps Amulet in the separate Worker.
    # Load GCC's unwinder before Endstone's bundled LLVM libunwind.
    ctypes.CDLL("libgcc_s.so.1", mode=ctypes.RTLD_GLOBAL)

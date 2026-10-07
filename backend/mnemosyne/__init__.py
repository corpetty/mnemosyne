"""Mnemosyne's backend."""

import os

# PyTorch compiles kernels (NeMo's models do, once at load) in a pool of worker processes by
# default: one per core, ~350 MB each, kept as long as the backend runs (21 of them on a 20-core
# machine, 2026-10-07). One thread compiles in-process instead. Set before torch is imported;
# ML code is imported lazily, after this package.
os.environ.setdefault("TORCHINDUCTOR_COMPILE_THREADS", "1")

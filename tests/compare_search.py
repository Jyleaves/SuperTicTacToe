"""Compare DLLs on identical legal positions; run after builds, without other load.

python tests/compare_search.py old.dll new.dll --rounds 7 --iters 128000
Reports search time and end-to-end ctypes time (including pool allocation/free).
"""
import argparse
import ctypes
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.bench_positions import positions


def load(path):
    lib = ctypes.CDLL(str(Path(path).resolve()))
    ptr = ctypes.POINTER(ctypes.c_int8)
    lib.sttt_search_raw.argtypes = [ptr, ptr, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int64, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_double]
    lib.sttt_search_raw.restype = ctypes.c_char_p
    return lib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dll', nargs='+')
    parser.add_argument('--rounds', type=int, default=7)
    parser.add_argument('--iters', type=int, default=128000)
    parser.add_argument('--threads', type=int, default=1)
    parser.add_argument('--cpu', type=int, help='pin this benchmark process to one logical CPU')
    args = parser.parse_args()
    if args.cpu is not None:
        if os.name == 'nt':
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            if not kernel.SetProcessAffinityMask(ctypes.c_void_p(-1), 1 << args.cpu):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.sched_setaffinity(0, {args.cpu})
    libs = [load(path) for path in args.dll]
    for name, game in positions():
        cells = (ctypes.c_int8 * 81)(*(c for row in game.cells for c in row))
        grids = (ctypes.c_int8 * 9)(*game.grids)
        samples = [[] for _ in libs]
        for round_id in range(args.rounds + 1):
            # Alternate library order to reduce warmup and thermal-order bias.
            order = range(len(libs)) if round_id % 2 else reversed(range(len(libs)))
            for i in order:
                started = time.perf_counter()
                result = json.loads(libs[i].sttt_search_raw(
                    cells, grids, -1 if game.forced is None else game.forced,
                    game.turn, args.iters, args.threads, 1, 0.0))
                elapsed = (time.perf_counter() - started) * 1000
                assert tuple(result['move']) in game.legal_moves()
                assert result['iters'] == args.iters
                if round_id:
                    samples[i].append((result['elapsed_ms'], elapsed))
        for i, rows in enumerate(samples):
            print(json.dumps(dict(library=Path(args.dll[i]).name, position=name,
                                  threads=args.threads, iters=args.iters,
                                  rounds=args.rounds,
                                  search_ms=round(statistics.median(r[0] for r in rows), 3),
                                  wall_ms=round(statistics.median(r[1] for r in rows), 3))))


if __name__ == '__main__':
    main()

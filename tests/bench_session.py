"""Measure state access during master-level AI search in a fresh DLL process.

python tests/bench_session.py path/to/sttt.dll [--stats]
Working set is for the Python/DLL process, excluding the WebView renderer.
"""
import argparse
import ctypes
import json
import statistics
import threading
import time
from pathlib import Path


def working_set_mib():
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in
            ('peak', 'working', 'pool_peak', 'pool', 'nonpaged_peak', 'nonpaged', 'page', 'page_peak')]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.working / 2**20


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dll')
    parser.add_argument('--stats', action='store_true')
    args = parser.parse_args()
    lib = ctypes.CDLL(str(Path(args.dll).resolve()))
    lib.sttt_new_game.argtypes = [ctypes.c_char_p] * 6
    for name in ('sttt_new_game', 'sttt_ai_move', 'sttt_stats', 'sttt_resign'):
        getattr(lib, name).restype = ctypes.c_char_p
    new_ms, state_ms, ai_ms = [], [], []
    for _ in range(3):
        started = time.perf_counter()
        lib.sttt_new_game(b'0', b'3', b'1', b'1', b'false', str(args.stats).encode())
        new_ms.append((time.perf_counter() - started) * 1000)
        def search():
            started = time.perf_counter()
            result = json.loads(lib.sttt_ai_move())
            assert result['turn'] == 2
            ai_ms.append((time.perf_counter() - started) * 1000)
        worker = threading.Thread(target=search)
        worker.start()
        time.sleep(0.04)
        assert worker.is_alive(), 'AI finished before the responsiveness probe'
        started = time.perf_counter()
        lib.sttt_stats()
        state_ms.append((time.perf_counter() - started) * 1000)
        worker.join(timeout=20)
        assert not worker.is_alive(), 'AI did not complete'
    assert len(ai_ms) == 3
    if args.stats:
        deadline = time.perf_counter() + 5
        while json.loads(lib.sttt_stats())['busy'] and time.perf_counter() < deadline:
            time.sleep(0.02)
    print(json.dumps(dict(library=Path(args.dll).name, stats=args.stats,
                         new_game_ms=round(statistics.median(new_ms), 3),
                         stats_during_ai_ms=round(statistics.median(state_ms), 3),
                         master_ai_ms=round(statistics.median(ai_ms), 3),
                         working_set_mib=round(working_set_mib(), 1))))
    lib.sttt_resign()


if __name__ == '__main__':
    main()

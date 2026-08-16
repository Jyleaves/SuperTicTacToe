"""DLL 内基准（消融实验统一标尺，避免 example 二进制的代码生成怪癖）。
运行：python tests/bench_dll.py [dll路径]
输出：开局/中局/残局 × 1/8 线程的中位迭代速率 + 几何均值。
"""
import ctypes
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    dll = sys.argv[1] if len(sys.argv) > 1 else os.path.join("super_ttt", "sttt.dll")
    lib = ctypes.CDLL(os.path.abspath(dll))
    lib.sttt_bench.restype = ctypes.c_char_p
    rows = json.loads(lib.sttt_bench().decode("utf-8"))
    print(f"{'workload':<9} {'thr':>3}   {'iters/s':>10}")
    logs = []
    for r in rows:
        print(f"{r['workload']:<9} {r['threads']:>3}   {r['iters_per_s']:>10.0f}")
        logs.append(math.log(r["iters_per_s"]))
    print(f"\ngeomean: {math.exp(sum(logs)/len(logs)):.0f}/s")


if __name__ == "__main__":
    main()

"""性能基准：Rust vs Python(numba) MCTS 迭代吞吐（迭代/秒）。
固定迭代数模式计时（消除调度噪声），单线程与 8 线程两种，
开局/中局两个局面（rollout 长度不同）。
运行：python tests/bench_rust.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from super_ttt import engine, mcts  # noqa: E402
from super_ttt.server import rust_search  # noqa: E402
from tests.bench_positions import positions  # noqa: E402


def midgame():
    return dict(positions())["midgame"]


def bench_python(g, iters, threads):
    t0 = time.perf_counter()
    mv, tree = mcts.search(g.cells, g.grids, g.forced, g.turn, 0.0, 1,
                           iters=iters, threads=threads)
    dt = time.perf_counter() - t0
    return iters / dt


def bench_rust(g, iters, threads):
    t0 = time.perf_counter()
    r = rust_search(g.cells, g.grids, g.forced, g.turn, iters, threads, 1, 0.0)
    return r["iters"] / (time.perf_counter() - t0)


def main():
    mcts.warmup()
    print(f"{'局面':<4} {'线程':<3} {'迭代数':>8} {'Python':>10} {'Rust':>10} {'加速比':>7}")
    total_ratio = []
    for name, g in positions():
        for threads, iters in ((1, 30_000), (8, 240_000)):
            py = bench_python(g, iters, threads)
            rs = bench_rust(g, iters, threads)
            ratio = rs / py
            total_ratio.append(ratio)
            print(f"{name:<4} {threads:<3} {iters:>8} {py:>9.0f}/s {rs:>9.0f}/s {ratio:>6.1f}x")
    print(f"\n平均加速比: {sum(total_ratio)/len(total_ratio):.1f}x")


if __name__ == "__main__":
    main()

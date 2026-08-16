"""迁移验证对弈：Rust 引擎 vs 现有 Python(numba) 引擎。

两种模式（判定标准）：
  iters  等迭代数、单线程 —— 验证算法移植无回退：同算法同预算，
         预期胜负比 ~50:50（显著低于 40% 才视为能力下降）；
  time   等思考时间、8 线程（生产配置）—— 验证速度优势兑现为棋力：
         预期 Rust 显著占优（同时间多跑数倍迭代）。

运行：python tests/duel_rust.py [iters|time|both]
"""
import multiprocessing
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from super_ttt import engine, mcts  # noqa: E402
from super_ttt.engine import CIRCLE, CROSS  # noqa: E402
from super_ttt.server import rust_search  # noqa: E402

MAX_ITERS_TIME_MODE = 2_000_000   # 时间模式上限（与 mcts.MAX_ITERATIONS 一致）


def rust_pick(g, mode_val, threads):
    r = rust_search(g.cells, g.grids, g.forced, g.turn,
                    iters=mode_val if mode_val > 0 else MAX_ITERS_TIME_MODE,
                    threads=threads, goal=1,
                    budget=0.0 if mode_val > 0 else mode_val * -1.0)
    mv = r["move"]
    return (mv[0], mv[1]) if mv else None


def py_pick(g, mode_val, threads, tree):
    budget = 0.0 if mode_val > 0 else -mode_val
    mv, tree = mcts.search(g.cells, g.grids, g.forced, g.turn, budget, 1,
                           root=tree,
                           iters=mode_val if mode_val > 0 else MAX_ITERS_TIME_MODE,
                           threads=threads)
    return (mv[0], mv[1]) if mv else None, tree


def play(args):
    """mode: 'iters'/'time'；val: 迭代数或秒；rust_first: Rust 执圈与否。"""
    mode, val, seed, rust_first, threads = args
    rnd = random.Random(seed)
    try:
        import numpy as np
        np.random.seed(seed + 500)
    except ImportError:
        pass
    g = engine.Game()
    py_tree = None
    steps = 0
    while not g.is_over():
        legal = g.legal_moves()
        if not legal:
            break
        is_rust = (steps % 2 == 0) == rust_first
        if is_rust:
            mv = rust_pick(g, val if mode == 'iters' else -val, threads)
        else:
            mv, py_tree = py_pick(g, val if mode == 'iters' else -val, threads,
                                  py_tree)
        if mv is None or tuple(mv) not in [tuple(m) for m in legal]:
            return ('illegal', steps, 'rust' if is_rust else 'py')
        g.apply_move(*mv)
        steps += 1
        if steps > 200:
            break
    if g.winner in (0, 3):
        return ('tie', steps, None)
    rust_won = (g.winner == CIRCLE and rust_first) or (g.winner == CROSS and not rust_first)
    return ('rust' if rust_won else 'py', steps, None)


def _worker_init():
    """worker 进程预热：numba 缓存加载耗时若发生在首个时间模式搜索内，
    会吃光 0.25s 预算导致 0 迭代（Python 侧已知脆弱点，生产由预热线程规避）。
    对齐生产条件：进对局前先编译/加载完。"""
    try:
        from super_ttt import mcts
        mcts.warmup()
    except Exception:
        pass


def run(label, tasks, max_procs=12):
    # time 模式每进程内部还有 8×2 引擎线程：进程数压到 5，避免过度超订
    ncpu = min(max_procs, max(1, (os.cpu_count() or 8) - 2))
    with multiprocessing.Pool(ncpu, initializer=_worker_init) as pool:
        results = pool.map(play, tasks)
    r = sum(1 for x, _, _ in results if x == 'rust')
    p = sum(1 for x, _, _ in results if x == 'py')
    t = len(results) - r - p
    ill = [x for x, _, w in results if x == 'illegal']
    games = len(results)
    print(f"[{label} x{games}] Rust {r} : Python {p} : 平 {t}"
          + (f"  ⚠ 非法步 {ill}" if ill else ""), flush=True)
    return r, p, t


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else 'both'
    mcts.warmup()                      # numba 预编译（公平起见）
    t0 = time.monotonic()

    if which in ('iters', 'both'):
        print("=== 模式一：等迭代数（单线程）——算法等价性/棋力不降 ===")
        for iters in (8_000, 32_000):
            games = 60
            tasks = [('iters', iters, 70000 + i, i % 2 == 0, 1) for i in range(games)]
            run(f"迭代 {iters} vs {iters}", tasks)

    if which in ('time', 'both'):
        print("=== 模式二：等思考时间（8 线程，生产配置）——速度优势 ===")
        # 注意进程数=2：time 模式下每进程峰值 16 活跃线程（py 8 + rust 8），
        # 再多会超订 20 核导致 Python 调用线程调度饥饿（0 迭代假 illegal，
        # 已实测：1 进程/单线程/1s 预算均不触发，纯并发装置效应）
        for secs in (0.25, 1.0):
            games = 20
            tasks = [('time', secs, 80000 + i, i % 2 == 0, 8) for i in range(games)]
            run(f"{secs}s vs {secs}s", tasks, max_procs=2)

    print(f"总耗时 {time.monotonic()-t0:.0f}s")


if __name__ == "__main__":
    main()

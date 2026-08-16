"""线程数-棋力曲线（单进程直测，无 monkeypatch/无进程池——
Windows spawn 下主进程补丁传不进 worker，此前 tsweep/t12 两脚本数据作废）。
Rust T 线程（等迭代数，无树复用）vs Python 1 线程（带树复用）。
运行：python tests/duel_rust_threads.py [games_per_config]
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from super_ttt import engine, mcts  # noqa: E402
from super_ttt.server import rust_search  # noqa: E402


def play_game(seed, iters, rust_threads):
    random.Random(seed)
    try:
        import numpy as np
        np.random.seed(seed + 500)
    except ImportError:
        pass
    g = engine.Game()
    py_tree = None
    steps = 0
    while not g.is_over():
        legal = [tuple(m) for m in g.legal_moves()]
        if not legal:
            break
        if (steps % 2 == 0) == (seed % 2 == 0):
            r = rust_search(g.cells, g.grids, g.forced, g.turn,
                            iters, rust_threads, 1, 0.0)
            mv = tuple(r["move"]) if r.get("move") else None
            if mv is None or mv not in legal:
                return "illegal"
        else:
            m2, py_tree = mcts.search(g.cells, g.grids, g.forced, g.turn, 0.0, 1,
                                      root=py_tree, iters=iters, threads=1)
            if m2 is None or tuple(m2) not in legal:
                return "illegal-py"
            mv = tuple(m2)
        g.apply_move(*mv)
        steps += 1
        if steps > 200:
            break
    if g.winner in (0, 3):
        return "tie"
    return "rust" if (g.winner == 1) == (seed % 2 == 0) else "py"


def main():
    games = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    mcts.warmup()
    print("等迭代数曲线：Rust T线程 vs Python 1线程（Rust 侧无树复用，Python 侧有）")
    for iters in (8_000, 32_000):
        for t in (1, 2, 4, 8, 12, 16):
            w = l = tie = 0
            base = 96000 + t * 100
            for s in range(base, base + games):
                r = play_game(s, iters, t)
                if r == "rust":
                    w += 1
                elif r == "py":
                    l += 1
                else:
                    tie += 1
            print(f"  {iters}迭代 Rust{t:>2}线程: Rust {w} : Python {l} : 平 {tie}",
                  flush=True)


if __name__ == "__main__":
    main()

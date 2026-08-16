"""棋力对比框架（三种模式，16 进程并行）。运行：python tests/duel_iter.py
模式：

1. 固定迭代：数组版 vs Python 树版各 N 次迭代（比信息利用效率）
2. 固定时间：各 T 秒（比速度转化）
3. 迭代比例：py 固定 N，数组版 k*N（找等价迭代比）

运行：python tests/duel_iter.py
"""

import multiprocessing
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from super_ttt import ai, engine, mcts  # noqa: E402
from super_ttt.engine import CIRCLE, CROSS  # noqa: E402


def py_search_fixed(g, iters, root=None):
    """Python 树版固定迭代数搜索。"""
    cells_t = tuple(c for row in g.cells for c in row)
    grids_t = tuple(g.grids)
    if root is None or root.turn != g.turn or root.cells != cells_t \
            or root.grids != grids_t or root.forced != g.forced:
        root = ai.Node(cells_t, grids_t, g.forced, g.turn)
    for _ in range(iters):
        leaf = ai._tree_policy(root)
        w = ai._big_winner(leaf.grids)
        if w:
            reward = 0 if w == 3 else 1
        else:
            reward = ai._rollout(leaf, 1)
        ai._backup(leaf, reward)
    if not root.children:
        return None, root
    wins = [ch for ch in root.children if ai._big_winner(ch.grids) == g.turn]
    if wins:
        best = max(wins, key=lambda c: c.visits)
        return best.move, best
    best = ai._best_child(root)
    return best.move, best


def mcts_search_fixed(g, iters, tree=None):
    return mcts.search(g.cells, g.grids, g.forced, g.turn, 0.0, 1,
                       root=tree, iters=iters)


def play(args):
    """mode: 'iters'/'time'/'ratio'；val: 迭代数或秒数；n2: py 固定迭代（ratio 用）。"""
    mode, val, n2, seed, i_first = args
    rnd = random.Random(seed)
    np.random.seed(seed + 500)
    g = engine.Game()
    tree_n = None
    root_p = None
    steps = 0
    while not g.is_over():
        legal = g.legal_moves()
        if not legal:
            break
        if (steps % 2 == 0) == i_first:
            if mode == 'time':
                mv, tree_n = mcts.search(g.cells, g.grids, g.forced, g.turn,
                                         val, 1, root=tree_n)
            else:
                mv, tree_n = mcts_search_fixed(g, val, tree_n)
        else:
            if mode == 'time':
                mv, root_p = ai.search(g.cells, g.grids, g.forced, g.turn,
                                       val, 1, root=root_p)
            else:
                mv, root_p = py_search_fixed(g, n2 if mode == 'ratio' else val,
                                             root_p)
        if mv is None or mv not in legal:
            return ('illegal', steps)
        g.apply_move(*mv)
        steps += 1
        if steps > 200:
            break
    if g.winner in (0, 3):
        return ('tie', steps)
    n_won = (g.winner == CIRCLE and i_first) or (g.winner == CROSS and not i_first)
    return ('n' if n_won else 'p', steps)


def run(mode, vals, games=16, py_fixed=None, procs=16):
    from super_ttt import mcts as _m
    _m.warmup()
    for val in vals:
        tasks = [(mode, val, py_fixed, 40000 + i, i % 2 == 0)
                 for i in range(games)]
        with multiprocessing.Pool(procs) as pool:
            results = pool.map(play, tasks)
        n = sum(1 for r, _ in results if r == 'n')
        p = sum(1 for r, _ in results if r == 'p')
        t = games - n - p
        label = (f"迭代 {val}" if mode == 'iters'
                 else f"{val}s" if mode == 'time'
                 else f"数组 {val}(x{val / py_fixed:.1f}) vs py {py_fixed}")
        print(f"[{label} x{games}] 数组版 {n} : py版 {p} : 平{t}")


def main():
    t0 = time.monotonic()
    print("=== 固定迭代 ===")
    run('iters', [1000, 1600, 3200, 6400], games=24)
    print("=== 固定时间 ===")
    run('time', [0.1, 0.2, 0.5, 1.0], games=16)
    print("=== 迭代比例（py 固定 1600）===")
    run('ratio', [800, 1200, 1600, 2400, 3200, 4800], games=24, py_fixed=1600)
    print(f"总耗时 {time.monotonic()-t0:.0f}s")


if __name__ == "__main__":
    main()

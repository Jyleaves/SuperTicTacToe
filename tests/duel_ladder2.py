"""梯度对弈 play 函数（被 tests/duel_gradient.py 复用，勿删）。
play(args)：lo/hi 迭代数同引擎对弈一局，带两步树复用；
args 可含第 5 项 cap（节点池容量，高迭代防重建干扰）。"""
import multiprocessing
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from super_ttt import engine, mcts  # noqa: E402
from super_ttt.engine import CIRCLE, CROSS  # noqa: E402


def play(args):
    """同引擎不同迭代数对弈：lo 迭代方 vs hi 迭代方。cap: 节点池容量（高迭代用大池防重建）。"""
    if len(args) > 4:
        lo, hi, seed, lo_first, cap = args
    else:
        lo, hi, seed, lo_first = args
        cap = None
    rnd = random.Random(seed)
    np.random.seed(seed + 500)
    g = engine.Game()
    tree_lo = mcts.MCTSTree(cap=cap) if cap else None
    tree_hi = mcts.MCTSTree(cap=cap) if cap else None
    last_lo = None
    last_hi = None
    steps = 0
    while not g.is_over():
        legal = g.legal_moves()
        if not legal:
            break
        is_lo = (steps % 2 == 0) == lo_first
        n = lo if is_lo else hi
        # 两步树复用（与 server 一致）
        tree = None
        if is_lo:
            if tree_lo is not None and last_lo is not None:
                tree = mcts.find_child(tree_lo, last_lo)
            if tree is not None and g.last_move is not None:
                tree = mcts.find_child(tree, g.last_move)
        else:
            if tree_hi is not None and last_hi is not None:
                tree = mcts.find_child(tree_hi, last_hi)
            if tree is not None and g.last_move is not None:
                tree = mcts.find_child(tree, g.last_move)
        mv, t = mcts.search(g.cells, g.grids, g.forced, g.turn, 0.0, 1,
                            root=tree, iters=n)
        if is_lo:
            tree_lo = t
            last_lo = mv
        else:
            tree_hi = t
            last_hi = mv
        if mv is None or mv not in legal:
            return ('illegal', steps)
        g.apply_move(*mv)
        steps += 1
        if steps > 200:
            break
    if g.winner in (0, 3):
        return ('tie', steps)
    lo_won = (g.winner == CIRCLE and lo_first) or (g.winner == CROSS and not lo_first)
    return ('lo' if lo_won else 'hi', steps)


def main():
    from super_ttt import mcts as _m
    _m.warmup()
    t0 = time.monotonic()
    levels = [800, 1600, 3200, 6400, 12800, 25600]
    games = 24
    for lo, hi in zip(levels, levels[1:]):
        tasks = [(lo, hi, 60000 + i, i % 2 == 0) for i in range(games)]
        with multiprocessing.Pool(16) as pool:
            results = pool.map(play, tasks)
        lo_w = sum(1 for r, _ in results if r == 'lo')
        hi_w = sum(1 for r, _ in results if r == 'hi')
        ties = games - lo_w - hi_w
        print(f"[{lo} vs {hi} x{games}] 低迭代 {lo_w} : 高迭代 {hi_w} : 平 {ties}")
    print(f"总耗时 {time.monotonic()-t0:.0f}s")


if __name__ == "__main__":
    main()

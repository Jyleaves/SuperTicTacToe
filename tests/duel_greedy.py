"""A/B 基线：当前内核（greedy-1 rollout）vs 随机 rollout（_rollout_bb），
同 1600 迭代 × 32 局（唯一变量 = 启发）。未来改动 rollout 时用它回归棋力。
运行：python tests/duel_greedy.py
历史基线：greedy 52 : 随机 25 : 平 19（96 局）——显著占优。"""
import multiprocessing
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from numba import njit

from super_ttt import ai, engine, mcts  # noqa: E402
from super_ttt.engine import CIRCLE, CROSS  # noqa: E402


@njit(cache=True)
def batch_rand(P, root, goal, batch, free_arr, cap):
    iters = 0
    while iters < batch:
        node = mcts._tree_policy(P, root, free_arr, cap)
        w = mcts._big_winner(P[1][node])
        if w:
            reward = 0 if w == 3 else goal
        else:
            cells = P[0][node].copy()
            grids = P[1][node].copy()
            reward = mcts._rollout_bb(cells, grids, P[2][node], P[3][node],
                                      P[3][node], goal)
        mcts._backup(P, node, root, reward)
        iters += 1
    return iters


@njit(cache=True)
def batch_greedy(P, root, goal, batch, free_arr, cap):
    iters = 0
    while iters < batch:
        node = mcts._tree_policy(P, root, free_arr, cap)
        w = mcts._big_winner(P[1][node])
        if w:
            reward = 0 if w == 3 else goal
        else:
            cells = P[0][node].copy()
            grids = P[1][node].copy()
            reward = mcts._rollout_bb_g(cells, grids, P[2][node], P[3][node],
                                        P[3][node], goal)
        mcts._backup(P, node, root, reward)
        iters += 1
    return iters


def _search(g, iters, tree, use_greedy):
    batch = batch_greedy if use_greedy else batch_rand
    if tree is None:
        tree = mcts.MCTSTree()
    cells_arr = np.asarray([c for r in g.cells for c in r], dtype=np.int8)
    grids_arr = np.asarray(g.grids, dtype=np.int8)
    forced_i = -1 if g.forced is None else g.forced
    reuse = (tree.root >= 0 and tree.turn[tree.root] == g.turn
             and tree.forced[tree.root] == forced_i
             and np.array_equal(tree.cells[tree.root], cells_arr)
             and np.array_equal(tree.grids[tree.root], grids_arr))
    if not reuse:
        tree.reset()
        tree.root = mcts._new_root(tree.P, cells_arr, grids_arr, forced_i,
                                   int(g.turn), tree.free)
    total = 0
    while total < iters:
        b = min(512, iters - total)
        total += batch(tree.P, tree.root, 1, b, tree.free, tree.cap)
    wch = mcts._winning_child(tree.P, tree.root, int(g.turn))
    if wch >= 0:
        return (int(tree.move[wch, 0]), int(tree.move[wch, 1])), tree
    bch = mcts._best_child(tree.P, tree.root)
    if bch < 0:
        return None, tree
    return (int(tree.move[bch, 0]), int(tree.move[bch, 1])), tree


def play(args):
    seed, i_first = args
    random.Random(seed)
    np.random.seed(seed + 500)
    g = engine.Game()
    tree_g = None
    tree_r = None
    last_g = None
    last_r = None
    steps = 0
    while not g.is_over():
        legal = g.legal_moves()
        if not legal:
            break
        is_g = (steps % 2 == 0) == i_first
        tree = None
        if is_g:
            if tree_g is not None and last_g is not None:
                tree = mcts.find_child(tree_g, last_g)
            if tree is not None and g.last_move is not None:
                tree = mcts.find_child(tree, g.last_move)
            mv, tree_g = _search(g, 1600, tree, True)
            last_g = mv
        else:
            if tree_r is not None and last_r is not None:
                tree = mcts.find_child(tree_r, last_r)
            if tree is not None and g.last_move is not None:
                tree = mcts.find_child(tree, g.last_move)
            mv, tree_r = _search(g, 1600, tree, False)
            last_r = mv
        if mv is None or mv not in legal:
            return ('illegal', steps)
        g.apply_move(*mv)
        steps += 1
        if steps > 200:
            break
    if g.winner in (0, 3):
        return ('tie', steps)
    g_won = (g.winner == CIRCLE and i_first) or (g.winner == CROSS and not i_first)
    return ('g' if g_won else 'r', steps)


def main():
    mcts.warmup()
    t0 = time.monotonic()
    games = 32
    tasks = [(195000 + i, i % 2 == 0) for i in range(games)]
    with multiprocessing.Pool(16) as pool:
        results = pool.map(play, tasks)
    g = sum(1 for r, _ in results if r == 'g')
    r = sum(1 for r, _ in results if r == 'r')
    ties = games - g - r
    print(f"[1600x{games}] greedy {g} : 随机 {r} : 平 {ties}")
    print(f"总耗时 {time.monotonic()-t0:.0f}s")


if __name__ == "__main__":
    main()

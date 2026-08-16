"""快速棋力回归（3 分钟内）：数组化 MCTS vs 纯 Python 树版。
运行：python tests/regress_numba.py
0.2s 预算（消融实验证明预算不改变相对结论）。"""
import multiprocessing
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from super_ttt import ai, engine  # noqa: E402
from super_ttt.engine import CIRCLE, CROSS  # noqa: E402


def selfplay_one(args):
    """自对弈一局（子进程）。mode: 'numba' 用当前 rollout / 'py' 用纯 Python rollout。"""
    seed, budget, mode = args
    rnd = random.Random(seed)
    try:
        import numpy as np
        np.random.seed(seed + 500)
    except ImportError:
        pass
    g = engine.Game()
    root = {CIRCLE: None, CROSS: None}
    while not g.is_over():
        moves = g.legal_moves()
        if not moves:
            break
        turn = g.turn
        r = None
        if root[turn] is not None and g.last_move is not None:
            r = ai.find_child(root[turn], g.last_move)
        if mode == 'py':
            saved = ai._rollout
            ai._rollout = ai._rollout_py
            try:
                move, node = ai.search(g.cells, g.grids, g.forced, turn, budget, 1, root=r)
            finally:
                ai._rollout = saved
        else:
            move, node = ai.search(g.cells, g.grids, g.forced, turn, budget, 1, root=r)
        if move is None:
            break
        root[turn] = node
        g.apply_move(*move)
    if g.winner == 3 or g.winner == 0:
        return 'tie'
    if (g.winner == CIRCLE and seed % 2 == 0) or (g.winner == CROSS and seed % 2 == 1):
        return 'first'
    return 'second'


def selfplay_batch(mode, games, budget=0.2, procs=16):
    tasks = [(100 + i, budget, mode) for i in range(games)]
    with multiprocessing.Pool(procs) as pool:
        results = pool.map(selfplay_one, tasks)
    f = results.count('first')
    s = results.count('second')
    t = results.count('tie')
    print(f"[自对弈 {mode}] {budget}s x {games} 局: 先手胜 {f} / 后手胜 {s} / 平 {t}")


def main():
    t0 = time.monotonic()
    from tests.duel_original import load_original, patch_original_fair, run_duel_parallel
    orig = load_original()
    fair = patch_original_fair(orig)

    run_duel_parallel(0.2, 12, "快速回归 vs 原版(修复强制格)", patched=True, procs=16)
    # 对照：同一预算下纯 Python rollout 的自对弈（验证 numba 无棋力影响）
    selfplay_batch('numba', 12)
    selfplay_batch('py', 12)
    print(f"总耗时 {time.monotonic() - t0:.0f}s")


if __name__ == "__main__":
    main()

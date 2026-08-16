"""AI 优化等价性验证。

验证 2026-08-07 的性能优化没有改变搜索行为：
1. 增量线检查（_sub_winner_inc / _big_winner_inc）与全量检查（_sub_winner / _big_winner）
   在"落子前无三连"的合法前提下逐格一致；
2. 优化版 rollout 与参考版（全量逻辑）在同一局面上的收益分布统计等价；
3. moves_left 机制：反复 tree_policy 后 children 恰好覆盖全部合法步。
"""

import random
import sys
import unittest

sys.path.insert(0, '.')

from super_ttt import ai, engine
from super_ttt.engine import CIRCLE, CROSS, EMPTY, GRID_OPEN

random.seed(20260807)


def random_position(steps=25):
    """随机合法对局 steps 步后的进行中局面（非终局）。"""
    g = engine.Game()
    for _ in range(steps):
        moves = g.legal_moves()
        if not moves or g.is_over():
            break
        g.apply_move(*random.choice(moves))
    return g


def reference_rollout(node, goal):
    """参考实现：原版全量逻辑的 rollout（性能优化前的行为）。"""
    cells = list(node.cells)
    grids = list(node.grids)
    forced = node.forced
    turn = node.turn
    mover = turn
    while True:
        moves = []
        if forced is not None and grids[forced] == GRID_OPEN:
            base = forced * 9
            moves = [(forced, c) for c in range(9) if cells[base + c] == EMPTY]
        else:
            for s in range(9):
                if grids[s] == GRID_OPEN:
                    base = s * 9
                    for c in range(9):
                        if cells[base + c] == EMPTY:
                            moves.append((s, c))
        if not moves:
            return 0
        sub, cell = random.choice(moves)
        cells[sub * 9 + cell] = turn
        w = ai._sub_winner(cells, sub)
        if w:
            grids[sub] = w
        elif all(cells[base] for base in ai._SUB_IDX[sub]):
            grids[sub] = 3
        winner = ai._big_winner(grids)
        if winner:
            break
        forced = cell if grids[cell] == GRID_OPEN else None
        turn = CROSS if turn == CIRCLE else CIRCLE
    if winner == 3:
        return 0
    if winner == mover:
        return -goal
    return goal


def live_positions(n, steps):
    """生成 n 个进行中局面。"""
    out = []
    while len(out) < n:
        g = random_position(steps)
        if not g.is_over():
            out.append(g)
    return out


class TestIncrementalChecks(unittest.TestCase):
    def test_sub_winner_inc_matches_full(self):
        for g in live_positions(20, 20):
            flat = tuple(c for row in g.cells for c in row)
            for sub in range(9):
                if ai._sub_winner(flat, sub):
                    continue            # 该小格已三连，跳过
                base = sub * 9
                for cell in range(9):
                    if flat[base + cell] != EMPTY:
                        continue
                    for turn in (CIRCLE, CROSS):
                        cells = list(flat)
                        cells[base + cell] = turn
                        full = ai._sub_winner(tuple(cells), sub)
                        inc = ai._sub_winner_inc(tuple(cells), sub, cell, turn)
                        self.assertEqual(full, inc,
                                         f"sub_winner mismatch sub={sub} cell={cell}")

    def test_big_winner_inc_matches_full(self):
        for g in live_positions(20, 20):
            grids = list(g.grids)
            if ai._big_winner(tuple(grids)):
                continue                # 大棋盘已三连，跳过
            for sub in range(9):
                for turn in (CIRCLE, CROSS):
                    gs = list(grids)
                    gs[sub] = turn
                    full = ai._big_winner(tuple(gs))
                    inc = ai._big_winner_inc(tuple(gs), sub, turn)
                    self.assertEqual(full, inc,
                                     f"big_winner mismatch sub={sub}")


class TestRolloutEquivalence(unittest.TestCase):
    def test_reward_distribution_close(self):
        """同一局面下，优化版与参考版 rollout 的收益分布应统计接近。"""
        for steps in (10, 20, 35, 45):
            g = random_position(steps)
            if g.is_over():
                continue
            cells = tuple(c for row in g.cells for c in row)
            node = ai.Node(cells, tuple(g.grids), g.forced, g.turn)
            n = 800
            dist_new = [0, 0, 0]
            dist_ref = [0, 0, 0]
            # 两边各自独立随机化：避免固定 seed 的确定性序列 vs 随机序列的伪偏差
            random.seed(1000 + steps)
            try:
                import numpy as np
                np.random.seed(2000 + steps)
            except ImportError:
                pass
            for _ in range(n):
                dist_new[ai._rollout(node, 1) + 1] += 1
            random.seed(3000 + steps)
            for _ in range(n):
                dist_ref[reference_rollout(node, 1) + 1] += 1
            # 每类相差 <= 8%（800 次采样下 ~2.5σ，三类联合误报率 <1%）
            for i in range(3):
                self.assertLessEqual(abs(dist_new[i] - dist_ref[i]), n * 0.08,
                                     f"分布偏差过大: new={dist_new} ref={dist_ref}")


class TestMovesLeft(unittest.TestCase):
    def test_children_cover_all_legal_moves(self):
        for g in live_positions(20, 15):
            cells = tuple(c for row in g.cells for c in row)
            grids = tuple(g.grids)
            root = ai.Node(cells, grids, g.forced, g.turn)
            expect = len(ai._legal(cells, grids, g.forced))
            while root.moves_left:          # 每次 tree_policy 展开 root 的一个步
                ai._tree_policy(root)
            self.assertEqual(len(root.children), expect,
                             f"children {len(root.children)} != legal {expect}")
            self.assertEqual({ch.move for ch in root.children},
                             set(ai._legal(cells, grids, g.forced)),
                             "children 与合法步集合不一致")


if __name__ == '__main__':
    unittest.main(verbosity=2)

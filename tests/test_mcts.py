"""数组化 MCTS（mcts.py）单测：展开正确性、搜索合法性、树复用、必胜手、
求败模式、容量管理、rollout 分布等价（与参考实现统计对比）。

运行：python -m unittest tests.test_mcts -v
"""

import random
import sys
import unittest

import numpy as np

sys.path.insert(0, ".")
from super_ttt import ai, engine, mcts  # noqa: E402
from super_ttt.engine import CIRCLE, CROSS  # noqa: E402
from tests.test_ai_perf_equiv import random_position, reference_rollout  # noqa: E402

# 8% 容差需要 >= 800 采样（经验值，见 DEBUG_LOG D9）
DIST_SAMPLES = 800
DIST_TOL = 0.08


def make_tree(game):
    t = mcts.MCTSTree()
    cells = np.asarray([c for r in game.cells for c in r], dtype=np.int8)
    grids = np.asarray(game.grids, dtype=np.int8)
    forced = -1 if game.forced is None else game.forced
    t.root = mcts._new_root(t.P, cells, grids, forced, int(game.turn), t.free)
    return t


class TestExpandCorrectness(unittest.TestCase):
    """展开的子节点局面必须与引擎模拟完全一致。"""

    def test_expand_matches_engine(self):
        g = engine.Game()
        g.apply_move(0, 0)
        g.apply_move(0, 1)
        t = make_tree(g)
        n_legal = int(t.legal_count[t.root])
        for k in range(n_legal):
            if t.free[0] < t.cap:
                t.free[0] += 1
                ch = mcts._expand(t.P, t.root, t.free[0] - 1)
                sub, cell = int(t.move[ch, 0]), int(t.move[ch, 1])
                g2 = engine.Game()
                g2.apply_move(0, 0)
                g2.apply_move(0, 1)
                g2.apply_move(sub, cell)
                self.assertEqual(
                    t.cells[ch].tolist(),
                    [c for r in g2.cells for c in r],
                    f"展开第{k}步局面不一致")
                self.assertEqual(t.grids[ch].tolist(), g2.grids)
                self.assertEqual(int(t.turn[ch]), g2.turn)
                self.assertEqual(int(t.forced[ch]),
                                 -1 if g2.forced is None else g2.forced)
        # k 越界（超过合法步数）应返回 -1 且不写坏数据
        cells = t.cells[t.root].copy()
        self.assertEqual(mcts._kth_legal(cells, t.grids[t.root],
                                         t.forced[t.root], n_legal), -1)

    def test_kth_legal(self):
        g = engine.Game()
        g.apply_move(0, 0)
        g.apply_move(0, 1)          # forced -> 1
        cells = np.asarray([c for r in g.cells for c in r], dtype=np.int8)
        grids = np.asarray(g.grids, dtype=np.int8)
        legal = g.legal_moves()
        forced = -1 if g.forced is None else g.forced
        for k, (sub, cell) in enumerate(legal):
            pick = mcts._kth_legal(cells, grids, forced, k)
            self.assertEqual((pick // 9, pick % 9), (sub, cell))
        self.assertEqual(mcts._kth_legal(cells, grids, forced, len(legal)), -1)
        # 自由落子局面
        g2 = engine.Game()
        cells2 = np.asarray([c for r in g2.cells for c in r], dtype=np.int8)
        grids2 = np.asarray(g2.grids, dtype=np.int8)
        for k, (sub, cell) in enumerate(g2.legal_moves()):
            pick = mcts._kth_legal(cells2, grids2, -1, k)
            self.assertEqual((pick // 9, pick % 9), (sub, cell))


class TestSearchLegality(unittest.TestCase):
    """search 返回的落子必须合法；完整对局无非法步。"""

    def test_full_game_no_illegal(self):
        rnd = random.Random(42)
        for _ in range(2):
            g = engine.Game()
            tree = None
            steps = 0
            for step in range(200):
                if g.is_over():
                    break
                if tree is not None and g.last_move is not None:
                    tree = mcts.find_child(tree, g.last_move)
                move, tree = mcts.search(g.cells, g.grids, g.forced, g.turn,
                                         0.1, 1, root=tree)
                if move is None:
                    break
                self.assertIn(move, g.legal_moves(),
                              f"非法落子 {move} @ step {step}")
                g.apply_move(*move)
                steps += 1
            self.assertTrue(g.is_over() or steps > 0)

    def test_forced_move_honored(self):
        """强制格规则：落子后 forced 指向目标大格时，下一步必须在该格。"""
        # 构造：CIRCLE 落 (0,0)，使 forced=0（未决出的 sub0）
        g = engine.Game()
        g.apply_move(0, 0)          # CIRCLE 落 sub0
        self.assertEqual(g.forced, 0)
        tree = None
        move, tree = mcts.search(g.cells, g.grids, g.forced, g.turn,
                                 0.2, 1, root=tree)
        self.assertIsNotNone(move)
        self.assertEqual(move[0], 0, "AI 必须下在强制格 sub0")


class TestTreeReuse(unittest.TestCase):
    """树复用：find_child 提升 root；复用后搜索仍正常。"""

    def test_find_child_and_reuse(self):
        g = engine.Game()
        g.apply_move(0, 0)
        tree = None
        move, tree = mcts.search(g.cells, g.grids, g.forced, g.turn,
                                 0.3, 1, root=tree)
        self.assertIsNotNone(move)
        g.apply_move(*move)
        human = g.legal_moves()[0]
        g.apply_move(*human)
        # AI 不保证展开全部子节点：找不到时 search 应自动重建树
        tree2 = mcts.find_child(tree, human)
        move2, tree3 = mcts.search(g.cells, g.grids, g.forced, g.turn,
                                   0.3, 1, root=tree2)
        self.assertIsNotNone(move2)
        self.assertIn(move2, g.legal_moves())

    def test_find_child_missing_returns_none(self):
        self.assertIsNone(mcts.find_child(None, (0, 0)))
        self.assertIsNone(mcts.find_child(None, None))


class TestWinLoseMode(unittest.TestCase):
    """求胜（goal=1）与求败（goal=-1）模式都可运行且行为不同。"""

    def test_goal_win(self):
        g = engine.Game()
        g.apply_move(0, 0)
        g.apply_move(0, 1)
        move, _ = mcts.search(g.cells, g.grids, g.forced, g.turn, 0.3, 1)
        self.assertIsNotNone(move)

    def test_goal_lose(self):
        g = engine.Game()
        g.apply_move(0, 0)
        g.apply_move(0, 1)
        move, _ = mcts.search(g.cells, g.grids, g.forced, g.turn, 0.3, -1)
        self.assertIsNotNone(move)

    def test_winning_move_preferred(self):
        """存在一步制胜时，AI 必须选它（必胜手优先）。
        直接构造局面：CIRCLE 已占大格 0 和 3，forced=6（sub6 未决出），
        sub6 中 CIRCLE 已有 (6,0)(6,1)——AI 下 (6,2) 完成 sub6 三连，
        大棋盘 (0,3,6) 三连即胜。"""
        g = engine.Game()
        g.grids = [1, 0, 0, 1, 0, 0, 0, 0, 0]     # sub0、sub3 已被 CIRCLE 占
        g.cells[6][0] = 1
        g.cells[6][1] = 1
        g.turn = CIRCLE
        g.forced = 6
        move, _ = mcts.search(g.cells, g.grids, g.forced, g.turn,
                              0.3, 1)
        self.assertEqual(move, (6, 2),
                         f"必胜手应选 (6,2)，实际 {move}")
        g.apply_move(*move)
        self.assertEqual(g.winner, CIRCLE)

    def test_capacity_recycle(self):
        """容量不足时自动重建根，搜索不崩溃。"""
        t = mcts.MCTSTree(cap=2048)
        g = engine.Game()
        cells = np.asarray([c for r in g.cells for c in r], dtype=np.int8)
        grids = np.asarray(g.grids, dtype=np.int8)
        t.root = mcts._new_root(t.P, cells, grids, -1, int(g.turn), t.free)
        # 手动把 free 推到接近上限
        t.free[0] = t.cap - mcts.RECYCLE_MARGIN + 10
        move, tree = mcts.search(g.cells, g.grids, g.forced, g.turn,
                                 0.3, 1, root=t)
        self.assertIsNotNone(move)
        self.assertIn(move, g.legal_moves())


class TestRolloutDistribution(unittest.TestCase):
    """rollout 收益分布与参考实现统计等价（两边独立随机流）。"""

    def test_distribution_close(self):
        rnd = random.Random(7)
        worst = 0.0
        for steps in (15, 30, 45):
            g = random_position(steps)
            if g.is_over():
                continue
            t = make_tree(g)
            dist_new = [0, 0, 0]
            dist_ref = [0, 0, 0]
            np.random.seed(100 + steps)
            random.seed(200 + steps)
            for _ in range(DIST_SAMPLES):
                # 直接从 root 局面 rollout（与 reference 同一起点）
                cells = t.cells[t.root].copy()
                grids = t.grids[t.root].copy()
                r = ai._rollout_numba(cells, grids, t.forced[t.root],
                                      t.turn[t.root], t.turn[t.root], 1)
                dist_new[int(r) + 1] += 1
            node = ai.Node(tuple(c for r in g.cells for c in r),
                           tuple(g.grids), g.forced, g.turn)
            random.seed(300 + steps)
            for _ in range(DIST_SAMPLES):
                r = reference_rollout(node, 1)
                dist_ref[int(r) + 1] += 1
            for i in range(2):
                dev = abs(dist_new[i] - dist_ref[i]) / DIST_SAMPLES
                worst = max(worst, dev)
                self.assertLessEqual(
                    dev, DIST_TOL,
                    f"局面{steps}步 类别{i} 分布偏差 {dev:.1%}："
                    f"new={dist_new} ref={dist_ref}")


if __name__ == "__main__":
    unittest.main()


class TestSearchStats(unittest.TestCase):
    """实时胜率统计：search 后 tree.stats 为本次迭代的终局分布（圈/平/叉）。"""

    def test_stats_sum_equals_iters(self):
        from super_ttt import mcts as _m
        _m.warmup()
        g = random_position(15)
        cells = np.asarray([c for r in g.cells for c in r], dtype=np.int8)
        grids = np.asarray(g.grids, dtype=np.int8)
        forced = -1 if g.forced is None else g.forced
        mv, tree = _m.search(cells, grids, forced, int(g.turn), 0.0, 1,
                             root=None, iters=4000)
        self.assertIsNotNone(mv)
        self.assertEqual(int(tree.stats.sum()), 4000)
        self.assertTrue(all(int(x) >= 0 for x in tree.stats))

    def test_stats_cleared_between_searches(self):
        from super_ttt import mcts as _m
        g = random_position(10)
        cells = np.asarray([c for r in g.cells for c in r], dtype=np.int8)
        grids = np.asarray(g.grids, dtype=np.int8)
        forced = -1 if g.forced is None else g.forced
        mv, tree = _m.search(cells, grids, forced, int(g.turn), 0.0, 1,
                             root=None, iters=2000)
        s1 = tree.stats.copy()
        mv, tree = _m.search(cells, grids, forced, int(g.turn), 0.0, 1,
                             root=tree, iters=1000)
        self.assertEqual(int(tree.stats.sum()), 1000)   # 第二次搜索只统计本次
        self.assertFalse(np.array_equal(tree.stats, s1))

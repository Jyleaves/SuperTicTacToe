"""AI（MCTS）单元测试 + 桥接集成测试。

TestSearch 针对 Python 参照实现（ai.py，保留作回退/对弈参照）；
TestApiIntegration 走真实后端（Rust sttt.dll，2026-08-16 迁移后）——
幼稚档 2000 迭代毫秒级完成，无需打桩。

运行：python -m unittest tests.test_ai -v
"""

import random
import unittest

from super_ttt import ai, engine
from super_ttt.engine import Game, CIRCLE, CROSS
from super_ttt.server import Api


class TestSearch(unittest.TestCase):
    def _state(self, g):
        return g.cells, g.grids, g.forced, g.turn

    def test_returns_legal_move(self):
        random.seed(1)
        g = Game()
        g.apply_move(0, 0)
        g.apply_move(0, 1)
        cells, grids, forced, turn = self._state(g)
        move, node = ai.search(cells, grids, forced, turn, budget=0.1)
        self.assertIsNotNone(move)
        self.assertIn(tuple(move), [tuple(m) for m in g.legal_moves()])
        self.assertIsNotNone(node)

    def test_terminal_position_returns_none(self):
        g = Game()
        for sub, mark in [(0, CIRCLE), (4, CIRCLE), (8, CIRCLE)]:
            g.cells[sub] = [mark] * 9
            g.grids[sub] = mark
        cells, grids, forced, turn = self._state(g)
        move, node = ai.search(cells, grids, forced, turn, budget=0.05)
        self.assertIsNone(move)

    def test_tree_reuse_find_child(self):
        random.seed(2)
        g = Game()
        g.apply_move(0, 0)
        cells, grids, forced, turn = self._state(g)
        move, node = ai.search(cells, grids, forced, turn, budget=0.15)
        self.assertIsNotNone(move)
        # 人类按 AI 的落子区域落子后，应能在子树中找到对应节点
        sub, cell = move
        g.apply_move(sub, cell)
        # 人类落子（强制格内任意合法位置）
        human_move = g.legal_moves()[0]
        g.apply_move(*human_move)
        cells, grids, forced, turn = self._state(g)
        reused = ai.find_child(node, human_move)
        self.assertIsNotNone(reused)
        visits_before = reused.visits
        move2, node2 = ai.search(cells, grids, forced, turn, budget=0.1,
                                 root=reused)
        self.assertIsNotNone(move2)
        self.assertIn(tuple(move2), [tuple(m) for m in g.legal_moves()])
        # 树复用真实生效：传入的根节点统计被继续使用（visits 增长）
        self.assertGreater(reused.visits, visits_before)

    def test_stale_root_rebuilds(self):
        """局面不匹配的复用根应被自动重建，不崩溃。"""
        random.seed(4)
        g = Game()
        g.apply_move(0, 0)
        cells, grids, forced, turn = self._state(g)
        _, node = ai.search(cells, grids, forced, turn, budget=0.1)
        g.apply_move(0, 1)              # 局面已变
        cells, grids, forced, turn = self._state(g)
        move, _ = ai.search(cells, grids, forced, turn, budget=0.1,
                            root=node)  # 旧根 → 重建
        self.assertIsNotNone(move)

    def test_lose_mode_loses_more_than_win_mode(self):
        """求败 AI 对随机对手的胜率应显著低于求胜 AI（回归原版 goal 语义）。"""
        random.seed(42)
        try:
            import numpy as np
            np.random.seed(42)          # Numba rollout 使用独立随机流
        except ImportError:
            pass
        wins_win_mode = self._win_count(goal=1, games=6)
        random.seed(42)
        try:
            import numpy as np
            np.random.seed(42)
        except ImportError:
            pass
        wins_lose_mode = self._win_count(goal=-1, games=6)
        self.assertLess(wins_lose_mode, wins_win_mode)

    def _win_count(self, goal, games):
        wins = 0
        for _ in range(games):
            g = Game()
            # AI 执叉（后手），随机执圈
            while not g.is_over():
                moves = g.legal_moves()
                if not moves:
                    break
                if g.turn == CROSS:
                    cells, grids, forced, turn = g.cells, g.grids, g.forced, g.turn
                    move, _ = ai.search(cells, grids, forced, turn, budget=0.06, goal=goal)
                    if move is None:
                        break
                    g.apply_move(*move)
                else:
                    g.apply_move(*random.choice(moves))
            wins += 1 if g.winner == CROSS else 0
        return wins


class TestApiIntegration(unittest.TestCase):
    """通过 Api 桥验证完整对局流程（真实 Rust 后端）。"""

    def test_new_game_computer_first(self):
        api = Api()
        st = api.new_game({"mode": 0, "difficulty": -1, "first": 1,
                           "goal": 1, "sound": True})
        self.assertIsNone(st["lastMove"])                 # 不再内置 AI 首步
        self.assertEqual(st["turn"], CIRCLE)              # 电脑先手 = 圈先（先手=圈惯例）
        self.assertEqual(len(st["moves"]), 81)
        st = api.ai_move()                                # 前端紧接着调 ai_move
        self.assertIsNotNone(st["lastMove"])
        self.assertEqual(st["turn"], CROSS)               # 电脑(圈)落子后轮到人(叉)

    def test_play_flow_human_then_ai(self):
        api = Api()
        api.new_game({"mode": 0, "difficulty": -1, "first": 0,
                      "goal": 1, "sound": True})
        st = api.play(0, 4)                           # 人（圈）落子：立即返回，不触发 AI
        self.assertEqual(st["turn"], CROSS)            # 轮到电脑
        self.assertIsNotNone(st["lastMove"])
        self.assertFalse(st["winner"])
        st = api.ai_move()                             # AI 落子
        self.assertEqual(st["turn"], CIRCLE)
        self.assertIsNotNone(st["lastMove"])

    def test_resign(self):
        api = Api()
        api.new_game({"mode": 0, "difficulty": -1, "first": 0,
                      "goal": 1, "sound": True})
        api.play(0, 4)
        st = api.resign()                          # 人机：人类认输 → AI 胜
        self.assertEqual(st["winner"], CROSS)
        self.assertIsNone(st["winLine"])
        self.assertEqual(st["moves"], [])

    def test_resign_pvp(self):
        api = Api()
        api.new_game({"mode": 1, "difficulty": -1, "first": 0,
                      "goal": 1, "sound": True})
        api.play(0, 0)                                 # 圈落子，轮到叉
        st = api.resign()                              # 当前回合方（叉）认输 → 圈胜
        self.assertEqual(st["winner"], CIRCLE)

    def test_ai_move_idempotent(self):
        """AI 思考完成后再次调用 ai_move 不应重复落子。"""
        api = Api()
        api.new_game({"mode": 0, "difficulty": -1, "first": 0,
                      "goal": 1, "sound": True})
        api.play(0, 4)
        api.ai_move()
        before = api.legal_moves(), api.stats()["version"]
        st = api.ai_move()                           # 已轮到人，不应再走
        self.assertEqual(st["moves"], before[0])
        self.assertEqual(st["turn"], CIRCLE)

    def test_illegal_play_ignored(self):
        random.seed(3)
        api = Api()
        api.new_game({"mode": 0, "difficulty": -1, "first": 1,
                      "goal": 1, "sound": True})
        api.ai_move()                              # 电脑先手落子
        before = api.legal_moves()
        legal = any(m == (0, 0) for m in before)
        st = api.play(0, 0)
        if legal:
            self.assertNotEqual(api.legal_moves(), before)
        else:
            self.assertEqual(api.legal_moves(), before)

    def test_pvp_no_ai(self):
        api = Api()
        st = api.new_game({"mode": 1, "difficulty": -1, "first": 0,
                           "goal": 1, "sound": True})
        st = api.play(0, 0)
        self.assertEqual(st["turn"], CROSS)            # 仅切换回合
        self.assertEqual(st["forced"], 0)              # 指向 0 大格


if __name__ == '__main__':
    unittest.main()

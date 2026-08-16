"""规则引擎单元测试。运行：python -m unittest tests.test_engine -v"""

import unittest

from super_ttt.engine import Game, CIRCLE, CROSS, GRID_OPEN, GRID_TIE


class TestInitial(unittest.TestCase):
    def test_81_legal_moves_at_start(self):
        g = Game()
        self.assertEqual(len(g.legal_moves()), 81)
        self.assertEqual(g.turn, CIRCLE)
        self.assertIsNone(g.forced)
        self.assertFalse(g.is_over())


class TestForcedGrid(unittest.TestCase):
    def test_move_points_to_forced_grid(self):
        g = Game()
        g.apply_move(0, 3)          # 下在 0 大格的 3 小格 → 强制 3 大格
        self.assertEqual(g.forced, 3)
        self.assertEqual(g.turn, CROSS)
        moves = g.legal_moves()
        self.assertEqual(len(moves), 9)
        self.assertTrue(all(m[0] == 3 for m in moves))

    def test_move_to_decided_grid_gives_free_play(self):
        g = Game()
        # 直接构造：让 3 大格已被圈占领
        g.cells[3] = [CIRCLE] * 9
        g.grids[3] = CIRCLE
        g.apply_move(0, 3)          # 指向已决出的 3 大格 → 自由落子
        self.assertIsNone(g.forced)
        # 其余 8 个未决大格共 72 格，减去刚落子的 (0,3)
        self.assertEqual(len(g.legal_moves()), 9 * 8 - 1)


class TestIllegal(unittest.TestCase):
    def setUp(self):
        self.g = Game()
        self.g.apply_move(0, 0)

    def test_wrong_forced_grid_rejected(self):
        self.assertFalse(self.g.apply_move(1, 0))   # 强制格是 0
        self.assertEqual(self.g.turn, CROSS)

    def test_occupied_cell_rejected(self):
        self.g.apply_move(0, 5)     # 合法：强制格 0 内
        self.assertFalse(self.g.apply_move(0, 5))   # 重复落子
        self.g.apply_move(5, 0)     # 轮到圈，指向 0 大格（未决出）→ 强制 0
        self.assertFalse(self.g.apply_move(0, 5))   # 仍被占

    def test_move_into_decided_grid_rejected(self):
        g = Game()
        g.cells[2] = [CIRCLE] * 9
        g.grids[2] = CIRCLE
        g.forced = 2
        # 强制格 2 已决出 → 自由落子，但不能落在 2 大格
        self.assertFalse(g.apply_move(2, 0))
        self.assertTrue(g.apply_move(0, 0))

    def test_move_after_game_over_rejected(self):
        g = Game()
        for sub, mark in [(0, CIRCLE), (4, CIRCLE), (8, CIRCLE)]:
            g.cells[sub] = [mark] * 9
            g.grids[sub] = mark
        g.restore(g.snapshot())     # 重算 winner
        self.assertTrue(g.is_over())
        self.assertEqual(g.legal_moves(), [])
        self.assertFalse(g.apply_move(1, 0))


class TestEndings(unittest.TestCase):
    def test_big_board_win(self):
        g = Game()
        for sub, mark in [(0, CIRCLE), (4, CIRCLE), (8, CIRCLE)]:
            g.cells[sub] = [mark] * 9
            g.grids[sub] = mark
        g.restore(g.snapshot())
        self.assertEqual(g.winner, CIRCLE)
        self.assertEqual(g.win_line, (0, 4, 8))

    def test_tie(self):
        g = Game()
        for sub in range(9):
            g.grids[sub] = GRID_TIE
        g.restore(g.snapshot())
        self.assertEqual(g.winner, 3)

    def test_three_ties_not_a_win(self):
        """平局值 3 不能构成三连（原版 game_is_over 只认 1/2，回归保护）。"""
        g = Game()
        g.grids[0] = g.grids[1] = g.grids[2] = GRID_TIE
        g.restore(g.snapshot())
        self.assertEqual(g.winner, 0)

    def test_subgrid_full_without_winner_is_tie(self):
        g = Game()
        g.cells[5] = [0, CIRCLE, CROSS,
                      CROSS, CROSS, CIRCLE,
                      CIRCLE, CROSS, CIRCLE]   # 留空格，无三连
        g.apply_move(5, 0)  # 圈下在空格 → 满盘无三连 → 平局
        self.assertEqual(g.grids[5], GRID_TIE)


class TestOriginalBugRegression(unittest.TestCase):
    """原版 P1 bug：真实棋盘用 4 标记"未决出可落子"，AI 搜索却只认 0，
    导致"自由落子"局面下合法步列表为空（崩溃）或强制格被无视。
    新引擎 forced 字段独立表达，两种局面都必须给出正确合法步。"""

    def test_free_play_after_decided_target(self):
        g = Game()
        g.cells[3] = [CIRCLE] * 9
        g.grids[3] = CIRCLE
        g.apply_move(0, 3)
        # 自由落子：所有未决大格（除 3 外共 8 个）都可下（减去刚落的 (0,3)）
        self.assertIsNone(g.forced)
        self.assertEqual(len(g.legal_moves()), 71)

    def test_forced_play_after_undecided_target(self):
        g = Game()
        g.apply_move(0, 3)
        self.assertEqual(g.forced, 3)
        self.assertEqual(len(g.legal_moves()), 9)


if __name__ == '__main__':
    unittest.main()

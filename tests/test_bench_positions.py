import unittest

from tests.bench_positions import positions, varied_positions


class TestBenchmarkPositions(unittest.TestCase):
    def test_distinct_legal_game_phases(self):
        games = dict(positions())
        self.assertEqual(list(games), ["opening", "midgame", "endgame"])
        self.assertEqual(
            [sum(bool(c) for row in game.cells for c in row) for game in games.values()],
            [0, 28, 56],
        )
        for game in games.values():
            self.assertFalse(game.is_over())
            self.assertTrue(game.legal_moves())
        self.assertGreaterEqual(sum(bool(g) for g in games["endgame"].grids), 5)

    def test_varied_positions_are_distinct_reproducible_and_cover_each_phase(self):
        first = varied_positions(901)
        repeated = varied_positions(901)
        other = varied_positions(902)
        boards = [tuple(c for row in g.cells for c in row) for _, g in first]
        self.assertEqual(len(set(boards)), 20)
        self.assertEqual(boards, [tuple(c for row in g.cells for c in row) for _, g in repeated])
        self.assertNotEqual(boards, [tuple(c for row in g.cells for c in row) for _, g in other])
        self.assertEqual([sum(bool(c) for c in board) for board in boards],
                         [plies for plies in (8, 20, 32, 44, 56) for _ in range(4)])
        for _, game in first:
            self.assertFalse(game.is_over())
            self.assertTrue(game.legal_moves())


if __name__ == "__main__":
    unittest.main()

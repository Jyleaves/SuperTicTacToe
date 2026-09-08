import unittest

from tests.bench_positions import positions


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


if __name__ == "__main__":
    unittest.main()

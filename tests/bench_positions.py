"""Shared, validated benchmark positions (no GUI or Rust library required)."""
from pathlib import Path
import random

from super_ttt.engine import Game


def positions():
    result = []
    fixture = Path(__file__).with_name("fixtures") / "search_positions.txt"
    for line in fixture.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, history = line.split(":", 1)
        game = Game()
        for pair in filter(None, history.split(";")):
            move = tuple(map(int, pair.split(",")))
            if not game.apply_move(*move):
                raise ValueError(f"Illegal benchmark move {name}: {move}")
        result.append((name, game))
    return result


def varied_positions(seed=20260909, per_phase=4):
    """A reproducible cross-section of nonterminal, legally reached positions."""
    rng = random.Random(seed)
    result = []
    for plies in (8, 20, 32, 44, 56):
        seen = set()
        attempts = 0
        while len(seen) < per_phase:
            attempts += 1
            if attempts > 1000:
                raise ValueError('Could not generate enough nonterminal positions')
            game = Game()
            for _ in range(plies):
                if game.is_over():
                    break
                if not game.apply_move(*rng.choice(game.legal_moves())):
                    raise AssertionError('Generated an illegal move')
            key = tuple(c for row in game.cells for c in row)
            if game.is_over() or key in seen:
                continue
            seen.add(key)
            result.append((f'ply{plies}-sample{len(seen)}', game))
    return result

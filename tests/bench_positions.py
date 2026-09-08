"""Shared, validated benchmark positions (no GUI or Rust library required)."""
from pathlib import Path

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

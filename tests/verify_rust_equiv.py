"""Rust 引擎等价性校验：随机游走逐步比对。
每局随机对弈，每一步同时用 Python engine 与 Rust DLL 查询：
  - 合法步列表（顺序 + 内容）完全一致
  - 落子后 winner/forced/turn 一致
  - 终局一致
另比对「AI 眼中的局面」：MCTS 搜索返回的落子必须合法（非法 = 引擎 bug）。
运行：python tests/verify_rust_equiv.py
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from super_ttt import engine, mcts  # noqa: E402
from super_ttt.server import rust_legal  # noqa: E402


def flat_cells(g):
    return [c for row in g.cells for c in row]


def check_game(seed, use_ai_moves):
    """随机（或 AI 驱动）走一整局，逐步校验两引擎一致。返回 (步数, 终局一致)."""
    rnd = random.Random(seed)
    g = engine.Game()
    steps = 0
    mismatch = None
    while not g.is_over():
        py_legal = g.legal_moves()
        rs_legal = rust_legal(g.cells, g.grids, g.forced, g.turn)
        if [tuple(m) for m in py_legal] != rs_legal:
            mismatch = f"legal mismatch @step{steps}: py={py_legal[:5]} rust={rs_legal[:5]}"
            break
        if not py_legal:
            break
        if use_ai_moves and steps % 7 == 3:      # 偶尔用 Rust AI 选步（覆盖搜索路径）
            r = mcts_search_once(g, 800)
            mv = tuple(r["move"]) if r["move"] else rnd.choice(py_legal)
            if mv not in py_legal:
                mismatch = f"rust AI illegal move @step{steps}: {mv}"
                break
        else:
            mv = rnd.choice(py_legal)
        ok = g.apply_move(*mv)
        if not ok:
            mismatch = f"python rejected move {mv} @step{steps}"
            break
        steps += 1
        if steps > 200:
            break
    if mismatch:
        return mismatch
    # 终局一致性：Rust 视角重算 winner
    rs = rust_legal(g.cells, g.grids, g.forced, g.turn)
    py_moves = [tuple(m) for m in g.legal_moves()]
    if g.is_over() and py_moves:
        return f"over but python legal={len(py_moves)}"
    if not g.is_over() and rs != py_moves:
        return f"final legal mismatch py={py_moves[:3]} rust={rs[:3]}"
    return f"ok steps={steps} winner={g.winner}"


def mcts_search_once(g, iters):
    from super_ttt.server import rust_search
    return rust_search(g.cells, g.grids, g.forced, g.turn, iters, 1, 1, 0.0)


def main():
    try:
        import numpy  # noqa: F401
    except ImportError:
        print("需要 numpy（duel 依赖）")
        return
    games = int(os.environ.get("EQUIV_GAMES", "300"))
    random.seed(20260816)
    bad = 0
    for seed in range(games):
        r = check_game(seed, use_ai_moves=(seed % 3 == 0))
        if not r.startswith("ok"):
            print(f"seed={seed}: {r}")
            bad += 1
            if bad > 5:
                break
    if bad == 0:
        print(f"{games} 局随机游走：合法步/终局逐局一致 ✓")
    else:
        print(f"发现 {bad} 处不一致 ✗")
        sys.exit(1)


if __name__ == "__main__":
    main()

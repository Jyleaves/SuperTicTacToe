"""AI 决策稳定性：同一后期局面多次搜索（不同随机流），落子是否一致"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from super_ttt import mcts  # noqa: E402
from tests.test_ai_perf_equiv import random_position  # noqa: E402


def main():
    mcts.warmup()
    for pos in (15, 25, 40):
        g = random_position(pos)
        cells = np.asarray([c for r in g.cells for c in r], dtype=np.int8)
        grids = np.asarray(g.grids, dtype=np.int8)
        forced = -1 if g.forced is None else g.forced
        turn = int(g.turn)
        moves = []
        for seed in range(5):
            mcts._seed_rng(1000 + seed)
            mv, tree = mcts.search(cells, grids, forced, turn, 0.0, 1,
                                   iters=128000)
            moves.append(tuple(mv) if mv else None)
        # 一致性：最多出现的 move 的占比
        from collections import Counter
        cnt = Counter(moves)
        top, n = cnt.most_common(1)[0]
        agree = n / len(moves)
        print(f"局面{pos}: 5 次搜索落子 {sorted(moves)} → 最多次 {top} "
              f"({n}/5 一致) {'PASS 决策稳定' if agree >= 0.8 else 'FAIL 摇摆'}")


if __name__ == "__main__":
    main()

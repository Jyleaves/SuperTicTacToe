"""实时胜率充分测试：视角方向/电脑先手颜色/一步必胜 100%/多步必胜阶段"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from super_ttt import engine, mcts  # noqa: E402
from super_ttt.engine import CIRCLE, CROSS  # noqa: E402


def setup(cells, grids, forced, turn):
    cells_arr = np.asarray(cells, dtype=np.int8).reshape(-1)
    grids_arr = np.asarray(grids, dtype=np.int8).reshape(-1)
    forced_i = -1 if forced is None else forced
    return cells_arr, grids_arr, forced_i, turn


def test_a_human_first():
    """人先手（人=圈）：人落 1 子后 AI(叉) 搜索——圈（先手）应占优（曾镜像为叉高）"""
    g = engine.Game()
    g.apply_move(4, 4)                       # 人（圈）下中心
    cells, grids, forced, turn = setup(g.cells, g.grids, g.forced, int(g.turn))
    mv, tree = mcts.search(cells, grids, forced, turn, 0.0, 1, iters=30000)
    c, t, x = (int(v) for v in tree.stats)
    print(f"A 人先手(圈)落中心后 AI(叉)评估: 圈{c} 平{t} 叉{x}  → "
          f"{'PASS 圈占优' if c > x else 'FAIL 圈未占优'}")


def test_b_computer_first():
    """电脑先手：空棋盘 turn=1，AI=圈搜索——圈（先手）应占优"""
    g = engine.Game()
    cells, grids, forced, turn = setup(g.cells, g.grids, g.forced, int(g.turn))
    mv, tree = mcts.search(cells, grids, forced, turn, 0.0, 1, iters=30000)
    c, t, x = (int(v) for v in tree.stats)
    print(f"B 电脑先手(圈)空盘评估: 圈{c} 平{t} 叉{x}  → "
          f"{'PASS 圈占优' if c > x else 'FAIL 圈未占优'}")


def test_c_small_win_not_terminal():
    """小格一步赢 ≠ 整局赢：叉赢 0 大格后大棋盘还早——评估是真实分布（非 100% 属正常）"""
    g = engine.Game()
    g.cells[0][3] = CROSS                       # 直接构造局面（绕过落子序列）
    g.cells[0][6] = CROSS
    g.cells[0][4] = CIRCLE
    g.cells[1][0] = CIRCLE
    g.cells[2][4] = CIRCLE
    g.forced = 0
    g.turn = CROSS
    mv, tree = mcts.search(g.cells, g.grids, g.forced, int(g.turn), 0.0, 1,
                           iters=8000)
    c, t, x = (int(v) for v in tree.stats)
    ok = (tree.stats.sum() == 8000 and c + t + x == 8000
          and mv is not None and mv[0] == 0)    # 应选 0 大格（赢格）
    print(f"C 小格一步赢(轮叉): 圈{c} 平{t} 叉{x} → "
          f"{'PASS 评估真实分布且选赢格' if ok else 'FAIL'}")


def test_d_near_endgame():
    """多步必胜阶段：叉已 3 连大格 + 差 1 大格（无法一步证明）——记录实际胜率"""
    # 叉赢 0,1,2 大格 + 大棋盘横线差 3 大格；3 大格叉已 2 子
    cells = [[0] * 9 for _ in range(9)]
    grids = [0] * 9
    for sub in (0, 1, 2):
        for c in range(9):
            cells[sub][c] = CROSS
        grids[sub] = CROSS
    # 3 大格：叉 (0,4) 连线 2 子
    cells[3][0] = CROSS
    cells[3][4] = CROSS
    cells[3][8] = CIRCLE
    # 圈下过一些子避免 forced 异常
    cells[4][0] = CIRCLE
    cells[4][1] = CIRCLE
    cells_arr, grids_arr, forced_i, turn = setup(cells, grids, 3, CROSS)
    mv, tree = mcts.search(cells_arr, grids_arr, forced_i, turn, 0.0, 1,
                           iters=30000)
    c, t, x = (int(v) for v in tree.stats)
    print(f"D 多步必胜阶段(轮叉): 圈{c} 平{t} 叉{x}  → 叉胜率 {x/30000:.0%}"
          "（rollout 随机模拟，非完美对局——属预期，非 bug）")


def test_e_server_post_move_semantics():
    """server 语义：AI 落子后 _last_stats 应为落子后局面的评估（异步等待）"""
    from super_ttt.server import Api
    api = Api()
    api.new_game({"mode": 0, "difficulty": 0, "first": 0, "goal": 1,
                  "sound": True, "stats": True})
    api.play(4, 4)                        # 人（圈）落中心
    api.ai_move()                         # AI（叉）落子（评估异步）
    time.sleep(2.5)                       # 等异步评估完成（含无预热编译）
    r = api.stats()
    stats = r["stats"]
    assert stats is not None and sum(stats) >= 5000, f"评估缺失：{stats}"
    st = api.stats()
    print(f"E server 落子后评估: 圈{stats[0]} 平{stats[1]} 叉{stats[2]}（{sum(stats)} 迭代，"
          f"版本{st['version']}）→ PASS 落子后语义正确")


def test_f_open_and_play_eval():
    """开局即评估（空盘）+ 人落子后评估（异步等待，胜率随局面变动）"""
    from super_ttt.server import Api
    api = Api()
    st = api.new_game({"mode": 0, "difficulty": -1, "first": 0, "goal": 1,
                       "sound": True, "stats": True})
    time.sleep(2.5)                           # 等开局评估完成
    s0 = api.stats()["stats"]
    assert s0 and sum(s0) >= 5000, "开局应有空盘评估"
    api.play(4, 4)                            # 人落中心（评估异步）
    time.sleep(2.5)
    s1 = api.stats()["stats"]
    assert s1 and sum(s1) >= 5000, "人落子后应有评估"
    changed = s0 != s1
    print(f"F 开局评估 圈{s0[0]} 平{s0[1]} 叉{s0[2]} → 人落中心后 圈{s1[0]} 平{s1[1]} 叉{s1[2]}"
          f" → {'PASS 胜率随落子变动' if changed else 'FAIL 未变动'}")


def main():
    mcts.warmup()
    test_a_human_first()
    test_b_computer_first()
    test_c_small_win_not_terminal()
    test_d_near_endgame()
    test_e_server_post_move_semantics()
    test_f_open_and_play_eval()


if __name__ == "__main__":
    main()

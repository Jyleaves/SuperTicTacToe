"""Rust 后端桥冒烟测试（headless，不开窗口）。
覆盖：new_game → ai_move（电脑先手）→ play → ai_move → stats 轮询 → resign。
运行：python tests/smoke_rust_bridge.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from super_ttt.server import Api  # noqa: E402


def main():
    api = Api()
    print("ping:", api.ping())
    print("precompile:", api.precompile_status())

    # ---- 人机：电脑先手（圈），中等难度
    st = api.new_game({"mode": 0, "difficulty": 1, "first": 1,
                       "goal": 1, "sound": True, "stats": True})
    assert st["turn"] == 1 and len(st["moves"]) == 81, st["turn"]
    assert st["stats"] is None
    print("new_game ok: 81 开局合法步，圈先手")

    t0 = time.perf_counter()
    st = api.ai_move()
    dt = time.perf_counter() - t0
    assert st["lastMove"] is not None and st["turn"] == 2
    print(f"ai_move(电脑先手) ok: {st['lastMove']}，耗时 {dt*1000:.0f}ms")

    # ---- 人（叉）落子 → AI 回应
    legal = [tuple(m) for m in st["moves"]]
    sub, cell = legal[0]
    st = api.play(sub, cell)
    assert st["turn"] == 1, "人落子后应轮到 AI（圈）"
    st = api.ai_move()
    assert st["lastMove"] is not None
    print("play + ai_move ok:", st["lastMove"])

    # ---- 胜率评估轮询（两阶段：2万快速值 → 20万细化。
    #      与 Python 原版一致：search 每次清零 stats，最终值为
    #      第二阶段 18 万迭代的分布）
    final = None
    for _ in range(100):
        r = api.stats()
        if r["stats"]:
            final = r["stats"]
            if sum(final) >= 180_000:
                break
        time.sleep(0.05)
    total = sum(final or [0, 0, 0])
    print(f"stats ok: {final}（共 {total} 次终局模拟, busy={r['busy']}）")
    assert total >= 180_000, "评估应细化到 18 万迭代"

    # ---- 认输
    st = api.resign()
    assert st["winner"] == 1 and st["winLine"] is None, st["winner"]  # 电脑=圈胜
    print("resign ok: 电脑（圈）获胜")

    # ---- 人人模式不触发 AI
    api.new_game({"mode": 1, "difficulty": -1, "first": 0,
                  "goal": 1, "sound": True, "stats": True})
    st = api.play(0, 0)
    assert st["turn"] == 2 and st["forced"] == 0
    st = api.ai_move()
    assert st["turn"] == 2, "人人模式 ai_move 不应落子"
    print("pvp ok: 人人模式 AI 不介入")

    # ---- 非法落子被忽略（当前强制格 0，turn=2）
    st = api.play(1, 0)
    assert st["forced"] == 0 and st["turn"] == 2
    print("illegal play ok: 强制格外的落子被拒绝")

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()

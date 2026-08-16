"""通用迭代梯度对弈：同引擎不同迭代数互搏（用于探索"更多迭代=更强？"）。

用法（命令行）：
  python tests/duel_gradient.py LO HI [--games N] [--cap N] [--procs N]

示例：
  python tests/duel_gradient.py 800 1600              # 24 局、16 进程、池自动
  python tests/duel_gradient.py 32000 128000 --games 24
  python tests/duel_gradient.py 512000 2048000 --games 12 --cap 2097152

说明：
  - 输出格式：[LO vs HI xN] 低迭代 a : 高迭代 b : 平 t | 高胜率 x%
  - 高迭代区（>=256k）平局率极高，建议 --games 12 即可；局数过多浪费时间。
  - --cap 为节点池容量：迭代数接近池容量会触发搜索中重建（干扰棋力），
    高迭代建议 cap >= HI*2（如 512k vs 2M 用 --cap 2097152，32GB 内存可跑 16 进程）。
  - 结论判读：低迭代区 2 倍差距显著（高胜率 54-67%）；256k 后 4 倍差距也无区分
    （平局率 75%+）——见 DEBUG_LOG 梯度上限条目。
"""
import argparse
import multiprocessing
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.duel_ladder2 import play  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('lo', type=int, help='低迭代方迭代数')
    ap.add_argument('hi', type=int, help='高迭代方迭代数')
    ap.add_argument('--games', type=int, default=24, help='对局数（默认 24）')
    ap.add_argument('--cap', type=int, default=0,
                    help='节点池容量（默认 max(hi*2, 262144)，防重建干扰）')
    ap.add_argument('--procs', type=int, default=16, help='并行进程数（默认 16）')
    args = ap.parse_args()

    from super_ttt import mcts as _m
    _m.warmup()

    cap = args.cap or max(args.hi * 2, 262_144)
    t0 = time.monotonic()
    tasks = [(args.lo, args.hi, 90_000 + i, i % 2 == 0, cap)
             for i in range(args.games)]
    with multiprocessing.Pool(args.procs) as pool:
        results = pool.map(play, tasks)
    lo_w = sum(1 for r, _ in results if r == 'lo')
    hi_w = sum(1 for r, _ in results if r == 'hi')
    ties = args.games - lo_w - hi_w
    print(f"[{args.lo} vs {args.hi} x{args.games}] 低迭代 {lo_w} : 高迭代 {hi_w}"
          f" : 平 {ties}  | 高胜率 {hi_w / args.games:.0%}")
    print(f"总耗时 {time.monotonic() - t0:.0f}s")


if __name__ == "__main__":
    main()

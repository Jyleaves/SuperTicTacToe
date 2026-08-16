"""蒙特卡洛树搜索（MCTS）电脑棋手。

设计要点
--------
- 标准 UCB1（c=1.0）。
- 收益约定（与原版一致，已用 1-ply 对局手工验证）：
  quality 以「节点行动方的对手」视角累计——rollout 返回 +1 当叶子行动方
  落败（对手获胜）；backup 沿路径逐层取反（行动方交替，故同视角累计）。
  best_child 取 quality/visits 最大者 = 对己方最有利。
- 纯函数式状态：节点保存不可变快照（flat tuple），搜索不触碰外部对象，
  天然适合后台线程运行。
- 树复用：search() 返回最佳子节点；下一手把「人类落子对应的子节点」作为
  根传入即可继续利用统计信息（找不到则重建）。
- goal=+1 求胜 / goal=-1 求败：整体乘以 goal 即翻转收益符号。
"""

from __future__ import annotations

import math
import random
import time

from .engine import CIRCLE, CROSS, EMPTY, GRID_OPEN, WIN_LINES

C_UCB = 0.8                 # UCB1 探索常数（略偏利用：短时间预算下更聚焦强线）
MAX_ITERATIONS = 40_000     # 单次搜索迭代上限（内存保护）

# Numba 加速（可选）：rollout 是纯数值热路径，用 JIT 编译可提速 5~15 倍。
# 若 numba 未安装或环境异常，自动回退纯 Python 实现（_rollout_py）。
try:
    import numpy as np
    from numba import njit
    _HAS_NUMBA = True
except Exception:
    _HAS_NUMBA = False

# 难度档位（2026-08-08 改为限次为主 + 软时间上限）：
# 迭代数由对弈实验标定（2000/8000/32000/128000 相邻胜率差 71-79%，
# 见 DEBUG_LOG「难度档位验证」）；软上限兜底慢电脑的等待时间。
DIFFICULTY_BUDGETS = {-1: 1.5, 0: 2.5, 1: 4.0, 2: 6.0}   # 兼容旧接口（保留）
DIFFICULTY_ITERS = {-1: 2000, 0: 8000, 1: 32000, 2: 128000, 3: 256000}
DIFFICULTY_CAPS = {-1: 1.0, 0: 2.0, 1: 3.5, 2: 6.0, 3: 12.0}  # 软时间上限（秒）


def budget_for(difficulty: int) -> float:
    """兼容旧接口：返回软时间上限（限次模式下作为兜底）。"""
    return DIFFICULTY_CAPS.get(difficulty, DIFFICULTY_CAPS[-1])


def difficulty_for(difficulty: int):
    """难度档位 -> (迭代数, 软时间上限秒)。未知档位回退最弱。"""
    return (DIFFICULTY_ITERS.get(difficulty, DIFFICULTY_ITERS[-1]),
            DIFFICULTY_CAPS.get(difficulty, DIFFICULTY_CAPS[-1]))

_SUB_IDX = tuple(tuple(s * 9 + c for c in range(9)) for s in range(9))

# 增量线检查预计算：
# _CELL_LINES[c] = 小格内包含第 c 格的线（最多 4 条，每条 3 个相对下标）
_CELL_LINES = {}
for c in range(9):
    r, col = c // 3, c % 3
    lines = [(3 * r, 3 * r + 1, 3 * r + 2), (col, col + 3, col + 6)]
    if r == col:
        lines.append((0, 4, 8))
    if r + col == 2:
        lines.append((2, 4, 6))
    _CELL_LINES[c] = tuple(lines)

# _GRID_LINES[s] = 大棋盘上包含第 s 大格的线（最多 4 条）
_GRID_LINES = {}
for s in range(9):
    r, col = s // 3, s % 3
    lines = [(3 * r, 3 * r + 1, 3 * r + 2), (col, col + 3, col + 6)]
    if r == col:
        lines.append((0, 4, 8))
    if r + col == 2:
        lines.append((2, 4, 6))
    _GRID_LINES[s] = tuple(lines)

if _HAS_NUMBA:
    # Numba 版线表（数组形式，-1 填充；njit 内不能使用 dict）
    _CELL_LINES_ARR = np.full((9, 4, 3), -1, dtype=np.int64)
    for c, lines in _CELL_LINES.items():
        for i, ln in enumerate(lines):
            _CELL_LINES_ARR[c, i] = ln
    _GRID_LINES_ARR = np.full((9, 4, 3), -1, dtype=np.int64)
    for s, lines in _GRID_LINES.items():
        for i, ln in enumerate(lines):
            _GRID_LINES_ARR[s, i] = ln


def _sub_winner(cells, sub):
    """cells 为 flat tuple（81 个）。返回小格赢家 1/2，无则 0。"""
    base = sub * 9
    for a, b, c in WIN_LINES:
        s = cells[base + a]
        if s in (CIRCLE, CROSS) and s == cells[base + b] == cells[base + c]:
            return s
    return 0


def _big_winner(grids):
    """返回 1/2（有人大棋盘三连）、3（全部决出且无胜者）、0（进行中）。"""
    for a, b, c in WIN_LINES:
        s = grids[a]
        if s in (CIRCLE, CROSS) and s == grids[b] == grids[c]:
            return s
    if all(g != GRID_OPEN for g in grids):
        return 3
    return 0


def _legal(cells, grids, forced):
    """返回局面 (cells, grids, forced) 的全部合法落子 [(sub, cell), ...]。"""
    if forced is not None and grids[forced] == GRID_OPEN:
        subs = (forced,)
    else:
        subs = tuple(i for i in range(9) if grids[i] == GRID_OPEN)
    moves = []
    for s in subs:
        base = s * 9
        for c in range(9):
            if cells[base + c] == EMPTY:
                moves.append((s, c))
    return moves


class Node:
    __slots__ = ("move", "parent", "children", "visits", "quality",
                 "cells", "grids", "forced", "turn", "moves_left")

    def __init__(self, cells, grids, forced, turn, move=None, parent=None):
        self.move = move          # 从父节点到本节点的落子 (sub, cell)；根为 None
        self.parent = parent
        self.children = []
        self.visits = 0
        self.quality = 0.0
        self.cells = cells        # flat tuple, 81 个 0/1/2
        self.grids = grids        # tuple, 9 个 0/1/2/3
        self.forced = forced      # int | None
        self.turn = turn          # 本节点行动方 1/2
        self.moves_left = _legal(cells, grids, forced)   # 未展开的合法步（展开时 pop）


def _apply(cells, grids, forced, turn, sub, cell):
    """在快照上落子，返回 (new_cells, new_grids, new_forced, new_turn)。"""
    lst = list(cells)
    lst[sub * 9 + cell] = turn
    cells = tuple(lst)

    g = list(grids)
    w = _sub_winner(cells, sub)
    if w:
        g[sub] = w
    elif all(cells[base] for base in _SUB_IDX[sub]):
        g[sub] = 3
    grids = tuple(g)

    new_forced = cell if grids[cell] == GRID_OPEN else None
    new_turn = CROSS if turn == CIRCLE else CIRCLE
    return cells, grids, new_forced, new_turn


def _expand(node, move):
    sub, cell = move
    cells, grids, forced, turn = _apply(
        node.cells, node.grids, node.forced, node.turn, sub, cell)
    child = Node(cells, grids, forced, turn, move=move, parent=node)
    node.children.append(child)
    return child


def _best_child(node):
    """UCB1 选择。children 的 visits 均 >= 1（展开后必先回传一次）。"""
    best, best_score = None, -math.inf
    ln_n = math.log(max(node.visits, 1))
    for ch in node.children:
        if ch.visits == 0:
            continue
        score = ch.quality / ch.visits + C_UCB * math.sqrt(ln_n / ch.visits)
        if score > best_score:
            best, best_score = ch, score
    return best


def _tree_policy(root):
    """从根向下选择：有未展开子节点则展开，否则按 UCB1 下行，直到终局。"""
    node = root
    while True:
        if _big_winner(node.grids):
            return node
        if node.moves_left:                       # 还有未展开的合法步
            i = random.randrange(len(node.moves_left))
            return _expand(node, node.moves_left.pop(i))
        node = _best_child(node)


def _sub_winner_inc(cells, sub, cell, turn):
    """落子后增量检查：只查包含该小格的线（<=4 条）。返回 1/2 或 0。"""
    base = sub * 9
    for a, b, c in _CELL_LINES[cell]:
        if cells[base + a] == cells[base + b] == cells[base + c] == turn:
            return turn
    return 0


def _big_winner_inc(grids, sub, turn):
    """大格刚被 turn 占领后增量检查：只查包含该大格的线。返回 1/2 或 0。"""
    for a, b, c in _GRID_LINES[sub]:
        if grids[a] == grids[b] == grids[c] == turn:
            return turn
    return 0


def _rollout_py(node, goal):
    """纯 Python 版 rollout（回退实现，逻辑与 Numba 版一致）。
    两遍扫描选步：先计数、再定位第 k 个（每步仅 1 次随机）。"""
    cells = list(node.cells)
    grids = list(node.grids)
    forced = node.forced
    turn = node.turn
    mover = turn
    decided = sum(1 for g in grids if g != GRID_OPEN)   # 已决出的大格数

    while True:
        # ---- 第一遍：统计合法步数 n ----
        n = 0
        if forced is not None and grids[forced] == GRID_OPEN:
            base = forced * 9
            for c in range(9):
                if cells[base + c] == EMPTY:
                    n += 1
        else:
            for s in range(9):
                if grids[s] == GRID_OPEN:
                    base = s * 9
                    for c in range(9):
                        if cells[base + c] == EMPTY:
                            n += 1
        if n == 0:                      # 无合法步且无胜者：平局
            return 0

        # ---- 第二遍：定位第 k 个合法步（命中后继续遍历，pick 不会被覆盖） ----
        k = int(random.random() * n)
        cnt = 0
        pick = -1
        if forced is not None and grids[forced] == GRID_OPEN:
            base = forced * 9
            for c in range(9):
                if cells[base + c] == EMPTY:
                    if cnt == k:
                        pick = base + c
                    cnt += 1
        else:
            for s in range(9):
                if grids[s] == GRID_OPEN:
                    base = s * 9
                    for c in range(9):
                        if cells[base + c] == EMPTY:
                            if cnt == k:
                                pick = base + c
                            cnt += 1
        sub = pick // 9
        cell = pick % 9
        cells[pick] = turn

        # ---- 增量更新小格状态 ----
        if _sub_winner_inc(cells, sub, cell, turn):
            grids[sub] = turn
            decided += 1
        else:
            base = sub * 9
            full = True
            for i in range(9):
                if cells[base + i] == EMPTY:
                    full = False
                    break
            if full:
                grids[sub] = 3
                decided += 1

        # ---- 增量大棋盘判定 ----
        if grids[sub] == turn and _big_winner_inc(grids, sub, turn):
            if turn == mover:
                return -goal
            return goal
        if decided == 9:                # 全部决出且无胜者：平局
            return 0

        forced = cell if grids[cell] == GRID_OPEN else None
        turn = CROSS if turn == CIRCLE else CIRCLE


if _HAS_NUMBA:
    @njit(cache=True)
    def _rollout_numba(cells, grids, forced, turn, mover, goal):
        """Numba 编译版 rollout：cells=int64[81], grids=int64[9],
        forced=-1 表示自由落子。返回 +1 当 mover 落败（对手获胜），x goal。"""
        decided = 0
        for i in range(9):
            if grids[i] != 0:
                decided += 1
        while True:
            # 第一遍：统计合法步数 n
            n = 0
            if forced >= 0 and grids[forced] == 0:
                base = forced * 9
                for c in range(9):
                    if cells[base + c] == 0:
                        n += 1
            else:
                for s in range(9):
                    if grids[s] == 0:
                        base = s * 9
                        for c in range(9):
                            if cells[base + c] == 0:
                                n += 1
            if n == 0:                      # 无合法步且无胜者：平局
                return 0

            # 第二遍：定位第 k 个合法步（命中后继续遍历，pick 不会被覆盖）
            k = int(np.random.random() * n)
            cnt = 0
            pick = -1
            if forced >= 0 and grids[forced] == 0:
                base = forced * 9
                for c in range(9):
                    if cells[base + c] == 0:
                        if cnt == k:
                            pick = base + c
                        cnt += 1
            else:
                for s in range(9):
                    if grids[s] == 0:
                        base = s * 9
                        for c in range(9):
                            if cells[base + c] == 0:
                                if cnt == k:
                                    pick = base + c
                                cnt += 1
            sub = pick // 9
            cell = pick % 9
            cells[pick] = turn

            # 增量更新小格状态（只查包含该格的线，<=4 条）
            w = 0
            base = sub * 9
            for i in range(4):
                a = _CELL_LINES_ARR[cell, i, 0]
                if a < 0:
                    break
                b = _CELL_LINES_ARR[cell, i, 1]
                d = _CELL_LINES_ARR[cell, i, 2]
                if cells[base + a] == turn and cells[base + b] == turn \
                        and cells[base + d] == turn:
                    w = turn
                    break
            if w:
                grids[sub] = turn
                decided += 1
            else:
                full = True
                for i in range(9):
                    if cells[base + i] == 0:
                        full = False
                        break
                if full:
                    grids[sub] = 3
                    decided += 1

            # 增量大棋盘判定
            if grids[sub] == turn:
                for i in range(4):
                    a = _GRID_LINES_ARR[sub, i, 0]
                    if a < 0:
                        break
                    b = _GRID_LINES_ARR[sub, i, 1]
                    d = _GRID_LINES_ARR[sub, i, 2]
                    if grids[a] == turn and grids[b] == turn and grids[d] == turn:
                        if turn == mover:
                            return -goal
                        return goal
            if decided == 9:                # 全部决出且无胜者：平局
                return 0

            forced = cell if grids[cell] == 0 else -1
            turn = 3 - turn

    def _rollout(node, goal):
        """从叶子局面随机模拟到终局。返回 +1 当叶子行动方落败（对手获胜），x goal。
        Numba 编译版优先；首次调用触发 JIT 编译（可由启动预热线程提前触发）。"""
        cells = np.array(node.cells, dtype=np.int64)
        grids = np.array(node.grids, dtype=np.int64)
        forced = node.forced if node.forced is not None else -1
        return int(_rollout_numba(cells, grids, forced, node.turn, node.turn, goal))

    def warmup():
        """触发 Numba 编译（磁盘缓存 cache=True），供启动后台线程调用。"""
        cells = np.zeros(81, dtype=np.int64)
        grids = np.zeros(9, dtype=np.int64)
        _rollout_numba(cells, grids, 0, 1, 1, 1)
else:
    _rollout = _rollout_py

    def warmup():
        """无 Numba 环境：无需预热。"""
        return None


def _backup(node, reward):
    while node is not None:
        node.visits += 1
        node.quality += reward
        reward = -reward
        node = node.parent


def find_child(node, move):
    """在 node.children 中查找落子为 move 的子节点，找不到返回 None。"""
    if node is None:
        return None
    for ch in node.children:
        if ch.move == move:
            return ch
    return None


def search(cells, grids, forced, turn, budget, goal=1, root=None):
    """为 turn 方搜索最佳落子。

    参数
    ----
    cells: 9x9 嵌套序列；grids: 9 个；forced: int|None；turn: 1/2
    budget: 时间预算（秒）；goal: +1 求胜 / -1 求败
    root: 树复用根（必须是当前局面对应的节点，否则自动重建）

    返回
    ----
    (move, node): move 为最佳落子 (sub, cell) 或 None（无合法步）；
    node 为最佳子节点，其 children 可传入下一手 search 复用。
    """
    cells_t = tuple(c for row in cells for c in row)   # 扁平化 81 元组
    grids_t = tuple(grids)
    # 复用校验：只看局面是否一致（复用的根节点 parent 非 None 是正常的）
    if root is None or root.turn != turn \
            or root.cells != cells_t or root.grids != grids_t \
            or root.forced != forced:
        root = Node(cells_t, grids_t, forced, turn)

    deadline = time.monotonic() + budget
    iters = 0
    while iters < MAX_ITERATIONS:
        if time.monotonic() >= deadline:
            break
        leaf = _tree_policy(root)
        w = _big_winner(leaf.grids)
        if w:                              # 叶子即终局：赢家必为对方（行动方落败）
            reward = 0 if w == 3 else goal
        else:
            reward = _rollout(leaf, goal)
        _backup(leaf, reward)
        iters += 1

    if not root.children:
        return None, root
    # 必胜手优先：存在落子后直接获胜的展开节点时，直接选它
    # （等价原版 sys.maxsize 奖励 hack 的意图，但语义干净，仅求胜模式生效）
    if goal > 0:
        wins = [ch for ch in root.children if _big_winner(ch.grids) == turn]
        if wins:
            best = max(wins, key=lambda c: c.visits)
            return best.move, best
    best = _best_child(root)
    return best.move, best

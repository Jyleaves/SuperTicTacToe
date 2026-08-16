"""全数组化 MCTS（numpy 节点池 + numba 全搜索循环）。

与 ai.py 的关系：ai.py 是「Python 对象树」实现（保留作回退/对照）；
本模块是性能版——树的所有状态存进固定容量的 numpy 数组池，
tree_policy / expand / backup / best_child 全部 njit 编译，
每次迭代只操作连续内存，避免 Python 对象与 tuple→array 转换开销。

设计要点
--------
- 收益约定与 ai.py 完全一致：rollout 返回 +1 当叶子行动方落败（对手获胜），
  backup 沿路径逐层取反；best_child 取 quality/visits 最大（含 UCB1 c=0.8）。
- 节点池布局（每节点一行，int8 为主，容量 NODE_CAP，可 Python 侧扩容）：
    cells       (N,81) int8   局面快照
    grids       (N,9)  int8   大棋盘状态（0 未决 / 1 / 2 / 3 平）
    forced      (N,)   int8   -1=自由，否则强制大格下标
    turn        (N,)   int8   本节点行动方 1/2
    parent      (N,)   int32  父节点索引，-1=根
    first_child (N,)   int32  子链表头，-1=无
    next_sib    (N,)   int32  兄弟链，-1=无（头插法）
    visits      (N,)   int32
    quality     (N,)   float64
    move        (N,2)  int8   从父到本节点的落子 (sub, cell)，根为 (-1,-1)
    legal_count (N,)   int32  本节点合法步总数（展开时算好，展开完即不再扫描）
    n_children  (N,)   int32  已展开子节点数（固定顺序展开第 n_children 个合法步）
- 展开顺序确定化：展开第 n_children 个合法步（按 sub-major 遍历序）。
  随机性完全来自 rollout（标准 MCTS 中 rollout 随机即足够）。
- 批量调度：njit 内跑 BATCH 次迭代，Python 侧按时间预算循环调度
  （时间检查留在 Python，避免 njit 内 clock 调用）。
- 树复用：search 返回 (move, tree)；下一手 find_child(tree, move) 把
  root 提升到人类落子对应的子节点；找不到或容量不足则重建根。
- 容量管理：free 指针单调增长；接近上限时自动重建根（树复用收益边际，
  重建仅损失步间继承，不影响单步搜索质量）。
"""

from __future__ import annotations

import math
import threading
import time

from . import ai
from .engine import CIRCLE, CROSS, GRID_OPEN

# 难度预算/迭代档位直接复用 ai 的定义
budget_for = ai.budget_for
difficulty_for = ai.difficulty_for

try:
    import numpy as np
    from numba import njit
    _HAS_NUMBA = True
except Exception:                                   # pragma: no cover
    _HAS_NUMBA = False

NODE_CAP = 524_288          # 初始节点池容量（~72MB）：大师档 256k 迭代 + 树复用增长需 512k 节点；容量满自动重建根（剖析实测池大小不影响迭代速率）
GROW_STEP = 196_608         # 扩容步长
BATCH = 512                 # njit 内单批迭代数（~20-30ms，时间精度足够）
MAX_ITERATIONS = 2_000_000  # 单次搜索迭代上限（6s x 20万/s = 120万，留余量）
RECYCLE_MARGIN = 65_536     # free 距上限余量，低于则重建根（搜索中）或下一手重建

_WIN_LINES = np.array([(0, 1, 2), (3, 4, 5), (6, 7, 8),
                       (0, 3, 6), (1, 4, 7), (2, 5, 8),
                       (0, 4, 8), (2, 4, 6)], dtype=np.int64)


@njit(cache=True, nogil=True)
def _big_winner(grids):
    """grids: int8[9]。返回 1/2（有人三连）、3（全决出无胜者）、0（进行中）。
    注意：不能用链式比较 s == a == b（numba 下语义错误，见 DEBUG_LOG D11）。"""
    for i in range(8):
        a = _WIN_LINES[i, 0]
        b = _WIN_LINES[i, 1]
        d = _WIN_LINES[i, 2]
        s = grids[a]
        if s != 0 and s != 3 and s == grids[b] and s == grids[d]:
            return s
    for i in range(9):
        if grids[i] == 0:
            return 0
    return 3


@njit(cache=True, nogil=True)
def _count_legal(cells, grids, forced):
    """cells: int8[81], grids: int8[9], forced: int8（-1=自由）。返回合法步数。"""
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
    return n


@njit(cache=True, nogil=True)
def _kth_legal(cells, grids, forced, k):
    """返回第 k 个合法步的 flat 下标（81 格坐标），k 越界返回 -1。"""
    cnt = 0
    if forced >= 0 and grids[forced] == 0:
        base = forced * 9
        for c in range(9):
            if cells[base + c] == 0:
                if cnt == k:
                    return base + c
                cnt += 1
    else:
        for s in range(9):
            if grids[s] == 0:
                base = s * 9
                for c in range(9):
                    if cells[base + c] == 0:
                        if cnt == k:
                            return base + c
                        cnt += 1
    return -1


@njit(cache=True, nogil=True)
def _bm_test(bm, node, i):
    """位图查询：格 i（0-80）是否已展开。"""
    if i < 64:
        return (bm[node, 0] >> i) & 1
    return (bm[node, 1] >> (i - 64)) & 1


@njit(cache=True, nogil=True)
def _bm_set(bm, node, i):
    """位图置位：标记格 i 已展开。"""
    if i < 64:
        bm[node, 0] = bm[node, 0] | (np.int64(1) << i)
    else:
        bm[node, 1] = bm[node, 1] | (np.int64(1) << (i - 64))


@njit(cache=True, nogil=True)
def _expand(P, node, free):
    """在 free 槽位创建 node 的下一子节点（真随机展开顺序：
    从剩余未展开合法步中均匀随机选一个，与 Python 版 randrange 语义一致）。
    P = (cells, grids, forced, turn, parent, first_child, next_sib,
         visits, quality, move, legal_count, n_children, bm)
    返回新节点索引。"""
    cells, grids, forced, turn, parent, first_child, next_sib, \
        visits, quality, move, legal_count, n_children, bm = P
    remaining = legal_count[node] - n_children[node]
    if remaining <= 0:
        return node
    k = int(np.random.random() * remaining)
    # 扫描：合法（空格 + 大格未决）且未展开 → 定位第 k 个
    cnt = 0
    pick = -1
    if forced[node] >= 0 and grids[node, forced[node]] == 0:
        base = forced[node] * 9
        for c in range(9):
            i = base + c
            if cells[node, i] == 0 and not _bm_test(bm, node, i):
                if cnt == k:
                    pick = i
                    break
                cnt += 1
    else:
        for s in range(9):
            if grids[node, s] == 0:
                base = s * 9
                for c in range(9):
                    i = base + c
                    if cells[node, i] == 0 and not _bm_test(bm, node, i):
                        if cnt == k:
                            pick = i
                            break
                        cnt += 1
    if pick < 0:                     # 防御：k 越界（正常路径不可达）
        return node                  # 当作无子可展，避免脏槽挂树
    _bm_set(bm, node, pick)
    # 拷贝父局面 + 落子
    for i in range(81):
        cells[free, i] = cells[node, i]
    for i in range(9):
        grids[free, i] = grids[node, i]
    sub = pick // 9
    cell = pick % 9
    t = turn[node]
    cells[free, pick] = t

    # 小格增量判定（只查包含该格的线，<=4 条）
    w = 0
    base = sub * 9
    for i in range(4):
        a = ai._CELL_LINES_ARR[cell, i, 0]
        if a < 0:
            break
        b = ai._CELL_LINES_ARR[cell, i, 1]
        d = ai._CELL_LINES_ARR[cell, i, 2]
        if cells[free, base + a] == t and cells[free, base + b] == t \
                and cells[free, base + d] == t:
            w = t
            break
    if w:
        grids[free, sub] = t
    else:
        full = True
        for i in range(9):
            if cells[free, base + i] == 0:
                full = False
                break
        if full:
            grids[free, sub] = 3

    forced[free] = cell if grids[free, cell] == 0 else -1
    turn[free] = 3 - t
    parent[free] = node
    move[free, 0] = sub
    move[free, 1] = cell
    visits[free] = 0
    quality[free] = 0.0
    first_child[free] = -1
    next_sib[free] = first_child[node]
    first_child[node] = free
    n_children[node] += 1
    n_children[free] = 0          # D17：必须清零——reset 复用池时残留旧值会阻止展开
    legal_count[free] = _count_legal(cells[free], grids[free], forced[free])
    bm[free, 0] = 0
    bm[free, 1] = 0
    return free


@njit(cache=True, nogil=True)
def _tree_policy(P, root, free_arr, cap):
    """从 root 下行：有未展开合法步则展开（容量满则返回当前节点做 rollout），
    否则 UCB1 选子，直到终局节点。返回叶节点索引。"""
    cells, grids, forced, turn, parent, first_child, next_sib, \
        visits, quality, move, legal_count, n_children, bm = P
    node = root
    while True:
        w = _big_winner(grids[node])
        if w:
            return node
        if n_children[node] < legal_count[node]:
            if free_arr[0] < cap:
                free_arr[0] = free_arr[0] + 1
                return _expand(P, node, free_arr[0] - 1)
            return node                    # 容量满：不再展开，直接 rollout
        # UCB1 选子（子节点 visits>=1：展开后必经一次 backup）
        best = -1
        best_score = -1e18
        ln_n = math.log(max(visits[node], 1))
        ch = first_child[node]
        while ch >= 0:
            if visits[ch] > 0:
                score = quality[ch] / visits[ch] \
                    + ai.C_UCB * math.sqrt(ln_n / visits[ch])
                if score > best_score:
                    best = ch
                    best_score = score
            ch = next_sib[ch]
        if best < 0:                  # 防御：无子可走（理论不可达）
            return node
        node = best


@njit(cache=True, nogil=True)
def _backup(P, node, root, reward):
    """沿 parent 链回传（含 root）。reward 每层取反（行动方交替）。"""
    visits, quality = P[7], P[8]
    parent = P[4]
    while True:
        visits[node] += 1
        quality[node] += reward
        reward = -reward
        if node == root:
            break
        node = parent[node]


@njit(cache=True, nogil=True)
def _mcts_batch(P, root, goal, batch, free_arr, cap, stats):
    """njit 内跑 batch 次迭代。返回实际迭代数。
    stats: int64[3] 终局分布计数（0=圈赢 1=平 2=叉赢），供前端实时胜率条。"""
    iters = 0
    while iters < batch:
        node = _tree_policy(P, root, free_arr, cap)
        w = _big_winner(P[1][node])
        if w:                                  # 叶子即终局
            if w == 1:
                stats[0] += 1
            elif w == 2:
                stats[2] += 1
            else:
                stats[1] += 1
            reward = 0 if w == 3 else goal
        else:
            cells = P[0][node].copy()
            grids = P[1][node].copy()
            mover = P[3][node]
            reward = _rollout_bb_g(
                cells, grids, P[2][node], mover, mover, goal)
            # P5 约定：rollout 返回 +goal 当行动方（mover）落败——
            # reward == -goal ⇔ mover 赢；reward == goal ⇔ 对方赢（曾写反导致胜率镜像，见 DEBUG_LOG D23）
            if reward == -goal:                # mover 赢
                if mover == 1:
                    stats[0] += 1
                else:
                    stats[2] += 1
            elif reward == goal:               # 对方赢（mover 落败）
                if mover == 1:
                    stats[2] += 1
                else:
                    stats[0] += 1
            else:
                stats[1] += 1
        _backup(P, node, root, reward)
        iters += 1
    return iters


@njit(cache=True, nogil=True)
def _best_child(P, node):
    """UCB1 最优子节点（与 ai._best_child 同公式）。返回索引或 -1。"""
    visits, quality = P[7], P[8]
    first_child, next_sib = P[5], P[6]
    best = -1
    best_score = -1e18
    ln_n = math.log(max(visits[node], 1))
    ch = first_child[node]
    while ch >= 0:
        if visits[ch] > 0:
            score = quality[ch] / visits[ch] \
                + ai.C_UCB * math.sqrt(ln_n / visits[ch])
            if score > best_score:
                best = ch
                best_score = score
        ch = next_sib[ch]
    return best


@njit(cache=True, nogil=True)
def _winning_child(P, node, turn):
    """必胜手优先：返回落子后直接获胜的子节点（仅求胜模式调用）。"""
    grids, first_child, next_sib = P[1], P[5], P[6]
    ch = first_child[node]
    while ch >= 0:
        if _big_winner(grids[ch]) == turn:
            return ch
        ch = next_sib[ch]
    return -1


@njit(cache=True, nogil=True)
def _find_child(P, node, sub, cell):
    """在 node 的子链中查找落子 (sub, cell) 的节点。返回索引或 -1。"""
    move, first_child, next_sib = P[0 + 9], P[5], P[6]
    ch = first_child[node]
    while ch >= 0:
        if move[ch, 0] == sub and move[ch, 1] == cell:
            return ch
        ch = next_sib[ch]
    return -1


@njit(cache=True, nogil=True)
def _new_root(P, cells, grids, forced, turn, free_arr):
    """在 free 槽位建立新局面根节点。返回索引。"""
    i = free_arr[0]
    free_arr[0] = i + 1
    cells_p, grids_p, forced_p, turn_p, parent, first_child, next_sib, \
        visits, quality, move, legal_count, n_children, bm = P
    for j in range(81):
        cells_p[i, j] = cells[j]
    for j in range(9):
        grids_p[i, j] = grids[j]
    forced_p[i] = forced
    turn_p[i] = turn
    parent[i] = -1
    first_child[i] = -1
    next_sib[i] = -1
    visits[i] = 0
    quality[i] = 0.0
    move[i, 0] = -1
    move[i, 1] = -1
    n_children[i] = 0
    legal_count[i] = _count_legal(cells_p[i], grids_p[i], forced)
    bm[i, 0] = 0
    bm[i, 1] = 0
    return i


class MCTSTree:
    """numpy 节点池 + 搜索入口。线程安全：单线程使用（pywebview 调用线程）。"""

    def __init__(self, cap=NODE_CAP):
        self.cap = cap
        self.cells = np.zeros((cap, 81), dtype=np.int8)
        self.grids = np.zeros((cap, 9), dtype=np.int8)
        self.forced = np.full(cap, -1, dtype=np.int8)
        self.turn = np.zeros(cap, dtype=np.int8)
        self.parent = np.full(cap, -1, dtype=np.int32)
        self.first_child = np.full(cap, -1, dtype=np.int32)
        self.next_sib = np.full(cap, -1, dtype=np.int32)
        self.visits = np.zeros(cap, dtype=np.int32)
        self.quality = np.zeros(cap, dtype=np.float64)
        self.move = np.full((cap, 2), -1, dtype=np.int8)
        self.legal_count = np.zeros(cap, dtype=np.int32)
        self.n_children = np.zeros(cap, dtype=np.int32)
        self.bm = np.zeros((cap, 2), dtype=np.int64)
        self.free = np.zeros(1, dtype=np.int64)
        self.stats = np.zeros(3, dtype=np.int64)   # 终局分布（圈/平/叉），search 后更新
        self.root = -1

    @property
    def P(self):
        return (self.cells, self.grids, self.forced, self.turn, self.parent,
                self.first_child, self.next_sib, self.visits, self.quality,
                self.move, self.legal_count, self.n_children, self.bm)

    def _grow(self):
        """扩容节点池（重新分配并拷贝，Python 侧搜索间隙调用）。"""
        new_cap = self.cap + GROW_STEP

        def g2(a):
            new = np.zeros((new_cap, a.shape[1]), dtype=a.dtype)
            new[:a.shape[0]] = a
            return new

        def g1(a, fill=0):
            new = np.full(new_cap, fill, dtype=a.dtype)
            new[:a.shape[0]] = a
            return new

        self.cells = g2(self.cells)
        self.grids = g2(self.grids)
        self.move = g2(self.move)
        self.forced = g1(self.forced, -1)
        self.turn = g1(self.turn)
        self.parent = g1(self.parent, -1)
        self.first_child = g1(self.first_child, -1)
        self.next_sib = g1(self.next_sib, -1)
        self.visits = g1(self.visits)
        self.quality = g1(self.quality)
        self.legal_count = g1(self.legal_count)
        self.n_children = g1(self.n_children)
        self.bm = g2(self.bm)
        self.cap = new_cap

    def reset(self):
        """丢弃整棵树（free 归零）。"""
        self.free[0] = 0
        self.root = -1


def warmup():
    """触发全部 njit 编译（int8 池路径 + rollout int8 签名），供启动线程调用。
    注意编译顺序：必须先编译简单函数（_best_child 等），再编译复杂函数
    （_mcts_batch）——实测反向顺序会触发 numba 编译卡死（见 DEBUG_LOG D16）。"""
    while not precompile_ready():
        warmup_next()
    return None


# 逐步预热（供主菜单引擎状态条使用）：按 D16 安全顺序，每步编译一个函数
_WARMUP_STATE = {"done": 0, "ready": False}

# (函数名, 权重)——权重按编译耗时粗略设定，进度条更贴近真实进度
_WARMUP_STEPS = [
    ("_count_legal", 1),
    ("_kth_legal", 1),
    ("_big_winner", 1),
    ("_backup", 1),
    ("_best_child", 1),
    ("_winning_child", 1),
    ("_find_child", 1),
    ("_expand", 2),
    ("_tree_policy", 2),
    ("_mcts_batch", 4),
]
_WARMUP_TOTAL = sum(w for _, w in _WARMUP_STEPS)


def _warmup_env():
    """构造预热用的最小树环境（线程内独立，避免共享状态）。"""
    t = MCTSTree(cap=1024)
    cells = np.zeros(81, dtype=np.int8)
    grids = np.zeros(9, dtype=np.int8)
    t.root = _new_root(t.P, cells, grids, -1, 1, t.free)
    return t, cells, grids


def precompile_ready():
    return _WARMUP_STATE["ready"]


def precompile_progress():
    """返回 0-100 的编译进度（就绪后恒 100）。"""
    st = _WARMUP_STATE
    if st["ready"]:
        return 100
    return int(st["done"] * 100 / _WARMUP_TOTAL)


def warmup_next():
    """编译下一步（线程安全：GIL 保护简单赋值）。返回 (progress, ready)。"""
    st = _WARMUP_STATE
    if not _HAS_NUMBA:
        st["ready"] = True
        return 100, True
    if st["ready"]:
        return 100, True
    step = st["done"]
    if step >= len(_WARMUP_STEPS):
        st["ready"] = True
        return 100, True
    t, cells, grids = _warmup_env()
    name = _WARMUP_STEPS[step][0]
    if name == "_count_legal":
        _count_legal(cells, grids, -1)
    elif name == "_kth_legal":
        _kth_legal(cells, grids, -1, 0)
    elif name == "_big_winner":
        _big_winner(grids)
    elif name == "_backup":
        _backup(t.P, t.root, t.root, 1)
    elif name == "_best_child":
        _best_child(t.P, t.root)
    elif name == "_winning_child":
        _winning_child(t.P, t.root, 1)
    elif name == "_find_child":
        _find_child(t.P, t.root, 0, 0)
    elif name == "_expand":
        _expand(t.P, t.root, 1)
    elif name == "_tree_policy":
        _tree_policy(t.P, t.root, t.free, t.cap)
    elif name == "_mcts_batch":
        _mcts_batch(t.P, t.root, 1, 8, t.free, t.cap, t.stats)
    st["done"] = step + 1
    if step + 1 >= len(_WARMUP_STEPS):
        st["ready"] = True
    return precompile_progress(), st["ready"]


def search(cells, grids, forced, turn, budget, goal=1, root=None, iters=None, threads=1):
    """为 turn 方搜索最佳落子（数组化 MCTS）。

    参数与 ai.search 兼容：cells 9x9 嵌套/81 扁平、grids 9 个、
    forced int|None、turn 1/2、budget 秒、goal ±1、
    root: MCTSTree（复用）或 None。
    iters: 固定迭代数模式（测试/对比用）——指定后忽略 budget。
    threads: 并行搜索线程数（root parallelization：每线程独立树，
    主树保留复用，从树每手重建；合并按子节点总访问数投票）。

    返回 (move, tree)：move 为 (sub, cell) 或 None；tree 可传入下一手复用。
    """
    if not _HAS_NUMBA:                        # pragma: no cover
        return ai.search(cells, grids, forced, turn, budget, goal, root)

    cells_arr = np.asarray(cells, dtype=np.int8).reshape(-1)
    grids_arr = np.asarray(grids, dtype=np.int8).reshape(-1)
    forced_i = -1 if forced is None else int(forced)
    turn_i = int(turn)

    if threads > 1 and iters is not None and iters > 5000:
        return _search_parallel(cells_arr, grids_arr, forced_i, turn_i,
                                budget, goal, root, iters, threads)

    # 树复用校验：局面一致才复用，否则重建根
    reuse = (root is not None and root.root >= 0
             and root.turn[root.root] == turn_i
             and root.forced[root.root] == forced_i
             and np.array_equal(root.cells[root.root], cells_arr)
             and np.array_equal(root.grids[root.root], grids_arr))
    if not reuse:
        if root is None:
            root = MCTSTree()
        else:
            root.reset()
        root.root = _new_root(root.P, cells_arr, grids_arr,
                              forced_i, turn_i, root.free)

    # 容量管理：余量不足则重建根（树复用收益边际，重建仅损失步间继承）
    if root.cap - root.free[0] < RECYCLE_MARGIN:
        root.reset()
        root.root = _new_root(root.P, cells_arr, grids_arr,
                              forced_i, turn_i, root.free)
    root.stats[:] = 0                      # 本次搜索的终局分布

    if iters is not None:                      # 固定迭代数模式（对比/难度档位用）
        max_it = int(iters)
        # budget 作为软时间上限（难度档位兜底慢电脑）；
        # budget<=0（测试/对比场景）表示不限时（D21：0.0 曾导致立即超时 0 迭代）
        deadline = time.monotonic() + (budget if budget > 0 else 1e9)
    else:
        max_it = MAX_ITERATIONS
        deadline = time.monotonic() + budget
    total = 0
    while total < max_it and time.monotonic() < deadline:
        # 容量管理：余量不足则重建根继续搜（树复用收益边际，重建仅损失步间继承）
        if root.cap - root.free[0] < BATCH:
            root.reset()
            root.root = _new_root(root.P, cells_arr, grids_arr,
                                  forced_i, turn_i, root.free)
        b = min(BATCH, max_it - total)
        total += _mcts_batch(root.P, root.root, goal, b, root.free, root.cap, root.stats)

    # 必胜手优先（仅求胜模式；与 ai.search 语义一致）
    if goal > 0:
        wch = _winning_child(root.P, root.root, turn_i)
        if wch >= 0:
            # 一步必胜：胜率条直接显示该方 100%（rollout 随机模拟会低估必胜）
            root.stats[:] = 0
            if turn_i == 1:
                root.stats[0] = total
            else:
                root.stats[2] = total
            return (int(root.move[wch, 0]), int(root.move[wch, 1])), root
    bch = _best_child(root.P, root.root)
    if bch < 0:
        return None, root
    return (int(root.move[bch, 0]), int(root.move[bch, 1])), root


def _search_parallel(cells_arr, grids_arr, forced_i, turn_i, budget, goal,
                     root, iters, threads):
    """并行搜索（root parallelization）：主线程跑主树（树复用），
    threads-1 个从树线程各搜 iters/threads 迭代；结束时按子节点总访问数
    合并选步，stats 求和。从树跨 search 池化复用（_SLAVE_POOL）。"""
    import threading

    # 主树：复用或重建（逻辑与 search 一致）
    reuse = (root is not None and root.root >= 0
             and root.turn[root.root] == turn_i
             and root.forced[root.root] == forced_i
             and np.array_equal(root.cells[root.root], cells_arr)
             and np.array_equal(root.grids[root.root], grids_arr))
    if not reuse:
        if root is None:
            root = MCTSTree()
        else:
            root.reset()
        root.root = _new_root(root.P, cells_arr, grids_arr,
                              forced_i, turn_i, root.free)
    if root.cap - root.free[0] < RECYCLE_MARGIN:
        root.reset()
        root.root = _new_root(root.P, cells_arr, grids_arr,
                              forced_i, turn_i, root.free)
    root.stats[:] = 0

    max_it = int(iters)
    deadline = time.monotonic() + (budget if budget > 0 else 1e9)
    per = max(1, max_it // threads)
    main_it = max_it - per * (threads - 1)
    results = []

    def slave_worker(idx, it):
        """从树线程：独立树 + 独立随机种子，跑 it 迭代。
        从树跨 search 池化复用（数组重分配是主要开销，reset 后重建根即可）。"""
        cap = max(65536, min(NODE_CAP, it + 65536))   # 节点数上限 = 迭代数
        with _SLAVE_POOL_LOCK:
            while len(_SLAVE_POOL) <= idx:
                _SLAVE_POOL.append(None)
            tree = _SLAVE_POOL[idx]
            if tree is None or tree.cap < cap:
                tree = MCTSTree(cap=cap)
                _SLAVE_POOL[idx] = tree
        tree.reset()
        tree.root = _new_root(tree.P, cells_arr, grids_arr,
                              forced_i, turn_i, tree.free)
        _seed_rng(0x9E3779B9 + idx * 0x85EBCA6B)   # 每线程独立随机流
        total = 0
        while total < it and time.monotonic() < deadline:
            if tree.cap - tree.free[0] < BATCH:
                tree.reset()
                tree.root = _new_root(tree.P, cells_arr, grids_arr,
                                      forced_i, turn_i, tree.free)
            b = min(BATCH, it - total)
            total += _mcts_batch(tree.P, tree.root, goal, b,
                                 tree.free, tree.cap, tree.stats)
        results.append(tree)

    threads_list = []
    for i in range(threads - 1):
        t = threading.Thread(target=slave_worker, args=(i, per), daemon=True)
        t.start()
        threads_list.append(t)

    # 主线程跑主树（树复用，stats 累计到 root.stats）
    total = 0
    while total < main_it and time.monotonic() < deadline:
        if root.cap - root.free[0] < BATCH:
            root.reset()
            root.root = _new_root(root.P, cells_arr, grids_arr,
                                  forced_i, turn_i, root.free)
        b = min(BATCH, main_it - total)
        total += _mcts_batch(root.P, root.root, goal, b,
                             root.free, root.cap, root.stats)
    for t in threads_list:
        t.join()

    # 合并 stats（全部线程的终局分布）
    for tree in results:
        root.stats += tree.stats

    # 必胜手优先（仅求胜模式）
    if goal > 0:
        wch = _winning_child(root.P, root.root, turn_i)
        if wch >= 0:
            root.stats[:] = 0
            if turn_i == 1:
                root.stats[0] = total
            else:
                root.stats[2] = total
            return (int(root.move[wch, 0]), int(root.move[wch, 1])), root

    # 合并选步：各树同 move 子节点总访问数最大者（root parallelization 投票）
    best_move = None
    best_visits = -1
    ch = root.first_child[root.root]
    while ch >= 0:
        v = int(root.visits[ch])
        for tree in results:
            idx = _find_child(tree.P, tree.root,
                              int(root.move[ch, 0]), int(root.move[ch, 1]))
            if idx >= 0:
                v += int(tree.visits[idx])
        if v > best_visits:
            best_visits = v
            best_move = (int(root.move[ch, 0]), int(root.move[ch, 1]))
        ch = root.next_sib[ch]
    if best_move is None:
        return None, root
    return best_move, root


def find_child(tree, move):
    """树复用：把 root 提升到落子 move 对应的子节点。返回 tree 或 None。"""
    if tree is None or move is None or tree.root < 0:
        return None
    idx = _find_child(tree.P, tree.root, int(move[0]), int(move[1]))
    if idx < 0:
        return None
    tree.root = idx
    return tree


@njit(cache=True, nogil=True)
def _seed_rng(seed):
    """njit 内重置 numba 随机流（测试/复现用；生产不需要）。"""
    np.random.seed(seed)


# 并行从树池（跨 search 复用，避免每次重分配 27MB 数组；锁保护懒创建）
_SLAVE_POOL = []
_SLAVE_POOL_LOCK = threading.Lock()


# ============================================================
# 位板 rollout（_rollout_bb）：81 格 → 2×uint64 位掩码
# SWAR popcount + de Bruijn ctz 全部 njit 内联（无函数调用开销）
# ============================================================
_SUB_MASK = np.zeros((9, 2), dtype=np.uint64)        # [sub] 大格 81 位掩码
for _s in range(9):
    for _c in range(9):
        _i = _s * 9 + _c
        _SUB_MASK[_s, _i // 64] |= np.uint64(1) << np.uint64(_i % 64)

_LINE_MASK = np.zeros((9, 9, 4, 2), dtype=np.uint64)  # [sub][cell][line] 棋盘 flat 线掩码
for _s in range(9):
    for _c in range(9):
        for _li, _ln in enumerate(ai._CELL_LINES[_c]):
            for _rel in _ln:
                _i = _s * 9 + _rel
                _LINE_MASK[_s, _c, _li, _i // 64] |= np.uint64(1) << np.uint64(_i % 64)

_POPCOUNT8_TABLE = np.zeros(256, dtype=np.uint8)
for _i in range(256):
    _POPCOUNT8_TABLE[_i] = bin(_i).count('1')
_DEBRUIJN = np.uint64(0x03F79D71B4CB0A89)
_CTZ_TABLE = np.zeros(64, dtype=np.uint8)            # de Bruijn ctz 查表
for _i in range(64):
    _CTZ_TABLE[int((_DEBRUIJN << np.uint64(_i)) >> np.uint64(58))] = _i


_POPCOUNT8_TABLE = np.zeros(256, dtype=np.uint8)
for _i in range(256):
    _POPCOUNT8_TABLE[_i] = bin(_i).count('1')


@njit(inline='always', cache=True)
def _xorshift(state):
    """xorshift64* 快速随机（rollout 每步 1 次，替代 np.random 调用开销）。"""
    state ^= state << np.uint64(13)
    state ^= state >> np.uint64(7)
    state ^= state << np.uint64(17)
    return state * np.uint64(0x2545F4914F6CDD1D)


@njit(inline='always', cache=True)
def _popcount8(x):
    """低 8 位 popcount（查表）。"""
    return int(_POPCOUNT8_TABLE[x & np.uint64(0xFF)])


@njit(cache=True, nogil=True)
def _popcount64(x):
    """SWAR popcount（uint64 → int）。"""
    x = x - ((x >> np.uint64(1)) & np.uint64(0x5555555555555555))
    x = (x & np.uint64(0x3333333333333333)) + \
        ((x >> np.uint64(2)) & np.uint64(0x3333333333333333))
    x = (x + (x >> np.uint64(4))) & np.uint64(0x0F0F0F0F0F0F0F0F)
    return int((x * np.uint64(0x0101010101010101)) >> np.uint64(56))


@njit(cache=True, nogil=True)
def _kth64(x, k):
    """64 位掩码内第 k 个置位的位号（de Bruijn ctz，清位循环）。k 越界返回 -1。"""
    cnt = 0
    while x != 0:
        low = x & (-x)
        if cnt == k:
            return int(_CTZ_TABLE[int((low * _DEBRUIJN) >> np.uint64(58))])
        x &= x - np.uint64(1)
        cnt += 1
    return -1


@njit(cache=True, nogil=True)
def _rollout_bb(cells, grids, forced, turn, mover, goal):
    """位板版 rollout：与 _rollout_numba 逻辑完全等价（同 seed 同结果），
    但合法步计数/选择/线检查全部位运算（SWAR popcount + de Bruijn ctz）。"""
    # cells int8[81] → 位板
    bm_c0 = np.uint64(0)
    bm_c1 = np.uint64(0)
    bm_x0 = np.uint64(0)
    bm_x1 = np.uint64(0)
    for i in range(81):
        v = cells[i]
        if v == 1:
            if i < 64:
                bm_c0 |= np.uint64(1) << np.uint64(i)
            else:
                bm_c1 |= np.uint64(1) << np.uint64(i - 64)
        elif v == 2:
            if i < 64:
                bm_x0 |= np.uint64(1) << np.uint64(i)
            else:
                bm_x1 |= np.uint64(1) << np.uint64(i - 64)
    decided = 0
    for i in range(9):
        if grids[i] != 0:
            decided += 1
    # 快速随机状态：从 np.random 取 53 位种子（保持与旧版同源可复现），
    # 之后每步用 xorshift（内联，无 np.random 调用开销）
    rng = np.uint64(np.random.random() * 9007199254740992.0)         ^ np.uint64(0x9E3779B97F4A7C15)
    while True:
        empty0 = ~(bm_c0 | bm_x0)
        empty1 = ~(bm_c1 | bm_x1)
        if forced >= 0 and grids[forced] == 0:
            legal0 = empty0 & _SUB_MASK[forced, 0]
            legal1 = empty1 & _SUB_MASK[forced, 1]
        else:
            legal0 = np.uint64(0)
            legal1 = np.uint64(0)
            for s in range(9):
                if grids[s] == 0:
                    legal0 |= empty0 & _SUB_MASK[s, 0]
                    legal1 |= empty1 & _SUB_MASK[s, 1]
        n = _popcount64(legal0) + _popcount64(legal1)
        if n == 0:                      # 无合法步且无胜者：平局
            return 0
        rng = _xorshift(rng)
        # 取高 32 位再模 n（xorshift 低位质量差；uint64 乘 n 截断后 >>64 在 x86 上
        # 等于不移位——会导致 k 越界死循环，见 DEBUG_LOG D22）
        k = int((rng >> np.uint64(32)) % np.uint64(n))
        n0 = _popcount64(legal0)
        if k < n0:
            pick = _kth64(legal0, k)
        else:
            pick = 64 + _kth64(legal1, k - n0)
        sub = pick // 9
        cell = pick % 9
        # 落子
        if turn == 1:
            if pick < 64:
                bm_c0 |= np.uint64(1) << np.uint64(pick)
            else:
                bm_c1 |= np.uint64(1) << np.uint64(pick - 64)
        else:
            if pick < 64:
                bm_x0 |= np.uint64(1) << np.uint64(pick)
            else:
                bm_x1 |= np.uint64(1) << np.uint64(pick - 64)
        # 小格判定：落子格的线掩码是否全部被 turn 占据
        w = 0
        for li in range(4):
            m0 = _LINE_MASK[sub, cell, li, 0]
            m1 = _LINE_MASK[sub, cell, li, 1]
            if m0 == 0 and m1 == 0:
                break
            if turn == 1:
                ok = (bm_c0 & m0) == m0 and (bm_c1 & m1) == m1
            else:
                ok = (bm_x0 & m0) == m0 and (bm_x1 & m1) == m1
            if ok:
                w = turn
                break
        if w:
            grids[sub] = turn
            decided += 1
        else:
            # 小格满判定：sub 掩码内全部被占
            if ((bm_c0 | bm_x0) & _SUB_MASK[sub, 0]) == _SUB_MASK[sub, 0] \
                    and ((bm_c1 | bm_x1) & _SUB_MASK[sub, 1]) == _SUB_MASK[sub, 1]:
                grids[sub] = 3
                decided += 1
        # 大棋盘判定（与 _expand 相同的内联逻辑）
        if grids[sub] == turn:
            for i in range(4):
                a = ai._GRID_LINES_ARR[sub, i, 0]
                if a < 0:
                    break
                b = ai._GRID_LINES_ARR[sub, i, 1]
                d = ai._GRID_LINES_ARR[sub, i, 2]
                if grids[a] == turn and grids[b] == turn and grids[d] == turn:
                    if turn == mover:
                        return -goal
                    return goal
        if decided == 9:
            return 0
        forced = cell if grids[cell] == 0 else -1
        turn = 3 - turn


@njit(inline='always', cache=True)
def _pick_kth(m0, m1, k):
    """从 (m0,m1) 两段掩码选第 k 个置位（k < popcount 总数）。"""
    n0 = _popcount64(m0)
    if k < n0:
        return _kth64(m0, k)
    return 64 + _kth64(m1, k - n0)

@njit(cache=True, nogil=True)
def _rollout_bb_g(cells, grids, forced, turn, mover, goal):
    """greedy-1 位板 rollout：增量维护'差一格成线'缺口集（我方/对方各 2×uint64），
    每步优先走'立即赢'格 → 其次'防立即输'格 → 否则随机。
    与 _rollout_bb 唯一差异 = 选步启发（落子/判定/终局逻辑完全相同）。
    缺口集冗余无害：查询时 & legal 过滤已占/已决出格。"""
    bm_c0 = np.uint64(0)
    bm_c1 = np.uint64(0)
    bm_x0 = np.uint64(0)
    bm_x1 = np.uint64(0)
    for i in range(81):
        v = cells[i]
        if v == 1:
            if i < 64:
                bm_c0 |= np.uint64(1) << np.uint64(i)
            else:
                bm_c1 |= np.uint64(1) << np.uint64(i - 64)
        elif v == 2:
            if i < 64:
                bm_x0 |= np.uint64(1) << np.uint64(i)
            else:
                bm_x1 |= np.uint64(1) << np.uint64(i - 64)
    decided = 0
    for i in range(9):
        if grids[i] != 0:
            decided += 1
    # 初始化缺口集：扫描已占格的 4 线（重复 OR 无害）
    my_gap0 = np.uint64(0)
    my_gap1 = np.uint64(0)
    op_gap0 = np.uint64(0)
    op_gap1 = np.uint64(0)
    empty0 = ~(bm_c0 | bm_x0)
    empty1 = ~(bm_c1 | bm_x1)
    for i in range(81):
        v = cells[i]
        if v != 0:
            s0 = i // 9
            if grids[s0] != 0:
                continue
            c0 = i % 9
            for li in range(4):
                m0 = _LINE_MASK[s0, c0, li, 0]
                m1 = _LINE_MASK[s0, c0, li, 1]
                if m0 == 0 and m1 == 0:
                    break
                n_c = _popcount64(bm_c0 & m0) + _popcount64(bm_c1 & m1)
                n_x = _popcount64(bm_x0 & m0) + _popcount64(bm_x1 & m1)
                if n_c == 2 and n_x == 0:
                    my_gap0 |= empty0 & m0
                    my_gap1 |= empty1 & m1
                elif n_x == 2 and n_c == 0:
                    op_gap0 |= empty0 & m0
                    op_gap1 |= empty1 & m1
    rng = np.uint64(np.random.random() * 9007199254740992.0) \
        ^ np.uint64(0x9E3779B97F4A7C15)
    while True:
        empty0 = ~(bm_c0 | bm_x0)
        empty1 = ~(bm_c1 | bm_x1)
        if forced >= 0 and grids[forced] == 0:
            legal0 = empty0 & _SUB_MASK[forced, 0]
            legal1 = empty1 & _SUB_MASK[forced, 1]
        else:
            legal0 = np.uint64(0)
            legal1 = np.uint64(0)
            for s in range(9):
                if grids[s] == 0:
                    legal0 |= empty0 & _SUB_MASK[s, 0]
                    legal1 |= empty1 & _SUB_MASK[s, 1]
        n = _popcount64(legal0) + _popcount64(legal1)
        if n == 0:
            return 0
        rng = _xorshift(rng)
        # 残局回退：只剩 <=2 个未决大格时启发价值低（树已深），且检查开销占比大
        # ——直接走纯随机路径（缺口查询/更新全部跳过）
        late = decided >= 7
        # 启发选格：立即赢 → 防立即输 → 随机
        if not late and turn == 1:
            w0 = my_gap0 & legal0
            w1 = my_gap1 & legal1
            d0 = op_gap0 & legal0
            d1 = op_gap1 & legal1
        else:
            w0 = op_gap0 & legal0
            w1 = op_gap1 & legal1
            d0 = my_gap0 & legal0
            d1 = my_gap1 & legal1
        nw = _popcount64(w0) + _popcount64(w1)
        nd = _popcount64(d0) + _popcount64(d1)
        if not late and nw > 0:
            k = int((rng >> np.uint64(32)) % np.uint64(nw))
            pick = _pick_kth(w0, w1, k)
        elif not late and nd > 0:
            k = int((rng >> np.uint64(32)) % np.uint64(nd))
            pick = _pick_kth(d0, d1, k)
        else:
            k = int((rng >> np.uint64(32)) % np.uint64(n))
            pick = _pick_kth(legal0, legal1, k)
        sub = pick // 9
        cell = pick % 9
        # 落子
        if turn == 1:
            if pick < 64:
                bm_c0 |= np.uint64(1) << np.uint64(pick)
            else:
                bm_c1 |= np.uint64(1) << np.uint64(pick - 64)
        else:
            if pick < 64:
                bm_x0 |= np.uint64(1) << np.uint64(pick)
            else:
                bm_x1 |= np.uint64(1) << np.uint64(pick - 64)
        # 更新缺口集 + 小格判定（同一 4 线循环）
        w = 0
        for li in range(4):
            m0 = _LINE_MASK[sub, cell, li, 0]
            m1 = _LINE_MASK[sub, cell, li, 1]
            if m0 == 0 and m1 == 0:
                break
            if turn == 1:
                if not late:
                    n_c = _popcount64(bm_c0 & m0) + _popcount64(bm_c1 & m1)
                    n_x = _popcount64(bm_x0 & m0) + _popcount64(bm_x1 & m1)
                    if n_c == 2 and n_x == 0:
                        my_gap0 |= empty0 & m0
                        my_gap1 |= empty1 & m1
                if (bm_c0 & m0) == m0 and (bm_c1 & m1) == m1:
                    w = turn
            else:
                if not late:
                    n_c = _popcount64(bm_c0 & m0) + _popcount64(bm_c1 & m1)
                    n_x = _popcount64(bm_x0 & m0) + _popcount64(bm_x1 & m1)
                    if n_x == 2 and n_c == 0:
                        op_gap0 |= empty0 & m0
                        op_gap1 |= empty1 & m1
                if (bm_x0 & m0) == m0 and (bm_x1 & m1) == m1:
                    w = turn
        if w:
            grids[sub] = turn
            decided += 1
        else:
            if ((bm_c0 | bm_x0) & _SUB_MASK[sub, 0]) == _SUB_MASK[sub, 0] \
                    and ((bm_c1 | bm_x1) & _SUB_MASK[sub, 1]) == _SUB_MASK[sub, 1]:
                grids[sub] = 3
                decided += 1
        if grids[sub] == turn:
            for i in range(4):
                a = ai._GRID_LINES_ARR[sub, i, 0]
                if a < 0:
                    break
                b = ai._GRID_LINES_ARR[sub, i, 1]
                d = ai._GRID_LINES_ARR[sub, i, 2]
                if grids[a] == turn and grids[b] == turn and grids[d] == turn:
                    if turn == mover:
                        return -goal
                    return goal
        if decided == 9:
            return 0
        forced = cell if grids[cell] == 0 else -1
        turn = 3 - turn
"""超级井字棋（Ultimate Tic-Tac-Toe）核心规则引擎。

纯逻辑实现，不依赖任何 GUI 库，可直接单元测试。

数据模型
--------
cells[sub][cell] : 小格内 9 个位置。0=空, 1=圈, 2=叉
grids[sub]       : 大格状态。0=未决出, 1=圈占领, 2=叉占领, 3=平
forced           : int|None。下一手被强制落子的大格；None 表示可在所有未决出大格自由落子
turn             : 当前行动方。1=圈, 2=叉
winner           : 0=进行中, 1=圈胜, 2=叉胜, 3=平局
win_line         : 大棋盘三连的三个大格下标（获胜时非 None）
"""

from __future__ import annotations

EMPTY, CIRCLE, CROSS = 0, 1, 2
GRID_OPEN, GRID_CIRCLE, GRID_CROSS, GRID_TIE = 0, 1, 2, 3

WIN_LINES = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
)


def line_winner(states: list) -> tuple:
    """states: 长度为 9 的序列（格值 0/1/2 或大格值 0/1/2/3）。
    只有圈(1)/叉(2)能构成三连，平局值(3)不算。返回 (赢家, 三连下标元组) 或 (0, None)。"""
    for a, b, c in WIN_LINES:
        s = states[a]
        if s in (CIRCLE, CROSS) and s == states[b] == states[c]:
            return s, (a, b, c)
    return 0, None


class Game:
    """一盘超级井字棋。落子前用 legal_moves() 校验，非法落子一律返回 False。"""

    def __init__(self):
        self.cells = [[EMPTY] * 9 for _ in range(9)]
        self.grids = [GRID_OPEN] * 9
        self.turn = CIRCLE
        self.forced: int | None = None
        self.last_move: tuple[int, int] | None = None
        self.winner = 0
        self.win_line = None

    # ------------------------------------------------------------------ 查询
    def undecided_grids(self) -> list:
        return [i for i in range(9) if self.grids[i] == GRID_OPEN]

    def legal_moves(self) -> list:
        """当前全部合法落子 [(sub, cell), ...]。"""
        if self.winner:
            return []
        subs = self.undecided_grids()
        if self.forced is not None and self.grids[self.forced] == GRID_OPEN:
            subs = [self.forced]
        moves = []
        for s in subs:
            row = self.cells[s]
            for c in range(9):
                if row[c] == EMPTY:
                    moves.append((s, c))
        return moves

    def is_over(self) -> bool:
        return self.winner != 0

    # ------------------------------------------------------------------ 落子
    def apply_move(self, sub: int, cell: int) -> bool:
        """落子并推进局面。非法返回 False（局面不变）。"""
        if self.winner:
            return False
        if not (0 <= sub < 9 and 0 <= cell < 9):
            return False
        if self.cells[sub][cell] != EMPTY or self.grids[sub] != GRID_OPEN:
            return False
        if self.forced is not None and self.grids[self.forced] == GRID_OPEN \
                and sub != self.forced:
            return False

        self.cells[sub][cell] = self.turn
        self.last_move = (sub, cell)

        # 小格状态
        w, _ = line_winner(self.cells[sub])
        if w:
            self.grids[sub] = w
        elif all(self.cells[sub]):
            self.grids[sub] = GRID_TIE

        # 大棋盘胜负 / 平局
        w, line = line_winner(self.grids)
        if w:
            self.winner, self.win_line = w, line
        elif all(g != GRID_OPEN for g in self.grids):
            self.winner = GRID_TIE

        if not self.winner:
            # 下一手的强制格 = 本次落子的小格序号；该大格已决出则自由落子
            self.forced = cell if self.grids[cell] == GRID_OPEN else None
            self.turn = CROSS if self.turn == CIRCLE else CIRCLE
        return True

    # ------------------------------------------------------------------ 快照
    def snapshot(self) -> tuple:
        """不可变快照，供 AI 搜索使用。"""
        return (tuple(tuple(r) for r in self.cells), tuple(self.grids),
                self.forced, self.turn)

    def restore(self, snap: tuple) -> None:
        cells, grids, forced, turn = snap
        self.cells = [list(r) for r in cells]
        self.grids = list(grids)
        self.forced = forced
        self.turn = turn
        self.last_move = None
        self.winner = 0
        self.win_line = None
        w, line = line_winner(self.grids)
        if w:
            self.winner, self.win_line = w, line
        elif all(g != GRID_OPEN for g in self.grids):
            self.winner = GRID_TIE

//! 超级井字棋（Ultimate Tic-Tac-Toe）规则引擎。
//! 语义与 Python 版 super_ttt/engine.py 逐条对齐（tests/test_engine.py 的
//! 全部用例已移植为 Rust 单元测试）。
//!
//! 值约定：格 0=空 1=圈 2=叉；大格 0=未决出 1=圈 2=叉 3=平。

#![allow(dead_code)]

pub const EMPTY: u8 = 0;
pub const CIRCLE: u8 = 1;
pub const CROSS: u8 = 2;
pub const GRID_OPEN: u8 = 0;
pub const GRID_TIE: u8 = 3;

pub const WIN_LINES: [[usize; 3]; 8] = [
    [0, 1, 2], [3, 4, 5], [6, 7, 8],
    [0, 3, 6], [1, 4, 7], [2, 5, 8],
    [0, 4, 8], [2, 4, 6],
];

/// 哨兵：线表不足 4 条时填充（合法相对下标 0..=8）
const SENT: u8 = 9;

const fn build_line_tables() -> ([[[u8; 3]; 4]; 9], [usize; 9]) {
    let mut out = [[[SENT, SENT, SENT]; 4]; 9];
    let mut counts = [2usize; 9];
    let mut c = 0;
    while c < 9 {
        let r = c / 3;
        let col = c % 3;
        out[c][0] = [(3 * r) as u8, (3 * r + 1) as u8, (3 * r + 2) as u8];
        out[c][1] = [col as u8, (col + 3) as u8, (col + 6) as u8];
        let mut n = 2;
        if r == col {
            out[c][n] = [0, 4, 8];
            n += 1;
        }
        if r + col == 2 {
            out[c][n] = [2, 4, 6];
            n += 1;
        }
        counts[c] = n;
        c += 1;
    }
    (out, counts)
}

/// CELL_LINES[c][li] = 包含小格 c 的线（相对下标，<=4 条，哨兵填充）
pub const CELL_LINES: [[[u8; 3]; 4]; 9] = build_line_tables().0;
pub const CELL_LINE_COUNT: [usize; 9] = build_line_tables().1;
/// 大棋盘与 3x3 几何相同，直接复用
pub const GRID_LINES: [[[u8; 3]; 4]; 9] = CELL_LINES;
pub const GRID_LINE_COUNT: [usize; 9] = CELL_LINE_COUNT;

/// 大棋盘胜负：返回 1/2（三连）、3（全部决出无胜者）、0（进行中）。
#[inline]
pub fn big_winner(grids: &[u8; 9]) -> u8 {
    for [a, b, c] in WIN_LINES {
        let s = grids[a];
        if s != 0 && s != GRID_TIE && s == grids[b] && s == grids[c] {
            return s;
        }
    }
    for g in grids {
        if *g == GRID_OPEN {
            return 0;
        }
    }
    GRID_TIE
}

/// 全量线检查（与 Python line_winner 等价）：返回 (赢家, 三连下标)。
pub fn line_winner(states: &[u8; 9]) -> (u8, Option<(u8, u8, u8)>) {
    for [a, b, c] in WIN_LINES {
        let s = states[a];
        if (s == CIRCLE || s == CROSS) && s == states[b] && s == states[c] {
            return (s, Some((a as u8, b as u8, c as u8)));
        }
    }
    (0, None)
}

#[derive(Clone, Copy)]
pub struct Game {
    pub cells: [[u8; 9]; 9],
    pub grids: [u8; 9],
    pub turn: u8,
    /// -1 = 自由落子（对应 Python None）
    pub forced: i8,
    pub last_move: Option<(u8, u8)>,
    pub winner: u8,
    pub win_line: Option<(u8, u8, u8)>,
}

impl Default for Game {
    fn default() -> Self {
        Self::new()
    }
}

impl Game {
    pub const fn new() -> Self {
        Game {
            cells: [[EMPTY; 9]; 9],
            grids: [GRID_OPEN; 9],
            turn: CIRCLE,
            forced: -1,
            last_move: None,
            winner: 0,
            win_line: None,
        }
    }

    pub fn is_over(&self) -> bool {
        self.winner != 0
    }

    /// 当前全部合法落子（sub-major 顺序，与 Python 版一致）。
    pub fn legal_moves(&self) -> Vec<(u8, u8)> {
        let mut moves = Vec::new();
        if self.winner != 0 {
            return moves;
        }
        let forced_sub: Option<usize> = if self.forced >= 0
            && self.grids[self.forced as usize] == GRID_OPEN
        {
            Some(self.forced as usize)
        } else {
            None
        };
        let subs: Vec<usize> = match forced_sub {
            Some(s) => vec![s],
            None => (0..9).filter(|&s| self.grids[s] == GRID_OPEN).collect(),
        };
        for s in subs {
            for c in 0..9 {
                if self.cells[s][c] == EMPTY {
                    moves.push((s as u8, c as u8));
                }
            }
        }
        moves
    }

    /// 落子并推进局面。非法返回 false（局面不变）。语义与 Python apply_move 对齐。
    pub fn apply_move(&mut self, sub: usize, cell: usize) -> bool {
        if self.winner != 0 {
            return false;
        }
        if sub >= 9 || cell >= 9 {
            return false;
        }
        if self.cells[sub][cell] != EMPTY || self.grids[sub] != GRID_OPEN {
            return false;
        }
        if self.forced >= 0
            && self.grids[self.forced as usize] == GRID_OPEN
            && sub as i8 != self.forced
        {
            return false;
        }

        self.cells[sub][cell] = self.turn;
        self.last_move = Some((sub as u8, cell as u8));

        // 小格状态
        let w = line_winner(&self.cells[sub]).0;
        if w != 0 {
            self.grids[sub] = w;
        } else if self.cells[sub].iter().all(|&v| v != EMPTY) {
            self.grids[sub] = GRID_TIE;
        }

        // 大棋盘胜负 / 平局
        let (w, line) = line_winner(&self.grids);
        if w != 0 {
            self.winner = w;
            self.win_line = line;
        } else if self.grids.iter().all(|&g| g != GRID_OPEN) {
            self.winner = GRID_TIE;
        }

        if self.winner == 0 {
            self.forced = if self.grids[cell] == GRID_OPEN {
                cell as i8
            } else {
                -1
            };
            self.turn = if self.turn == CIRCLE { CROSS } else { CIRCLE };
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn initial_81_legal_moves() {
        let g = Game::new();
        assert_eq!(g.legal_moves().len(), 81);
        assert_eq!(g.turn, CIRCLE);
        assert_eq!(g.forced, -1);
        assert!(!g.is_over());
    }

    #[test]
    fn forced_grid() {
        let mut g = Game::new();
        assert!(g.apply_move(0, 3));
        assert_eq!(g.forced, 3);
        assert_eq!(g.turn, CROSS);
        let moves = g.legal_moves();
        assert_eq!(moves.len(), 9);
        assert!(moves.iter().all(|m| m.0 == 3));
    }

    #[test]
    fn free_play_after_decided_target() {
        let mut g = Game::new();
        g.cells[3] = [CIRCLE; 9];
        g.grids[3] = CIRCLE;
        assert!(g.apply_move(0, 3));
        assert_eq!(g.forced, -1);
        assert_eq!(g.legal_moves().len(), 71);
    }

    #[test]
    fn illegal_moves() {
        let mut g = Game::new();
        assert!(g.apply_move(0, 0));
        assert!(!g.apply_move(1, 0)); // 强制格是 0
        assert_eq!(g.turn, CROSS);
        assert!(g.apply_move(0, 5));
        assert!(!g.apply_move(0, 5)); // 重复落子
        assert!(g.apply_move(5, 0));
        assert!(!g.apply_move(0, 5)); // 仍被占

        let mut g2 = Game::new();
        g2.cells[2] = [CIRCLE; 9];
        g2.grids[2] = CIRCLE;
        g2.forced = 2;
        assert!(!g2.apply_move(2, 0)); // 已决出大格不可落子
        assert!(g2.apply_move(0, 0));
    }

    #[test]
    fn big_board_win_and_tie() {
        let mut g = Game::new();
        for sub in [0usize, 4, 8] {
            g.cells[sub] = [CIRCLE; 9];
            g.grids[sub] = CIRCLE;
        }
        // 模拟 restore 重算
        let (w, line) = line_winner(&g.grids);
        g.winner = w;
        g.win_line = line;
        assert_eq!(g.winner, CIRCLE);
        assert_eq!(g.win_line, Some((0, 4, 8)));
        assert!(g.is_over());
        assert!(g.legal_moves().is_empty());

        let mut t = Game::new();
        t.grids = [GRID_TIE; 9];
        assert_eq!(big_winner(&t.grids), GRID_TIE);

        // 平局值 3 不构成三连
        let mut nt = Game::new();
        nt.grids[0] = GRID_TIE;
        nt.grids[1] = GRID_TIE;
        nt.grids[2] = GRID_TIE;
        assert_eq!(big_winner(&nt.grids), 0);
    }

    #[test]
    fn subgrid_full_without_winner_is_tie() {
        let mut g = Game::new();
        g.cells[5] = [0, CIRCLE, CROSS, CROSS, CROSS, CIRCLE, CIRCLE, CROSS, CIRCLE];
        assert!(g.apply_move(5, 0));
        assert_eq!(g.grids[5], GRID_TIE);
    }
}

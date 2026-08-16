//! 全数组化 MCTS（Rust 移植版，自 super_ttt/mcts.py numba 实现）。
//!
//! 与 Python 版的关键差异（性能压榨点，语义不变）：
//! - 节点池直接存位板（每节点 4×u64 石子掩码）而非 81 字节数组——
//!   rollout 零转换启动，节点拷贝 48 字节；
//! - 合法步计数 / 选择 / 线检查全部位运算（popcnt / ctz 硬件指令）；
//! - 热路径（tree_policy / expand / backup / rollout）无边界检查。
//!
//! 算法语义逐条对齐 Python 版：
//! - UCB1 c=0.8，best_child 取 quality/visits + c·sqrt(lnN/n) 最大；
//! - 收益约定：rollout 返回 +goal 当叶子行动方落败；backup 逐层取反；
//! - 展开顺序：从剩余未展开合法步中均匀随机选一个（与 Python randrange 语义一致）；
//! - greedy-1 rollout：立即赢 → 防立即输 → 随机；decided>=7 残局回退纯随机；
//! - 必胜手优先（仅 goal>0）；树复用两步提升；root parallelization 投票合并。

use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::engine::{
    big_winner, CELL_LINES, CELL_LINE_COUNT, GRID_LINES, GRID_LINE_COUNT,
};

pub const C_UCB: f64 = 0.8;
pub const NODE_CAP: usize = 524_288;
pub const BATCH: usize = 512;
pub const MAX_ITERATIONS: u64 = 2_000_000;
pub const RECYCLE_MARGIN: usize = 65_536;

// ---------------------------------------------------------------- 位板常量
// flat 位 i = sub*9 + cell；字 0 = 位 0..63，字 1 = 位 64..80

const fn build_sub_masks() -> [(u64, u64); 9] {
    let mut m = [(0u64, 0u64); 9];
    let mut s = 0;
    while s < 9 {
        let mut c = 0;
        while c < 9 {
            let i = s * 9 + c;
            if i < 64 {
                m[s].0 |= 1u64 << i;
            } else {
                m[s].1 |= 1u64 << (i - 64);
            }
            c += 1;
        }
        s += 1;
    }
    m
}

/// SUB_MASK[s] = 大格 s 的 81 位掩码
pub const SUB_MASK: [(u64, u64); 9] = build_sub_masks();

const fn build_line_masks() -> [[[(u64, u64); 4]; 9]; 9] {
    let mut lm = [[[(0u64, 0u64); 4]; 9]; 9];
    let mut sub = 0;
    while sub < 9 {
        let mut cell = 0;
        while cell < 9 {
            let mut li = 0;
            while li < 4 {
                let mut k = 0;
                while k < 3 {
                    let rel = CELL_LINES[cell][li][k] as usize;
                    if rel < 9 {
                        let i = sub * 9 + rel;
                        if i < 64 {
                            lm[sub][cell][li].0 |= 1u64 << i;
                        } else {
                            lm[sub][cell][li].1 |= 1u64 << (i - 64);
                        }
                    }
                    k += 1;
                }
                li += 1;
            }
            cell += 1;
        }
        sub += 1;
    }
    lm
}

/// LINE_MASK[sub][cell][li] = 小格 (sub,cell) 所在线的 flat 掩码
pub const LINE_MASK: [[[(u64, u64); 4]; 9]; 9] = build_line_masks();

const fn build_open_masks() -> [(u64, u64); 512] {
    let mut t = [(0u64, 0u64); 512];
    let mut p = 0;
    while p < 512 {
        let mut m0 = 0u64;
        let mut m1 = 0u64;
        let mut s = 0;
        while s < 9 {
            if p >> s & 1 == 1 {
                m0 |= SUB_MASK[s].0;
                m1 |= SUB_MASK[s].1;
            }
            s += 1;
        }
        t[p] = (m0, m1);
        p += 1;
    }
    t
}

/// OPEN_MASK[开放位图] = 所有开放大格的合并 81 位掩码。
/// 位图位 s = 大格 s 未决出；自由落子的合法域一次查表即可。
pub const OPEN_MASK: [(u64, u64); 512] = build_open_masks();

/// 大格开放位图：位 s = grids[s]==0
#[inline(always)]
fn open_pattern(grids: &[u8; 9]) -> u16 {
    let mut p = 0u16;
    for s in 0..9 {
        p |= ((grids[s] == 0) as u16) << s;
    }
    p
}

// ---------------------------------------------------------------- 工具

/// xorshift64*：纯函数形式（返回乘积，内部状态由调用方持有）
#[inline(always)]
fn xs64(state: u64) -> u64 {
    let mut x = state;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    x.wrapping_mul(0x2545_F491_4F6C_DD1D)
}

/// BMI2 pdep 硬件指令可用性（启动后首次探测缓存）
#[cfg(target_arch = "x86_64")]
fn bmi2() -> bool {
    use std::sync::OnceLock;
    static BMI2: OnceLock<bool> = OnceLock::new();
    *BMI2.get_or_init(|| std::arch::is_x86_feature_detected!("bmi2"))
}

/// 掩码内第 k 个置位的位号（调用方保证 k < popcount）。
/// x86_64 + BMI2：pdep 单指令（把 1<<k 押到 x 的第 k 个置位上）；
/// 否则回退循环清位。
#[inline(always)]
fn kth64(x: u64, k: u64) -> usize {
    #[cfg(target_arch = "x86_64")]
    {
        if bmi2() {
            // SAFETY: bmi2 已探测；k < popcount(x) ≤ 64 由调用方保证
            let bit = unsafe { std::arch::x86_64::_pdep_u64(1u64 << k, x) };
            debug_assert!(bit != 0);
            return bit.trailing_zeros() as usize;
        }
    }
    kth64_loop(x, k)
}

#[inline(always)]
fn kth64_loop(mut x: u64, k: u64) -> usize {
    let mut c = 0u64;
    while x != 0 {
        let low = x & x.wrapping_neg();
        if c == k {
            return low.trailing_zeros() as usize;
        }
        x ^= low;
        c += 1;
    }
    0 // 不可达：k < popcount 由调用方保证
}

#[inline(always)]
fn pop2(w0: u64, w1: u64) -> u64 {
    w0.count_ones() as u64 + w1.count_ones() as u64
}

/// Lemire 乘移位取 [0,n)：k = (rng 高 32 位 × n) >> 32，替代除法取模
#[inline(always)]
fn mul_hi(rng: u64, n: u64) -> u64 {
    ((rng >> 32).wrapping_mul(n)) >> 32
}

/// (w0,w1) 两段掩码内第 k 个置位的位号（调用方保证 k < pop2）
#[inline(always)]
fn kth_bit(w0: u64, w1: u64, k: u64) -> usize {
    let n0 = w0.count_ones() as u64;
    if k < n0 {
        kth64(w0, k)
    } else {
        64 + kth64(w1, k - n0)
    }
}

/// 合法步掩码：forced 指向开放大格 → 仅该大格空位；否则开放大格并集（查表）
#[inline(always)]
fn legal_masks(c0: u64, c1: u64, x0: u64, x1: u64, open: u16, forced: i8) -> (u64, u64) {
    let e0 = !(c0 | x0);
    let e1 = !(c1 | x1);
    if forced >= 0 && (open >> forced) & 1 == 1 {
        let (m0, m1) = SUB_MASK[forced as usize];
        (e0 & m0, e1 & m1)
    } else {
        let (m0, m1) = OPEN_MASK[open as usize];
        (e0 & m0, e1 & m1)
    }
}

// ---------------------------------------------------------------- 局面

/// 搜索输入局面（位板形式，Copy）
#[derive(Clone, Copy)]
pub struct Pos {
    pub c0: u64,
    pub c1: u64,
    pub x0: u64,
    pub x1: u64,
    pub grids: [u8; 9],
    pub forced: i8,
    pub turn: u8,
}

impl Pos {
    /// cells: 81 扁平（sub*9+cell），值 0/1/2
    pub fn from_flat(cells: &[u8], grids: &[u8], forced: i8, turn: u8) -> Pos {
        let mut p = Pos {
            c0: 0, c1: 0, x0: 0, x1: 0,
            grids: [0; 9],
            forced, turn,
        };
        p.grids.copy_from_slice(&grids[..9]);
        for i in 0..81 {
            match cells[i] {
                1 => {
                    if i < 64 { p.c0 |= 1u64 << i; } else { p.c1 |= 1u64 << (i - 64); }
                }
                2 => {
                    if i < 64 { p.x0 |= 1u64 << i; } else { p.x1 |= 1u64 << (i - 64); }
                }
                _ => {}
            }
        }
        p
    }
}

// ---------------------------------------------------------------- 节点池

pub struct Pool {
    pub cap: usize,
    pub free: usize,
    pub root: i32,
    // 局面（位板）
    c0: Vec<u64>, c1: Vec<u64>, x0: Vec<u64>, x1: Vec<u64>,
    grids: Vec<[u8; 9]>,
    forced: Vec<i8>,
    turn: Vec<u8>,
    // 树结构：头插链表（消融实验 E1 证实 arena 连续布局因内存足迹膨胀
    // 反而慢 40-60%，链表的时间局部性已足够——见 PROGRESS.md 消融表）
    parent: Vec<i32>,
    first_child: Vec<i32>,
    next_sib: Vec<i32>,
    // 统计
    visits: Vec<i32>,
    quality: Vec<f64>,
    mv_sub: Vec<u8>,
    mv_cell: Vec<u8>,
    legal_count: Vec<i32>,
    n_children: Vec<i32>,
    bm0: Vec<u64>,
    bm1: Vec<u64>,
    // 搜索产物
    pub stats: [i64; 3],   // [圈赢, 平, 叉赢]
    pub done: u64,         // 本次 search 实际迭代数
    rng: u64,
}

impl Pool {
    pub fn new(cap: usize) -> Pool {
        Pool::with_seed(cap, seed_from_time())
    }

    pub fn with_seed(cap: usize, seed: u64) -> Pool {
        Pool {
            cap, free: 0, root: -1,
            c0: vec![0; cap], c1: vec![0; cap], x0: vec![0; cap], x1: vec![0; cap],
            grids: vec![[0; 9]; cap],
            forced: vec![-1; cap],
            turn: vec![0; cap],
            parent: vec![-1; cap],
            first_child: vec![-1; cap],
            next_sib: vec![-1; cap],
            visits: vec![0; cap],
            quality: vec![0.0; cap],
            mv_sub: vec![u8::MAX; cap],
            mv_cell: vec![u8::MAX; cap],
            legal_count: vec![0; cap],
            n_children: vec![0; cap],
            bm0: vec![0; cap],
            bm1: vec![0; cap],
            stats: [0; 3],
            done: 0,
            rng: seed | 1,
        }
    }

    /// 丢弃整棵树（free 归零）。stats/done 由 search 入口管理。
    pub fn recycle(&mut self) {
        self.free = 0;
        self.root = -1;
    }

    fn matches_root(&self, pos: &Pos) -> bool {
        self.root >= 0
            && self.turn[self.root as usize] == pos.turn
            && self.forced[self.root as usize] == pos.forced
            && self.grids[self.root as usize] == pos.grids
            && self.c0[self.root as usize] == pos.c0
            && self.c1[self.root as usize] == pos.c1
            && self.x0[self.root as usize] == pos.x0
            && self.x1[self.root as usize] == pos.x1
    }

    /// 在 free 槽位建立局面根节点
    fn new_root(&mut self, pos: &Pos) {
        let i = self.free;
        self.free += 1;
        self.c0[i] = pos.c0;
        self.c1[i] = pos.c1;
        self.x0[i] = pos.x0;
        self.x1[i] = pos.x1;
        self.grids[i] = pos.grids;
        self.forced[i] = pos.forced;
        self.turn[i] = pos.turn;
        self.parent[i] = -1;
        self.first_child[i] = -1;
        self.next_sib[i] = -1;
        self.visits[i] = 0;
        self.quality[i] = 0.0;
        self.mv_sub[i] = u8::MAX;
        self.mv_cell[i] = u8::MAX;
        self.n_children[i] = 0;
        self.bm0[i] = 0;
        self.bm1[i] = 0;
        let (l0, l1) = legal_masks(pos.c0, pos.c1, pos.x0, pos.x1,
                                  open_pattern(&pos.grids), pos.forced);
        self.legal_count[i] = pop2(l0, l1) as i32;
        self.root = i as i32;
    }

    /// 在 slot 槽位创建 node 的下一子节点（真随机展开：剩余未展开合法步中均匀选一）。
    /// slot 由调用方在自增前取出（Python 版语义：free_arr[0]-1），
    /// 与池容量边界严格隔离。
    #[inline]
    fn expand(&mut self, node: usize, slot: usize) -> usize {
        let free = slot;
        debug_assert!(free < self.cap);

        let (l0, l1) = legal_masks(
            unsafe { *self.c0.get_unchecked(node) },
            unsafe { *self.c1.get_unchecked(node) },
            unsafe { *self.x0.get_unchecked(node) },
            unsafe { *self.x1.get_unchecked(node) },
            open_pattern(unsafe { &*self.grids.get_unchecked(node) }),
            unsafe { *self.forced.get_unchecked(node) },
        );
        let a0 = l0 & !unsafe { *self.bm0.get_unchecked(node) };
        let a1 = l1 & !unsafe { *self.bm1.get_unchecked(node) };
        let remaining = pop2(a0, a1);
        debug_assert!(remaining > 0);
        self.rng = xs64(self.rng);
        let k = mul_hi(self.rng, remaining);
        let pick = kth_bit(a0, a1, k);
        if pick < 64 {
            self.bm0[node] |= 1u64 << pick;
        } else {
            self.bm1[node] |= 1u64 << (pick - 64);
        }

        // 拷贝父局面 + 落子
        unsafe {
            *self.c0.get_unchecked_mut(free) = *self.c0.get_unchecked(node);
            *self.c1.get_unchecked_mut(free) = *self.c1.get_unchecked(node);
            *self.x0.get_unchecked_mut(free) = *self.x0.get_unchecked(node);
            *self.x1.get_unchecked_mut(free) = *self.x1.get_unchecked(node);
            *self.grids.get_unchecked_mut(free) = *self.grids.get_unchecked(node);
        }
        let sub = pick / 9;
        let cell = pick % 9;
        let t = self.turn[node];
        if t == 1 {
            if pick < 64 {
                self.c0[free] |= 1u64 << pick;
            } else {
                self.c1[free] |= 1u64 << (pick - 64);
            }
        } else {
            if pick < 64 {
                self.x0[free] |= 1u64 << pick;
            } else {
                self.x1[free] |= 1u64 << (pick - 64);
            }
        }

        // 小格增量判定（只查包含该格的线，<=4 条）
        let mut w = 0u8;
        let nlines = CELL_LINE_COUNT[cell];
        for li in 0..nlines {
            let (m0, m1) = LINE_MASK[sub][cell][li];
            let ok = if t == 1 {
                (self.c0[free] & m0) == m0 && (self.c1[free] & m1) == m1
            } else {
                (self.x0[free] & m0) == m0 && (self.x1[free] & m1) == m1
            };
            if ok {
                w = t;
                break;
            }
        }
        if w != 0 {
            self.grids[free][sub] = w;
        } else {
            let occ0 = self.c0[free] | self.x0[free];
            let occ1 = self.c1[free] | self.x1[free];
            if (occ0 & SUB_MASK[sub].0) == SUB_MASK[sub].0
                && (occ1 & SUB_MASK[sub].1) == SUB_MASK[sub].1
            {
                self.grids[free][sub] = 3;
            }
        }

        self.forced[free] = if self.grids[free][cell] == 0 { cell as i8 } else { -1 };
        self.turn[free] = 3 - t;
        self.parent[free] = node as i32;
        self.mv_sub[free] = sub as u8;
        self.mv_cell[free] = cell as u8;
        self.visits[free] = 0;
        self.quality[free] = 0.0;
        self.first_child[free] = -1;
        self.next_sib[free] = self.first_child[node];
        self.first_child[node] = free as i32;
        self.n_children[node] += 1;
        self.n_children[free] = 0;
        self.bm0[free] = 0;
        self.bm1[free] = 0;
        let g = self.grids[free];
        let f = self.forced[free];
        let (cl0, cl1) = legal_masks(
            self.c0[free], self.c1[free], self.x0[free], self.x1[free],
            open_pattern(&g), f,
        );
        self.legal_count[free] = pop2(cl0, cl1) as i32;
        free
    }

    /// 从 root 下行：有未展开合法步则展开（容量满则返回当前节点），
    /// 否则 UCB1 选子，直到终局节点。返回叶节点索引。
    #[inline]
    fn tree_policy(&mut self, root: usize) -> usize {
        let mut node = root;
        loop {
            if big_winner(unsafe { &*self.grids.get_unchecked(node) }) != 0 {
                return node;
            }
            let lc = unsafe { *self.legal_count.get_unchecked(node) } as usize;
            let nc = unsafe { *self.n_children.get_unchecked(node) } as usize;
            if nc < lc {
                if self.free < self.cap {
                    let slot = self.free;   // 槽位必须在自增前取出（边界语义同 Python free_arr[0]-1）
                    self.free += 1;
                    return self.expand(node, slot);
                }
                return node; // 容量满：不再展开，直接 rollout
            }
            // UCB1 选子（子节点 visits>=1：展开后必经一次 backup）
            let ln_n = (unsafe { *self.visits.get_unchecked(node) }.max(1) as f64).ln();
            let mut best: i32 = -1;
            let mut best_score = f64::NEG_INFINITY;
            let mut ch = unsafe { *self.first_child.get_unchecked(node) };
            while ch >= 0 {
                let c = ch as usize;
                let v = unsafe { *self.visits.get_unchecked(c) };
                if v > 0 {
                    let vf = v as f64;
                    let score = unsafe { *self.quality.get_unchecked(c) } / vf
                        + C_UCB * (ln_n / vf).sqrt();
                    if score > best_score {
                        best = ch;
                        best_score = score;
                    }
                }
                ch = unsafe { *self.next_sib.get_unchecked(c) };
            }
            if best < 0 {
                return node; // 防御：无子可走（理论不可达）
            }
            node = best as usize;
        }
    }

    /// 沿 parent 链回传（含 root）。reward 每层取反。
    #[inline]
    fn backup(&mut self, mut node: usize, root: usize, mut reward: f64) {
        loop {
            unsafe {
                *self.visits.get_unchecked_mut(node) += 1;
                *self.quality.get_unchecked_mut(node) += reward;
            }
            reward = -reward;
            if node == root {
                break;
            }
            node = unsafe { *self.parent.get_unchecked(node) } as usize;
        }
    }

    /// njit 版 _mcts_batch 等价：batch 次迭代 + 终局分布统计。
    fn mcts_batch(&mut self, root: usize, goal: i32, batch: usize) {
        for _ in 0..batch {
            let node = self.tree_policy(root);
            let w = big_winner(unsafe { &*self.grids.get_unchecked(node) });
            let reward: f64;
            if w != 0 {
                match w {
                    1 => self.stats[0] += 1,
                    2 => self.stats[2] += 1,
                    _ => self.stats[1] += 1,
                }
                reward = if w == 3 { 0.0 } else { goal as f64 };
            } else {
                // P5 约定：rollout 返回 +goal 当行动方（mover）落败
                let mover = unsafe { *self.turn.get_unchecked(node) };
                let r = self.rollout(node, goal);
                if r == -goal {
                    if mover == 1 { self.stats[0] += 1 } else { self.stats[2] += 1 }
                } else if r == goal {
                    if mover == 1 { self.stats[2] += 1 } else { self.stats[0] += 1 }
                } else {
                    self.stats[1] += 1;
                }
                reward = r as f64;
            }
            self.backup(node, root, reward);
        }
    }

    /// greedy-1 位板 rollout（Python _rollout_bb_g 的逐行移植）。
    /// 增量维护"差一格成线"缺口集：立即赢 → 防立即输 → 随机。
    fn rollout(&mut self, node: usize, goal: i32) -> i32 {
        let mut c0 = unsafe { *self.c0.get_unchecked(node) };
        let mut c1 = unsafe { *self.c1.get_unchecked(node) };
        let mut x0 = unsafe { *self.x0.get_unchecked(node) };
        let mut x1 = unsafe { *self.x1.get_unchecked(node) };
        let mut grids = unsafe { *self.grids.get_unchecked(node) };
        let mut forced = unsafe { *self.forced.get_unchecked(node) };
        let mut turn = unsafe { *self.turn.get_unchecked(node) };
        let mover = turn;
        let mut decided = grids.iter().filter(|&&g| g != 0).count() as u32;
        let mut open = open_pattern(&grids);

        // 初始化缺口集：扫描已占格的线（重复 OR 无害，查询时 & legal 过滤）
        let mut my_gap0 = 0u64; let mut my_gap1 = 0u64;
        let mut op_gap0 = 0u64; let mut op_gap1 = 0u64;
        let mut empty0 = !(c0 | x0);
        let mut empty1 = !(c1 | x1);
        // 已占格位迭代：只访问有棋子的格（中局约 30-40 格 vs 81 格全扫）。
        // 圈叉互斥，四个字各扫一遍无重复。
        macro_rules! scan_word {
            ($word:expr, $base:expr) => {{
                let mut w = $word;
                while w != 0 {
                    let low = w & w.wrapping_neg();
                    w ^= low;
                    let i = $base + low.trailing_zeros() as usize;
                    let s0 = i / 9;
                    if grids[s0] != 0 {
                        continue;
                    }
                    let c0i = i % 9;
                    for li in 0..CELL_LINE_COUNT[c0i] {
                        let (m0, m1) = LINE_MASK[s0][c0i][li];
                        let n_c = pop2(c0 & m0, c1 & m1);
                        let n_x = pop2(x0 & m0, x1 & m1);
                        if n_c == 2 && n_x == 0 {
                            my_gap0 |= empty0 & m0;
                            my_gap1 |= empty1 & m1;
                        } else if n_x == 2 && n_c == 0 {
                            op_gap0 |= empty0 & m0;
                            op_gap1 |= empty1 & m1;
                        }
                    }
                }
            }};
        }
        scan_word!(c0, 0);
        scan_word!(c1, 64);
        scan_word!(x0, 0);
        scan_word!(x1, 64);

        // mover 视角：mover_gap = 当前行动方的缺口集（回合翻转时交换，
        // 主循环内不再有 turn 分支——语义与圈/叉视角完全一致）
        let (mut mover_gap0, mut mover_gap1, mut opp_gap0, mut opp_gap1) =
            if turn == 1 {
                (my_gap0, my_gap1, op_gap0, op_gap1)
            } else {
                (op_gap0, op_gap1, my_gap0, my_gap1)
            };

        let mut rng = self.rng;
        let r;
        loop {
            empty0 = !(c0 | x0);
            empty1 = !(c1 | x1);
            let (legal0, legal1) = legal_masks(c0, c1, x0, x1, open, forced);
            let n = pop2(legal0, legal1);
            if n == 0 {
                r = 0; // 无合法步且无胜者：平局
                break;
            }
            rng = xs64(rng);
            // 残局回退：只剩 <=2 个未决大格时启发价值低，直接纯随机
            let late = decided >= 7;
            let (w0, w1, d0, d1) = if !late {
                (mover_gap0 & legal0, mover_gap1 & legal1,
                 opp_gap0 & legal0, opp_gap1 & legal1)
            } else {
                (0, 0, 0, 0)
            };
            let nw = pop2(w0, w1);
            let nd = pop2(d0, d1);
            let pick = if !late && nw > 0 {
                kth_bit(w0, w1, mul_hi(rng, nw))
            } else if !late && nd > 0 {
                kth_bit(d0, d1, mul_hi(rng, nd))
            } else {
                kth_bit(legal0, legal1, mul_hi(rng, n))
            };
            let sub = pick / 9;
            let cell = pick % 9;
            // 落子 + 每步一次的 me/op 掩码视角选择
            if turn == 1 {
                if pick < 64 { c0 |= 1u64 << pick; } else { c1 |= 1u64 << (pick - 64); }
            } else {
                if pick < 64 { x0 |= 1u64 << pick; } else { x1 |= 1u64 << (pick - 64); }
            }
            // me* = 行动方石子，op* = 对方（本步内所有线检查共用，无 turn 分支）
            let (me0, me1, op0, op1) = if turn == 1 {
                (c0, c1, x0, x1)
            } else {
                (x0, x1, c0, c1)
            };
            // 更新缺口集 + 小格判定（同一 <=4 线循环；empty 为落子前的值，与 Python 一致）
            let mut w = 0u8;
            let nlines = CELL_LINE_COUNT[cell];
            for li in 0..nlines {
                let (m0, m1) = LINE_MASK[sub][cell][li];
                let n_me = pop2(me0 & m0, me1 & m1);
                let n_op = pop2(op0 & m0, op1 & m1);
                if !late && n_me == 2 && n_op == 0 {
                    mover_gap0 |= empty0 & m0;
                    mover_gap1 |= empty1 & m1;
                }
                if (me0 & m0) == m0 && (me1 & m1) == m1 {
                    w = turn;
                }
            }
            if w != 0 {
                grids[sub] = turn;
                decided += 1;
                open &= !(1u16 << sub);
            } else if ((c0 | x0) & SUB_MASK[sub].0) == SUB_MASK[sub].0
                && ((c1 | x1) & SUB_MASK[sub].1) == SUB_MASK[sub].1
            {
                grids[sub] = 3;
                decided += 1;
                open &= !(1u16 << sub);
            }
            // 大棋盘判定（只查包含该大格的线）
            if grids[sub] == turn {
                let mut won = false;
                for li in 0..GRID_LINE_COUNT[sub] {
                    let l = GRID_LINES[sub][li];
                    let a = l[0] as usize;
                    let b = l[1] as usize;
                    let d = l[2] as usize;
                    if grids[a] == turn && grids[b] == turn && grids[d] == turn {
                        won = true;
                        break;
                    }
                }
                if won {
                    r = if turn == mover { -goal } else { goal };
                    break;
                }
            }
            if decided == 9 {
                r = 0;
                break;
            }
            forced = if grids[cell] == 0 { cell as i8 } else { -1 };
            turn = 3 - turn;
            std::mem::swap(&mut mover_gap0, &mut opp_gap0);
            std::mem::swap(&mut mover_gap1, &mut opp_gap1);
        }
        self.rng = rng;
        r
    }

    /// UCB1 最优子节点。返回 (sub, cell) 或 None。
    pub fn best_move(&self) -> Option<(u8, u8)> {
        let root = self.root as usize;
        let mut best: i32 = -1;
        let mut best_score = f64::NEG_INFINITY;
        let ln_n = (self.visits[root].max(1) as f64).ln();
        let mut ch = self.first_child[root];
        while ch >= 0 {
            let c = ch as usize;
            if self.visits[c] > 0 {
                let vf = self.visits[c] as f64;
                let score = self.quality[c] / vf + C_UCB * (ln_n / vf).sqrt();
                if score > best_score {
                    best = ch;
                    best_score = score;
                }
            }
            ch = self.next_sib[c];
        }
        if best < 0 {
            None
        } else {
            Some((self.mv_sub[best as usize], self.mv_cell[best as usize]))
        }
    }


    /// 必胜手优先：落子后直接获胜的子节点（仅求胜模式调用）。
    fn winning_child(&self, turn: u8) -> Option<(u8, u8)> {
        let root = self.root as usize;
        let mut ch = self.first_child[root];
        while ch >= 0 {
            let c = ch as usize;
            if big_winner(&self.grids[c]) == turn {
                return Some((self.mv_sub[c], self.mv_cell[c]));
            }
            ch = self.next_sib[c];
        }
        None
    }

    /// 树复用：把 root 提升到落子 move 对应的子节点。失败返回 None。
    pub fn find_child(&mut self, mv: (u8, u8)) -> Option<()> {
        if self.root < 0 {
            return None;
        }
        let mut ch = self.first_child[self.root as usize];
        while ch >= 0 {
            let c = ch as usize;
            if self.mv_sub[c] == mv.0 && self.mv_cell[c] == mv.1 {
                self.root = ch;
                return Some(());
            }
            ch = self.next_sib[c];
        }
        None
    }

    /// 在 root 的子链中查找落子对应节点索引（不动 root，合并投票用）。
    fn find_child_idx(&self, sub: u8, cell: u8) -> Option<usize> {
        if self.root < 0 {
            return None;
        }
        let mut ch = self.first_child[self.root as usize];
        while ch >= 0 {
            let c = ch as usize;
            if self.mv_sub[c] == sub && self.mv_cell[c] == cell {
                return Some(c);
            }
            ch = self.next_sib[c];
        }
        None
    }

    /// 搜索收尾：必胜手优先（stats 直接置该方 100%），否则 UCB1 最优子。
    fn finish(&mut self, goal: i32, turn: u8, total: u64) -> Option<(u8, u8)> {
        if goal > 0 {
            if let Some(mv) = self.winning_child(turn) {
                self.stats = [0, 0, 0];
                if turn == 1 {
                    self.stats[0] = total as i64;
                } else {
                    self.stats[2] = total as i64;
                }
                return Some(mv);
            }
        }
        self.best_move()
    }

    /// 单树搜索（树复用 + 容量管理 + 批次时间检查）。
    /// 返回最佳落子；stats/done 为本次搜索产物。
    pub fn search(&mut self, pos: &Pos, goal: i32, iters: u64, budget: f64) -> Option<(u8, u8)> {
        if !self.matches_root(pos) {
            self.recycle();
            self.new_root(pos);
        }
        if self.cap - self.free < RECYCLE_MARGIN {
            self.recycle();
            self.new_root(pos);
        }
        self.stats = [0; 3];
        self.done = 0;
        let deadline = Instant::now()
            + Duration::from_secs_f64(if budget > 0.0 { budget } else { 1e9 });
        self.run_batches(pos, goal, iters, deadline);
        self.finish(goal, pos.turn, self.done)
    }

    /// 批次循环：前提 root 有效、stats/done 已清零。
    /// 容量压力时就地重建根（stats/done 跨重建累计，与 Python 一致）。
    fn run_batches(&mut self, pos: &Pos, goal: i32, max_it: u64, deadline: Instant) {
        while self.done < max_it && Instant::now() < deadline {
            if self.cap - self.free < BATCH {
                self.recycle();
                self.new_root(pos);
            }
            let b = BATCH.min((max_it - self.done) as usize);
            let root = self.root as usize;
            self.mcts_batch(root, goal, b);
            self.done += b as u64;
        }
    }

    /// 并行 worker（从树）：重置后独立随机流跑 per 次迭代。
    fn run_slave(&mut self, pos: &Pos, goal: i32, per: u64, deadline: Instant) {
        self.recycle();
        self.new_root(pos);
        self.stats = [0; 3];
        self.done = 0;
        self.run_batches(pos, goal, per, deadline);
    }
}

fn seed_from_time() -> u64 {
    let t = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0x1234_5678);
    t ^ 0x9E37_79B9_7F4A_7C15 | 1
}

// ---------------------------------------------------------------- 并行从树池

static SLAVE_POOL: Mutex<Vec<Option<Pool>>> = Mutex::new(Vec::new());

fn take_slaves(n: usize, per: u64) -> Vec<Pool> {
    let need = NODE_CAP.min(per as usize + 65_536).max(65_536);
    let mut guard = SLAVE_POOL.lock().unwrap();
    while guard.len() < n {
        guard.push(None);
    }
    (0..n)
        .map(|i| {
            let slot = &mut guard[i];
            match slot.take() {
                Some(p) if p.cap >= need => p,
                _ => Pool::new(need),
            }
        })
        .collect()
}

fn return_slaves(slaves: Vec<Pool>) {
    let mut guard = SLAVE_POOL.lock().unwrap();
    for (i, p) in slaves.into_iter().enumerate() {
        if i < guard.len() {
            guard[i] = Some(p);
        }
    }
}

/// 并行搜索（root parallelization）：主树复用 + threads-1 从树，
/// 结束按子节点总访问数投票合并，stats 求和。
pub fn search_parallel(
    main: &mut Pool,
    pos: &Pos,
    goal: i32,
    iters: u64,
    budget: f64,
    threads: usize,
) -> Option<(u8, u8)> {
    if !main.matches_root(pos) {
        main.recycle();
        main.new_root(pos);
    }
    if main.cap - main.free < RECYCLE_MARGIN {
        main.recycle();
        main.new_root(pos);
    }
    main.stats = [0; 3];
    main.done = 0;

    let per = (iters / threads as u64).max(1);
    let main_it = iters - per * (threads - 1) as u64;
    let deadline = Instant::now()
        + Duration::from_secs_f64(if budget > 0.0 { budget } else { 1e9 });

    let mut slaves = take_slaves(threads - 1, per);
    std::thread::scope(|s| {
        for (i, p) in slaves.iter_mut().enumerate() {
            let seed = 0x9E37_79B9u64
                .wrapping_add((i as u64).wrapping_mul(0x85EB_CA6B))
                | 1;
            p.rng = seed;
            s.spawn(move || p.run_slave(pos, goal, per, deadline));
        }
        // 主线程跑主树（树复用：root 已在上方校验/重建）
        main.run_batches(pos, goal, main_it, deadline);
    });

    let mut total = main.done;
    for p in &slaves {
        main.stats[0] += p.stats[0];
        main.stats[1] += p.stats[1];
        main.stats[2] += p.stats[2];
        total += p.done;
    }
    main.done = total;

    // 必胜手优先（仅求胜模式）
    if goal > 0 {
        if let Some(mv) = main.winning_child(pos.turn) {
            main.stats = [0, 0, 0];
            if pos.turn == 1 {
                main.stats[0] = total as i64;
            } else {
                main.stats[2] = total as i64;
            }
            return_slaves(slaves);
            return Some(mv);
        }
    }

    // 合并选步：各树同 move 子节点总访问数最大者
    let mut best_mv: Option<(u8, u8)> = None;
    let mut best_visits: i64 = -1;
    let mut ch = main.first_child[main.root as usize];
    while ch >= 0 {
        let c = ch as usize;
        let sub = main.mv_sub[c];
        let cell = main.mv_cell[c];
        let mut v = main.visits[c] as i64;
        for p in &slaves {
            if let Some(idx) = p.find_child_idx(sub, cell) {
                v += p.visits[idx] as i64;
            }
        }
        if v > best_visits {
            best_visits = v;
            best_mv = Some((sub, cell));
        }
        ch = main.next_sib[c];
    }
    return_slaves(slaves);
    best_mv
}

/// 搜索入口（对齐 Python mcts.search 的调度逻辑）：
/// threads>1 且 iters>5000 → 并行；否则单树。
pub fn search_dispatch(
    main: &mut Pool,
    pos: &Pos,
    goal: i32,
    iters: u64,
    budget: f64,
    threads: usize,
) -> Option<(u8, u8)> {
    if threads > 1 && iters > 5000 {
        search_parallel(main, pos, goal, iters, budget, threads)
    } else {
        main.search(pos, goal, iters, budget)
    }
}

/// 难度档位 -> (迭代数, 软时间上限秒)。与 ai.DIFFICULTY_ITERS/CAPS 一致。
pub fn difficulty_for(d: i32) -> (u64, f64) {
    match d {
        -1 => (2_000, 1.0),
        0 => (8_000, 2.0),
        1 => (32_000, 3.5),
        2 => (128_000, 6.0),
        3 => (256_000, 12.0),
        _ => (2_000, 1.0),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn opening_pos() -> Pos {
        Pos::from_flat(&[0u8; 81], &[0u8; 9], -1, 1)
    }

    #[test]
    fn search_opening_returns_legal() {
        let mut p = Pool::new(65_536);
        let pos = opening_pos();
        let mv = p.search(&pos, 1, 5_000, 0.0);
        assert!(mv.is_some());
        assert!(p.done >= 4_900); // 不限时模式下应跑满
        assert!(p.stats[0] + p.stats[1] + p.stats[2] > 0);
    }

    #[test]
    fn search_midgame_after_moves() {
        // 两手棋后的局面（与 test_ai.test_returns_legal_move 同型）
        let mut g = crate::engine::Game::new();
        assert!(g.apply_move(0, 0));
        assert!(g.apply_move(0, 1));
        let flat: Vec<u8> = g.cells.iter().flat_map(|r| r.iter().copied()).collect();
        let pos = Pos::from_flat(&flat, &g.grids, g.forced, g.turn);
        let mut p = Pool::new(65_536);
        let mv = p.search(&pos, 1, 5_000, 0.0);
        assert!(mv.is_some());
        let legal: Vec<(u8, u8)> = g.legal_moves();
        assert!(legal.contains(&mv.unwrap()));
    }

    #[test]
    fn terminal_returns_none() {
        let mut p = Pool::new(4_096);
        let mut grids = [0u8; 9];
        grids[0] = 1; grids[4] = 1; grids[8] = 1;
        let pos = Pos::from_flat(&[0u8; 81], &grids, -1, 2);
        let mv = p.search(&pos, 1, 100, 0.0);
        assert_eq!(mv, None);
    }

    #[test]
    fn tree_reuse_and_find_child() {
        let mut p = Pool::new(65_536);
        let mut g = crate::engine::Game::new();
        assert!(g.apply_move(0, 0));
        let flat: Vec<u8> = g.cells.iter().flat_map(|r| r.iter().copied()).collect();
        let mut pos = Pos::from_flat(&flat, &g.grids, g.forced, g.turn);
        let mv = p.search(&pos, 1, 8_000, 0.0).unwrap();
        assert!(g.apply_move(mv.0 as usize, mv.1 as usize));
        let human = g.legal_moves()[0];
        assert!(g.apply_move(human.0 as usize, human.1 as usize));
        let flat: Vec<u8> = g.cells.iter().flat_map(|r| r.iter().copied()).collect();
        pos = Pos::from_flat(&flat, &g.grids, g.forced, g.turn);
        // 两步提升：AI 落子 → 人类落子
        assert!(p.find_child(mv).is_some());
        assert!(p.find_child(human).is_some());
        let visits_before = p.visits[p.root as usize];
        let mv2 = p.search(&pos, 1, 2_000, 0.0);
        assert!(mv2.is_some());
        // 复用真实生效：传入的根节点统计被继续使用（visits 增长）
        assert!(p.visits[p.root as usize] > visits_before);
    }

    #[test]
    fn parallel_matches_single_legality() {
        let mut g = crate::engine::Game::new();
        assert!(g.apply_move(4, 4));
        let flat: Vec<u8> = g.cells.iter().flat_map(|r| r.iter().copied()).collect();
        let pos = Pos::from_flat(&flat, &g.grids, g.forced, g.turn);
        let mut main = Pool::new(NODE_CAP);
        let mv = search_parallel(&mut main, &pos, 1, 16_000, 0.0, 4);
        assert!(mv.is_some());
        let legal: Vec<(u8, u8)> = g.legal_moves();
        assert!(legal.contains(&mv.unwrap()));
    }

    #[test]
    fn tiny_pool_boundary_regression() {
        // 回归：树池打满边界（free==cap-1 → expand）曾差一越界（2026-08-16 time 模式对弈触发）。
        // 小池 + 大迭代强制反复触碰边界与就地重建。
        let pos = opening_pos();
        let mut p = Pool::new(1_024);
        let mv = p.search(&pos, 1, 60_000, 0.0);
        assert!(mv.is_some());
        assert!(p.done >= 55_000);
    }

    #[test]
    fn rollout_reward_domain() {
        let mut p = Pool::new(4_096);
        let pos = opening_pos();
        p.recycle();
        p.new_root(&pos);
        for _ in 0..200 {
            let r = p.rollout(0, 1);
            assert!(r == -1 || r == 0 || r == 1, "reward out of domain: {r}");
        }
    }
}

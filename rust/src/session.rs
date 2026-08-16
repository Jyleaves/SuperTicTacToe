//! 游戏会话层：持有对局状态 + AI 树复用 + 异步胜率评估。
//! 语义对齐 Python super_ttt/server.py 的 Api 类（前端 JS 契约不变）。
//!
//! 锁协议：
//! - SESSION 保护对局状态与统计快照；ai_move 搜索期间持锁（前端 S.pending
//!   期间不会并发调用 play，stats() 轮询仅延迟不僵死）；
//! - EVAL_LOCK 串行化评估 worker（对齐 Python _eval_lock）；
//! - EVAL_BUSY 原子量供 stats() 无锁读取。锁序：EVAL_LOCK → SESSION（单向）。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};

use crate::engine::{Game, CIRCLE, CROSS, GRID_OPEN};
use crate::mcts::{difficulty_for, search_dispatch, Pool, Pos, NODE_CAP};

pub const EVAL_ITERS: u64 = 200_000;
pub const EVAL_PHASE1: u64 = 20_000;
pub const EVAL_BUDGET: f64 = 0.8;

/// AI 搜索线程数 = 1（单树）。
/// 消融实测（等迭代数 vs Python 单线程，tests/duel_rust_threads.py）：
///   1线程 8:15 / 8线程 5:21 / 12线程 4:28 / 16线程 4:27
/// 根分裂投票随线程数单调稀释棋力；难度迭代档位本就按单树标定。
/// 单线程下大师档 256k 迭代 ~0.5s（软上限 12s），并行换速度不划算。
/// 副产品：单树让两步树复用的收益吃满（从树的统计在并行下会被重建浪费）。
const AI_THREADS: usize = 1;

/// 胜率评估线程数：display-only（不影响棋力语义），并行仅为加速胜率条细化
fn eval_threads() -> usize {
    let cpus = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4);
    (cpus / 4).clamp(1, 4)
}

pub struct Session {
    pub game: Game,
    pub mode: i32,
    pub difficulty: i32,
    pub first: i32,
    pub goal: i32,
    ai_tree: Option<Pool>,
    last_ai_move: Option<(u8, u8)>,
    version: u64,
    eval_stats: Option<[i64; 3]>,
    eval_tree: Option<Pool>,
    threads: usize,
}

impl Session {
    pub fn new() -> Session {
        Session {
            game: Game::new(),
            mode: 0,
            difficulty: -1,
            first: 0,
            goal: 1,
            ai_tree: None,
            last_ai_move: None,
            version: 0,
            eval_stats: None,
            eval_tree: None,
            threads: AI_THREADS,
        }
    }

    fn pos(&self) -> Pos {
        let flat: Vec<u8> = self.game.cells.iter().flat_map(|r| r.iter().copied()).collect();
        Pos::from_flat(&flat, &self.game.grids, self.game.forced, self.game.turn)
    }

    /// AI 颜色：电脑先手=圈(1)，人先手=叉(2)（先手=圈惯例）
    fn ai_color(&self) -> u8 {
        if self.first == 1 { CIRCLE } else { CROSS }
    }
}

static SESSION: OnceLock<Mutex<Session>> = OnceLock::new();
static EVAL_LOCK: Mutex<()> = Mutex::new(());
static EVAL_BUSY: AtomicBool = AtomicBool::new(false);

fn session() -> &'static Mutex<Session> {
    SESSION.get_or_init(|| Mutex::new(Session::new()))
}

/// 落子/开局后启动异步评估（调用方持有 SESSION 锁；本函数不加锁）。
/// 评估与难度/AI 目标无关——固定最强精度快评。
fn spawn_eval(mv: Option<(u8, u8)>, ver: u64) {
    EVAL_BUSY.store(true, Ordering::Release);
    std::thread::spawn(move || eval_worker(mv, ver));
}

/// 对齐 Python _worker_eval：两阶段评估（2 万快速出值 → 20 万细化），
/// 完成时校验版本号，过期结果丢弃。
fn eval_worker(mv: Option<(u8, u8)>, ver: u64) {
    let _g = EVAL_LOCK.lock().unwrap();
    // 取快照 + 评估树（树提升复用：先找落子对应子节点，失败则重建根）
    let (pos, tree) = {
        let mut s = session().lock().unwrap();
        let mut t = s.eval_tree.take();
        let mut dead = false;
        if let (Some(tree), Some(m)) = (&mut t, mv) {
            if tree.find_child(m).is_none() {
                dead = true;
            }
        }
        if dead {
            t = None;
        }
        (s.pos(), t)
    };
    let mut t = tree.unwrap_or_else(|| Pool::new(NODE_CAP));
    let ethreads = eval_threads();
    // 阶段一：2 万迭代快速出值（不限时，并行加速）
    search_dispatch(&mut t, &pos, 1, EVAL_PHASE1, 0.0, ethreads);
    {
        let mut s = session().lock().unwrap();
        if s.version == ver {
            s.eval_stats = Some(t.stats);
        }
    }
    // 阶段二：细化到 20 万（0.8s 软上限）；局面已过期则跳过（省 CPU）
    if session().lock().unwrap().version == ver {
        search_dispatch(&mut t, &pos, 1, EVAL_ITERS - EVAL_PHASE1, EVAL_BUDGET, ethreads);
        let mut s = session().lock().unwrap();
        if s.version == ver {
            s.eval_stats = Some(t.stats);
            s.eval_tree = Some(t);
        }
    }
    // 仅最新版本的 worker 允许清除 busy——过期 worker 结束时
    // 必有更新版本的 worker 在跑/排队，标志由它负责清（比 Python 版更严谨）
    if session().lock().unwrap().version == ver {
        EVAL_BUSY.store(false, Ordering::Release);
    }
}

// ---------------------------------------------------------------- 对局流程

pub fn new_game(mode: i32, difficulty: i32, first: i32, goal: i32) {
    let mut s = session().lock().unwrap();
    s.mode = mode;
    s.difficulty = difficulty;
    s.first = first;
    s.goal = goal;
    s.game = Game::new();
    s.ai_tree = None;
    s.last_ai_move = None;
    s.eval_tree = None;
    s.eval_stats = None; // 清空旧局胜率
    s.version += 1;      // 丢弃上局仍在跑的评估线程结果
    if mode == 0 {
        let ver = s.version;
        spawn_eval(None, ver); // 开局空盘评估
    }
}

pub fn play(sub: i32, cell: i32) {
    let mut s = session().lock().unwrap();
    if !s.game.is_over() {
        s.game.apply_move(sub.max(0) as usize, cell.max(0) as usize);
        s.version += 1;
        if s.mode == 0 {
            let ver = s.version;
            spawn_eval(Some((sub as u8, cell as u8)), ver);
        }
    }
}

pub fn ai_move() {
    let mut s = session().lock().unwrap();
    if !(s.mode == 0 && !s.game.is_over() && s.game.turn == s.ai_color()) {
        return;
    }
    let goal = if s.goal == 1 { 1 } else { -1 };
    let (iters, cap) = difficulty_for(s.difficulty);
    let pos = s.pos();
    // 树复用（两步提升）：先找上一手 AI 落子，再找人类落子
    let mut tree = s.ai_tree.take();
    if let Some(t) = &mut tree {
        if let Some(m) = s.last_ai_move {
            if t.find_child(m).is_none() {
                tree = None;
            }
        }
    }
    if let Some(t) = &mut tree {
        if let Some(m) = s.game.last_move {
            if t.find_child(m).is_none() {
                tree = None;
            }
        }
    }
    let mut t = tree.unwrap_or_else(|| Pool::new(NODE_CAP));
    let mv = search_dispatch(&mut t, &pos, goal, iters, cap, s.threads);
    if let Some(m) = mv {
        s.game.apply_move(m.0 as usize, m.1 as usize);
        s.last_ai_move = Some(m);
        s.version += 1;
        let ver = s.version;
        spawn_eval(Some(m), ver);
        s.ai_tree = Some(t);
    } else {
        s.eval_stats = Some(t.stats);
        s.ai_tree = Some(t);
    }
}

pub fn resign() {
    let mut s = session().lock().unwrap();
    if !s.game.is_over() {
        if s.mode == 0 {
            s.game.winner = s.ai_color();
        } else {
            s.game.winner = if s.game.turn == CROSS { CIRCLE } else { CROSS };
        }
        s.game.win_line = None;
    }
}

// ---------------------------------------------------------------- JSON 状态

pub fn ping_json() -> String {
    "{\"ok\":true,\"game\":\"super-tic-tac-toe\"}".to_string()
}

/// Rust 为预编译机器码：恒就绪（保留前端预热轮询契约）
pub fn precompile_json() -> String {
    "{\"ready\":true,\"progress\":100}".to_string()
}

fn push_moves(s: &mut String, moves: &[(u8, u8)]) {
    s.push('[');
    for (i, m) in moves.iter().enumerate() {
        if i > 0 { s.push(','); }
        s.push_str(&format!("[{},{}]", m.0, m.1));
    }
    s.push(']');
}

pub fn state_json() -> String {
    let s = session().lock().unwrap();
    let g = &s.game;
    let mut out = String::with_capacity(1200);
    out.push_str("{\"cells\":[");
    for (i, row) in g.cells.iter().enumerate() {
        if i > 0 { out.push(','); }
        out.push('[');
        for (j, v) in row.iter().enumerate() {
            if j > 0 { out.push(','); }
            out.push_str(&v.to_string());
        }
        out.push(']');
    }
    out.push_str("],\"grids\":[");
    for (i, v) in g.grids.iter().enumerate() {
        if i > 0 { out.push(','); }
        out.push_str(&v.to_string());
    }
    out.push_str("],\"forced\":");
    if g.forced >= 0 {
        out.push_str(&(g.forced as i32).to_string());
    } else {
        out.push_str("null");
    }
    out.push_str(",\"turn\":");
    out.push_str(&g.turn.to_string());
    out.push_str(",\"lastMove\":");
    match g.last_move {
        Some(m) => out.push_str(&format!("[{},{}]", m.0, m.1)),
        None => out.push_str("null"),
    }
    out.push_str(",\"winner\":");
    out.push_str(&g.winner.to_string());
    out.push_str(",\"winLine\":");
    match g.win_line {
        Some(l) => out.push_str(&format!("[{},{},{}]", l.0, l.1, l.2)),
        None => out.push_str("null"),
    }
    out.push_str(",\"moves\":");
    push_moves(&mut out, &g.legal_moves());
    out.push_str(",\"stats\":");
    match s.eval_stats {
        Some(st) => out.push_str(&format!("[{},{},{}]", st[0], st[1], st[2])),
        None => out.push_str("null"),
    }
    out.push('}');
    out
}

pub fn stats_json() -> String {
    let (stats, version) = {
        let s = session().lock().unwrap();
        (s.eval_stats, s.version)
    };
    let busy = EVAL_BUSY.load(Ordering::Acquire);
    let st = match stats {
        Some(v) => format!("[{},{},{}]", v[0], v[1], v[2]),
        None => "null".to_string(),
    };
    format!("{{\"stats\":{st},\"version\":{version},\"busy\":{busy}}}")
}

pub fn legal_moves_json() -> String {
    let s = session().lock().unwrap();
    let mut out = String::with_capacity(600);
    push_moves(&mut out, &s.game.legal_moves());
    out
}

/// 引擎等价性校验用：给定局面返回合法步 JSON（与 Python 引擎逐局面比对）。
pub fn position_legal_json(cells: &[u8; 81], grids: &[u8; 9], forced: i32, turn: i32) -> String {
    let mut g = Game {
        cells: [[0; 9]; 9],
        grids: *grids,
        turn: if turn == 2 { CROSS } else { CIRCLE },
        forced: if forced >= 0 { forced as i8 } else { -1 },
        last_move: None,
        winner: 0,
        win_line: None,
    };
    for s in 0..9 {
        for c in 0..9 {
            g.cells[s][c] = cells[s * 9 + c];
        }
    }
    // 重算 winner（对齐 Python restore 语义）
    let (w, line) = crate::engine::line_winner(&g.grids);
    if w != 0 {
        g.winner = w;
        g.win_line = line;
    } else if g.grids.iter().all(|&x| x != GRID_OPEN) {
        g.winner = 3;
    }
    let mut out = String::with_capacity(600);
    push_moves(&mut out, &g.legal_moves());
    out
}

/// 一次性搜索（对弈验证 / 基准测试用，不触碰会话状态）。
/// 返回 JSON：{"move":[s,c]|null,"stats":[a,b,c],"iters":n,"elapsed_ms":x}
pub fn search_json(
    cells: &[u8; 81],
    grids: &[u8; 9],
    forced: i32,
    turn: i32,
    iters: i64,
    threads: i32,
    goal: i32,
    budget: f64,
) -> String {
    let pos = Pos::from_flat(
        cells,
        grids,
        if forced >= 0 { forced as i8 } else { -1 },
        if turn == 2 { CROSS } else { CIRCLE },
    );
    let iters = if iters > 0 { iters as u64 } else { crate::mcts::MAX_ITERATIONS };
    let threads = threads.max(1) as usize;
    let mut pool = Pool::new(NODE_CAP.min((iters + 65_536) as usize).max(65_536));
    let t0 = std::time::Instant::now();
    let mv = search_dispatch(&mut pool, &pos, goal, iters, budget, threads);
    let ms = t0.elapsed().as_secs_f64() * 1000.0;
    let iters_done = pool.done;
    let st = pool.stats;
    match mv {
        Some(m) => format!(
            "{{\"move\":[{},{}],\"stats\":[{},{},{}],\"iters\":{},\"elapsed_ms\":{:.3}}}",
            m.0, m.1, st[0], st[1], st[2], iters_done, ms
        ),
        None => format!(
            "{{\"move\":null,\"stats\":[{},{},{}],\"iters\":{},\"elapsed_ms\":{:.3}}}",
            st[0], st[1], st[2], iters_done, ms
        ),
    }
}

/// 进程内基准矩阵（消融实验标尺 + PGO 训练负载）。
/// 开局/中局/残局 × (1,8) 线程，各 5 轮取中位，返回 JSON 行数组。
pub fn bench_json() -> String {
    use crate::engine::Game;

    fn pos_of(g: &Game) -> Pos {
        let flat: Vec<u8> = g.cells.iter().flat_map(|r| r.iter().copied()).collect();
        Pos::from_flat(&flat, &g.grids, g.forced, g.turn)
    }

    let mut opening = Game::new();
    let mut mid = Game::new();
    for mv in [(4, 4), (4, 0), (0, 0), (0, 4), (8, 8), (8, 4), (2, 2), (2, 8)] {
        mid.apply_move(mv.0, mv.1);
    }
    let mut late = Game::new();
    for mv in [(4, 4), (4, 0), (0, 0), (0, 4), (8, 8), (8, 4), (2, 2), (2, 8),
               (6, 2), (6, 6), (3, 3), (3, 5), (5, 1), (5, 7), (1, 6), (1, 2),
               (7, 5), (7, 3)] {
        late.apply_move(mv.0, mv.1);
    }
    let _ = &mut opening;

    let mut out = String::from("[");
    let mut first = true;
    for (name, g) in [("opening", opening), ("midgame", mid), ("endgame", late)] {
        let pos = pos_of(&g);
        for threads in [1usize, 8] {
            let iters: u64 = if threads == 1 { 30_000 } else { 240_000 };
            let mut runs = Vec::new();
            for _ in 0..5 {
                let mut pool =
                    Pool::new(NODE_CAP.min((iters + 65_536) as usize).max(65_536));
                let t0 = std::time::Instant::now();
                let _ = search_dispatch(&mut pool, &pos, 1, iters, 0.0, threads);
                let rate = pool.done as f64 / t0.elapsed().as_secs_f64();
                runs.push(rate);
            }
            runs.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let med = runs[2];
            if !first {
                out.push(',');
            }
            first = false;
            out.push_str(&format!(
                "{{\"workload\":\"{name}\",\"threads\":{threads},\"iters_per_s\":{:.0}}}",
                med
            ));
        }
    }
    out.push(']');
    out
}

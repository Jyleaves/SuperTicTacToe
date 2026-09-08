//! 会话状态只在短临界区内读写，搜索在锁外执行。
//! AI_LOCK 串行化选步；评估工作线程只消费最新任务。
//! 锁序为 SESSION -> 评估队列；工作线程取出任务后先释放队列锁。

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, OnceLock};

use crate::engine::{Game, CIRCLE, CROSS, GRID_OPEN};
use crate::mcts::{
    difficulty_for, search_dispatch, search_dispatch_with_cancel, Cancellation, Pool, Pos, NODE_CAP,
};

pub const EVAL_ITERS: u64 = 200_000;
pub const EVAL_PHASE1: u64 = 20_000;
pub const EVAL_BUDGET: f64 = 0.8;
const AI_THREADS: usize = 1;

fn eval_threads() -> usize {
    let cpus = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4);
    (cpus / 4).clamp(1, 4)
}

fn pool_capacity(iterations: u64) -> usize {
    (iterations
        .saturating_mul(2)
        .saturating_add(crate::mcts::RECYCLE_MARGIN as u64))
    .clamp(65_536, NODE_CAP as u64) as usize
}

pub struct Session {
    pub game: Game,
    pub mode: i32,
    pub difficulty: i32,
    pub first: i32,
    pub goal: i32,
    ai_tree: Option<Pool>,
    last_ai_move: Option<(u8, u8)>,
    game_id: u64,
    version: u64,
    active: bool,
    eval_enabled: bool,
    eval_busy: bool,
    eval_stats: Option<[i64; 3]>,
    threads: usize,
}

impl Default for Session {
    fn default() -> Self {
        Self::new()
    }
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
            game_id: 0,
            version: 0,
            active: false,
            eval_enabled: true,
            eval_busy: false,
            eval_stats: None,
            threads: AI_THREADS,
        }
    }

    fn pos(&self) -> Pos {
        let flat: [u8; 81] = std::array::from_fn(|i| self.game.cells[i / 9][i % 9]);
        Pos::from_flat(&flat, &self.game.grids, self.game.forced, self.game.turn)
    }

    fn ai_color(&self) -> u8 {
        if self.first == 1 {
            CIRCLE
        } else {
            CROSS
        }
    }
}

static SESSION: OnceLock<Mutex<Session>> = OnceLock::new();
static AI_LOCK: Mutex<()> = Mutex::new(());
static POSITION_VERSION: AtomicU64 = AtomicU64::new(0);
static EVAL_GENERATION: AtomicU64 = AtomicU64::new(0);
static EVAL_QUEUE: OnceLock<Arc<EvalQueue>> = OnceLock::new();

#[cfg(test)]
pub(crate) static TEST_SESSION_LOCK: Mutex<()> = Mutex::new(());
#[cfg(test)]
type TestGate = (std::sync::mpsc::Sender<()>, std::sync::mpsc::Receiver<()>);
#[cfg(test)]
static AI_TEST_GATE: Mutex<Option<TestGate>> = Mutex::new(None);

#[cfg(test)]
fn wait_at_ai_test_gate() {
    let gate = AI_TEST_GATE.lock().unwrap().take();
    if let Some((entered, resume)) = gate {
        entered.send(()).unwrap();
        resume.recv().unwrap();
    }
}

fn session() -> &'static Mutex<Session> {
    SESSION.get_or_init(|| Mutex::new(Session::new()))
}

struct EvalJob {
    pos: Pos,
    mv: Option<(u8, u8)>,
    version: u64,
    generation: u64,
}

struct EvalQueue {
    pending: Mutex<Option<EvalJob>>,
    ready: Condvar,
}

fn eval_queue() -> &'static Arc<EvalQueue> {
    EVAL_QUEUE.get_or_init(|| {
        let queue = Arc::new(EvalQueue {
            pending: Mutex::new(None),
            ready: Condvar::new(),
        });
        let worker_queue = Arc::clone(&queue);
        std::thread::Builder::new()
            .name("sttt-eval".into())
            .spawn(move || eval_worker(worker_queue))
            .expect("start evaluation worker");
        queue
    })
}

/// Caller holds SESSION. A state change also cancels any in-flight AI search.
fn advance_version(s: &mut Session) {
    s.version += 1;
    POSITION_VERSION.store(s.version, Ordering::Release);
}

fn invalidate_eval(s: &mut Session) {
    EVAL_GENERATION.fetch_add(1, Ordering::AcqRel);
    s.eval_busy = false;
    s.eval_stats = None;
    if let Some(queue) = EVAL_QUEUE.get() {
        *queue.pending.lock().unwrap() = None;
    }
}

/// Caller holds SESSION. Replaces queued work; no search or pool allocation here.
fn schedule_eval(s: &mut Session, mv: Option<(u8, u8)>) {
    invalidate_eval(s);
    if !s.active || !s.eval_enabled || s.mode != 0 || s.game.is_over() {
        return;
    }
    let job = EvalJob {
        pos: s.pos(),
        mv,
        version: s.version,
        generation: EVAL_GENERATION.load(Ordering::Acquire),
    };
    s.eval_busy = true;
    let queue = eval_queue();
    *queue.pending.lock().unwrap() = Some(job);
    queue.ready.notify_one();
}

/// The one worker owns its pool across jobs; a stale root is recycled by search.
fn eval_worker(queue: Arc<EvalQueue>) {
    let mut tree: Option<Pool> = None;
    loop {
        let job = {
            let mut pending = queue.pending.lock().unwrap();
            while pending.is_none() {
                pending = queue.ready.wait(pending).unwrap();
            }
            pending.take().unwrap()
        };
        let cancel = Cancellation {
            generation: &EVAL_GENERATION,
            expected: job.generation,
        };
        if cancel.is_cancelled() {
            continue;
        }
        let threads = eval_threads();
        let t = tree.get_or_insert_with(|| Pool::new(pool_capacity(EVAL_ITERS / threads as u64)));
        if let Some(mv) = job.mv {
            if t.find_child(mv).is_none() {
                t.recycle();
            }
        }
        search_dispatch_with_cancel(t, &job.pos, 1, EVAL_PHASE1, 0.0, threads, Some(cancel));
        {
            let mut s = session().lock().unwrap();
            if cancel.is_cancelled() || s.version != job.version {
                continue;
            }
            s.eval_stats = Some(t.stats);
        }
        search_dispatch_with_cancel(
            t,
            &job.pos,
            1,
            EVAL_ITERS - EVAL_PHASE1,
            EVAL_BUDGET,
            threads,
            Some(cancel),
        );
        let mut s = session().lock().unwrap();
        if !cancel.is_cancelled() && s.version == job.version {
            if t.done > 0 {
                s.eval_stats = Some(t.stats);
            }
            s.eval_busy = false;
        }
    }
}

// ---------------------------------------------------------------- 对局流程

pub fn new_game(mode: i32, difficulty: i32, first: i32, goal: i32) {
    new_game_with_stats(mode, difficulty, first, goal, true);
}

pub fn new_game_with_stats(
    mode: i32,
    difficulty: i32,
    first: i32,
    goal: i32,
    stats: bool,
) -> String {
    let mut s = session().lock().unwrap();
    let old_tree = s.ai_tree.take();
    s.mode = mode;
    s.difficulty = difficulty;
    s.first = first;
    s.goal = goal;
    s.game = Game::new();
    s.last_ai_move = None;
    s.game_id += 1;
    s.active = true;
    s.eval_enabled = stats;
    advance_version(&mut s);
    schedule_eval(&mut s, None);
    // Capture this game's identity before another request can create a new one.
    let state = state_json_for(&s);
    drop(s);
    drop(old_tree);
    state
}

pub fn play(sub: i32, cell: i32) {
    play_at_version(sub, cell, None);
}

pub fn play_at_version(sub: i32, cell: i32, expected: Option<u64>) {
    let mut s = session().lock().unwrap();
    if !s.active
        || expected.is_some_and(|v| v != s.version)
        || (s.mode == 0 && s.game.turn == s.ai_color())
        || sub < 0
        || cell < 0
    {
        return;
    }
    if s.game.apply_move(sub as usize, cell as usize) {
        advance_version(&mut s);
        schedule_eval(&mut s, Some((sub as u8, cell as u8)));
    }
}

pub fn ai_move() {
    ai_move_at_version(None);
}

pub fn ai_move_at_version(expected: Option<u64>) {
    let _ai = AI_LOCK.lock().unwrap();
    let (pos, tree, goal, iters, budget, threads, version) = {
        let mut s = session().lock().unwrap();
        if !s.active
            || expected.is_some_and(|v| v != s.version)
            || !(s.mode == 0 && !s.game.is_over() && s.game.turn == s.ai_color())
        {
            return;
        }
        // Give move selection priority over display-only evaluation. A fresh
        // evaluation is scheduled after the AI move has been committed.
        invalidate_eval(&mut s);
        let mut tree = s.ai_tree.take();
        if let Some(t) = &mut tree {
            if let Some(mv) = s.last_ai_move {
                if t.find_child(mv).is_none() {
                    t.recycle();
                }
            }
            if let Some(mv) = s.game.last_move {
                if t.find_child(mv).is_none() {
                    t.recycle();
                }
            }
        }
        let (iters, budget) = difficulty_for(s.difficulty);
        (
            s.pos(),
            tree,
            if s.goal == 1 { 1 } else { -1 },
            iters,
            budget,
            s.threads,
            s.version,
        )
    };

    #[cfg(test)]
    wait_at_ai_test_gate();

    let cancel = Cancellation {
        generation: &POSITION_VERSION,
        expected: version,
    };
    if cancel.is_cancelled() {
        return;
    }
    // Keep the original AI capacity: shrinking it discards reusable search
    // statistics earlier. The display-only evaluator may use smaller pools.
    let mut t = tree.unwrap_or_else(|| Pool::new(NODE_CAP));
    let mv = search_dispatch_with_cancel(&mut t, &pos, goal, iters, budget, threads, Some(cancel));
    let mut s = session().lock().unwrap();
    if !s.active || s.version != version {
        t.recycle();
        if s.active && s.mode == 0 && s.ai_tree.is_none() {
            s.ai_tree = Some(t);
        }
        return;
    }
    if let Some(mv) = mv {
        if s.game.apply_move(mv.0 as usize, mv.1 as usize) {
            s.last_ai_move = Some(mv);
            advance_version(&mut s);
            schedule_eval(&mut s, Some(mv));
        }
    }
    s.ai_tree = Some(t);
}

pub fn resign() {
    resign_game(None);
}

pub fn resign_game(expected_game: Option<u64>) {
    let mut s = session().lock().unwrap();
    if !s.active || expected_game.is_some_and(|id| id != s.game_id) || s.game.is_over() {
        return;
    }
    s.game.winner = if s.mode == 0 {
        s.ai_color()
    } else {
        3 - s.game.turn
    };
    s.game.win_line = None;
    advance_version(&mut s);
    invalidate_eval(&mut s);
}

pub fn cancel_game(expected_game: Option<u64>) {
    let mut s = session().lock().unwrap();
    if expected_game.is_some_and(|id| id != s.game_id) {
        return;
    }
    s.active = false;
    advance_version(&mut s);
    invalidate_eval(&mut s);
}

pub fn set_stats_enabled(enabled: bool, expected_game: Option<u64>) {
    let mut s = session().lock().unwrap();
    if expected_game.is_some_and(|id| id != s.game_id) {
        return;
    }
    s.eval_enabled = enabled;
    schedule_eval(&mut s, None);
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
        if i > 0 {
            s.push(',');
        }
        s.push_str(&format!("[{},{}]", m.0, m.1));
    }
    s.push(']');
}

pub fn state_json() -> String {
    let s = session().lock().unwrap();
    state_json_for(&s)
}

fn state_json_for(s: &Session) -> String {
    let g = &s.game;
    let mut out = String::with_capacity(1200);
    out.push_str("{\"cells\":[");
    for (i, row) in g.cells.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push('[');
        for (j, v) in row.iter().enumerate() {
            if j > 0 {
                out.push(',');
            }
            out.push_str(&v.to_string());
        }
        out.push(']');
    }
    out.push_str("],\"grids\":[");
    for (i, v) in g.grids.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
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
    out.push_str(&format!(
        ",\"version\":{},\"gameId\":{}",
        s.version, s.game_id
    ));
    out.push('}');
    out
}

pub fn stats_json() -> String {
    let (stats, version, game_id, busy) = {
        let s = session().lock().unwrap();
        (s.eval_stats, s.version, s.game_id, s.eval_busy)
    };
    let st = match stats {
        Some(v) => format!("[{},{},{}]", v[0], v[1], v[2]),
        None => "null".to_string(),
    };
    format!("{{\"stats\":{st},\"version\":{version},\"gameId\":{game_id},\"busy\":{busy}}}")
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
#[allow(clippy::too_many_arguments)] // Mirrors the public C ABI search parameters.
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
    let total_start = std::time::Instant::now();
    let pos = Pos::from_flat(
        cells,
        grids,
        if forced >= 0 { forced as i8 } else { -1 },
        if turn == 2 { CROSS } else { CIRCLE },
    );
    let iters = if iters > 0 {
        (iters as u64).min(crate::mcts::MAX_ITERATIONS)
    } else {
        crate::mcts::MAX_ITERATIONS
    };
    let threads = threads.clamp(1, 32) as usize;
    let mut pool = Pool::new(NODE_CAP.min((iters + 65_536) as usize).max(65_536));
    let t0 = std::time::Instant::now();
    let mv = search_dispatch(&mut pool, &pos, goal, iters, budget, threads);
    let ms = t0.elapsed().as_secs_f64() * 1000.0;
    let total_ms = total_start.elapsed().as_secs_f64() * 1000.0;
    let iters_done = pool.done;
    let st = pool.stats;
    match mv {
        Some(m) => format!(
            "{{\"move\":[{},{}],\"stats\":[{},{},{}],\"iters\":{},\"elapsed_ms\":{:.3},\"total_elapsed_ms\":{:.3}}}",
            m.0, m.1, st[0], st[1], st[2], iters_done, ms, total_ms
        ),
        None => format!(
            "{{\"move\":null,\"stats\":[{},{},{}],\"iters\":{},\"elapsed_ms\":{:.3},\"total_elapsed_ms\":{:.3}}}",
            st[0], st[1], st[2], iters_done, ms, total_ms
        ),
    }
}

/// 进程内基准矩阵（消融实验标尺 + PGO 训练负载）。
/// 开局/中局/残局 × (1,8) 线程，各 5 轮取中位，返回 JSON 行数组。
pub fn benchmark_positions() -> Vec<(&'static str, Game)> {
    include_str!("../../tests/fixtures/search_positions.txt")
        .lines()
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
        .map(|line| {
            let (name, moves) = line.split_once(':').expect("benchmark name:history");
            let mut game = Game::new();
            for pair in moves.split(';').filter(|pair| !pair.is_empty()) {
                let (sub, cell) = pair.split_once(',').expect("benchmark sub,cell");
                let sub = sub.parse().expect("benchmark sub index");
                let cell = cell.parse().expect("benchmark cell index");
                assert!(
                    game.apply_move(sub, cell),
                    "illegal benchmark move {name}: {pair}"
                );
            }
            (name, game)
        })
        .collect()
}

pub fn bench_json() -> String {
    fn pos_of(g: &Game) -> Pos {
        let flat: Vec<u8> = g.cells.iter().flat_map(|r| r.iter().copied()).collect();
        Pos::from_flat(&flat, &g.grids, g.forced, g.turn)
    }

    let mut out = String::from("[");
    let mut first = true;
    for (name, g) in benchmark_positions() {
        let pos = pos_of(&g);
        for threads in [1usize, 8] {
            let iters: u64 = if threads == 1 { 30_000 } else { 240_000 };
            let mut runs = Vec::new();
            let mut total_runs = Vec::new();
            for _ in 0..5 {
                let total_start = std::time::Instant::now();
                let mut pool = Pool::new(NODE_CAP.min((iters + 65_536) as usize).max(65_536));
                let t0 = std::time::Instant::now();
                let _ = search_dispatch(&mut pool, &pos, 1, iters, 0.0, threads);
                let rate = pool.done as f64 / t0.elapsed().as_secs_f64();
                total_runs.push(pool.done as f64 / total_start.elapsed().as_secs_f64());
                runs.push(rate);
            }
            runs.sort_by(|a, b| a.partial_cmp(b).unwrap());
            total_runs.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let med = runs[2];
            if !first {
                out.push(',');
            }
            first = false;
            out.push_str(&format!(
                "{{\"workload\":\"{name}\",\"threads\":{threads},\"iters_per_s\":{:.0},\"with_pool_iters_per_s\":{:.0}}}",
                med, total_runs[2]
            ));
        }
    }
    out.push(']');
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;
    use std::time::Duration;

    #[test]
    fn benchmark_histories_cover_distinct_game_phases() {
        let positions = benchmark_positions();
        assert_eq!(positions.len(), 3);
        let counts: Vec<usize> = positions
            .iter()
            .map(|(_, g)| g.cells.iter().flatten().filter(|&&cell| cell != 0).count())
            .collect();
        assert_eq!(counts, [0, 28, 56]);
        assert!(positions
            .iter()
            .all(|(_, g)| !g.is_over() && !g.legal_moves().is_empty()));
        assert!(
            positions[2]
                .1
                .grids
                .iter()
                .filter(|&&g| g != GRID_OPEN)
                .count()
                >= 5
        );
    }

    #[test]
    fn concurrent_new_games_return_their_own_identity() {
        let _guard = TEST_SESSION_LOCK.lock().unwrap();
        let mut ids = std::thread::scope(|scope| {
            let workers: Vec<_> = (0..16)
                .map(|_| {
                    scope.spawn(|| {
                        let state = new_game_with_stats(1, -1, 0, 1, false);
                        state
                            .split("\"gameId\":")
                            .nth(1)
                            .unwrap()
                            .trim_end_matches('}')
                            .parse::<u64>()
                            .unwrap()
                    })
                })
                .collect();
            workers
                .into_iter()
                .map(|w| w.join().unwrap())
                .collect::<Vec<_>>()
        });
        ids.sort_unstable();
        ids.dedup();
        assert_eq!(ids.len(), 16);
    }

    #[test]
    fn rejected_moves_and_disabled_stats_do_not_start_work() {
        let _guard = TEST_SESSION_LOCK.lock().unwrap();
        new_game_with_stats(0, -1, 0, 1, false);
        let version = session().lock().unwrap().version;
        play(-1, 0);
        assert_eq!(session().lock().unwrap().version, version);
        play_at_version(0, 0, Some(version + 1));
        assert_eq!(session().lock().unwrap().version, version);
        play_at_version(0, 0, Some(version));
        let after = session().lock().unwrap().version;
        play(0, 1); // Legal geometrically, but it is the AI's turn.
        assert_eq!(session().lock().unwrap().version, after);
        assert!(!session().lock().unwrap().eval_busy);
        assert!(session().lock().unwrap().eval_stats.is_none());
        new_game(0, -1, 0, 1);
        set_stats_enabled(false, None);
        assert!(!session().lock().unwrap().eval_busy);
        new_game(1, -1, 0, 1);
        assert!(!session().lock().unwrap().eval_busy);
    }

    #[test]
    fn ai_search_releases_session_and_discards_cancelled_move() {
        let _guard = TEST_SESSION_LOCK.lock().unwrap();
        new_game_with_stats(0, -1, 0, 1, false);
        play(0, 4);
        let (entered_tx, entered_rx) = mpsc::channel();
        let (resume_tx, resume_rx) = mpsc::channel();
        *AI_TEST_GATE.lock().unwrap() = Some((entered_tx, resume_rx));
        let ai = std::thread::spawn(ai_move);
        let reached = entered_rx.recv_timeout(Duration::from_secs(10));
        if reached.is_err() {
            let _ = resume_tx.send(());
            panic!("AI did not reach its unlocked search phase: {reached:?}");
        }
        let (reply_tx, reply_rx) = mpsc::channel();
        let ui = std::thread::spawn(move || {
            let stats = stats_json();
            resign();
            reply_tx.send(stats).unwrap();
        });
        let response = reply_rx.recv_timeout(Duration::from_secs(5));
        resume_tx.send(()).unwrap();
        ai.join().unwrap();
        ui.join().unwrap();
        assert!(
            response.is_ok(),
            "UI state access waited for the blocked search"
        );
        let s = session().lock().unwrap();
        assert_eq!(s.game.winner, CROSS);
        assert_eq!(
            s.game.cells.iter().flatten().filter(|&&x| x != 0).count(),
            1
        );
        assert!(!s.eval_busy);
    }

    #[test]
    fn an_old_game_cannot_cancel_or_resign_a_new_game() {
        let _guard = TEST_SESSION_LOCK.lock().unwrap();
        new_game_with_stats(0, -1, 0, 1, false);
        let old_id = session().lock().unwrap().game_id;
        new_game_with_stats(0, -1, 0, 1, false);
        cancel_game(Some(old_id));
        resign_game(Some(old_id));
        let s = session().lock().unwrap();
        assert!(s.active);
        assert_eq!(s.game.winner, 0);
    }
}

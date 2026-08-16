//! SuperTicTacToe Rust 后端（cdylib）。
//! 通过 C ABI + JSON 字符串与 Python(pywebview) 前端桥接，
//! 另暴露原始指针接口供对弈验证 / 基准测试调用。
//!
//! 返回值指向 thread-local 缓冲区，在下一次同线程调用前有效——
//! ctypes 调用后立即 decode 即安全。

#![allow(clippy::missing_safety_doc)]

pub mod engine;
pub mod mcts;
pub mod session;

use std::cell::RefCell;
use std::ffi::c_char;

thread_local! {
    static OUT: RefCell<String> = RefCell::new(String::new());
}

fn ret_json(mut s: String) -> *const c_char {
    // String 不保证 NUL 结尾：显式压入终止符，ctypes 按 C 字符串读取
    s.push('\0');
    OUT.with(|b| {
        *b.borrow_mut() = s;
        b.borrow().as_ptr() as *const c_char
    })
}

/// FFI 保险：panic 不允许穿越 extern "C" 边界（UB）——捕获后返回错误 JSON。
fn ffi_guard(f: impl FnOnce() -> String + std::panic::UnwindSafe) -> *const c_char {
    match std::panic::catch_unwind(f) {
        Ok(s) => ret_json(s),
        Err(_) => ret_json("{\"error\":\"rust panic\"}".to_string()),
    }
}

fn cstr(s: *const c_char) -> String {
    if s.is_null() {
        return String::new();
    }
    unsafe {
        let mut len = 0usize;
        while *s.add(len) != 0 {
            len += 1;
        }
        let bytes = std::slice::from_raw_parts(s as *const u8, len);
        String::from_utf8_lossy(bytes).into_owned()
    }
}

/// 兼容 mock/测试：JSON 设置串或裸整数均可解析
fn parse_int(s: &str) -> i32 {
    s.trim().trim_matches('"').parse::<i32>().unwrap_or(0)
}

// ---------------------------------------------------------------- 会话 API

#[no_mangle]
pub extern "C" fn sttt_ping() -> *const c_char {
    ret_json("{\"ok\":true,\"game\":\"super-tic-tac-toe\"}".to_string())
}

/// Rust 已预编译：恒就绪（保留前端预热轮询契约）
#[no_mangle]
pub extern "C" fn sttt_precompile_status() -> *const c_char {
    ret_json("{\"ready\":true,\"progress\":100}".to_string())
}

#[no_mangle]
pub extern "C" fn sttt_new_game(
    mode: *const c_char,
    difficulty: *const c_char,
    first: *const c_char,
    goal: *const c_char,
    _sound: *const c_char,
    _stats: *const c_char,
) -> *const c_char {
    session::new_game(
        parse_int(&cstr(mode)),
        parse_int(&cstr(difficulty)),
        parse_int(&cstr(first)),
        parse_int(&cstr(goal)),
    );
    ffi_guard(|| session::state_json())
}

#[no_mangle]
pub extern "C" fn sttt_play(sub: i32, cell: i32) -> *const c_char {
    ffi_guard(|| {
        session::play(sub, cell);
        session::state_json()
    })
}

#[no_mangle]
pub extern "C" fn sttt_ai_move() -> *const c_char {
    ffi_guard(|| {
        session::ai_move();
        session::state_json()
    })
}

#[no_mangle]
pub extern "C" fn sttt_resign() -> *const c_char {
    ffi_guard(|| {
        session::resign();
        session::state_json()
    })
}

#[no_mangle]
pub extern "C" fn sttt_stats() -> *const c_char {
    ffi_guard(|| session::stats_json())
}

#[no_mangle]
pub extern "C" fn sttt_legal_moves() -> *const c_char {
    ffi_guard(|| session::legal_moves_json())
}

#[no_mangle]
pub extern "C" fn sttt_reset_session() {
    // 测试隔离用：重置会话到初始状态
    session::new_game(1, -1, 0, 1);
}

// ---------------------------------------------------------------- 原始接口（验证 / 基准）

/// 一次性搜索。iters<=0 表示时间模式（budget 秒，上限 MAX_ITERATIONS）。
#[no_mangle]
pub unsafe extern "C" fn sttt_search_raw(
    cells: *const i8,
    grids: *const i8,
    forced: i32,
    turn: i32,
    iters: i64,
    threads: i32,
    goal: i32,
    budget: f64,
) -> *const c_char {
    let cells: [u8; 81] = std::array::from_fn(|i| (*cells.add(i)).max(0) as u8);
    let grids_arr: [u8; 9] = std::array::from_fn(|i| (*grids.add(i)).max(0) as u8);
    ffi_guard(|| {
        session::search_json(&cells, &grids_arr, forced, turn, iters, threads, goal, budget)
    })
}

/// 引擎等价性校验：给定局面返回合法步 JSON（与 Python 引擎比对）
#[no_mangle]
pub unsafe extern "C" fn sttt_legal_raw(
    cells: *const i8,
    grids: *const i8,
    forced: i32,
    turn: i32,
) -> *const c_char {
    let cells: [u8; 81] = std::array::from_fn(|i| (*cells.add(i)).max(0) as u8);
    let grids_arr: [u8; 9] = std::array::from_fn(|i| (*grids.add(i)).max(0) as u8);
    ffi_guard(|| session::position_legal_json(&cells, &grids_arr, forced, turn))
}

// ---------------------------------------------------------------- 自检

/// 全套自检（Python 侧冷启动可调用）：跑一遍引擎单测关键路径
#[no_mangle]
pub extern "C" fn sttt_selfcheck() -> i32 {
    let mut g = engine::Game::new();
    if g.legal_moves().len() != 81 {
        return 1;
    }
    if !g.apply_move(0, 3) || g.forced != 3 {
        return 2;
    }
    let pos = {
        let flat: Vec<u8> = g.cells.iter().flat_map(|r| r.iter().copied()).collect();
        mcts::Pos::from_flat(&flat, &g.grids, g.forced, g.turn)
    };
    let mut pool = mcts::Pool::new(65_536);
    if pool.search(&pos, 1, 2_000, 0.0).is_none() {
        return 3;
    }
    if pool.done < 1_900 {
        return 4;
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_state_shape() {
        session::new_game(1, -1, 0, 1); // 人人模式（不触发评估线程）
        let s = session::state_json();
        assert!(s.starts_with("{\"cells\":[[0,0,0,0,0,0,0,0,0],"));
        assert!(s.contains("\"forced\":null"));
        assert!(s.contains("\"turn\":1"));
        assert!(s.contains("\"stats\":null"));
        // 81 个开局合法步
        assert!(s.matches("[").count() >= 81);
    }

    #[test]
    fn play_and_ai_flow() {
        session::new_game(0, -1, 0, 1); // 人机：人先手（圈），AI=叉
        session::play(0, 4);
        session::ai_move();
        let s = session::state_json();
        assert!(s.contains("\"turn\":1")); // AI 落子后轮到人（圈）
        assert!(s.contains("\"lastMove\":["));
    }

    #[test]
    fn resign_flow() {
        session::new_game(0, -1, 0, 1);
        session::resign();
        let s = session::state_json();
        assert!(s.contains("\"winner\":2")); // AI（叉）胜
        assert!(s.contains("\"winLine\":null"));
    }

    #[test]
    fn pvp_flow() {
        session::new_game(1, -1, 0, 1);
        session::play(0, 0);
        let s = session::state_json();
        assert!(s.contains("\"forced\":0"));
        assert!(s.contains("\"turn\":2"));
    }

    #[test]
    fn selfcheck_passes() {
        assert_eq!(sttt_selfcheck(), 0);
    }
}

/// 进程内基准矩阵（消融实验标尺，DLL 内运行）
#[no_mangle]
pub extern "C" fn sttt_bench() -> *const c_char {
    ffi_guard(session::bench_json)
}

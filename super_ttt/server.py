"""pywebview JS ↔ Rust 后端桥。

2026-08-16 后端迁移：规则引擎 + MCTS 全部重写为 Rust（rust/ 目录，
cdylib sttt.dll，零依赖、无 GC、原生多线程）。本文件只剩薄桥：
参数转 JSON → ctypes 调用 → json.loads 返回。

前端 JS 契约与旧 Python 后端完全一致（new_game / play / ai_move /
resign / stats / precompile_status / legal_moves / ping）。
游戏状态全部由 Rust 会话持有；AI 搜索在 Rust 内部并行（root
parallelization ×N），ctypes 调用期间释放 GIL，UI 永不阻塞。

Rust 已是预编译机器码：无 JIT 预热，precompile_status 恒就绪。
"""

from __future__ import annotations

import ctypes
import json
import os

try:
    import webview
except ImportError:            # 无 GUI 环境（headless 测试）也可用本模块
    webview = None

_BASE = os.path.dirname(os.path.abspath(__file__))
_DLL_CANDIDATES = [
    os.path.join(_BASE, "sttt.dll"),
    os.path.join(os.path.dirname(_BASE), "rust", "target", "release", "sttt.dll"),
]


def _load_dll() -> ctypes.CDLL:
    for path in _DLL_CANDIDATES:
        if os.path.exists(path):
            lib = ctypes.CDLL(path)
            _bind(lib)
            if lib.sttt_selfcheck() != 0:
                raise RuntimeError("sttt.dll 自检失败: " + path)
            return lib
    raise FileNotFoundError(
        "未找到 sttt.dll（Rust 后端）。请先运行 rust\\build.cmd "
        "或 cargo build --release 后重试。")


def _bind(lib: ctypes.CDLL) -> None:
    c_char_p = ctypes.c_char_p
    lib.sttt_ping.restype = c_char_p
    lib.sttt_precompile_status.restype = c_char_p
    lib.sttt_new_game.argtypes = [c_char_p] * 6
    lib.sttt_new_game.restype = c_char_p
    lib.sttt_play.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.sttt_play.restype = c_char_p
    lib.sttt_ai_move.restype = c_char_p
    lib.sttt_resign.restype = c_char_p
    lib.sttt_stats.restype = c_char_p
    lib.sttt_legal_moves.restype = c_char_p
    # 对弈验证 / 基准测试用原始接口
    i8p = ctypes.POINTER(ctypes.c_int8)
    lib.sttt_search_raw.argtypes = [i8p, i8p, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int64, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_double]
    lib.sttt_search_raw.restype = c_char_p
    lib.sttt_legal_raw.argtypes = [i8p, i8p, ctypes.c_int, ctypes.c_int]
    lib.sttt_legal_raw.restype = c_char_p
    lib.sttt_selfcheck.restype = ctypes.c_int


_LIB = _load_dll()


def _call(fn, *args):
    return json.loads(fn(*args).decode("utf-8"))


def _state(d):
    """状态字典归一化：JSON 数组 → tuple，保持与旧 Python 后端
    完全相同的返回类型（lastMove/moves/winLine 为 tuple）。"""
    if isinstance(d, dict) and "cells" in d:
        if d.get("lastMove") is not None:
            d["lastMove"] = tuple(d["lastMove"])
        if d.get("winLine") is not None:
            d["winLine"] = tuple(d["winLine"])
        d["moves"] = [tuple(m) for m in d["moves"]]
    return d


def _call_state(fn, *args):
    return _state(_call(fn, *args))


def rust_search(cells, grids, forced, turn, iters, threads=1, goal=1, budget=0.0):
    """一次性搜索（测试/对弈验证用，不触碰会话状态）。
    cells: 81 扁平或 9x9 嵌套；返回 dict(move/stats/iters/elapsed_ms)。"""
    flat = [c for row in cells for c in row] if isinstance(cells[0], (list, tuple)) \
        else list(cells)
    arr = (ctypes.c_int8 * 81)(*flat)
    garr = (ctypes.c_int8 * 9)(*grids)
    return _call(_LIB.sttt_search_raw, arr, garr,
                 -1 if forced is None else int(forced), int(turn),
                 int(iters), int(threads), int(goal), float(budget))


def rust_legal(cells, grids, forced, turn):
    """引擎等价性校验用：给定局面的合法步列表。"""
    flat = [c for row in cells for c in row] if isinstance(cells[0], (list, tuple)) \
        else list(cells)
    arr = (ctypes.c_int8 * 81)(*flat)
    garr = (ctypes.c_int8 * 9)(*grids)
    raw = _LIB.sttt_legal_raw(arr, garr,
                              -1 if forced is None else int(forced),
                              int(turn)).decode("utf-8")
    return [tuple(m) for m in json.loads(raw)]


class Api:
    """与旧 Python 后端 Api 同名同签名——前端零改动。"""

    def __init__(self):
        _LIB.sttt_precompile_status()      # 加载即就绪（保持调用形态）

    # ------------------------------------------------------------- 生命周期
    def ping(self):
        return {"ok": True, "game": "super-tic-tac-toe"}

    def precompile_status(self):
        """Rust 为预编译机器码：恒就绪（前端预热悬浮窗因此不再出现）。"""
        return {"ready": True, "progress": 100}

    def exit_app(self):
        if webview is not None:
            for w in webview.windows:
                w.destroy()
        return True

    # ------------------------------------------------------------- 对局流程
    def new_game(self, settings):
        st = _call_state(_LIB.sttt_new_game,
                   str(int(settings["mode"])).encode(),
                   str(int(settings["difficulty"])).encode(),
                   str(int(settings["first"])).encode(),
                   str(int(settings["goal"])).encode(),
                   str(bool(settings.get("sound", True))).encode(),
                   str(bool(settings.get("stats", True))).encode())
        # 落子后的开局评估是异步的：立即返回首帧状态
        return st

    def play(self, sub, cell):
        return _call_state(_LIB.sttt_play, int(sub), int(cell))

    def ai_move(self):
        return _call_state(_LIB.sttt_ai_move)

    def resign(self):
        return _call_state(_LIB.sttt_resign)

    def stats(self):
        return _call(_LIB.sttt_stats)

    def legal_moves(self):
        raw = _LIB.sttt_legal_moves().decode("utf-8")
        return [tuple(m) for m in json.loads(raw)]

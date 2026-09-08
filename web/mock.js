/* ============================================================
   Mock 后端：仅在普通浏览器中调试用（无 pywebview 时）。
   规则镜像与 Python `super_ttt/engine.py` 保持一致——这是唯一的
   逻辑重复点，改动规则时必须同步两处（engine.py 为主）。
   IIFE 隔离：不污染全局，只暴露 window.MockBackend。
   ============================================================ */
(function () {
  'use strict';

  const CIRCLE = 1, CROSS = 2;
  const WIN_LINES = [
    [0, 1, 2], [3, 4, 5], [6, 7, 8],
    [0, 3, 6], [1, 4, 7], [2, 5, 8],
    [0, 4, 8], [2, 4, 6],
  ];

  function legalMoves(st) {
    const out = [];
    if (st.winner) return out;
    const subs = [];
    if (st.forced !== null && st.grids[st.forced] === 0) subs.push(st.forced);
    else for (let i = 0; i < 9; i++) if (st.grids[i] === 0) subs.push(i);
    for (const s of subs) for (let c = 0; c < 9; c++) if (st.cells[s][c] === 0) out.push([s, c]);
    return out;
  }

  function subWinner(row) {
    for (const [a, b, c] of WIN_LINES) {
      const v = row[a];
      if ((v === CIRCLE || v === CROSS) && v === row[b] && v === row[c]) return v;
    }
    return 0;
  }

  function bigWinner(grids) {
    for (const [a, b, c] of WIN_LINES) {
      const v = grids[a];
      if ((v === CIRCLE || v === CROSS) && v === grids[b] && v === grids[c]) return [v, [a, b, c]];
    }
    if (grids.every(g => g !== 0)) return [3, null];
    return [0, null];
  }

  function applyMove(st, sub, cell) {
    st.cells[sub][cell] = st.turn;
    st.lastMove = [sub, cell];
    const w = subWinner(st.cells[sub]);
    if (w) st.grids[sub] = w;
    else if (st.cells[sub].every(x => x)) st.grids[sub] = 3;
    const [bw, line] = bigWinner(st.grids);
    if (bw) { st.winner = bw; st.winLine = line; }
    else if (bw === 3) { st.winner = 3; st.winLine = null; }
    if (!st.winner) {
      st.forced = st.grids[cell] === 0 ? cell : null;
      st.turn = st.turn === CIRCLE ? CROSS : CIRCLE;
    }
    return st;
  }

  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let current = null, settings = {}, active = false, gameId = 0, version = 0;
  const matches = (expected, actual) => expected == null || expected === actual;
  const aiColor = () => settings.first === 1 ? CIRCLE : CROSS;
  function snapshot() {
    if (!current) return null;
    return {
      ...current, cells: current.cells.map(row => row.slice()), grids: current.grids.slice(),
      lastMove: current.lastMove && current.lastMove.slice(),
      winLine: current.winLine && current.winLine.slice(),
      moves: legalMoves(current), stats: null, gameId, version,
    };
  }

  window.MockBackend = {
    async precompile_status() { return { ready: true, progress: 100 }; },
    async new_game(nextSettings) {
      settings = { ...nextSettings };
      current = {
        cells: Array.from({ length: 9 }, () => Array(9).fill(0)),
        grids: Array(9).fill(0), forced: null, turn: CIRCLE,
        lastMove: null, winner: 0, winLine: null,
      };
      active = true; gameId++; version++;
      return snapshot();
    },
    async play(sub, cell, expectedVersion) {
      if (!active || !current || current.winner || !matches(expectedVersion, version) ||
          (settings.mode === 0 && current.turn === aiColor())) return snapshot();
      if (legalMoves(current).some(m => m[0] === sub && m[1] === cell)) {
        applyMove(current, sub, cell); version++;
      }
      return snapshot();
    },
    async ai_move(expectedVersion) {
      if (!active || !current || current.winner || settings.mode !== 0 ||
          current.turn !== aiColor() || !matches(expectedVersion, version)) return snapshot();
      const position = current, searchVersion = version;
      await sleep(600);
      if (active && current === position && version === searchVersion) {
        const moves = legalMoves(current);
        if (moves.length) {
          applyMove(current, ...moves[Math.floor(Math.random() * moves.length)]);
          version++;
        }
      }
      return snapshot();
    },
    async resign(expectedGame) {
      if (active && current && !current.winner && matches(expectedGame, gameId)) {
        const loser = settings.mode === 0 ? 3 - aiColor() : current.turn;
        current.winner = 3 - loser;
        current.winLine = null;
        version++;
      }
      return snapshot();
    },
    async cancel_game(expectedGame) {
      if (matches(expectedGame, gameId)) { active = false; version++; }
      return { ok: true };
    },
    async set_stats_enabled(enabled, expectedGame) {
      if (matches(expectedGame, gameId)) settings.stats = enabled;
      return this.stats();
    },
    async stats() { return { stats: null, gameId, version, busy: false }; },
    exit_app() { window.close(); },
  };
})();

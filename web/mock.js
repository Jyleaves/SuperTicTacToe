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

  async function mockAiTurn(st) {
    await sleep(600);
    const moves = legalMoves(st);
    if (moves.length) applyMove(st, ...moves[Math.floor(Math.random() * moves.length)]);
  }

  window.MockBackend = {
    async precompile_status() {
      return { ready: true, progress: 100 };   // Mock 无需编译
    },
    async new_game(settings) {
      const st = {
        cells: Array.from({ length: 9 }, () => Array(9).fill(0)),
        grids: Array(9).fill(0), forced: null, turn: CIRCLE,
        lastMove: null, winner: 0, winLine: null,
      };
      if (settings.mode === 0 && settings.first === 1) st.turn = CROSS;
      return st;
    },
    async play(sub, cell) {
      const st = window.S ? window.S.game : null;
      if (!st || st.winner) return st;
      if (!legalMoves(st).some(m => m[0] === sub && m[1] === cell)) return st;
      applyMove(st, sub, cell);
      return st;
    },
    async ai_move() {
      const st = window.S ? window.S.game : null;
      if (!st || st.winner) return st;
      if (window.S.settings.mode === 0 && st.turn === CROSS) await mockAiTurn(st);
      return st;
    },
    async resign() {
      const st = window.S ? window.S.game : null;
      if (st && !st.winner) {
        const loser = window.S.settings.mode === 0 ? CIRCLE : st.turn;
        st.winner = loser === CIRCLE ? CROSS : CIRCLE;
        st.winLine = null;
      }
      return st;
    },
    exit_app() { window.close(); },
  };
})();

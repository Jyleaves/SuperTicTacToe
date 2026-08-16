/* ============================================================
   超级井字棋 · 前端逻辑
   游戏状态全部由 Python 后端持有（pywebview js_api），
   本文件只负责：渲染、交互、动画、音效。
   （在普通浏览器中打开时使用内置 Mock 后端，便于开发调试。）
   ============================================================ */
'use strict';

/* ---------------- 常量 ---------------- */
const CIRCLE = 1, CROSS = 2;
const WIN_LINES = [
  [0, 1, 2], [3, 4, 5], [6, 7, 8],
  [0, 3, 6], [1, 4, 7], [2, 5, 8],
  [0, 4, 8], [2, 4, 6],
];

/* ---------------- 全局状态 ---------------- */
const S = {
  settings: { mode: 0, difficulty: -1, first: 0, goal: 1, sound: true, stats: true },
  lastStats: null,   // 最近一次 AI 搜索的终局分布 [圈赢, 平, 叉赢]
  game: null,      // 最近一次后端返回的状态
  pending: false,  // 等待后端响应（AI 思考中）
};
window.S = S;   // 暴露给 mock.js（仅浏览器调试用）

/* ---------------- 难度档位（设置页 / 对局 HUD 共用，与 ai.DIFFICULTY_BUDGETS 对应） ---------------- */
const DIFFICULTIES = [
  { value: -1, label: '幼稚', dot: '#22C55E' },
  { value: 0,  label: '简单', dot: '#2E6BE6' },
  { value: 1,  label: '中等', dot: '#F59E0B' },
  { value: 2,  label: '困难', dot: '#E5484D' },
  { value: 3,  label: '大师', dot: '#8B5CF6' },
];

/* ---------------- 后端桥（pywebview / Mock） ----------------
   注意：pywebview 的 window.pywebview 是页面加载后才注入的，
   因此这里用惰性 Proxy：每次调用时再决定走真实桥还是 Mock
   （Mock 定义在 mock.js，仅浏览器调试用）。 */
const bridge = new Proxy({}, {
  get(_, prop) {
    const api = (window.pywebview && window.pywebview.api) || window.MockBackend;
    if (!api) return undefined;
    const v = api[prop];
    return typeof v === 'function' ? v.bind(api) : v;
  },
});

/* ============================================================
   音效（WebAudio 合成，零资源）
   ============================================================ */
let actx = null;
function ensureAudio() {
  if (!S.settings.sound) return;
  try {
    if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
    if (actx.state === 'suspended') actx.resume();
  } catch (e) { actx = null; }
}
function tone(freq, t0, dur, type = 'sine', vol = 0.16) {
  if (!S.settings.sound) return;    // 关闭后即使 actx 已初始化也静音（曾漏检）
  if (!actx) return;
  const o = actx.createOscillator(), g = actx.createGain();
  o.type = type; o.frequency.value = freq;
  g.gain.setValueAtTime(0.0001, t0);
  g.gain.exponentialRampToValueAtTime(vol, t0 + 0.012);
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  o.connect(g); g.connect(actx.destination);
  o.start(t0); o.stop(t0 + dur + 0.06);
}
const SFX = {
  click() { if (!actx) return; const t = actx.currentTime; tone(720, t, 0.05, 'triangle', 0.10); },
  place() { if (!actx) return; const t = actx.currentTime; tone(520, t, 0.09, 'sine', 0.16); tone(780, t + 0.045, 0.12, 'sine', 0.10); },
  win()   { if (!actx) return; const t = actx.currentTime; [523, 659, 784, 1047].forEach((f, i) => tone(f, t + i * 0.11, 0.30, 'sine', 0.15)); },
  lose()  { if (!actx) return; const t = actx.currentTime; [392, 311, 262].forEach((f, i) => tone(f, t + i * 0.14, 0.34, 'sine', 0.13)); },
  tie()   { if (!actx) return; const t = actx.currentTime; tone(440, t, 0.18, 'sine', 0.13); tone(440, t + 0.24, 0.18, 'sine', 0.13); },
};

/* ============================================================
   设置持久化
   ============================================================ */
function loadSettings() {
  try {
    const s = JSON.parse(localStorage.getItem('sttt.settings'));
    if (s) Object.assign(S.settings, s);
  } catch (e) { /* 忽略 */ }
}
function saveSettings() {
  try { localStorage.setItem('sttt.settings', JSON.stringify(S.settings)); } catch (e) { /* 忽略 */ }
}

/* ============================================================
   屏幕切换
   ============================================================ */
function showScreen(id) {
  document.querySelectorAll('.screen').forEach(el => el.classList.remove('active'));
  document.getElementById('screen-' + id).classList.add('active');
}

/* ============================================================
   设置界面
   ============================================================ */
function buildSettings(containerId, inGame) {
  const rows = document.getElementById(containerId);
  rows.innerHTML = '';

  const defs = [
    { key: 'mode', label: '模式', aiOnly: false,
      options: [[0, '人机对战'], [1, '人人对战']] },
    { key: 'difficulty', label: '难度', aiOnly: true,
      options: DIFFICULTIES.map(d => [d.value, d.label]) },
    { key: 'first', label: '先后手', aiOnly: true,
      options: [[0, '人先手'], [1, '电脑先手']] },
    { key: 'goal', label: 'AI 目标', aiOnly: true,
      options: [[1, '赢得对局'], [-1, '输掉对局']] },
    { key: 'sound', label: '音效', aiOnly: false, instant: true,
      options: [[true, '开'], [false, '关']] },
    { key: 'stats', label: '胜率条', aiOnly: false, instant: true,
      options: [[true, '开'], [false, '关']] },
  ];

  const shown = inGame ? defs.filter(d => d.instant) : defs;   // 对局内只留即时生效项
  for (const d of shown) {
    const row = document.createElement('div');
    row.className = 'setting-row' + (d.aiOnly ? ' ai-only' : '');

    const label = document.createElement('span');
    label.className = 'setting-label';
    label.textContent = d.label;

    const seg = document.createElement('div');
    seg.className = 'seg';
    for (const [val, text] of d.options) {
      const b = document.createElement('button');
      b.textContent = text;
      b.dataset.val = String(val);
      const active = () => b.classList.toggle('active', S.settings[d.key] === val);
      active();
      b.addEventListener('click', () => {
        ensureAudio(); SFX.click();
        S.settings[d.key] = val;
        seg.querySelectorAll('button').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        if (d.key === 'mode') syncAiRows(rows);
        if (d.key === 'stats') renderStats();   // 开关即时生效
        saveSettings();
      });
      seg.appendChild(b);
    }
    row.append(label, seg);
    rows.appendChild(row);
  }
  syncAiRows(rows);
}

function syncAiRows(rows) {
  const hidden = S.settings.mode === 1; // 人人对战隐藏 AI 选项
  rows.querySelectorAll('.setting-row.ai-only').forEach(r => {
    r.classList.toggle('hidden-row', hidden);
  });
}

/* ============================================================
   规则界面（两页，内容取自 超级井字棋规则.docx）
   ============================================================ */
const RULE_PAGES = [
  `
  <h3>基本规则</h3>
  <p>超级井字棋由一个大 <b>3×3</b> 井字棋盘组成，其中每个单元格自身也是一个独立的 <b>3×3</b> 井字棋盘。</p>
  <p>游戏目标：在大棋盘上形成三连（横、竖、斜）。玩家必须先在<b>小棋盘</b>上赢得局部胜利，才能在大棋盘上占领对应格子。</p>
  <p>游戏开始时，先手可以在任意 <b>81</b> 个格子中落子。</p>
  <h3>颜色图例</h3>
  <div class="legend">
    <div class="legend-item"><span class="legend-chip green"></span>可落子</div>
    <div class="legend-item"><span class="legend-chip red"></span>○ 已占领</div>
    <div class="legend-item"><span class="legend-chip blue"></span>× 已占领</div>
    <div class="legend-item"><span class="legend-chip gray"></span>平局</div>
  </div>
  `,
  `
  <h3>落子决定对方的下一个区域</h3>
  <p>你在某大格的 <span class="hl">第 N 小格</span> 落子，对方下一步就<b>必须</b>在 <span class="hl">第 N 大格</span> 落子：</p>
  <div class="diagram">
    <div>
      <div class="mini">
        <span class="mcell"></span><span class="mcell"></span><span class="mcell"></span>
        <span class="mcell"></span><span class="mcell hl"></span><span class="mcell"></span>
        <span class="mcell"></span><span class="mcell"></span><span class="mcell"></span>
      </div>
      <div class="mini-caption">下在第 5 小格</div>
    </div>
    <span class="arrow">→</span>
    <div>
      <div class="mini mini-board">
        <span class="mcell"></span><span class="mcell"></span><span class="mcell"></span>
        <span class="mcell"></span><span class="mcell hl"></span><span class="mcell"></span>
        <span class="mcell"></span><span class="mcell"></span><span class="mcell"></span>
      </div>
      <div class="mini-caption">对方必须下在第 5 大格</div>
    </div>
  </div>
  <p>一旦某小棋盘已分出胜负或被填满，就<b>不能再在其中落子</b>。</p>
  <p>若你被指派到已完成的区域，则可以<b>自由选择</b>任何未完成的区域落子。</p>
  <h3>特殊模式</h3>
  <p>默认模式下 AI 会全力取胜；在设置中选择「<span class="hlr">输掉对局</span>」后，AI 会故意放水，适合轻松休闲的对局。</p>
  `,
];

let rulePage = 0;
function renderRules() {
  document.getElementById('rules-body').innerHTML = RULE_PAGES[rulePage];
  document.getElementById('rule-page-ind').textContent = (rulePage + 1) + ' / ' + RULE_PAGES.length;
  document.getElementById('rules-body').scrollTop = 0;   // 翻页回到顶部
}
function flipRulePage(d) {
  rulePage = (rulePage + d + RULE_PAGES.length) % RULE_PAGES.length;
  renderRules();
}

/* ============================================================
   棋盘渲染
   ============================================================ */
function buildBoard() {
  const board = document.getElementById('board');
  board.innerHTML = '';
  for (let sub = 0; sub < 9; sub++) {
    const g = document.createElement('div');
    g.className = 'subgrid';
    g.dataset.sub = sub;
    for (let c = 0; c < 9; c++) {
      const cell = document.createElement('div');
      cell.className = 'cell';
      cell.dataset.sub = sub;
      cell.dataset.cell = c;
      cell.addEventListener('click', () => onCellClick(sub, c));
      cell.addEventListener('mouseenter', () => onCellHover(sub, c, true));
      cell.addEventListener('mouseleave', () => onCellHover(sub, c, false));
      g.appendChild(cell);
    }
    board.appendChild(g);
  }
  const line = document.createElement('div');
  line.className = 'win-line';
  line.innerHTML = '<div class="bar"></div>';
  board.appendChild(line);
}

function updateBoard(st) {
  const board = document.getElementById('board');
  const legal = new Set(st.moves.map(m => m[0] * 9 + m[1]));

  board.querySelectorAll('.cell').forEach(cell => {
    const sub = +cell.dataset.sub, c = +cell.dataset.cell;
    cell.className = 'cell';
    const v = st.cells[sub][c];
    if (v === CIRCLE) cell.classList.add('circle');
    else if (v === CROSS) cell.classList.add('cross');
    if (st.lastMove && st.lastMove[0] === sub && st.lastMove[1] === c) {
      cell.classList.add('last');
    }
    if (!st.winner && legal.has(sub * 9 + c)) cell.classList.add('playable');
  });

  board.querySelectorAll('.subgrid').forEach(g => {
    const sub = +g.dataset.sub;
    g.classList.remove('won-circle', 'won-cross', 'tie', 'playable', 'forced');
    const gs = st.grids[sub];
    if (gs === CIRCLE) g.classList.add('won-circle');
    else if (gs === CROSS) g.classList.add('won-cross');
    else if (gs === 3) g.classList.add('tie');
    else if (!st.winner) {
      const forced = st.forced !== null && st.grids[st.forced] === 0 && st.forced === sub;
      if (forced || st.forced === null) g.classList.add('playable');
      if (forced) g.classList.add('forced');
    }
  });

  // 胜利连线
  const line = board.querySelector('.win-line');
  const hasWin = st.winner === CIRCLE || st.winner === CROSS;
  if (hasWin && st.winLine) {
    line.style.opacity = 1;
    styleWinLine(line, st.winLine);
  } else {
    line.style.opacity = 0;
  }
}

function styleWinLine(line, ln) {
  const [a, b, c] = ln;
  const r = Math.floor(a / 3), col = a % 3;
  const same = (x, y) => x === y;
  if (same(r, Math.floor(b / 3)) && same(r, Math.floor(c / 3))) {
    line.style.top = (r * 33.333 + 16.666) + '%';
    line.style.left = '5%'; line.style.width = '90%'; line.style.height = '12px';
    line.style.transform = 'translateY(-50%)';
  } else if (same(col, b % 3) && same(col, c % 3)) {
    line.style.left = (col * 33.333 + 16.666) + '%';
    line.style.top = '5%'; line.style.height = '90%'; line.style.width = '12px';
    line.style.transform = 'translateX(-50%)';
  } else {
    // (0,4,8) 主对角线（左上→右下）：+45°；(2,4,6) 副对角线（右上→左下）：-45°
    const main = (a === 0 && b === 4 && c === 8);
    line.style.left = '50%'; line.style.top = '50%';
    line.style.width = '138%'; line.style.height = '13px';
    line.style.transform = 'translate(-50%,-50%) rotate(' + (main ? 45 : -45) + 'deg)';
  }
  line.querySelector('.bar').style.animation = 'none';
  void line.querySelector('.bar').offsetWidth;  // 重启动画
  line.querySelector('.bar').style.animation = '';
}

/* ============================================================
   实时胜率条：AI 每次 MCTS 搜索后的终局分布（圈赢/平/叉赢）
   ============================================================ */
let statsHadValue = false;   // 上次渲染是否有值（首次空→有触发两端动画）

function renderStats() {
  const bar = document.getElementById('stats-bar');
  if (!bar) return;
  const show = S.settings.stats && S.settings.mode === 0;   // 人机模式 + 开关
  bar.classList.toggle('hidden', !show);
  if (!show) return;
  const st = S.lastStats;
  const empty = document.getElementById('stats-empty');
  const ids = ['stats-c', 'stats-t', 'stats-x'];
  if (!st) {                            // 尚未评估（开局人先手第一手前）
    statsHadValue = false;
    empty.textContent = '等待评估…';
    empty.classList.remove('hidden');
    for (const id of ids) {
      document.getElementById(id).style.width = '0%';
      document.getElementById(id).textContent = '';
    }
    return;
  }
  const firstShow = !statsHadValue;     // 首次从空到有
  statsHadValue = true;
  if (firstShow) {                      // 两端往中间挤（一次性，动画结束移除）
    bar.classList.remove('pop-in');
    void bar.offsetWidth;
    bar.classList.add('pop-in');
    bar.addEventListener('animationend', () => bar.classList.remove('pop-in'),
                         { once: true });
  }
  empty.classList.add('hidden');
  const total = st[0] + st[1] + st[2] || 1;
  const segs = [['stats-c', st[0]], ['stats-t', st[1]], ['stats-x', st[2]]];
  for (const [id, n] of segs) {
    const el = document.getElementById(id);
    const pct = n / total * 100;
    el.style.width = pct.toFixed(1) + '%';
    el.textContent = pct >= 10 ? pct.toFixed(1) + '%' : '';  // 1 位小数；段太窄不显示
  }
}

/* 异步评估轮询：落子/开局的胜率评估在后台线程跑（~0.3s），
   前端轮询取最新结果——落子显示不被评估阻塞。
   后端 worker 已完成版本校验（过期丢弃），前端有值即更新。 */
let statsPollTimer = null;

function pollStats(tries = 20, delay = 60) {
  if (tries <= 0) return;
  clearTimeout(statsPollTimer);
  statsPollTimer = setTimeout(async () => {
    try {
      const r = await bridge.stats();
      if (r.stats) {                         // 有值即显示（快速值 2 万 → 细化后更新）
        S.lastStats = r.stats;
        renderStats();
      }
      if (r.busy && tries > 0) {
        pollStats(tries - 1, 150);           // 仍在细化：继续轮询最终值
      }
    } catch (e) { /* 桥未就绪/对局外，忽略 */ }
  }, delay);
}

/* ============================================================
   顶部信息 / 结算
   ============================================================ */
function updateHud(st, pending) {
  const card = document.getElementById('turn-card');
  const icon = document.getElementById('turn-icon');
  const text = document.getElementById('turn-text');
  const meta = document.getElementById('game-meta');
  const thinking = document.getElementById('thinking');

  icon.className = 'turn-icon';
  if (!st) {
    icon.classList.add('neutral');
    text.textContent = '准备中…';
  } else if (pending) {
    icon.classList.add('neutral');
    text.textContent = 'AI 思考中…';
  } else if (S.settings.mode === 0) {
    const human = st.turn === humanColor();
    icon.classList.add(human ? 'circle' : 'cross');
    text.textContent = human ? '你的回合' : 'AI 的回合';
  } else {
    icon.classList.add(st.turn === CIRCLE ? 'circle' : 'cross');
    text.textContent = (st.turn === CIRCLE ? '○' : '×') + ' 的回合';
  }

  if (S.settings.mode === 0) {
    const d = DIFFICULTIES.find(x => x.value === S.settings.difficulty) || DIFFICULTIES[0];
    const goal = S.settings.goal === 1 ? '赢得对局' : '输掉对局';
    const goalDot = S.settings.goal === 1 ? 'var(--primary)' : '#94A3B8';
    meta.innerHTML =
      '<span class="chip"><i class="dot" style="background:' + d.dot + '"></i>难度 · ' + d.label + '</span>' +
      '<span class="chip"><i class="dot" style="background:' + goalDot + '"></i>' + goal + '</span>';
  } else {
    meta.innerHTML = '<span class="chip"><i class="dot neutral"></i>人人对战</span>';
  }
  thinking.classList.toggle('hidden', !pending);
  renderStats();
}

function showEnd(winner, resigned) {
  const overlay = document.getElementById('end-overlay');
  if (!overlay.classList.contains('hidden')) return;   // 防重复弹出
  const title = document.getElementById('end-title');
  const sub = document.getElementById('end-subtitle');
  sub.classList.add('hidden');
  if (winner === CIRCLE) {
    title.textContent = '○ 圈赢了 ！';
    title.className = 'end-title circle-win';
  } else if (winner === CROSS) {
    title.textContent = '× 叉赢了 ！';
    title.className = 'end-title cross-win';
  } else {
    title.textContent = '平 局';
    title.className = 'end-title tie';
  }
  if (resigned) {
    sub.textContent = '你认输了';
    sub.classList.remove('hidden');
    SFX.lose();
  } else if (winner === CIRCLE || winner === CROSS) {
    if (S.settings.mode === 0) {
      // 人机模式：按玩家颜色判断输赢音效（曾固定播 win——AI 赢也播胜利音）
      if (winner === humanColor()) SFX.win();
      else SFX.lose();
    } else {
      SFX.win();                        // 人人模式：有赢家即播胜利音
    }
  } else {
    SFX.tie();
  }
  overlay.classList.remove('hidden');
}

function hideEnd() {
  document.getElementById('end-overlay').classList.add('hidden');
  document.getElementById('end-subtitle').classList.add('hidden');
}

/* ============================================================
   对局流程
   ============================================================ */
function humanColor() {                 // 人机模式人类棋子颜色（先手=圈惯例）
  return S.settings.first === 0 ? CIRCLE : CROSS;
}

function aiColor() {                      // AI 棋子颜色 = 人类的对侧
  return 3 - humanColor();
}

function myTurn(st) {
  return !st.winner && (S.settings.mode === 1 || st.turn === humanColor());
}

function onCellHover(sub, cell, on) {
  const el = document.querySelector(`.cell[data-sub="${sub}"][data-cell="${cell}"]`);
  if (!el) return;
  el.classList.remove('ghost-circle', 'ghost-cross');
  if (!on) return;
  const st = S.game;
  if (!st || st.winner || S.pending || !myTurn(st)) return;
  if (!st.moves.some(m => m[0] === sub && m[1] === cell)) return;
  el.classList.add(st.turn === CIRCLE ? 'ghost-circle' : 'ghost-cross');
}

async function onCellClick(sub, cell) {
  const st = S.game;
  if (!st || st.winner || S.pending || !myTurn(st)) return;
  if (!st.moves.some(m => m[0] === sub && m[1] === cell)) return;

  try {
    // 第一步：只落人类的子，立即返回并渲染（棋子马上出现）
    const nst = await bridge.play(sub, cell);
    S.game = nst;
    updateBoard(nst);
    updateHud(nst, false);
    if (nst.winner) { showEnd(nst.winner); return; }
    SFX.place();
    pollStats();                          // 人落子后的异步评估
    // 第二步：轮到电脑则异步思考
    if (S.settings.mode === 0 && nst.turn === aiColor()) {
      await doAiTurn();
    }
  } catch (e) {
    console.error('play failed:', e);
  }
}

async function doAiTurn() {
  S.pending = true;
  updateHud(S.game, true);
  try {
    const nst = await bridge.ai_move();
    S.game = nst;
    if (nst.stats) S.lastStats = nst.stats;   // 有值才更新（评估异步，防闪空）
    updateBoard(nst);
    updateHud(nst, false);
    if (nst.winner) { showEnd(nst.winner); return; }
    if (nst.lastMove) SFX.place();
    pollStats();                          // AI 落子后的异步评估
  } catch (e) {
    console.error('ai_move failed:', e);
  }
  S.pending = false;
}

async function startGame() {
  ensureAudio();
  saveSettings();
  hideEnd();
  buildBoard();
  S.game = null;
  // 胜率条保留旧局值直到新评估完成——"直接切换"（有值→有值平滑过渡），
  // 只有真空条（首次进入）才显示"等待评估…"并两端挤入
  S.pending = true;
  showScreen('game');
  updateHud(null, true);
  try {
    const st = await bridge.new_game(S.settings);
    S.game = st;
    if (st.stats) S.lastStats = st.stats; // 有值才更新（评估异步，保留旧值直到新评估就位）
    updateBoard(st);
    updateHud(st, false);
    pollStats();                          // 异步评估完成时更新胜率条
    if (st.winner) { showEnd(st.winner); return; }
    if (S.settings.mode === 0 && st.turn === aiColor()) {
      await doAiTurn();                    // 电脑先手
    }
  } catch (e) {
    console.error('new_game failed:', e);
  }
  S.pending = false;
}

/* ============================================================
   对局内：认输 / 设置弹窗 / 通用确认
   ============================================================ */
let confirmAction = null;   // 确认弹窗的确认回调

function showConfirm(title, yesText, onYes) {
  if (!S.game || S.game.winner) return;
  SFX.click();
  document.getElementById('confirm-title').textContent = title;
  document.getElementById('btn-confirm-yes').textContent = yesText;
  confirmAction = onYes;
  document.getElementById('confirm-overlay').classList.remove('hidden');
}

function closeConfirm() {
  document.getElementById('confirm-overlay').classList.add('hidden');
  confirmAction = null;
}

function onConfirmYes() {
  const action = confirmAction;
  closeConfirm();
  if (action) action();
}

function openConfirmResign() {
  showConfirm('确定要认输吗？', '认输', onResign);
}

async function onResign() {
  if (!S.game || S.game.winner) return;
  try {
    const nst = await bridge.resign();
    S.game = nst;
    updateBoard(nst);
    updateHud(nst, false);
    showEnd(nst.winner, true);
  } catch (e) {
    console.error('resign failed:', e);
  }
}

function confirmExitToMenu() {
  showConfirm('返回主菜单？当前对局将丢失', '返回', () => {
    showScreen('menu');
  });
}

function openInGameSettings() {
  if (!S.game) return;
  SFX.click();
  buildSettings('in-game-setting-rows', true);   // 对局内：只显示即时生效项
  document.getElementById('settings-overlay').classList.remove('hidden');
}

function closeInGameSettings() {
  document.getElementById('settings-overlay').classList.add('hidden');
}



/* ============================================================
   事件绑定
   ============================================================ */
function bindEvents() {
  document.getElementById('btn-start').addEventListener('click', () => { ensureAudio(); SFX.click(); startGame(); });
  document.getElementById('btn-settings').addEventListener('click', () => { ensureAudio(); SFX.click(); showScreen('settings'); });
  document.getElementById('btn-rules').addEventListener('click', () => { ensureAudio(); SFX.click(); rulePage = 0; renderRules(); showScreen('rules'); });
  document.getElementById('btn-exit').addEventListener('click', () => { ensureAudio(); SFX.click(); bridge.exit_app(); });
  document.getElementById('btn-settings-ok').addEventListener('click', () => { SFX.click(); saveSettings(); showScreen('menu'); });
  document.getElementById('btn-settings-back').addEventListener('click', () => { SFX.click(); showScreen('menu'); });
  document.getElementById('btn-again').addEventListener('click', () => { SFX.click(); startGame(); });
  document.getElementById('btn-menu').addEventListener('click', () => { SFX.click(); showScreen('menu'); });
  document.getElementById('btn-rule-prev').addEventListener('click', () => { SFX.click(); flipRulePage(-1); });
  document.getElementById('btn-rule-next').addEventListener('click', () => { SFX.click(); flipRulePage(1); });
  document.getElementById('btn-rule-back').addEventListener('click', () => { SFX.click(); showScreen('menu'); });
  document.getElementById('btn-in-settings').addEventListener('click', openInGameSettings);
  document.getElementById('btn-in-menu').addEventListener('click', confirmExitToMenu);
  document.getElementById('btn-resign').addEventListener('click', openConfirmResign);
  document.getElementById('btn-confirm-cancel').addEventListener('click', closeConfirm);
  document.getElementById('btn-confirm-yes').addEventListener('click', onConfirmYes);
  document.getElementById('btn-in-settings-close').addEventListener('click', closeInGameSettings);

  document.addEventListener('keydown', e => {
    const active = document.querySelector('.screen.active');
    if (!active) return;
    const id = active.id;
    if (id === 'screen-rules') {
      if (e.key === 'ArrowLeft') { flipRulePage(-1); SFX.click(); }
      else if (e.key === 'ArrowRight') { flipRulePage(1); SFX.click(); }
      else if (e.key === 'Escape') showScreen('menu');
    } else if (id === 'screen-game') {
      if (e.key === 'Escape') {
        if (!document.getElementById('settings-overlay').classList.contains('hidden')) closeInGameSettings();
        else if (!document.getElementById('confirm-overlay').classList.contains('hidden')) closeConfirm();
        else showScreen('menu');
      }
    } else if (id === 'screen-settings') {
      if (e.key === 'Escape') showScreen('menu');
      else if (e.key === 'Enter') { SFX.click(); saveSettings(); showScreen('menu'); }
    }
  });
}

/* ============================================================
   首次启动悬浮窗（numba 编译 ~10s；有缓存时 0.5s 就绪，几乎不可见）
   ============================================================ */
// 模糊化文案：简短自然，不卖萌不做作（进度条本身已传达“在加载”）
const ENGINE_TIPS = [
  '正在准备…',
  '马上就好…',
  '即将开始…',
];

async function watchEngineStatus() {
  const el = document.getElementById('engine-overlay');
  const fill = document.getElementById('engine-overlay-fill');
  const txt = document.getElementById('engine-overlay-text');
  if (!el) return;
  // 等待 pywebview 真桥注入（页面加载后才可用）。
  // 注意：不能用 MockBackend 回退——Mock 恒返回 ready，会让轮询误判就绪（D20）。
  for (let i = 0; i < 40 && !window.pywebview; i++) {
    await new Promise(r => setTimeout(r, 250));
  }
  if (!window.pywebview) return;             // 纯浏览器调试（Mock）：无需引擎状态
  const t0 = Date.now();
  let tipIdx = 0;
  txt.textContent = ENGINE_TIPS[0];
  for (let i = 0; i < 200; i++) {            // 最多轮询 100s
    let ready = false;
    try {
      const st = await bridge.precompile_status();
      if (st && st.ready) ready = true;
      else if (st) {
        // 快速就绪（缓存命中）时不显示悬浮窗，避免闪烁
        if (Date.now() - t0 > 900) {
          el.hidden = false;
          requestAnimationFrame(() => el.classList.add('show'));
          fill.style.width = (st.progress || 0) + '%';
        }
        // 文案轮换（每 1.6s 换一句）
        const k = Math.floor((Date.now() - t0) / 2500) % ENGINE_TIPS.length;
        if (k !== tipIdx) { tipIdx = k; txt.textContent = ENGINE_TIPS[k]; }
      }
    } catch (e) { /* 桥未就绪：下一轮再试 */ }
    if (ready) {
      if (!el.hidden) {
        txt.textContent = '准备就绪，开始对局吧！';
        fill.style.width = '100%';
        await new Promise(r => setTimeout(r, 1100));  // 进度走满(0.6s过渡) + 停顿0.5s
        el.classList.remove('show');
        setTimeout(() => { el.hidden = true; }, 600);
      }
      return;
    }
    await new Promise(r => setTimeout(r, 500));
  }
}

/* ============================================================
   启动
   ============================================================ */
// 启动：加载设置 → 构建设置界面 → 绑定事件 → 监听引擎预热（异常写入 window.__bootErr 供调试）
try {
  loadSettings();
  buildSettings('setting-rows');
  bindEvents();
  renderStats();
  watchEngineStatus();
} catch (e) {
  window.__bootErr = String((e && e.stack) || e);
  console.error('boot failed:', e);
}

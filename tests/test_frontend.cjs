const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const appSource = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const definitions = appSource.slice(0, appSource.indexOf('// 启动：加载设置'));
const mockSource = fs.readFileSync(path.join(__dirname, '../web/mock.js'), 'utf8');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function position(overrides = {}) {
  return { gameId: 1, version: 1, winner: 0, turn: 1,
    moves: [[0, 0], [0, 1]], stats: null, lastMove: null, ...overrides };
}

function setup(api) {
  let nextTimer = 0;
  const timers = new Map();
  const elements = new Map();
  const document = {
    querySelectorAll: () => [],
    getElementById(id) {
      if (!elements.has(id)) {
        elements.set(id, { classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } } });
      }
      return elements.get(id);
    },
  };
  const context = vm.createContext({
    window: api ? { pywebview: { api } } : {}, document, console,
    setTimeout(fn) { const id = ++nextTimer; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(definitions, context);
  if (!api) vm.runInContext(mockSource, context);
  vm.runInContext(`
    updateBoard = () => {}; updateHud = () => {}; renderStats = () => {};
    showEnd = () => {}; buildBoard = () => {}; hideEnd = () => {};
    saveSettings = () => {}; ensureAudio = () => {};
  `, context);
  const S = context.window.S;
  S.screen = 'game';
  S.game = position();
  return {
    context, S, timers,
    async nextTimer() {
      const entry = timers.entries().next().value;
      assert.ok(entry, 'expected a scheduled callback');
      timers.delete(entry[0]);
      return entry[1]();
    },
  };
}

test('a pending human move cannot be submitted twice', async () => {
  const reply = deferred();
  let calls = 0;
  const h = setup({ play(sub, cell, version) {
    calls++; assert.equal(version, 1); return reply.promise;
  } });
  h.S.settings.mode = 1;
  const first = h.context.onCellClick(0, 0);
  await h.context.onCellClick(0, 1);
  assert.equal(calls, 1);
  assert.equal(h.S.pending, true);
  reply.resolve(position({ version: 2, turn: 2 }));
  await first;
  assert.equal(h.S.pending, false);
});

test('a late move reply cannot overwrite a new game or clear its pending flag', async () => {
  const reply = deferred();
  const h = setup({ play: () => reply.promise });
  h.S.settings.mode = 1;
  const task = h.context.onCellClick(0, 0);
  h.S.epoch++;
  const nextGame = position({ gameId: 2, version: 9 });
  h.S.game = nextGame;
  h.S.pending = true;
  reply.resolve(position({ version: 2 }));
  await task;
  assert.equal(h.S.game, nextGame);
  assert.equal(h.S.pending, true);
});

test('AI terminal responses release pending', async () => {
  const h = setup({ ai_move: async () => position({ winner: 2, version: 2 }) });
  await h.context.doAiTurn();
  assert.equal(h.S.pending, false);
  assert.equal(h.S.game.winner, 2);
});

test('resigning invalidates an in-flight AI response', async () => {
  const reply = deferred();
  const h = setup({ ai_move: () => reply.promise,
    resign: async () => position({ winner: 2, version: 3 }) });
  const search = h.context.doAiTurn();
  await h.context.onResign();
  reply.resolve(position({ winner: 0, version: 2 }));
  await search;
  assert.equal(h.S.game.winner, 2);
  assert.equal(h.S.game.version, 3);
  assert.equal(h.S.pending, false);
});

test('stats from a previous position are ignored', async () => {
  const reply = deferred();
  const h = setup({ stats: () => reply.promise });
  h.context.pollStats();
  const poll = h.nextTimer();
  h.S.game = position({ version: 2 });
  reply.resolve({ gameId: 1, version: 1, stats: [100, 0, 0], busy: false });
  await poll;
  assert.equal(h.S.lastStats, null);
});

test('both evaluation phases render for the current position', async () => {
  const replies = [
    { gameId: 1, version: 1, stats: [8, 3, 9], busy: true },
    { gameId: 1, version: 1, stats: [80, 30, 70], busy: false },
  ];
  const h = setup({ stats: async () => replies.shift() });
  h.context.pollStats();
  await h.nextTimer();
  assert.deepEqual(h.S.lastStats, [8, 3, 9]);
  assert.equal(h.timers.size, 1);
  await h.nextTimer();
  assert.deepEqual(h.S.lastStats, [80, 30, 70]);
  assert.equal(h.timers.size, 0);
});

test('stopping stats prevents an in-flight reply from rendering or rescheduling', async () => {
  const reply = deferred();
  const h = setup({ stats: () => reply.promise });
  h.context.pollStats();
  const poll = h.nextTimer();
  h.context.stopStatsPolling();
  reply.resolve({ gameId: 1, version: 1, stats: [100, 0, 0], busy: true });
  await poll;
  assert.equal(h.S.lastStats, null);
  assert.equal(h.timers.size, 0);
});

test('leaving during new_game cancels the backend game once its identity arrives', async () => {
  const reply = deferred();
  const cancelled = [];
  const h = setup({ new_game: () => reply.promise,
    cancel_game: async id => { cancelled.push(id); } });
  h.S.screen = 'menu';
  const start = h.context.startGame();
  h.context.showScreen('menu');
  reply.resolve(position({ gameId: 7 }));
  await start;
  assert.deepEqual(cancelled, [7]);
  assert.equal(h.S.screen, 'menu');
  assert.equal(h.S.game, null);
  assert.equal(h.S.pending, false);
});

test('the browser mock supports computer-first and legal versioned state', async () => {
  const h = setup();
  h.S.settings.first = 1;
  h.S.settings.stats = false;
  const api = h.context.window.MockBackend;
  h.S.game = await api.new_game(h.S.settings);
  assert.equal(h.S.game.turn, 1);
  assert.equal(h.S.game.moves.length, 81);
  const search = h.context.doAiTurn();
  await h.nextTimer();
  await search;
  assert.equal(h.S.game.turn, 2);
  assert.equal(h.S.game.cells.flat().filter(Boolean).length, 1);
  assert.equal(h.S.pending, false);
});

test('the browser mock cannot apply a cancelled AI move to a replacement game', async () => {
  const h = setup();
  const api = h.context.window.MockBackend;
  const first = await api.new_game({ mode: 0, first: 1 });
  const search = api.ai_move(first.version);
  const second = await api.new_game({ mode: 0, first: 1 });
  await h.nextTimer();
  const result = await search;
  assert.equal(result.gameId, second.gameId);
  assert.equal(result.cells.flat().filter(Boolean).length, 0);
});


test('starting a game twice issues one request and clears old stats', async () => {
  const reply = deferred();
  let calls = 0;
  const h = setup({new_game() { calls++; return reply.promise; }});
  h.S.screen = 'menu';
  h.S.lastStats = [100,0,0];
  const first = h.context.startGame();
  await h.context.startGame();
  assert.equal(calls, 1);
  assert.equal(h.S.lastStats, null);
  reply.resolve(position());
  await first;
  assert.equal(h.S.pending, false);
});

test('a failed AI request releases input and can be retried', async () => {
  let calls = 0;
  const h = setup({async ai_move() {
    if (++calls === 1) throw new Error('injected bridge failure');
    return position({version: 2});
  }});
  h.context.console = {error() {}};
  await h.context.doAiTurn();
  assert.equal(h.S.pending, false);
  h.context.retryGameRequest();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 2);
  assert.equal(h.S.game.version, 2);
  assert.equal(h.S.pending, false);
});


test('leaving and restarting preserves backend creation order', async () => {
  const replies = [deferred(), deferred()];
  let calls = 0;
  const cancelled = [];
  const h = setup({new_game: () => replies[calls++].promise,
    cancel_game: async id => { cancelled.push(id); }});
  h.S.screen = 'menu';
  const oldStart = h.context.startGame();
  await new Promise(resolve => setImmediate(resolve));
  h.context.showScreen('menu');
  const newStart = h.context.startGame();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 1, 'new creation must wait for the old creation to finish');
  replies[0].resolve(position({gameId: 7}));
  await oldStart;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 2);
  replies[1].resolve(position({gameId: 8}));
  await newStart;
  assert.deepEqual(cancelled, [7]);
  assert.equal(h.S.game.gameId, 8);
  assert.equal(h.S.screen, 'game');
});

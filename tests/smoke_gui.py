"""整机冒烟测试：真实 pywebview 窗口 + 桥接 + 页面 + 完整交互流程。
覆盖：菜单 → 对局 → 落子即时显示 → AI 思考 → 认输 → 设置弹窗 → 规则翻页。
运行：python tests/smoke_gui.py（窗口自动出现并关闭，无需人工操作）
"""

import os
import sys
import threading
import time

import webview

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from super_ttt.server import Api  # noqa: E402

FLOW_JS = """
(async () => {
  const r = {};
  r.jsErrors = [];
  window.addEventListener('error', e => r.jsErrors.push('err:' + e.message));
  window.addEventListener('unhandledrejection', e => r.jsErrors.push('rej:' + String(e.reason)));
  const origErr = console.error;
  console.error = (...a) => { r.jsErrors.push('log:' + a.map(x => String(x)).join(' ')); origErr(...a); };
  const W = ms => new Promise(res => setTimeout(res, ms));
  async function waitFor(fn, ms = 20000) {
    const t0 = Date.now();
    while (true) {
      try { if (fn()) return; } catch (e) {}
      if (Date.now() - t0 > ms) throw new Error('timeout: ' + fn.toString().slice(0, 60));
      await W(120);
    }
  }
  try {
    r.dom = document.querySelectorAll('.screen').length;
    r.menu = document.getElementById('screen-menu').classList.contains('active');
    // 设置：人机/幼稚/人先手/赢得对局/音效关（并直接改 S.settings 模拟已持久化）
    localStorage.setItem('sttt.settings', JSON.stringify(
      {mode:0, difficulty:-1, first:0, goal:1, sound:false}));
    S.settings.sound = false;

    // ---- 开局 ----
    document.getElementById('btn-start').click();
    await waitFor(() => S.game && !S.pending && document.querySelector('.screen.active').id === 'screen-game');
    r.started = {cells: document.querySelectorAll('.cell').length,
                 playable: document.querySelectorAll('.cell.playable').length};

    // ---- 人类落子：棋子必须立即显示（AI 思考尚未结束） ----
    document.querySelector('.cell.playable').click();
    await W(150);   // AI 思考需 1.5s，此刻必然仍在思考中
    r.immediate = {
      humanPieceShown: document.querySelectorAll('.cell.circle').length === 1,
      aiThinking: !document.getElementById('thinking').classList.contains('hidden'),
      turn: S.game.turn,
    };

    // ---- 等 AI 落子完成 ----
    await waitFor(() => S.game && !S.pending && S.game.turn === 1
                  && document.querySelectorAll('.cell.circle, .cell.cross').length === 2);
    r.afterAi = {last: S.game.lastMove, turn: S.game.turn,
                 pieces: document.querySelectorAll('.cell.circle, .cell.cross').length};

    // ---- 认输流程 ----
    document.getElementById('btn-resign').click();
    r.resignConfirmShown = !document.getElementById('confirm-overlay').classList.contains('hidden');
    document.getElementById('btn-confirm-yes').click();
    await waitFor(() => !document.getElementById('end-overlay').classList.contains('hidden'));
    r.resign = {endShown: true,
                subtitle: document.getElementById('end-subtitle').textContent,
                winner: S.game.winner,
                evalStats: S.lastStats};      // 实时 MCTS 评估（AI 落子后应有数据）

    // ---- 结算页返回主菜单 ----
    document.getElementById('btn-menu').click();
    await waitFor(() => document.querySelector('.screen.active').id === 'screen-menu');
    r.endCardMenuWorks = true;

    // ---- 新对局 + 对局内设置弹窗 ----
    document.getElementById('btn-start').click();
    await waitFor(() => S.game && !S.pending && document.querySelector('.screen.active').id === 'screen-game');
    document.getElementById('btn-in-settings').click();
    r.settingsModal = {
      shown: !document.getElementById('settings-overlay').classList.contains('hidden'),
      rows: document.querySelectorAll('#in-game-setting-rows .setting-row').length,
    };
    document.getElementById('btn-in-settings-close').click();
    r.settingsModal.closed = document.getElementById('settings-overlay').classList.contains('hidden');

    // ---- 主菜单按钮：先取消（留在对局），再确认（返回主菜单） ----
    document.getElementById('btn-in-menu').click();
    r.menuConfirmShown = !document.getElementById('confirm-overlay').classList.contains('hidden');
    document.getElementById('btn-confirm-cancel').click();
    r.menuCancelStays = document.querySelector('.screen.active').id === 'screen-game';
    document.getElementById('btn-in-menu').click();
    document.getElementById('btn-confirm-yes').click();
    await waitFor(() => document.querySelector('.screen.active').id === 'screen-menu');
    r.menuConfirmWorks = true;

    // ---- ESC 返回主菜单 ----
    document.getElementById('btn-start').click();
    await waitFor(() => S.game && !S.pending && document.querySelector('.screen.active').id === 'screen-game');
    document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
    await waitFor(() => document.querySelector('.screen.active').id === 'screen-menu');
    r.escWorks = true;

    // ---- 规则页翻页 ----
    document.getElementById('btn-rules').click();
    await waitFor(() => document.querySelector('.screen.active').id === 'screen-rules');
    r.rules = {
      prev: !!document.getElementById('btn-rule-prev'),
      next: !!document.getElementById('btn-rule-next'),
      back: !!document.getElementById('btn-rule-back'),
      page1: document.getElementById('rule-page-ind').textContent,
      panelH1: document.querySelector('.rules-panel').getBoundingClientRect().height,
    };
    document.getElementById('btn-rule-next').click();
    r.rules.pageAfterNext = document.getElementById('rule-page-ind').textContent;
    r.rules.panelH2 = document.querySelector('.rules-panel').getBoundingClientRect().height;
    r.rules.heightStable = Math.abs(r.rules.panelH1 - r.rules.panelH2) < 0.5;
    document.getElementById('btn-rule-prev').click();
    r.rules.pageAfterPrev = document.getElementById('rule-page-ind').textContent;
    document.getElementById('btn-rule-back').click();
    r.rules.backToMenu = document.querySelector('.screen.active').id === 'screen-menu';
    r.rules.indicatorCount = document.querySelectorAll('#rule-page-ind').length;
  } catch (e) {
    r.error = String(e);
  }
  window.__smoke = JSON.stringify(r);
})();
"""


def main():
    api = Api()
    window = webview.create_window(
        "冒烟测试", os.path.join(BASE, "web", "index.html"),
        js_api=api, width=660, height=740, text_select=False, easy_drag=False,
    )

    def run():
        time.sleep(1.5)                     # 等页面加载
        try:
            window.evaluate_js(FLOW_JS)
        except Exception as e:
            print("EVAL ERROR:", repr(e))
        deadline = time.monotonic() + 40
        result = None
        while time.monotonic() < deadline:
            time.sleep(0.3)
            try:
                result = window.evaluate_js("window.__smoke || null")
            except Exception:
                result = None
            if result:
                break
        print("SMOKE RESULTS:", result)
        window.destroy()

    threading.Timer(60, lambda: (window.destroy(), None)).start()  # 兜底
    webview.start(func=run, gui="edgechromium")
    time.sleep(1)


if __name__ == "__main__":
    main()
    print("SMOKE: DONE")

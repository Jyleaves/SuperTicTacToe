"""电脑先手验证：真实窗口，AI（圈）应自动落子 + 胜率条开局即有值"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webview
from super_ttt.server import Api

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FLOW = """
(async () => {
  const sum3 = a => a[0] + a[1] + a[2];
  const r = {};
  localStorage.setItem('sttt.settings', JSON.stringify(
    {mode:0, difficulty:-1, first:1, goal:1, sound:false, stats:true}));
  Object.assign(S.settings, {mode:0, difficulty:-1, first:1, goal:1,
                             sound:false, stats:true});
  document.getElementById('btn-start').click();
  await new Promise(res => setTimeout(res, 1500));
  // 开局后：胜率条应有值（空盘评估）、AI 应开始思考
  r.statsAtStart = S.lastStats;
  r.barShown = !document.getElementById('stats-bar').classList.contains('hidden');
  // 等 AI 落子（电脑先手自动 doAiTurn）
  for (let i = 0; i < 100; i++) {
    if (S.game && S.game.lastMove && !S.pending) break;
    await new Promise(res => setTimeout(res, 200));
  }
  r.aiMoved = !!(S.game && S.game.lastMove);
  r.turnAfterAi = S.game ? S.game.turn : null;
  r.statsAfter = S.lastStats;
  r.pendingDone = !S.pending;
  // 诊断：手动调 bridge.ai_move
  try {
    const nst = await bridge.ai_move();
    r.manualMoved = !!nst.lastMove;
    r.manualTurn = nst.turn;
    r.manualStats = nst.stats ? sum3(nst.stats) : null;
  } catch (e) { r.manualErr = String(e && e.message || e); }
  r.bootErr = window.__bootErr || null;
  window.__v = r;
})(); true
"""


def main():
    window = webview.create_window(
        "AI先手验证", os.path.join(BASE, "web", "index.html"),
        js_api=Api(), width=660, height=740, text_select=False, easy_drag=False,
    )

    def run():
        time.sleep(1.5)
        try:
            window.evaluate_js(FLOW)
        except Exception as e:
            print("EVAL ERROR:", repr(e))
            window.destroy()
            return
        deadline = time.monotonic() + 40
        r = None
        while time.monotonic() < deadline:
            time.sleep(0.3)
            try:
                r = window.evaluate_js("window.__v || null")
            except Exception:
                r = None
            if r:
                break
        ok = (r and r['statsAtStart'] and sum(r['statsAtStart']) > 0
              and r['barShown'] and r['aiMoved'] and r['pendingDone']
              and r['statsAfter'] and sum(r['statsAfter']) > 0)
        print("AI先手验证:", "PASS" if ok else "FAIL")
        print("详情:", r)
        window.destroy()

    threading.Timer(60, lambda: (window.destroy(), None)).start()
    webview.start(func=run, gui="edgechromium")


if __name__ == "__main__":
    main()

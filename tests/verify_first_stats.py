"""首显速度验证：从点击开始到胜率条首次有值的时间（生产预热模拟）"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webview
from super_ttt.server import Api
from super_ttt import mcts

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FLOW = """
(async () => {
  const r = {};
  Object.assign(S.settings, {mode:0, difficulty:-1, first:0, goal:1,
                             sound:false, stats:true});
  const t0 = performance.now();
  document.getElementById('btn-start').click();
  // 等开局完成
  for (let i = 0; i < 30 && !(S.game && !S.pending); i++)
    await new Promise(res => setTimeout(res, 50));
  // 等胜率条首次有值
  for (let i = 0; i < 100; i++) {
    if (S.lastStats) break;
    await new Promise(res => setTimeout(res, 50));
  }
  r.firstStatsMs = Math.round(performance.now() - t0);
  r.firstStatsSum = S.lastStats ? S.lastStats[0] + S.lastStats[1] + S.lastStats[2] : 0;
  // 等细化完成（>=180000）
  for (let i = 0; i < 60; i++) {
    if (S.lastStats && S.lastStats[0] + S.lastStats[1] + S.lastStats[2] >= 180000) break;
    await new Promise(res => setTimeout(res, 100));
  }
  r.finalSum = S.lastStats ? S.lastStats[0] + S.lastStats[1] + S.lastStats[2] : 0;
  r.finalMs = Math.round(performance.now() - t0);
  window.__v = r;
})(); true
"""


def main():
    mcts.warmup()   # 预热（模拟生产），消除测试环境编译影响
    window = webview.create_window(
        "首显速度验证", os.path.join(BASE, "web", "index.html"),
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
        deadline = time.monotonic() + 30
        r = None
        while time.monotonic() < deadline:
            time.sleep(0.3)
            try:
                r = window.evaluate_js("window.__v || null")
            except Exception:
                r = None
            if r:
                break
        ok = (r and r['firstStatsSum'] >= 18000 and r['firstStatsMs'] <= 800
              and r['finalSum'] >= 180000)
        print("首显速度:", "PASS" if ok else "FAIL")
        print("详情:", r)
        window.destroy()

    threading.Timer(45, lambda: (window.destroy(), None)).start()
    webview.start(func=run, gui="edgechromium")


if __name__ == "__main__":
    main()

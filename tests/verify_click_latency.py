"""点击即时性验证：点击后棋子应 <100ms 显示（评估异步不阻塞），胜率条随后更新"""
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
  const r = {};
  Object.assign(S.settings, {mode:0, difficulty:-1, first:0, goal:1,
                             sound:false, stats:true});
  document.getElementById('btn-start').click();
  await new Promise(res => setTimeout(res, 800));
  // 等开局完成
  for (let i = 0; i < 50 && !(S.game && !S.pending); i++)
    await new Promise(res => setTimeout(res, 100));
  // 点击落子，测延迟：点击 → 棋子 DOM 出现
  const cell = document.querySelector('.cell.playable');
  const t0 = performance.now();
  cell.click();
  const t1 = performance.now();                       // click 返回时刻
  // 等棋子出现
  let appear = -1;
  for (let i = 0; i < 50; i++) {
    if (document.querySelectorAll('.cell.circle, .cell.cross').length > 0) {
      appear = performance.now() - t0;
      break;
    }
    await new Promise(res => setTimeout(res, 10));
  }
  r.clickReturnMs = Math.round(t1 - t0);
  r.pieceAppearMs = Math.round(appear);
  r.pieces = document.querySelectorAll('.cell.circle, .cell.cross').length;
  // 胜率条随后应更新（异步评估）
  const v0 = S.lastStats;
  await new Promise(res => setTimeout(res, 2500));   // 无预热测试环境：编译 0.5s + 评估排队
  r.statsChanged = JSON.stringify(S.lastStats) !== JSON.stringify(v0);
  r.lastStats = S.lastStats;
  window.__v = r;
})(); true
"""


def main():
    window = webview.create_window(
        "点击即时性验证", os.path.join(BASE, "web", "index.html"),
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
        ok = (r and r['clickReturnMs'] <= 60 and r['pieceAppearMs'] <= 150
              and r['pieces'] > 0 and r['statsChanged'])
        print("点击即时性:", "PASS" if ok else "FAIL")
        print("详情:", r)
        window.destroy()

    threading.Timer(45, lambda: (window.destroy(), None)).start()
    webview.start(func=run, gui="edgechromium")


if __name__ == "__main__":
    main()

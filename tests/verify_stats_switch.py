"""胜率条切换语义验证：再来一局=旧值保持直接切换；首次进入=真空条两端挤"""
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
  const bar = document.getElementById('stats-bar');
  const popinSeen = [];
  new MutationObserver(muts => {
    for (const m of muts) {
      if (m.type === 'attributes' && m.attributeName === 'class'
          && bar.className.includes('pop-in')) popinSeen.push(1);
    }
  }).observe(bar, { attributes: true });
  Object.assign(S.settings, {mode:0, difficulty:-1, first:0, goal:1,
                             sound:false, stats:true});
  // 首次进入：真空条 → 两端挤
  document.getElementById('btn-start').click();
  for (let i = 0; i < 60 && !S.lastStats; i++)
    await new Promise(res => setTimeout(res, 100));
  r.firstPopIn = popinSeen.length > 0;
  r.firstStats = S.lastStats ? S.lastStats[0] + S.lastStats[1] + S.lastStats[2] : 0;
  // 等细化完成
  for (let i = 0; i < 40 && S.lastStats && S.lastStats[0] + S.lastStats[1] + S.lastStats[2] < 180000; i++)
    await new Promise(res => setTimeout(res, 100));
  // 认输 → 再来一局
  document.getElementById('btn-resign').click();
  await new Promise(res => setTimeout(res, 200));
  document.getElementById('btn-confirm-yes').click();
  await new Promise(res => setTimeout(res, 600));
  popinSeen.length = 0;                       // 只观察"再来一局后"的 pop-in
  document.getElementById('btn-again').click();
  // 再来一局后立即：胜率条应保留旧值（非空、非等待）
  r.keepOldAfterAgain = !!S.lastStats;
  r.popInDuringAgain = popinSeen.length > 0;   // 新局不应再触发两端挤
  // 等新局评估完成（直接切换：分布内容变化）
  const oldDist = JSON.stringify(S.lastStats);
  for (let i = 0; i < 60; i++) {
    if (S.lastStats && JSON.stringify(S.lastStats) !== oldDist) break;
    await new Promise(res => setTimeout(res, 100));
  }
  r.finalSum = S.lastStats ? S.lastStats[0] + S.lastStats[1] + S.lastStats[2] : 0;
  r.switched = JSON.stringify(S.lastStats) !== oldDist;
  window.__v = r;
})(); true
"""


def main():
    mcts.warmup()
    window = webview.create_window(
        "胜率条切换语义验证", os.path.join(BASE, "web", "index.html"),
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
        deadline = time.monotonic() + 45
        r = None
        while time.monotonic() < deadline:
            time.sleep(0.3)
            try:
                r = window.evaluate_js("window.__v || null")
            except Exception:
                r = None
            if r:
                break
        ok = (r and r['firstPopIn'] and r['firstStats'] >= 18000
              and r['keepOldAfterAgain'] and not r['popInDuringAgain']
              and r['switched'])
        print("切换语义:", "PASS" if ok else "FAIL")
        print("详情:", r)
        window.destroy()

    threading.Timer(60, lambda: (window.destroy(), None)).start()
    webview.start(func=run, gui="edgechromium")


if __name__ == "__main__":
    main()

"""实时胜率条 UI 验证：AI 评估驱动渲染/百分比/开关/模式逻辑"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webview

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FLOW = """
(async () => {
  const r = {};
  // 1) 无评估（开局人先手）：条可见、显示"等待评估…"
  S.settings.stats = true; S.settings.mode = 0; S.lastStats = null;
  renderStats();
  r.waitShown = !document.getElementById('stats-bar').classList.contains('hidden');
  r.waitText = document.getElementById('stats-empty').textContent;
  // 1.5) 首次空→有：触发两端往中间动画（pop-in class）
  S.lastStats = [50, 20, 30];
  renderStats();
  r.firstShowAnim = document.getElementById('stats-bar').classList.contains('pop-in');
  // 2) 注入评估 [50,20,30] → 三段 50/20/30%
  S.lastStats = [50, 20, 30];
  renderStats();
  r.segs = ['stats-c','stats-t','stats-x'].map(id => {
    const el = document.getElementById(id);
    return el.style.width + '|' + el.textContent;
  });
  r.emptyHidden = document.getElementById('stats-empty').classList.contains('hidden');
  // 3) 开关关 → 隐藏
  S.settings.stats = false; renderStats();
  r.barHiddenWhenOff = document.getElementById('stats-bar').classList.contains('hidden');
  // 4) 人人模式 → 隐藏
  S.settings.stats = true; S.settings.mode = 1; renderStats();
  r.barHiddenInPvP = document.getElementById('stats-bar').classList.contains('hidden');
  // 5) 窄段不显示数字（[98,1,1]）
  S.settings.mode = 0;
  S.lastStats = [98, 1, 1];
  renderStats();
  r.narrow = ['stats-t','stats-x'].map(id =>
    document.getElementById(id).textContent === '');
  window.__v = r;
})(); true
"""


def main():
    window = webview.create_window(
        "实时胜率条验证", os.path.join(BASE, "web", "index.html"),
        js_api=None, width=660, height=740, text_select=False, easy_drag=False,
    )

    def run():
        time.sleep(1.5)
        try:
            window.evaluate_js(FLOW)
        except Exception as e:
            print("EVAL ERROR:", repr(e))
            window.destroy()
            return
        deadline = time.monotonic() + 10
        r = None
        while time.monotonic() < deadline:
            time.sleep(0.3)
            try:
                r = window.evaluate_js("window.__v || null")
            except Exception:
                r = None
            if r:
                break
        ok = (r and r['waitShown'] and r['waitText'] == '等待评估…'
              and r['firstShowAnim']
              and r['segs'] == ['50%|50.0%', '20%|20.0%', '30%|30.0%']
              and r['emptyHidden'] and r['barHiddenWhenOff']
              and r['barHiddenInPvP'] and r['narrow'] == [True, True])
        print("实时胜率条验证:", "PASS" if ok else "FAIL")
        print("详情:", r)
        window.destroy()

    threading.Timer(30, lambda: (window.destroy(), None)).start()
    webview.start(func=run, gui="edgechromium")


if __name__ == "__main__":
    main()

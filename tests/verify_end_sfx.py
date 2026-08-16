"""结束音效验证：人机模式按玩家颜色判输赢，人人模式有赢家即胜利音"""
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
  const calls = [];
  SFX.win = () => calls.push('win');
  SFX.lose = () => calls.push('lose');
  SFX.tie = () => calls.push('tie');
  S.settings.mode = 0;
  // 人先手（人=圈）
  S.settings.first = 0;
  showEnd(CIRCLE, false);      // 玩家赢 → win
  hideEnd();
  showEnd(CROSS, false);       // AI 赢 → lose
  hideEnd();
  showEnd(0, false);           // 平局 → tie
  hideEnd();
  // 电脑先手（人=叉）
  S.settings.first = 1;
  showEnd(CIRCLE, false);      // AI(圈) 赢 → lose
  hideEnd();
  showEnd(CROSS, false);       // 玩家赢 → win
  hideEnd();
  // 认输
  showEnd(CROSS, true);        // 认输 → lose
  hideEnd();
  // 人人模式
  S.settings.mode = 1;
  showEnd(CIRCLE, false);      // 有赢家 → win
  r.calls = calls.join(',');
  window.__v = r;
})(); true
"""


def main():
    window = webview.create_window(
        "结束音效验证", os.path.join(BASE, "web", "index.html"),
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
        expect = 'win,lose,tie,lose,win,lose,win'
        ok = r and r['calls'] == expect
        print("结束音效验证:", "PASS" if ok else "FAIL")
        print("详情:", r)
        window.destroy()

    threading.Timer(30, lambda: (window.destroy(), None)).start()
    webview.start(func=run, gui="edgechromium")


if __name__ == "__main__":
    main()

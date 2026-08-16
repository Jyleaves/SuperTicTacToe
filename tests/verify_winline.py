"""胜利黄线全场景验证：8 种三连方向 × 圈/叉双方 = 16 场景。
运行：python tests/verify_winline.py

验证每个场景下黄线的：
1. 方向（transform matrix：水平/垂直/主对角 +45°/副对角 -45°）
2. 位置（水平线行中心 / 垂直线列中心）
3. 尺寸与可见性

运行：python tests/verify_winline.py（自动弹窗并关闭）
"""

import os
import sys
import threading
import time

import webview

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from super_ttt.server import Api  # noqa: E402

# 8 条线：winLine + 期望类型
LINES = [
    ([0, 1, 2], 'h', 0),   # 顶行
    ([3, 4, 5], 'h', 1),   # 中行
    ([6, 7, 8], 'h', 2),   # 底行
    ([0, 3, 6], 'v', 0),   # 左列
    ([1, 4, 7], 'v', 1),   # 中列
    ([2, 5, 8], 'v', 2),   # 右列
    ([0, 4, 8], 'd', 45),  # 主对角（左上->右下）
    ([2, 4, 6], 'd', -45), # 副对角（右上->左下）
]
WINNERS = [1, 2]           # 圈 / 叉

VERIFY_JS = r"""
(async () => {
  const out = [];
  const LINES = __LINES__;
  const WINNERS = __WINNERS__;
  buildBoard();
  showScreen('game');
  await new Promise(r => setTimeout(r, 300));
  const board = document.getElementById('board');
  const boardRect = board.getBoundingClientRect();

  for (const winner of WINNERS) {
    for (const [winLine, kind, idx] of LINES) {
      const cells = Array.from({length: 9}, () => Array(9).fill(0));
      for (const s of winLine) for (let c = 0; c < 9; c++) cells[s][c] = winner;
      const grids = Array(9).fill(0);
      for (const s of winLine) grids[s] = winner;
      const st = {cells, grids, forced: null, turn: winner === 1 ? 2 : 1,
                  lastMove: [winLine[2], 0], winner, winLine, moves: []};
      updateBoard(st);
      await new Promise(r => setTimeout(r, 30));
      const line = document.querySelector('.win-line');
      const cs = getComputedStyle(line);
      const tr = cs.transform;
      const rect = line.getBoundingClientRect();
      let ok = true, why = '';

      // 方向判定
      const m = tr.match(/matrix\(([^)]+)\)/);
      const vals = m ? m[1].split(',').map(Number) : null;
      if (!vals) { ok = false; why = 'no transform'; }
      else if (kind === 'h') {
        if (Math.abs(vals[0] - 1) > 0.01 || Math.abs(vals[3] - 1) > 0.01) { ok = false; why = 'h transform'; }
        // 行中心位置
        const expectY = boardRect.top + boardRect.height * (idx * 33.333 + 16.666) / 100;
        if (Math.abs(rect.y + rect.height / 2 - expectY) > 3) { ok = false; why = 'h pos y=' + rect.y; }
      } else if (kind === 'v') {
        if (Math.abs(vals[0] - 1) > 0.01 || Math.abs(vals[3] - 1) > 0.01) { ok = false; why = 'v transform'; }
        const expectX = boardRect.left + boardRect.width * (idx * 33.333 + 16.666) / 100;
        if (Math.abs(rect.x + rect.width / 2 - expectX) > 3) { ok = false; why = 'v pos x=' + rect.x; }
      } else {
        // 对角线：matrix(cos, sin, -sin, cos) —— b 的符号决定方向
        const sinVal = vals[1];
        const expectSin = idx > 0 ? 0.707107 : -0.707107;   // +45° 顺时针 / -45°
        if (Math.abs(sinVal - expectSin) > 0.01) { ok = false; why = 'd transform ' + tr; }
      }
      // 尺寸与可见性
      if (Math.abs(rect.width) < 10 || Math.abs(rect.height) < 5) { ok = false; why += ' size ' + rect.width + 'x' + rect.height; }
      if (line.style.opacity === '0') { ok = false; why += ' invisible'; }
      // 三连跨整个棋盘：水平/垂直线长度 >= 棋盘 80%
      if ((kind === 'h' && rect.width < boardRect.width * 0.8) ||
          (kind === 'v' && rect.height < boardRect.height * 0.8)) { ok = false; why += ' short'; }

      out.push((ok ? 'PASS' : 'FAIL') + ' winner=' + winner + ' line=' + winLine.join(',')
               + ' (' + kind + idx + ')' + (why ? '  <-- ' + why : ''));
    }
  }
  window.__verify = out.join('\n');
})();
"""


def main():
    api = Api()
    window = webview.create_window("黄线验证", os.path.join(BASE, "web", "index.html"),
                                   js_api=api, width=660, height=740)

    def run():
        time.sleep(1.5)
        import json
        js = VERIFY_JS.replace("__LINES__", json.dumps(LINES)) \
                      .replace("__WINNERS__", json.dumps(WINNERS))
        try:
            window.evaluate_js(js)
        except Exception as e:
            print("EVAL ERROR:", repr(e))
        deadline = time.monotonic() + 20
        result = None
        while time.monotonic() < deadline:
            time.sleep(0.3)
            try:
                result = window.evaluate_js("window.__verify || null")
            except Exception:
                result = None
            if result:
                break
        if result:
            lines = result.split("\n")
            passed = sum(1 for l in lines if l.startswith("PASS"))
            failed = sum(1 for l in lines if l.startswith("FAIL"))
            for l in lines:
                print(l)
            print(f"\n汇总: {passed} 通过 / {failed} 失败 / 共 {len(lines)} 场景")
        else:
            print("超时：未拿到验证结果")
        window.destroy()

    threading.Timer(40, lambda: window.destroy()).start()
    webview.start(func=run, gui="edgechromium")


if __name__ == "__main__":
    main()

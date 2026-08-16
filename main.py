"""SuperTicTacToe 入口：启动 pywebview 原生窗口（Edge WebView2）。

2026-08-16 后端迁移 Rust：规则引擎 + MCTS 全部在 sttt.dll 中运行
（原生机器码，无 JIT 预热），Python 只剩窗口壳与本文件。
"""

from __future__ import annotations

import os
import sys

import webview

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from super_ttt.server import Api  # noqa: E402

WINDOW_SIZE = (660, 740)


def main():
    api = Api()
    webview.create_window(
        "超级井字棋",
        url=os.path.join(BASE_DIR, "web", "index.html"),
        js_api=api,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=(560, 640),
        background_color="#EEF1F7",
        text_select=False,
        easy_drag=False,
    )
    webview.start(gui="edgechromium", debug=False)


if __name__ == "__main__":
    main()

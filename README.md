# 超级井字棋 SuperTicTacToe

[![CI](https://github.com/Jyleaves/SuperTicTacToe/actions/workflows/ci.yml/badge.svg)](https://github.com/Jyleaves/SuperTicTacToe/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

经典超级井字棋（Ultimate Tic-Tac-Toe）桌面版：**Rust 后端 + WebView 前端**，
单文件绿色免安装，开箱即玩。

> Ultimate Tic-Tac-Toe with a Rust engine, bitboard MCTS and a native WebView
> shell. Single-file portable exe, no install, no runtime deps.

## 亮点

- **纯 Rust 引擎**（`rust/`，零第三方依赖）：规则引擎 + 数组池 MCTS
  （UCB1 + SoA 位板节点池 + greedy-1 位板 rollout + 两步树复用 + 必胜手优先）
- **单文件分发**：`SuperTicTacToe.exe` 内嵌全部前端资源，静态链接 CRT，
  拷给别人即可运行（仅需系统自带 WebView2 运行时）
- **快**：单树搜索约 60 万迭代/秒（对比 Python(numba) 版 2.0-2.1×），
  大师档 25.6 万迭代 0.6s 内出招；前端桥往返延迟 0.2ms
- **棋力有据可查**：迁移自经过对弈验证的 Python(numba) 引擎，
  等迭代数对弈多轮持平或领先；每一轮性能优化都附消融实验
  （含"线程数-棋力曲线"：等迭代数下强度随并行度单调下降，
  故 AI 采用单树搜索以保住标定棋力——详见 PROGRESS.md）
- **体验细节**：落子即时反馈、AI 思考动画、强制区域高亮、胜利连线、
  实时胜率条（每步后台 20 万次模拟）、Web Audio 合成音效、设置持久化

## 运行

**方式一（推荐）**：从 [Releases](../../releases) 下载 `SuperTicTacToe.exe`，
放到任意可写目录双击。Windows 11 / 已更新的 Windows 10 开箱即用；
缺少 WebView2 运行时时程序会弹窗指引（装 Edge 浏览器即可解决）。

**方式二（pywebview 回退，需要 Python）**：

```bash
pip install -r requirements.txt
python main.py          # 或双击 start.bat
```

**方式三（源码构建）**：安装 [Rust](https://rustup.rs) 后运行 `rustuild.cmd`，
产出 `SuperTicTacToe.exe` 与 `super_ttt\sttt.dll`。

## 玩法与功能

- 在小棋盘上三连即可占领对应大格，大棋盘三连获胜；
  落子的小格序号决定对手的强制区域
- 人机对战（五档难度：幼稚/简单/中等/困难/大师，限次迭代
  2000/8000/32000/128000/256000）或人人对战
- 先后手选择；AI 目标可选"赢得对局"或"输掉对局"（放水模式）
- 音效 / 胜率条开关，设置持久化

## 架构

```
SuperTicTacToe.exe      wry/tao 窗口应用（前端 web/ 内嵌，JS↔Rust 直连 IPC）
rust/
  src/engine.rs         规则引擎（Python 版用例全量移植）
  src/mcts.rs           数组池 MCTS：位板 rollout / 并行搜索 / 树复用
  src/session.rs        会话：对局状态 + 异步胜率评估 + 基准导出
  src/lib.rs            C ABI + JSON（ctypes 桥接口）
  app/                  窗口应用（wry/tao，前端零改动注入兼容垫片）
super_ttt/
  server.py             pywebview 回退路径的 ctypes 薄桥
  engine.py/ai.py/mcts.py  迁移前 Python(numba) 实现（对弈验证基准，原样保留）
web/                    前端：HTML/CSS/原生 JS，矢量绘制零资源
tests/                  单元测试 / 引擎等价性 / 新旧引擎对弈 / 性能基准
```

两条运行路径共用同一 Rust 引擎：纯 Rust 窗口（JS→ipc→Rust，0.2ms）与
pywebview 回退（JS→Python→ctypes→Rust，1.1ms）。

## 性能与验证（节选，完整数据见 PROGRESS.md）

| 指标 | Python(numba) 迁移前 | Rust 迁移后 |
|---|---|---|
| MCTS 吞吐 · 单线程 | 238-280 千迭代/s | **484-601 千迭代/s**（2.0-2.1×） |
| MCTS 吞吐 · 8 线程 | 1.28-1.63 百万/s | 1.9-3.7 百万/s |
| 桥往返延迟 | 1.14 ms | **0.19 ms** |
| 启动到内容就绪 | >1.5s + 白屏 | 约 0.95s（就绪才显示，无白屏） |

验证方法论：300 局随机游走逐步比对引擎等价；新旧引擎等迭代数对弈
（多轮 60 局，Rust 持平或领先）；等思考时间对弈；每项优化附消融
（PGO/native/arena 子节点布局/线程数等 7 项假设中 5 项被实测否决）。

## 测试

```bash
cargo test --release --manifest-path rust/Cargo.toml   # Rust 18 项
python -m unittest tests.test_engine tests.test_ai -v  # Python 25 项
python tests/smoke_rust_bridge.py                      # 桥冒烟（headless）
python tests/verify_rust_equiv.py                      # 引擎等价性（300 局）
python tests/bench_dll.py                              # 吞吐基准
python tests/duel_rust.py                              # 新旧引擎对弈
python tests/duel_rust_threads.py                      # 线程数-棋力曲线
python tests/smoke_gui.py                              # 真实窗口整机冒烟
```

## 文档

- [PROGRESS.md](PROGRESS.md) — 完整开发/迁移/消融实验记录
- [DEBUG_LOG.md](DEBUG_LOG.md) — 早期移植问题记录

## 致谢

项目从一个 pygame 单文件版本起步，先后经历
Python 重构（pywebview + numba MCTS）与 Rust 迁移两个阶段，
每一步的行为等价性都有对弈实验背书。

## License

[MIT](LICENSE)

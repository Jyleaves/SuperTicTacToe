# 超级井字棋 SuperTicTacToe

[![CI](https://github.com/Jyleaves/SuperTicTacToe/actions/workflows/ci.yml/badge.svg)](https://github.com/Jyleaves/SuperTicTacToe/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Windows 桌面版 Ultimate Tic-Tac-Toe。Rust 规则引擎与 MCTS AI，WebView2 界面，前端资源内嵌于可执行文件。

## 使用

从 [Releases](https://github.com/Jyleaves/SuperTicTacToe/releases) 下载 `SuperTicTacToe.exe`，放到可写目录运行。
需要 Windows x64 和 [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/#download-section)，无需安装 Python。
WebView2 用户数据保存在应用旁的运行时数据目录；该目录不应提交到仓库。

小棋盘三连即可占领对应大格，大棋盘三连获胜。落子的小格序号决定对方下一步的强制区域；目标大格已经结束时可自由落子。

- 人机或人人对战；可选择先后手及 AI 求胜、求败目标。
- 五档难度：幼稚 / 简单 / 中等 / 困难 / 大师，分别最多搜索 2,000 / 8,000 / 32,000 / 128,000 / 256,000 次。
- 音效、胜率条和设置持久化；可用 Tab 与 Enter / 空格操作棋盘。
- AI 思考时可以认输或返回菜单；关闭胜率条会取消后台评估。
- 胜率条显示模拟终局分布，供参考，不是经过校准的真实胜率。

## 构建

安装 Rust MSVC 工具链和 Visual Studio C++ 构建工具，在仓库根目录执行：

```powershell
.\rust\build.cmd
```

产物是根目录的 `SuperTicTacToe.exe` 和 `super_ttt/sttt.dll`。脚本固定产物目录、检查构建及复制结果，映射本机源码路径并移除调试符号；DLL 与 EXE 使用相同的 Release 配置。
默认保留通用 CPU 基线，在支持的 x64 CPU 上运行时选择 BMI2 / POPCNT / LZCNT 加速内核。
不要给通用发布包添加 `target-cpu=native` 或全局指令集要求。

Python 窗口路径需要先构建 DLL，再执行：

```powershell
python -m pip install -r requirements.txt
python main.py
```

`start.bat` 优先启动 EXE，找不到时尝试 Python 窗口。直接打开 `web/index.html` 使用简化的浏览器 Mock，仅供界面开发。

## 测试

需要 Python 3.12、Node.js 22+ 和 Rust；以下命令均从仓库根目录运行。

```powershell
python -m pip install -r requirements.txt numpy numba
.\rust\build.cmd
cargo test --release --locked --manifest-path rust/Cargo.toml
cargo test --release --locked --features portable --manifest-path rust/Cargo.toml
node --test tests/test_frontend.cjs
python -m unittest discover -s tests -p 'test_*.py' -v
python tests/smoke_rust_bridge.py
python tests/verify_rust_equiv.py
python tests/smoke_gui.py
```

`portable` 强制执行软件回退测试。等价性脚本默认跑 300 局，逐步对比合法步、棋盘、强制区域、轮次和终局；GUI 测试会自动打开并关闭真实窗口，失败时返回非零退出码。

## 性能

AI 保持单树搜索与树复用；后台评估只保留最新任务，AI 选步期间暂停评估。
模拟使用共 1.5 KiB 的缺口和胜负查表，节点缓存未展开合法步与终局结果；AI 保持原有节点容量，显示用评估池按工作量分配。前端复用棋盘节点，仅更新改变的棋子样式。

测量条件、原项目 / 修正包 / 最终版对照与局限见 [PROGRESS.md](PROGRESS.md)。求胜模式优先直接制胜，并在存在安全走法时排除允许对手下一步直接获胜的落子。难度和树内搜索策略保持不变；尚无足够对弈证据宣称整体棋力提升。

```powershell
python tests/bench_dll.py
python tests/compare_search.py old.dll new.dll --rounds 7 --cpu 0
python tests/compare_search.py old.dll new.dll --varied --iters 32000 --rounds 7 --cpu 0
python tests/bench_session.py super_ttt/sttt.dll
```

`--varied` 补充 20 个来自不同阶段的随机合法局面，`--seed` 控制棋谱生成。最后一个脚本测量搜索期间的状态查询延迟及 Python/DLL 进程工作集。CPU 核心绑定仅用于可重复基准，不改变应用调度。
EXE 自带 `--bench-engine`、`?bench`、`?startup` 诊断入口，分别生成引擎、桥延迟和启动计时文件；这些本地结果不纳入版本控制。

## 搜索一致性与棋力对照

`--equivalence` 使用独立种子生成每条轨迹，在同一局面、相同搜索种子下比较两版落子、统计和迭代数；双方均复用树，任一结果不同即失败。

```powershell
git fetch --tags
python tests/duel_versions.py --reference v1.1.1 --games 16 --iterations 8000 --seed 90101 --cpu 0 --equivalence --output target/search-equivalence.jsonl
```

`--goal -1` 验证求败模式，`--portable` 强制软件路径，`--threads 4` 验证多线程。
去掉 `--equivalence` 可进行交换颜色的对弈；胜计 1 分、和计 0.5 分，并按开局配对重采样计算区间。
对弈模式增加 `--time-ms 12 --iterations 2000000` 可测等时间预算；每 512 次迭代检查时间，会有超时量。
默认使用 v1.1.1 的完整节点池；对照 v1.1.0 时增加 `--reference-pool adaptive`。
脚本直接测试 Rust 搜索，不包含窗口、后台胜率评估或随机种子的时间来源。
它输出每局种子、棋谱与汇总；胜率和区间只适用于所测开局与预算。

## 目录

| 目录 | 内容 |
|---|---|
| `rust/src/` | 规则、搜索、会话管理及 C ABI |
| `rust/app/` | 原生 Windows 窗口与 IPC |
| `super_ttt/` | Python 桥及保留的 Python 算法参照 |
| `web/` | HTML、CSS、JavaScript 界面 |
| `tests/` | 回归测试、共享棋谱及基准工具 |

[DEBUG_LOG.md](DEBUG_LOG.md) 记录已修问题及复现入口；历史开发记录可通过 Git 历史查阅。

## License

[MIT](LICENSE)

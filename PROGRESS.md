# SuperTicTacToe — 进度维护

## 后端迁移 Rust（2026-08-16）

**动机**：把运算性能压榨到极限 + 降低前端桥延迟。

| 项 | 迁移前 | 迁移后 |
|---|---|---|
| 规则引擎 / MCTS | Python + numba（JIT，启动需编译或读缓存） | Rust（AOT 机器码，`rust/` 目录，零第三方依赖） |
| 节点存储 | numpy 数组池（每节点 81 字节 cells） | SoA 位板池（每节点 4×u64 石子掩码，rollout 零转换） |
| GUI 宿主 | pywebview（JS→.NET/COM→Python 线程→ctypes→DLL） | `SuperTicTacToe.exe`：wry/tao 纯 Rust 窗口（JS→ipc→Rust，前端 web/ 零改动） |
| 桥往返延迟 | 1.14 ms/次 | 0.26 ms/次（~4×） |
| 启动 | 拉起 Python 解释器 + numba 缓存加载 | 直接启动 exe（前端内嵌二进制） |

- 算法逐行移植自 `mcts.py`（UCB1 c=0.8、greedy-1 位板 rollout、必胜手优先、
  两步树复用、root parallelization 投票、难度迭代档位、两阶段胜率评估），
  语义对齐点见 `rust/src/mcts.rs` 头注释。
- `super_ttt/server.py` 改为 ctypes 薄桥（pywebview 路径保留作回退）；
  `engine.py`/`ai.py`/`mcts.py` 原样保留（对弈验证的参照实现 + 回退实现）。
- 构建入口：`rust\build.cmd`（同时产出 `sttt.dll` 与 `SuperTicTacToe.exe`）。

### 第三轮实验：发散优化 + 消融（2026-08-16 深夜）

统一标尺：`tests/bench_dll.py`（DLL 内进程基准 `sttt_bench`，中位 5 轮；
开局/中局/残局 × 1/8 线程，geomean）。**消融总表：**

| 实验 | 假设 | 结果 | 决定 |
|---|---|---|---|
| u128 位板 | 81 位单字运算更快 | LLVM 本就把 u128 下译为双 64 位通道，机器码等价 | 不实施（纸面推演证伪） |
| E5 rollout 去分支（mover 视角缺口集） | 去掉每步 turn 分支 | 零收益（分支预测器已满），语义等价 | 保留（代码更整洁） |
| E1 子节点 arena 连续布局 | 树阶段缓存局部性 | **-40~60% 回退**：预留区使内存足迹膨胀；链表的时间局部性本已够好 | **回退**（发现并修复了两处实现陷阱：纯追加不连续 → 预留区间；顺带定位 example 二进制在 cargo clean 后的代码生成翻转移出测量体系） |
| E2 PGO（profile-generate/use + llvm-tools） | 分支布局优化 +5~15% | +1.1%（噪声内）：分支高度可预测，静态布局已最优 | 不采用（不值得双阶段构建） |
| E3 线程数 × 棋力（用户追问触发完整消融） | 近线性吞吐扩展（16t 4.35M/s） | **等迭代数强度随线程数单调下降**（单进程直测：8000迭代 1t 12:8 / 2t 11:11 / 4t 6:18 / 8t 4:19 / 12t 6:25 / 16t 6:25）——根分裂投票稀释；且难度迭代档位本按单树标定，并行一直在压扁难度梯度 | **生产改为单树（threads=1）**：大师档 603ms（上限 12s），树复用吃满，棋力回到标定强度。首批 t12/tsweep 数据因「monkeypatch 在 Windows spawn worker 失效」作废，已换单进程直测脚本并复核历史种子段（71000: 9:5 领先）排除 DLL 回归 |
| E4 target-cpu=native | AVX 等额外指令集 | 零收益（geomean 1269.5k vs 1269.8k）：标量/位运算负载无自动向量化空间 | 保留可移植指令集组合 |
| Python 端 | 剩余计算开销 | stats 2.2µs / legal_moves 23µs / AI 一步 6ms → **Python 计算占比 0.04%** | 无需优化（纯封送层） |

最终态（vs 迁移前 Python numba）：geomean **1.28M 迭代/s**（开局 467k/2.53M、
中局 586k/3.36M、残局 581k/3.32M @8t）；等迭代对弈多轮累计 Rust 明确 ≥ 平手；
IPC 桥 0.187ms；启动 962ms 到内容就绪。

方法论记录：本轮 5 项实验中 4 项被消融证伪/否决——每项先立假设、再实测、
不达预期即回退，全部留痕。example 二进制在 cargo clean 后出现同源码同参数
构建慢 2× 的代码生成翻转（DLL 产物不受影响），已将基准测量全部迁移到
`sttt_bench` DLL 导出路径规避。

### 第二轮优化（2026-08-16 晚）：启动观感 + 各环节延迟

| 环节 | 优化 | 实测 |
|---|---|---|
| 启动白屏 | 窗口隐藏创建 → DOM 就绪再显示（+3s 兜底） | 显示与 DOM 就绪 0ms 差；不再有浅色空窗 |
| 启动耗时 | 尝试 additional_browser_args（禁后台网络等） | 无实质收益（0.86-0.90s vs 0.84-0.92s，噪声内），~0.9s 为 WebView2 进程拉起硬底；参数保留 |
| 启动计量 | `?startup` 分阶段计时（构建/就绪/显示 → startup_result.txt） | webview 构建 ~880ms，DOM 就绪 +~40ms |
| 搜索吞吐 | backup/mcts_batch 热路径去边界检查；缺口集初始化 81 格线性扫描改已占格位迭代 | 单线程 429-563k → **484-601k/s**；8 线程中局最高 **3.73M/s**（平均 2.0×） |
| 胜率评估 | 单线程 → search_dispatch 并行（核数/4，上限 4） | 首值(2万) 31ms、细化(18万) **125ms**（原 ~450ms，3.6×） |
| 回归 | 18 项单测、300 局等价、对弈抽查 8000/32000 | 全绿；24:15:21 与 15:13:32 |

### 验证记录（2026-08-16）
- Rust 单元测试 18/18（规则引擎用例自 test_engine.py 移植 + 搜索/复用/并行
  + 树池边界回归）；
- 引擎等价性：300 局随机游走（100 局混入 Rust AI 落子），合法步/终局逐局一致；
- 对弈（vs Python numba 现役引擎，tests/duel_rust.py）：
  - 等迭代数（单线程）：8000 迭代 Rust 23:15:22 与 16:20:24（两轮）；
    32000 迭代 22:7:31 —— 同预算持平或领先（Python 侧带树复用加成，Rust
    每手新建树仍不落下风）→ 棋力无下降；
  - 等思考时间（8 线程生产配置，进程数=2 防超订）：0.25s Rust 5:2:13；
    1.0s 1:1:18；
- 吞吐（tests/bench_rust.py）：单线程 429~563k/s vs 238~280k/s（1.8~2.0×），
  8 线程 2.18~3.00M/s vs 1.28~1.63M/s；
- 桥延迟（500 次 ping 均值）：pywebview 1.14ms → wry 纯 Rust 窗口 0.26ms；
- 整机：pywebview 路径 smoke_gui.py 全绿（25 项单测亦全绿）。

### 验证过程中发现并修复/记录的问题
1. **[已修复] Rust 树池边界差一越界**：tree_policy 展开时槽位取自增后的
   free（应为自增前，Python 版传 free_arr[0]-1），free==cap-1 时恰好越界。
   等迭代数模式树打不满池所以从未触发；等时间模式（2M 迭代上限）必触发。
   已修复并加入 tiny_pool_boundary_regression 回归测试。
2. **[已修复] Rust String 非 NUL 结尾**：ctypes c_char_p 读到堆垃圾 →
   ret_json 显式压 '\0'。
3. **[记录] Python(numba) 时间模式并发脆弱点**：多进程并发 + 8 线程 +
   短预算（0.25s）时，mcts.search 调用线程可能被调度器饿死整个预算周期
   （root_visits=0 → 返回 None）。生产不受影响（单搜索 + iters 模式 +
   预热线程）；对弈脚本用进程数=2 规避。属迁移前已有行为，未改动 mcts.py。
4. **[记录] Windows heredoc + multiprocessing 不兼容**：`python - <<EOF`
   里建 Pool，spawn worker 无法重导入 `<stdin>` 主模块。对弈脚本须落盘。

## 项目目标
重构 `E:\Python\All_Python\超级井字棋\超井GUI版.pyw`（pygame 单文件 180KB、base64 内嵌图片）。
要求：美观简洁、操作自然、UX 好；逻辑可移植/修正；分模块维护。

## 技术栈决策（2026-08-07）
| 项 | 选择 | 理由 |
|---|---|---|
| 前端 | HTML + CSS + 原生 JS | 最易做到美观简洁；无构建工具、无 npm |
| 窗口容器 | pywebview 6.2.1（Edge WebView2） | 原生窗口体验、双击即用；WebView2 运行时本机已装（151.0.4129.59） |
| 游戏逻辑 | Python `super_ttt/engine.py` | 纯逻辑、零 GUI 依赖、可单元测试 |
| AI | Python MCTS + 后台线程 | 通过 js_api 桥接异步调用（每次调用独立线程），UI 永不卡顿 |
| 音效 | Web Audio API 合成 | 零资源文件 |
| 图片资源 | 全部 SVG/CSS 矢量绘制 | 彻底消灭 base64 资源（原版 125KB×2 规则截图 + 10 张小图） |

已安装依赖：`pywebview==6.2.1`、`pythonnet`（pywebview 的 Windows 后端）。

## 目录结构
```
E:\Python\SuperTicTacToe\
├── main.py            # 入口：创建 pywebview 窗口（660×740）
├── start.bat          # 双击启动
├── requirements.txt
├── README.md
├── PROGRESS.md        # 本文件
├── DEBUG_LOG.md       # debug/移植问题记录
├── super_ttt\
│   ├── __init__.py
│   ├── engine.py      # 规则引擎（纯逻辑）
│   ├── ai.py          # MCTS AI（UCB1 + 树复用）
│   └── server.py      # pywebview JS↔Python 桥
├── web\
│   ├── index.html     # 单页应用骨架
│   ├── style.css      # 全部视觉样式
│   └── app.js         # 前端逻辑（渲染/交互/动画/音效）
└── tests\
    ├── test_engine.py # 13 项引擎测试
    ├── test_ai.py     # 9 项 AI/桥接测试
    └── smoke_gui.py   # 整机冒烟测试
```

## 功能清单（与原版对齐）
- [x] 主菜单：开始 / 设置 / 规则 / 退出
- [x] 设置：模式（人机/人人）、难度（幼稚/简单/中等/困难/大师）、先后手、AI 目标（赢得/输掉对局）、音效开关；localStorage 持久化
- [x] 对局：9×9 棋盘、强制落子区域规则、悬停幽灵预览、最近落子金色高亮、落子弹入动画、强制格呼吸高亮、胜利连线动画
- [x] AI 思考中动画（后台线程，界面不冻结）
- [x] 结算：圈赢/叉赢/平局 + 再来一局 / 返回主菜单
- [x] 规则页：2 页程序化排版（内容取自 超级井字棋规则.docx），含颜色图例与强制格示意图
- [x] ESC/按钮返回主菜单；窗口关闭干净退出
- [x] 双模式支持：pywebview 真实桥（生产）+ 浏览器 Mock 后端（开发调试）

## 任务状态
- [x] 2026-08-07 原版代码审查（9 项问题，详见 DEBUG_LOG.md）
- [x] 2026-08-07 引擎 engine.py 实现 + 13 项单测通过
- [x] 2026-08-07 AI ai.py 实现 + 9 项单测通过（含求胜/求败、树复用、原版 bug 回归）
- [x] 2026-08-07 前端 web/ 三件套实现
- [x] 2026-08-07 桥接 server.py + main.py
- [x] 2026-08-07 整机冒烟测试通过（真实窗口：菜单→开局→人类落子→AI 真实思考→AI 落子）
- [x] 2026-08-07 README / PROGRESS / DEBUG_LOG 完成
- [x] 2026-08-07 Numba JIT 集成（rollout 提速 6.6 倍，整体迭代 7320→11390/s）+ 冷启动后台预热验证通过
- [x] 2026-08-07 数组化 MCTS（mcts.py）：节点池全 numpy + 全搜索 numba 化（D16 冷启动安全编译顺序）
- [x] 2026-08-08 AI 实力充分测量（用户要求）：D17（reset 复用池 n_children 残留）与 D18（orig_to_engine 不同步 turn）两个重大 bug 修复后——同迭代数持平、同时间碾压（12-15:0-1）、vs 修复版原版 11:8:5 不降反升（详见 DEBUG_LOG）
- [x] 2026-08-08 难度档位限次改造（用户要求）：限时 → 限次 2000/8000/32000/128000 + 软时间上限（配置无关棋力、老电脑等待可控）；档位区分度实验验证
- [x] 2026-08-08 greedy-1 rollout（算法层面）：增量缺口集启发（立即赢→防立即输→随机），零开销，棋力 96 局 52:25:19 显著提升；残局回退（decided>=7 纯随机）
- [x] 2026-08-08 迭代梯度上限探索（用户要求）：800→128k 区间 2 倍差距显著（54-67%），128k→256k 仍显著（8:2），**256k 后 4 倍差距（2048k）也无区分（平局率 83%+）——棋力上限 ~256k-512k 迭代**
- [x] 2026-08-08 大师档（用户要求）：256k 迭代（本机 ~1s/步），NODE_CAP 262k→524k（72MB），前端五档
- [x] 2026-08-08 迭代构成剖析（用户要求）：rollout 60-75% / tree_policy 20-30% / backup 5-10%；fastmath 实测无收益放弃；NODE_CAP 786k→262k 省 75MB 内存；位板方向记录（收益边际低暂缓）；D21 修复（iters+budget=0 超时）
- [x] 2026-08-08 位板 rollout（用户要求：最耗时部分优化/换语言）：C/ctypes 路线否决（调用开销 10μs > rollout 本身）；位板 2.42μs/rollout（1.7x）；池位板化净提速≈0 已回滚
- [x] 2026-08-08 测试清理（用户要求）：一次性诊断/梯度脚本删除，保留通用模板 `tests/duel_gradient.py`（命令行 LO HI 即可跑）
- [x] 2026-08-08 实时胜率条（用户要求）：**MCTS 评估的当前局面胜率**——_mcts_batch 统计终局分布挂 tree.stats；语义=落子后当前局面（统一 _eval_after_move 5000 迭代树提升快评，~20ms 与难度无关）；**开局空盘即评估**、**人落子后立即评估**（胜率随落子变动）、AI 落子后评估；1 位小数显示；D23 修复镜像 bug；电脑先手=圈
- [x] 2026-08-08 评估异步化（用户反馈点击卡顿）：10 万迭代评估改后台线程（锁+版本号防过期覆盖），play/ai_move/new_game 立即返回（棋子 11ms 显示）；前端 pollStats 轮询（150ms×20）更新胜率条；新增 stats() API；修复轮询两个逻辑 bug（版本同步后不更新/版本相同值变化不更新）
- [x] 2026-08-08 结束音效修复（用户反馈）：showEnd 曾固定播胜利音——AI 赢也播 win；改按玩家颜色判断（humanColor），人人模式保持有赢家即胜利音；7 场景专项验证 PASS
- [x] 2026-08-08 胜率条体验打磨（用户反馈）：评估两阶段（2万快速出值 ~60ms → 树提升细化 20 万）+ 异步线程 + busy 标志轮询；startGame 保留旧值（再来一局直接切换，仅真空条两端挤入动画）；D24 轮询状态机系列 bug（有值即停拿不到细化值/busy 时不显示快速值）；音效关闭修复（tone 检查 sound）；pop-in 禁 width transition；new_game 重置 _version 防旧局评估残留；njit 全加 nogil 防 GIL 卡顿
- [x] 2026-08-08 对局内设置精简（用户反馈）：去掉"应用并重开"——对局内弹窗只留即时生效项（音效/胜率条），其余设置回主菜单调；弹窗改名"快速设置"
- [x] 2026-08-08 多线程并行 MCTS（root parallelization）：8 线程实测 164万/s（~10 倍加速，接近线性）；
  主树保留树复用、从树跨搜索池化复用（_SLAVE_POOL）；合并按子节点总访问数投票；
  每线程独立随机流；高迭代（256k）结果与单线程一致（并行更稳定）；难度档位思考时间 ÷5~8
- [x] 2026-08-08 残局精确求解实验（已回滚）：单大格解表（按线条件 DP 构建，19683×2 查表 O(1)）
  + 无 memo minimax 求解器（显式栈）；对弈验证 2000/8000/32000 迭代三档均无棋力差异
  （MCTS 树在残局已充分展开，rollout 精确化不改变决策）→ 回滚，结论记录于 DEBUG_LOG D25
- [ ] 用户验收

## 性能基准（真实 MCTS）
| 版本 | 迭代速率 | 说明 |
|---|---|---|
| 纯 Python（优化后） | 7320/s | rollout 蓄水池→两遍扫描 + 增量线检查 |
| Numba rollout | 11390/s | rollout 39μs（6.6x），但 tree_policy/backup 仍是 Python 对象（占 64%） |
| 数组化 MCTS + 位板 rollout + greedy（当前） | 27~31 万/s | 位板 2.42μs/rollout（1.7x）+ greedy 启发（棋力 2:1）+ 池位板化已回滚；较最初纯 Python 树版 ~127 倍 |
| **+ 多线程并行（8 线程）** | **~160-190 万/s** | root parallelization：主树复用 + 从树池化，合并按总访问数投票；8 线程 ~10 倍加速（近线性） |

难度限次 2000/8000/32000/128000/256000 迭代 + 软时间上限（1/2/3.5/6/12 秒）；AI 全程带树复用（每手继承上一手搜索树）。
并行后本机各档思考时间：大师 256k ≈0.17s（原 0.95s）、困难 128k ≈0.09s（原 0.5s）、中等 32k ≈0.03s。

## 数组化 MCTS 实施记录（2026-08-07）
- [x] mcts.py：节点池（cells/grids/forced/turn/parent/first_child/next_sib/visits/quality/move/legal_count/n_children/step_pool，容量 78 万可扩容）+ njit 核心（_tree_policy/_expand/_backup/_best_child/_winning_child/_find_child/_mcts_batch 批量 512 迭代）+ Python 包装（MCTSTree/search/find_child，签名与 ai.search 兼容）
- [x] 伪随机展开顺序（互质步长 k=(n×step)%legal，杜绝确定性排列弱点）
- [x] 等价性：展开 vs 引擎逐格一致、kth 定位、rollout 分布等价（8%@800 采样）、40 项单测全过
- [x] 棋力：固定时间 0.2s 7:3；必胜手 30/30（完整结论见 DEBUG_LOG 对弈汇总节——D17 修复后已重测）
- [x] server.py 接入 mcts（_ai_node → _ai_tree，find_child 树复用）；冒烟 + 冷启动验证通过
- [x] 调试记录：D11（numba 链式比较）、D12（rollout 污染池）、D13（best=-1 死循环）、D14（展开计数）、D15（容量打满）、D16（编译顺序卡死）——详见 DEBUG_LOG.md

## 后续可做（暂不做，控制范围）
- 落子悔棋/复盘
- 胜率统计面板
- 打包 exe（pyinstaller + pywebview）

# SuperTicTacToe — Debug / 移植问题记录（精简版）

> 原版：`E:\Python\All_Python\超级井字棋\超井GUI版.pyw`（1401 行单文件，"在 bug 中运行"，不要直接读取，里面存在超长base64图片常量）
> 完整历史：见 PROGRESS.md 与聊天记录。此处仅保留结论与教训。

## 移植自原版的问题（P 系列，已全部处理）

| # | 严重度 | 问题 | 处理 |
|---|---|---|---|
| P1 | 严重 | AI 无视强制格：搜索用 `==0` 判可落子区，真实规则用 0/4 哨兵 → 搜非法分支、可下到非强制格、自由局面崩溃 | 新引擎 `forced: int\|None` 单一出口 |
| P2 | 严重 | AI 首手被错误限制在第 4 大格 | 首手与任意手一致（forced=None 自由落子）|
| P3 | 中等 | MCTS 循环内轮询 pygame 事件（吞事件、UI 冻结 1.5-6s）| pywebview 独立线程 + JS Promise + 思考动画 |
| P4 | 中等 | UCB 公式非标准（c 位置错）+ sys.maxsize 奖励 hack | 标准 UCB1 c=1.0（后调 0.8）、奖励 ±1/0 |
| P5 | 中等 | reward 符号约定错误（AI 反向选择，求胜必输）| quality 以对手视角累计，backup 逐层取反，max 选择 |
| P6 | 低 | base64 图片写盘再删（崩溃残留）| 全部 SVG/CSS 绘制 |
| P7 | 低 | hover 每事件最多 81 次 flip | 浏览器 DOM + CSS transition，60fps |
| P8 | 低 | 窗口居中依赖 win32api、字体硬编码 kaiti | pywebview 原生居中 + 字体栈 |
| P9 | 低 | 平局值 3 被误判为三连 | line_winner 明确排除 3 |

## 开发期问题（D 系列，已全部处理）

| # | 严重度 | 问题 | 处理 |
|---|---|---|---|
| D1 | 严重 | pywebview 注入时序：app.js 取 api 为 undefined → ReferenceError → 前端全死 | 惰性 Proxy 桥，调用时再解析 |
| D2 | 中等 | js_api 方法名 snake_case（new_game 非 newGame）| 统一 snake_case |
| D3 | 中等 | 树复用被 `parent is not None` 校验静默禁用（复用根 parent 非空）| 校验只看局面一致性 |
| D4 | 中等 | 电脑先手时引擎 turn 未设叉 | new_game 显式设置 |
| D5 | 低 | 嵌套/扁平 cells 不一致（IndexError）| 统一扁平 81 元组 |
| D6 | 严重 | 规则页页码重复 id（getElementById 只更新第一个）| 删冗余副本 + 冒烟断言 |
| D7 | 中等 | 音效关闭时 actx 空指针 → 按钮全失效 | SFX 全部 `if (!actx) return` |
| D8 | 严重 | 落子不即时显示（play 一次调用含 AI 搜索）| 拆 play/ai_move，人类落子立即返回 |
| D9 | 中等 | 启动期脚本错误终止整个脚本（syncAiRows 漏参）| 补参 + bootErr 调试钩子 |
| D10 | 中等 | 胜利黄线副对角线方向错误（两条对角线都给 +45°）| 主对角 +45° / 副对角 -45°，16/16 验证 |
| D11 | 严重 | numba 链式比较语义错误（`a==b==c` 编译成 `(a==b)==c`）→ 迭代速率虚高 140万/s 假象 | 显式 `a==b and b==c`。**教训：njit 不用链式比较** |
| D12 | 严重 | rollout 就地修改节点池（局面污染，78万/s 假速率）| rollout 前 cells/grids copy |
| D13 | 中等 | tree_policy best=-1 无防御（负索引死循环，warmup 卡死）| `if best < 0: return node` |
| D14 | 中等 | 展开计数 `n_children = k+1` 错位 | `n_children[node] += 1` |
| D15 | 中高 | 容量/迭代上限打满（困难只搜 2.5s/设计 6s）| NODE_CAP 78万、MAX_ITERATIONS 200万、满时重建根 |
| D16 | 严重 | numba 编译顺序依赖卡死（先复杂后简单必卡 >120s）| warmup 固定安全顺序：简单函数 → 复杂函数，冷编译 10.3s |
| D17 | 严重 | reset 复用池时 n_children 残留旧值 → 树停止生长 → 对弈 8:31 大败 | `_expand` 初始化 `n_children[free]=0`。教训：fresh 进程才隔离 numba 随机流；free 数插桩最快定位 |
| D18 | 严重 | orig_to_engine 不同步 turn（AI 以错误视角搜索，0:24 全输假象）| 同步 `g.turn = og.game_turn + 1`；修复后 11:8:5 不降反升 |
| D19 | 性能 | server 树复用从未生效（find_child 找人类落子永远失败）| 保存 _last_ai_move：先 AI 落子再人类落子两步提升 |
| D20 | 前端 | 引擎轮询误回退 Mock（悬浮窗永不显示）| 等待真桥注入（10s 上限）|
| D21 | 中等 | iters 模式 budget=0 立即超时（0 迭代）| `deadline = now + (budget if budget > 0 else 1e9)` |
| D22 | 严重 | 位板 rollout `(rng*n)>>64` 死循环：uint64 乘法截断后 `>>64` 在 x86 上 mod 64 不移位 → k 越界 → pick=-1 | `k = int((rng >> 32) % n)`。**教训：uint64 移位 ≥64 未定义；改后先 5s 冒烟** |
| D23 | 严重 | 实时胜率条镜像（人先手时显示叉高）：`_mcts_batch` 统计条件写反——P5 约定 rollout 返回 +goal 当行动方**落败**，我却按「reward==goal→mover 赢」统计 | reward==-goal ⇔ mover 赢。另修：resign 的 AI 胜者颜色写反（3-_ai_color → _ai_color）；电脑先手改为圈先（先手=圈惯例）；胜率语义=AI 落子后当前局面（树提升 2000 迭代快评） |

## 性能里程碑

| 版本 | 迭代速率 | 备注 |
|---|---|---|
| 重构最初纯 Python 树版 | 2,157/s（中盘稳态实测）| 对照基准 |
| 纯 Python（蓄水池/增量线/moves_left 优化）| 7,320/s | 消融 16 局×4 组无棋力变化 |
| Numba rollout 集成 | 11,390/s | rollout 6.6x，但 tree_policy/backup 仍 Python |
| 数组化 MCTS（mcts.py）| 15-22万/s | 节点池 numpy + 全搜索 njit |
| 位板 rollout | 约 26万/s | SWAR popcount + de Bruijn ctz + xorshift，rollout 1.7x |
| **当前（位板 + greedy-1）** | **约 27-31万/s** | 较最初纯 Python 版 **约 127 倍** |

关键实验结论：
- fastmath 实测反而慢一倍——放弃；NODE_CAP 786k→262k 省 75MB（速率无差）
- C/ctypes 路线否决：ctypes 调用开销 约 10μs/次 > rollout 本身；全 C 重写记录为远期
- 池位板化（节点池存位板）净提速 ≈0——已回滚（LLVM 已把转换优化好，ctz 初始化抵消收益）

## 算法改进（greedy-1 rollout）

- 增量维护"差一格成线"缺口集（2×uint64），每步优先走**立即赢格 → 防立即输格 → 随机**；
  落子后 4 线循环内同时做缺口更新+小格判定——几乎零开销
- 残局回退：`decided >= 7`（剩 ≤2 未决大格）跳过缺口逻辑走纯随机（树已深，启发价值低）
- 棋力：1600 迭代对弈累计 **96 局 52:25:19**（胜率 54% vs 26%）——显著提升
- 速度 A/B：中盘快 9-36%（抢赢/防守提前终结），残局慢 26-170%（绝对量 <1.2μs 无感）

## 迭代梯度与棋力上限（2026-08-08，24 局/组，大池防重建）

- 800→12800：2 倍差距显著（高迭代胜率 54-67%）——迭代数决定棋力
- 12800→25600：钝化（38-42%，平局多）；32k vs 128k（4 倍）46% 显著；128k vs 256k 33% 显著
- **256k 后无区分**：256k vs 512k 平 12/24、512k vs 1024k 平 19/24、512k vs 2048k（4 倍）平 10/12
  ——**棋力上限 约 256k-512k 迭代**（本机 0.8-1.6s/步）
- 平局率随迭代飙升（4→19）——高迭代双方防守趋近完美，棋力增长体现在"少输"
- **教训：判断"饱和"必须用足够大的差距组合**（2 倍差距在高迭代区测不出，4 倍才显著）

## 难度档位（当前五档）

| 档 | 迭代 | 软上限 | 本机耗时 | 老电脑(1万/s) |
|---|---|---|---|---|
| 幼稚 | 2,000 | 1.0s | 8ms | 0.2s |
| 简单 | 8,000 | 2.0s | 30ms | 0.8s |
| 中等 | 32,000 | 3.5s | 119ms | 3.2s |
| 困难 | 128,000 | 6.0s | 约 500ms | 6s 截断 |
| 大师 | 256,000 | 12.0s | 约 950ms | 12s 截断 |

设计：限次为主（迭代数决定棋力、配置无关）+ 软时间上限兜底慢电脑。NODE_CAP=524288（72MB）。

## 对弈结论汇总（D17/D18/D19 修复后）

- 固定迭代（同迭代公平）：数组版与 py 版持平（每迭代效率 ≈1.2-1.3 倍）
- 固定时间：数组版碾压（0.1-1.0s 全部 12-15:0-1）
- 迭代比例扫描：等价点 x0.8-1.0
- vs 修复版原版（0.5s×24）：11:8:5 不降反升
- 先手优势极大（超级井字棋固有）：对弈脚本必须交替先后手

### D24. 胜率条异步轮询状态机系列 bug（2026-08-08，前端）- 4 个轮询 bug 叠加：①版本号同步后不再轮询（stats 从 null→有值但版本未变时不更新）；
  ②有值即停轮询（两阶段评估的细化值永远拿不到）；③busy 时不更新（快速值 2 万从未显示，
  用户白等 700ms）；④再来一局旧局评估残留（new_game 不重置 _version → 旧线程结果污染新局）。
- 修复后模式：**worker 校验版本（过期丢弃）+ 前端"有值即更新 + busy 继续轮询 + 空则继续"**。
- 另修：tone() 只查 actx 导致关闭音效后照播（需查 S.settings.sound）；pop-in 动画被 width
  transition 干扰（动画期间 transition:none）；njit 默认持 GIL 阻塞桥接（全加 nogil=True）；
  startGame 清空 S.lastStats 导致"清空动画+重填"（保留旧值直接切换，仅真空条两端挤入）。
- 教训：异步 UI 更新的状态机要覆盖"值变化但标志相同/有值但未完成/完成但未取值"三类边界；
  颜色语义改动（先手=圈）必须系统排查所有关联逻辑（触发/音效/认输/显示）。

### D25. 残局精确求解实验（2026-08-08，已回滚）
- 动机：迭代饱和（256k+）后 rollout 随机模拟的评估噪声疑似棋力天花板。
- 实施：①显式栈 minimax 求解器（numba 递归不可 cache，赢家优先剪枝，实测 2 大格
  残局最坏 43 节点/0.1ms）；②单大格解表（decided==8 按线条件 3^4=81 种 DP 构建
  19683×2 表，O(1) 查表；线条件 = X 的 4 条大棋盘线两邻居同为 1/2/其他，规避
  "小格三连后大棋盘是否三连"依赖）；③跨 rollout memo 哈希表（世代惰性清空）——
  因无 memo 求解在"空格多"残局爆预算（9 空 = 362k 节点 > 200k）且 2 大格局面空间
  爆炸（3^18）导致命中率低，被解表方案取代。
- 对弈验证（解表 vs 无解表，同迭代公平）：2000it 11:13:8、8000it 7:8:9、32000it 6:5:13
  ——三档均无棋力差异。结论：MCTS 树在残局段已充分展开（合法步少、每分支访问深），
  rollout 评估精度不是瓶颈；残局精确化的价值被树统计完全吸收。
- 已回滚（复杂度/编译时间无谓增加）。教训：**"评估更准"不等于"决策更好"——
  先验证瓶颈假设再做算法投入**（本实验用对弈 A/B 快速证伪）。

### D26. 多线程并行 MCTS（2026-08-08）
- 实现：root parallelization——主线程跑主树（保留树复用），threads-1 个从树线程
  各搜 iters/threads 迭代（独立随机流 _seed_rng），合并按子节点总访问数投票，
  stats 求和；从树跨 search 池化（_SLAVE_POOL，容量 it+65536，避免每次重分配
  27MB 数组）；numba 全 nogil 已就绪，Python 线程直接并行。
- 实测：8 线程 164万/s（约 10 倍加速近线性）；256k 高迭代结果与单线程一致
  （64k 低迭代单线程自身三次都不同——并行反而更稳定，8 树平均方差）。
- 坑：①搜索线程首次调用触发 numba 编译竞争（8 线程同时编译同一函数 → 串行卡死）
  ——warmup 覆盖后无影响；②主树容量检查在并行路径同样需要（从树满时 reset 重建根）。
- 接入：server.py AI_THREADS = min(8, cpu//2)；迭代数 < 5000（幼稚档）不并行
  （线程开销 > 迭代时间）。

## 测试记录

| 时间 | 内容 | 结果 |
|---|---|---|
| 2026-08-07 | 全套单测 40 项（engine 13 + ai 12 + perf_equiv 4 + mcts 11）| ✅ 通过 |
| 2026-08-07 | 整机冒烟（真实窗口全流程，零 JS 错误）| ✅ 通过 |
| 2026-08-07 | 冷启动（清 numba 缓存）：窗口 1s + 后台预热 10.3s，首次 AI 思考 4.8s（一次性）| ✅ 通过 |
| 2026-08-07 | 胜利黄线 16 场景（8 方向 × 圈叉）| ✅ 通过 |
| 2026-08-08 | 位板 rollout 等价性（分布偏差 1.6%）、greedy 棋力（96 局 52:25:19）、梯度上限 | ✅ 通过 |

## 测试工具（tests/）

- `test_*.py`：单测主体（40 项），`python -m unittest tests.test_engine tests.test_ai tests.test_ai_perf_equiv tests.test_mcts`
- `smoke_gui.py`：整机冒烟（真实 pywebview 窗口自动跑全流程）
- `cold_start.py`：冷启动验证（先清 super_ttt/__pycache__/*.nbc）
- `duel_gradient.py`：**通用梯度对弈模板**——`python tests/duel_gradient.py LO HI [--games N] [--cap N] [--procs N]`
- `duel_greedy.py`：A/B 基线（当前 greedy vs 随机 rollout），改动 rollout 后回归用
- `duel_iter.py`：棋力对比框架（固定迭代/固定时间/比例三种模式）
- `regress_numba.py`：快速回归（数组版 vs 纯 Python 树版）
- `verify_winline.py`：胜利黄线 UI 回归；`verify_stats.py`：胜率条渲染验证（冒烟含结算写入断言）；`kill_game.ps1`：杀进程

> 已删除（一次性验证/过时）：diagnose_ai、ablation_ai、duel_ladder3、duel_sat_check、
> duel_gradient_greedy、duel_hi_iter、duel_cap_confirm、duel_bb_vs_scan、duel_difficulty、
> regress_bb、bench_vs_original、verify_statusbar、regress_vs_orig（依赖已删的 duel_original）。
> 梯度实验今后用 `duel_gradient.py` 模板。
- `duel_ladder2.py`：play 函数库（被 duel_gradient 复用，勿删）

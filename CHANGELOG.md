# Changelog

发布时手写维护，格式约定见 [docs/guides/release.md](docs/guides/release.md)。

## [2.0.0] - 2026-09-13

> 架构代际发布：相对 dev 线（三阶段流水线时代，2026-08-22 基线）的全部差异。v2 架构的设计推导见 [docs/architecture/v2-architecture.md](docs/architecture/v2-architecture.md)，关键决策见 [docs/decisions/](docs/decisions/README.md)（ADR-005 ~ ADR-017）。

### 🌟 主要更新

- **业务层组织方式换代：Input→Decision→Output 三阶段流水线退役，"Agent + 工具"登场。** 系统由 Agent 驱动——主播 Agent 自我驱动，直播期间持续运行决策循环，没有观众也有节目推进；游戏 Agent 命令驱动，收到命令才运行任务循环、完成即停；其余能力一律是工具（ToolProvider/ToolRegistry），由 LLM 在决策中经 function calling 直接调用。对开发者：新增直播内容 = 新增一个 Agent 包（`src/agents/<name>/`）或工具提供者 + 配置，框架零改动。MaiBot 桥接决策器退役，决策核心回归 Amaidesu 自有。
- **主播决策内核升级为 StreamerAgent。** 原 decision 阶段 amaidesu_decider 的 planner/replyer/outline 节目单整体迁移为一个自主运行的主播 Agent：决策链 ReAct 化，发言、查记忆、看屏幕、控制虚拟形象都是决策中直接调用的工具；outline 节目单重构为流程单（Rundown），WebUI 可视化编辑、改动实时写穿；新增思考流旁路通道（观察面实时流式 / 播出面整段）与付费消息优先回应；上下文以 live_chat 为单一事实源按四层组装（事实源 / 配对窗口 / 回灌 / 画像）；直播场次显式开场收场，决策过程与 LLM 请求全程可观测。
- **游戏 Agent 成为新范式。** 新增 MinecraftAgent（AI 玩家）：事件驱动决策循环、与主播双向交流（游戏主动上报 game.report + 命令唤醒）、思考旁路、工具轮次、私有 MCP 工具集。原 text_adv_game 采集器重写为观察循环范式的 TextAdvGameAgent：VLM 读取游戏窗口画面（稳定判定 + 双层去重），键鼠与窗口后端执行操作，坐标三步反算，auto 标志控制自主推进。做一个新游戏 = 写一个新的 Agent 包。
- **工具系统与外部连接重建。** 新增通用 MCP 工具源通道，把外部 MCP server 的工具接入 LLM 工具面；支持 Agent 私有 MCP 与工具归属限定、可见名单控制 LLM 可见的工具集合；工具熔断探活自动恢复 + 手动重连（MCP / OBS / VTS / Warudo）。屏幕感知从读屏采集器常驻拼装上下文，改为按需工具 `vision_look_at_screen`：mss 多显示器抓屏 + VLM 读屏，提问式契约，支持区域框选与超时。
- **配置、存储与 WebUI 换代。** 配置从 5 文件（core / model / input / decision / output）收敛为 6 文件（agents / collectors / tools / model / storage / infra），每文件带 `[meta]` 版本元数据，启动自动执行升级钩子并写回；存储统一为单一 SQLite 库，场次、决策、LLM 请求、用量等自动建表入库；WebUI 重构为直播控制台：首页直播间状态板、Agent 控制面（启停 / 心跳 / 自动重建）、流程单编辑器、会话视图对话优先与过程折叠、工具页健康徽标与工具级停用、设置页按配置 Schema 自描述；Trace 链路移除。

### 细节更改

**主播与游戏**

- 主动推进成为决策循环的原生行为：默认开启，配动态开关与场次闸
- 观众命令并入主播 Agent 命令接线（原 command 决策器退役），命令配置化
- 游戏叙事按类型标记接入主播决策上下文，支持待定夺触发
- Sticker 表情事件链移除，VTS 表情 / 动作经工具直接调用
- avatar：新增 vrchat 独立包，热键按名触发，动作目录配置化

**LLM**

- 统一 `generate` 双入口（文本 / 视觉）与流式直通（打字机效果）；错误分类按类型重试；hard_timeout 真生效
- LLM 请求历史与缓存命中落库，Dashboard 展示请求详情与用量统计
- llm_service 按 profile 列表选择与故障切换；修复流式中立消息翻译丢文本与工具上下文缺失

**事件系统**

- 事件名统一语义域常量（CoreEvents），EventBus 支持通配订阅；新增事件拦截器机制（显式优先级）
- NormalizedMessage 统一消息载荷退役，各事件载荷按域定义（`modules/events/payloads/`）
- 新增业务事件：streamer.speech、game.report、上舰、思考流旁路

**基础设施**

- TTS：引擎契约 Protocol 化，同步请求移出事件循环线程池化，未启动时错误转译为简短提示；AudioStreamChannel 音频总线移除，TTS 输出直达播放
- 字幕：从输出处理器改为基础模块（多后端协议）；OBS 字幕窗无边框透明、emoji 单色轮廓兜底渲染、窗口高度随内容自适应（修复多行字幕首尾行被裁）
- 记忆：独立 memory 模块 + 记忆查询工具开放给 Agent；原 context 画像数据结构清理
- 采集器统一为主动推事件模型（后台任务 collect → EventBus）；控制台输入专用线程隔离，规避终端注入干扰
- 模拟器统一为世界发射器（mock / 回放 / LLM 三模式），LLM 模拟器为官方开发基础设施（ADR-006）

**开发者体验**

- 依赖：移除 maim-message、dashscope、python-logging-loki、pygame、librosa；fastmcp 升至 3.x
- 存储拆域仓储，schema 迁移一版本一文件；行尾归一 LF 并启用 .gitattributes
- AGENTS.md 重写为纯行为约束；文档按消费模式重组（architecture / guides / decisions）；关键决策落 ADR-005 ~ ADR-017

### 升级注意

- **配置**：新布局为 `config/` 下 6 文件（agents / collectors / tools / model / storage / infra）。旧 5 文件不再读取，且同名旧 `model.toml` 缺少 `[meta]` 版本元数据会触发启动硬错——升级请先备份并移走旧 `config/` 目录，首次启动自动生成新文件后重填（模型 key、人设、采集器与工具开关等）。`[meta].version` 由系统维护，勿手工改动。
- **数据**：`data/outlines/*.toml` 节目单不再被读取，流程单请在 WebUI 重新编排；旧画像等文件数据无自动迁移。首次启动自动创建 SQLite 库并建表（SCHEMA v9）。
- **行为变化**：主动发言默认开启（可配置关闭）；事件历史仅内存保存，重启即清（Dashboard 实时查询）；场次需显式开场，无默认兜底；MCP 服务端（原对外暴露 Amaidesu 给 Claude Desktop 等外部 AI 客户端的能力）已移除，v2 的 MCP 角色为客户端——接入外部工具。
- **依赖**：`uv sync` 重新安装（fastmcp 主版本升级 2.x → 3.x）。
- **代码库**（开发者）：`src/stages/` 与 `modules/pipeline`、`modules/di`、`modules/streaming`、`modules/context` 移除，扩展点改为 Agent 包 / 工具提供者 / 事件订阅；拉取后建议 `git add --renormalize .` 对齐 LF 行尾。

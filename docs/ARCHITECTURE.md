# 架构重规划（2026-11-27）

> 现状问题：功能是按“先跑起来”拼出来的，五个模块各自混了好几层职责。
> 目标：分层 + 单向依赖 + 每层可单独测试。重构分阶段做，每阶段都能独立部署，
> 46 个提醒回归测试 + 29 个文件访问测试全程保持通过。

## 一、现状：哪里揉在一起了

| 文件 | 现在混了哪些职责 | 症状 |
|---|---|---|
| `agent/server.py`（205 行） | HTTP 路由 + 文件服务 + 提醒调度线程 + 聊天页 HTML 内嵌在 Python 字符串里 | 改前端要动后端文件；调度器和 HTTP 生命周期绑死 |
| `agent/memory.py`（547 行） | SQLite 表结构 + 上下文组装 + 时间解析 + 提醒调度 + 确定性抽取 `extract_explicit` | 最大的文件；改提醒时间规则要翻 500 多行，怕牵连记忆 |
| `agent/graph.py`（220 行） | 图定义 + LLM 客户端创建 + 系统提示拼接 + `save_memory` 直接写库 | 图里直接碰数据库，换模型/换存储都要动图 |
| `agent/tools.py`（306 行） | 工具定义 + 提醒/待办业务逻辑 + `browse_page` 实现 | 工具既是接口又是业务，测一个提醒要拉起整个工具集 |
| `agent/tenant.py`（131 行） | 租户校验 + 路径安全 + 建表 | 权限和存储建表绑在一起 |
| `agent/config.py`（92 行） | 环境变量 + SYSTEM_PROMPT + TOOLS_DOC | 改一句提示词和改配置走同一个文件 |
| `gateway/gateway.py`（431 行） | 登录鉴权 + token + Docker 编排 + 反向代理 + 登录页 HTML | 网关既是认证服务又是容器编排器又是代理 |
| `browser/browser.py` | 共享浏览器服务，无租户概念 | 所有租户共用，截图落不到租户卷里（已知限制） |
| `docker-compose.yml` | 常驻服务 + 构建 profile + 可选服务混写 | 新人看不出哪些是必须的、哪些是可选的 |

根因不是“文件不够多”，而是**依赖方向乱了**：上层（图、工具）直接碰底层（SQLite、HTTP），
底层（memory）又反过来懂业务规则（提醒时间解析）。改一处，牵三处。

## 二、目标分层

上一版这里是一张直线层级图，顺序本身有问题（2026-11-27 自查）——
一张图同时想表达“请求流向”和“依赖方向”，结果两个都错了。
这一版改成两张图：**请求流**（时间顺序，谁先谁后）和**依赖方向**（谁可以 import 谁），
外加进程边界和控制面的正确位置。

### 图 A：一次聊天请求的流向（时间顺序）

```
用户浏览器
  │
  ▼ ① 网关进程（mt-gateway）
┌─────────────────────────────────────────┐
│ Edge（数据面）：登录 / token 校验 / 路由到租户容器 / 限流 │
└─────────────────────────────────────────┘
  │ ② 控制面 Control Plane 不在这里——它不在请求路径上，
  │   是另一条 orthogonal 的线（见下文），别再把它画进请求流里
  ▼ ③ 租户容器进程（mt-tenant-<租户>）
┌─────────────────────────────────────────┐
│ Transport：HTTP 路由、请求校验、统一错误码  │
├─────────────────────────────────────────┤
│ Session：解析/创建会话（创建/恢复/fork/归档）│
├─────────────────────────────────────────┤
│ Application：ChatService 编排一次请求      │
├─────────────────────────────────────────┤
│ Loop Control：驱动循环                    │
│   ┌───────────────────────────────┐     │
│   │ ④ Context：拼上下文（摘要+事实+ │     │
│   │    最近对话+待办/提醒，按预算截断）│     │
│   └───────────────┬───────────────┘     │
│   ┌───────────────▼───────────────┐     │
│   │ ⑤ Model Access：调模型        │     │
│   └───────────────┬───────────────┘     │
│   ┌───────────────▼───────────────┐     │
│   │ ⑥ Tools：有工具调用就执行      │     │
│   │    （权限管线 deny > ask > allow）│  │
│   └───────────────┬───────────────┘     │
│   └────── ⑦ 循环回 ④，直到答完/到上限   │
├─────────────────────────────────────────┤
│ Data：事件日志只追加、Turn 记录、落盘      │
└─────────────────────────────────────────┘
  ▼ ⑧ 响应原路返回
```

关键修正（相对上一版直线图）：

- **控制面移出请求流**：上一版把它画在 Edge 和 Transport 之间，暗示请求要经过它——
  错的。控制面（租户开通/删除/配额/管理 API）根本不在聊天请求的路径上，它是另一条线，
  只对内、不对外，只和 Runtime / Data 打交道。
- **上下文层上移**：上一版把它画在模型接入层之下、领域层之上，等于“先调模型、再拼上下文”——
  时序反了。上下文必须在每次调模型**之前**拼好，所以它属于循环内的一步，紧挨着 Loop，
  读领域层的预算规则和数据层的事件日志。
- **工具层和模型接入层是平级**：上一版一上一下，暗示谁依赖谁——错的。两者都是循环调的
  平级服务，Loop 同时调它们，不存在上下级。
- **进程边界标出来**：上一版把网关进程和租户容器进程的层画在一根柱子里，好像跨进程
  还有上下级——没有。网关进程只有 Edge + Control Plane + Runtime；租户容器里才是
  Transport 及以下。跨进程的只有 HTTP 调用和 docker run。

### 图 B：依赖方向（洋葱，谁可以 import 谁）

```
外层 → 内层，依赖只能向内，不许向外：
┌──────────────────────────────────────────────────────┐
│ Edge / Transport / Session / Application / Loop      │
│   只许 import：Domain 的接口 + 下层服务的接口           │
├──────────────────────────────────────────────────────┤
│ Tools / Model Access / Context（平级服务层）           │
│   只许 import：Domain（规则 + ports）                  │
├──────────────────────────────────────────────────────┤
│ Data / Infra（实现层）                                │
│   实现 Domain 定义的 ports；只许 import Domain         │
├──────────────────────────────────────────────────────┤
│ Domain（核心，不 import 任何东西）                     │
│   Reminder / Memory / Tenant / Grounding 规则 + ports  │
│   不 import：sqlite3、http、langchain、docker          │
└──────────────────────────────────────────────────────┘
```

这一张才是判断“放哪层对不对”的标尺：上一版直线图把 Domain 画在 Data 之上，
按“上层依赖下层”读，等于 Domain 依赖 Data——正好违反“领域层不 import 任何东西”。
正确关系是反过来的：**Data 实现 Domain 的接口，所以 Data 依赖 Domain**，
在依赖图里 Data 在 Domain 外面。以后争论某块代码归属时，看这张图，不看图 A。

### 各层一句话（内容不变，只纠正归属）

- **Edge（网关进程，数据面）**：登录 / token 校验 / 路由到租户容器 / 限流；不认识聊天、提醒、记忆、文件内容。
- **Control Plane（网关进程，另一条线）**：租户开通 / 删除 / 改密码 / 配额 / 用量汇总 / 管理 API；只对内。现状：租户写死在 .env，没有开通流程，删租户要手动删卷。
- **Transport（租户容器）**：HTTP 路由、请求校验、统一错误码、返回 JSON/文件；不认识业务规则、SQL、模型调用。
- **Session**：会话创建 / 恢复 / fork / 归档（照 Codex 的 Thread 原语）；会话 id、所属租户、多轮状态归属；每次请求先解析出会话再交给应用层。
- **Application**：ChatService / ReminderService / TodoService / FileService；把一个用户请求编排成“读上下文 → 调模型/工具 → 落盘”；不认识 HTTP、SQL、LangGraph 图细节。
- **Loop Control**：驱动一轮对话的循环；管工具轮数上限、递归上限、超时、重试、停止条件、流式输出控制；钩子 beforeToolCall / afterToolCall / beforeLLM / afterLLM / onStop（照 pi-agent 的 AgentLoopConfig）；循环本身不知道任何具体工具和业务，只认 config 对象。现在它埋在 graph.py 里（route() + MAX_TOOL_ROUNDS + recursion_limit 30）。
- **Tools**：注册表（发现、参数 schema、租户开关）+ 权限管线（deny > ask > allow，默认拒绝；沙箱与审批分开）+ 执行器（校验 → 权限 → 执行 → 审计日志）；工具自己不写业务逻辑，只调 service。
- **Model Access**：模型调用唯一入口；按任务选模型；重试、超时、降级；计量（每次调用记一笔 token/成本，先做到每租户可查，不承诺计费级精度）；密钥隔离——DeepSeek key 只存在这一层。
- **Context**：把一次请求要用的上下文拼出来（摘要 + 长期事实 + 最近对话 + 待办/提醒 + 待送达提醒，按预算截断，触发压缩）；读领域层预算规则，读数据层事件日志，不碰 HTTP 和模型。现在：load_context 在 memory.py 里读库，_system_prompt 在 graph.py 里拼串，两处各拼一半。
- **Domain**：Reminder（时间解析、状态机、去重规则）、Memory（上下文预算、摘要策略）、Tenant（id 合法性、路径安全）、Grounding（没调工具不许认“已记下”，工具返回时间必须核对）；不 import 任何外部东西。
- **Data**：事件日志（chat_log 只追加，摘要后迁归档 + 记“已压缩”事件，绝不删除）、Turn 记录（每轮工作目录、审批/沙箱策略、模型、本轮 token）、Store（SQLite/Postgres 仓储）；换存储只改这一层。
- **Infra**：Sandbox（沙箱执行）、BrowserService（浏览器服务）、Runtime（Docker 编排、起/停租户容器、闲置回收）、Scheduler（提醒调度器）。
- **表现层**：聊天页 / 登录页前端，拆出 Python 字符串，放 static/，后端只给 JSON 和文件。

横切（不占一层，贯穿所有层，但每项都要有具体模块）：
- 安全 security/：租户隔离、路径安全、审计日志；密钥管理 secrets.py；输入防线 guardrails.py（提示词注入检查点，现在完全没有，而 agent 会执行命令，这是真缺口）。
- 可观测 observability/：结构化日志（JSON、每请求一个 id、每条带租户 id）、指标（延迟、失败率、token 用量）、健康检查端点。
- 配置 config/：分层配置——默认值 → 环境变量 → 每租户覆盖；提示词和配置分开文件。
- 错误 errors/：统一错误分类（可重试 / 不可重试 / 租户隔离违规 / 模型失败）。

## 三、模块落点：现在的文件拆到哪里去

```
agent/
├── config.py            → infra/config.py（只剩环境变量） + domain/prompts.py（SYSTEM_PROMPT/TOOLS_DOC）
├── server.py            → transport/http_server.py（路由+鉴权入口）
│                          + transport/file_server.py（/files/）
│                          + transport/errors.py（统一错误码）
│                          + web/chat.html（从 PAGE 字符串拆出）
├── memory.py（547 行）   → domain/reminder.py（时间解析、状态机、去重规则）
│                          + domain/memory.py（上下文预算、摘要策略）
│                          + infra/store/sqlite_store.py（建表 + 全部 SQL）
│                          + infra/scheduler.py（提醒调度线程）
│                          + app/reminder_service.py（提醒用例：设/改/查/到点）
├── graph.py 的会话部分   → session/session.py（会话创建/恢复/fork/归档）
│                          + session/store.py（会话元数据存在哪）
├── graph.py 的上下文部分  → context/assembler.py（拼一次请求的上下文：
│                            摘要+事实+最近对话+待办/提醒+待送达，按预算截断）
│                          + context/compaction.py（触发压缩、记压缩事件）
├── graph.py             → app/chat_service.py（编排：读上下文→调模型→落盘）
│                          + loop/loop.py（循环驱动：调模型→执行工具→再调模型，
│                            轮数封顶、超时、停止条件；只认 config，不认具体工具）
│                          + loop/config.py（AgentLoopConfig：轮数上限、递归上限、
│                            超时、重试策略；照 pi-agent 的做法）
│                          + loop/hooks.py（beforeToolCall / afterToolCall /
│                            beforeLLM / afterLLM / onStop 钩子点）
│                          + model/router.py（按任务选模型）
│                          + model/client.py（模型 SDK 封装、重试/超时/降级）
│                          + model/metering.py（每租户 token 用量统计）
│                          + domain/prompt.py（系统提示拼接规则）
│                          + infra/graph.py（只剩 LangGraph 节点接线，
│                            不含任何控制逻辑）
├── tools.py             → tools/registry.py（注册表：发现、schema、租户开关）
│                          + tools/permissions.py（新增：deny > ask > allow
│                            权限管线，默认拒绝；沙箱与审批分开）
│                          + tools/executor.py（参数校验→权限→执行→审计日志）
│                          + tools/definitions/*.py（每类工具一个文件：
│                            todo/reminder/memory/file/browser/sandbox）
│                          工具只做参数校验 + 调 service，不再自己写业务
├── memory.py 的存储部分   → data/sqlite_store.py（建表 + 全部 SQL，见 P0）
│                          + data/event_log.py（新增：chat_log 只追加，
│                            摘要后旧行迁归档表 + 记“已压缩”事件，绝不删除）
│                          + data/turns.py（新增：每轮 Turn 记录）
│                          以后加 data/postgres_store.py，领域层不动
├── tenant.py            → domain/tenant.py（id 规则、路径安全）
│                          + infra/store/sqlite_store.py（建表部分）
├── sandbox.py           → infra/sandbox.py（基本不动，只是位置）
└── app.py               →  thin 入口：只做“组装各层 + 启动”，不含逻辑

gateway/
├── gateway.py（431 行）  → edge/auth.py（登录、token 校验、cookie）
│                          + edge/proxy.py（反向代理、Content-Type 透传）
│                          + infra/runtime.py（Docker 编排、闲置回收）
│                          + control/tenants.py（新增：租户开通/删除/改密码/
│                            配额/用量汇总，只对内，不对外暴露）
│                          + web/login.html（从 LOGIN_PAGE 字符串拆出）
└── main.py              → thin 入口

新增的顶层目录（之前完全没有）：
├── observability/       → logging.py（结构化日志）+ metrics.py（指标）
│                          + health.py（健康检查）
├── security/            → secrets.py（密钥管理）+ guardrails.py（输入防线）
├── errors/              → 统一错误分类
└── tests/               → 测试必须进仓库，按层分：
                           tests/unit（domain/loop，零 I/O）
                           + tests/integration（store/transport，用 stub）
                           + tests/e2e（提醒对话等场景，fixture 化）
                           现在 46+29 个测试全在 /tmp 里，不在仓库里，
                           换台机器就丢了——这是 P0 先要修的。

browser/
└── browser.py           → infra/browser_service.py（本阶段只挪位置；
                           按租户隔离是 P4 的可选工作）
```

## 四、实施顺序：分四阶段，每阶段独立可部署

**P0 —— 定边界，不动行为（先做，约最小风险）**
- 先抽 `domain/ports.py` 接口 + 把 `memory.py` 里的纯规则函数（时间解析、状态机、上下文预算）
  原样挪进 `domain/`，不改逻辑；`data/sqlite_store.py` 只挪 SQL；
  同时把模型调用收进 `model/client.py` 一个入口（先只做封装，不加路由/计量）。
- 对照后新增：`data/event_log.py` 先把“删除旧行”改成“迁归档 + 记压缩事件”，
  行为变化只有这一处，单独测、单独提交。
- 全面自查新增：先把 `/tmp/test_reminders.py`、`/tmp/test_files.py` 搬进仓库 `tests/`，
  按 unit/integration/e2e 分好；再把 `load_context` + `_system_prompt` 的拼上下文逻辑
  抽成 `context/assembler.py`（只挪代码，不改截断行为）。
- 验收：现有 46+29 个 stub 测试全部通过，不用改测试。

**P1 —— 传输层瘦身**
- `server.py` 只剩路由；聊天页、登录页拆成 `web/*.html` 静态文件；统一错误码。
- 验收：`curl` 登录、聊天、`/files/calculator.html` 行为不变。

**P2 —— 网关拆分**
- `gateway.py` 拆成 `edge/auth.py` + `edge/proxy.py` + `infra/runtime.py`。
- 全面自查新增：补 `control/tenants.py`（租户开通/删除/配额，只对内）；
  **把“每次部署手动删旧租户容器”这个土办法设计掉**——网关比对租户镜像的 digest，
  发现镜像变了就自动重建租户容器（数据在卷里，不丢），不再靠人记着删。
  这是现在部署流程里最丑的一处，重构不修它等于白重构。
- 验收：登录、租户容器起停、闲置回收、代理行为不变；新镜像部署后租户容器自动重建。

**P3 —— 应用层收口**
- `tools.py` 按用例拆文件，工具只调 service；`graph.py` 只剩图定义，
  编排收进 `app/chat_service.py`。
- `graph.py` 里的循环控制抽出来：`loop/loop.py` + `loop/config.py` + `loop/hooks.py`，
  MAX_TOOL_ROUNDS、recursion_limit、超时、停止条件全部搬过去；
  `infra/graph.py` 只剩 LangGraph 节点接线，不含控制逻辑。
- 对照后新增：`tools/permissions.py` 权限管线（deny > ask > allow，默认拒绝），
  `model/metering.py` 每调用记一笔 token/成本，`data/turns.py` 每轮记 Turn 记录。
- 验收：提醒全流程回归（21:20 设提醒、改时间、裸 8 点问上下午、只提醒一次）；
  另加循环回归：工具轮数到上限强制收尾、多轮工具调用正常、超时不挂死。

**P4 —— 可选演进（不同意就不做）**
- （a）浏览器按租户隔离：每租户一个 browser 容器，截图落进租户卷；
- （b）存储换 Postgres：infra/store 加一个实现，domain 不动；
- （c）加 checkpointer：图内原生多轮消息，替代“历史拼进提示词”。

每一阶段结束都 commit + 推 + 部署验证，不攒一个大爆炸式提交。
部署时老规矩：删旧租户容器，让网关按新镜像重建。

## 五、对照成熟 agent 的借鉴（2026-11-27 调研）

查了三家：Claude Code（Anthropic，闭源，看官方文档和 SDK）、ChatGPT agent / Codex（OpenAI）、
pi-agent（badlogic/pi-mono，开源，读了实际仓库结构）。结论出奇地一致：**核心循环都很小，
真正做产品的是外面那层 harness**。下面是值得抄的，和不值得抄的。

### 对照后本文档实际改了什么（修改记录）

上一版只是在末尾加了这一节，正文的分层图和落点都没动，所以你看不出来——这版把借鉴点
**写进了正文**，改动都在下面这几处，对着看：

| 改动 | 位置 |
|---|---|
| 工具层加“权限管线”：deny > ask > allow，默认拒绝；沙箱与审批分开 | 第二节分层图 → 工具层；第三节 → 新增 `tools/permissions.py`；第四节 P3 |
| 模型接入层加“密钥隔离”：DeepSeek key 只存在这一层 | 第二节分层图 → 模型接入层 |
| 模型接入层计量明确“先做到每租户可查，不承诺计费级精度” | 第二节分层图 → 模型接入层；第四节 P3（`model/metering.py`） |
| 数据层加“事件日志”：chat_log 只追加，摘要后迁归档 + 记“已压缩”事件，绝不删除 | 第二节分层图 → 数据层；第三节 → 新增 `data/event_log.py`；第四节 P0 |
| 数据层加“Turn 记录”：每轮记工作目录、审批/沙箱策略、模型、本轮 token | 第二节分层图 → 数据层；第三节 → 新增 `data/turns.py`；第四节 P3 |
| 前端协议先定下来（聊天/工具调用/提醒统一事件结构） | 第五节“对我们方案的具体修正”第 5 条（尚未落到分层图，等 P1 定协议细节时再画进去） |
| 循环控制层：从应用层和工具层之间独立出来（之前漏了，见 2026-11-27 讨论） | 第二节分层图 → 应用层之下新增“循环控制层”；第三节 → `loop/loop.py`、`loop/config.py`、`loop/hooks.py`；第四节 P3 |
| 全面自查新增（2026-11-27，用户指出“说一个改一个，没有自己的想法”之后，通盘自查一遍）： | —— |
| 会话层：会话创建/恢复/fork/归档之前完全没人管，`chat()` 每次现建图 | 第二节 → 传输层之下新增“会话层”；第三节 → `session/session.py` |
| 上下文层：拼上下文的逻辑散在 `memory.py` 和 `graph.py` 两处，没人认领 | 第二节 → 新增“上下文层”；第三节 → `context/assembler.py`、`context/compaction.py`；第四节 P0 |
| 控制面：租户开通/删除/配额没有管理接口，租户写死在 `.env` | 第二节 → 新增“控制面”；第三节 → `control/tenants.py`；第四节 P2 |
| 可观测、安全、错误：之前只在横切层写了一句话，没有具体模块 | 第二节 → 横切层改成每个都有具体模块；第三节 → 新增 `observability/`、`security/`、`errors/` |
| 测试不在仓库里：46+29 个测试全在 `/tmp`，换机器就丢 | 第三节 → 新增 `tests/` 分层；第四节 P0 先搬测试 |
| 部署土办法：每次发版靠人手动删旧租户容器 | 第四节 P2 → 网关按镜像 digest 自动重建 |
| 直线分层图的误导：实际依赖是有向无环图，工具层直通领域/数据层 | 第二节末 → 明确标注 |
| 层级顺序本身有问题（2026-11-27，用户问“顺序有没有问题”之后重画）： | —— |
| 控制面移出请求流：它不在聊天请求路径上，是另一条线 | 第二节 → 图 A 专门标注，图外另述 |
| 上下文层上移：原来在模型接入层之下等于“先调模型再拼上下文”，时序反了 | 第二节 → 图 A 里上下文是循环内第 ④ 步，紧挨 Loop |
| 工具层与模型接入层改成平级：原来一上一下暗示依赖关系，并不存在 | 第二节 → 图 A 里两者并列在循环内，图 B 里同属服务层 |
| 进程边界标出：网关进程与租户容器进程不再画在一根柱子里 | 第二节 → 图 A 标出 ① 网关进程 / ③ 租户容器进程 |
| 依赖方向单独画一张洋葱图：Data 实现 Domain 的接口，所以 Data 在 Domain 外面 | 第二节 → 新增图 B；以后争归属看图 B 不看图 A |
| 诚实规则从提示词升格为领域规则（没调工具不许认“已记下”） | 第二节 → 领域层新增 Grounding |

### 三家的共同形状

- **小循环 + 大 harness**：pi-agent 的核心循环只有 6 个文件（`agent-loop.ts` 等），
  里面零 I/O；Claude Code 和 Codex 也一样，模型循环本身很薄，权限、沙箱、会话、观测
  全在外层。
- **append-only 事件日志是事实来源**：三家都把会话存成只追加的日志（Codex 是
  `~/.codex/sessions/` 下的 JSONL + SQLite 索引；pi-agent 是 `~/.pi/agent/sessions/` 下的
  JSONL 树，可 fork/分支），上下文是**从日志派生出来的**，而不是直接改写历史。
  我们现在是把旧对话摘要后**删除** chat_log 行，这一点要改（见下）。
- **权限和沙箱是两个独立的轴**：Claude Code 和 Codex 都是“沙箱模式”和“审批策略”
  分开设置；权限默认拒绝（deny-first），fail-closed，hook 不能推翻拒绝。
  我们现在只有路径白名单 + 容器隔离，没有显式的审批管线。
- **压缩（compaction）是一个有记录的边界**，不是悄悄删东西：Codex 在达到模型上限
  90% 时压缩，压缩本身记一条事件；pi-agent 压缩留 `firstKeptEntryId` 检查点。

### 各家值得单抄的点

- **Claude Code —— brain / hands / session 三分**（Anthropic Managed Agents）：
  模型+循环不碰凭证、文件和持久状态；工具只是 `execute(name, input) -> string`；
  会话是循环之外的、可查询的追加日志，可用 `wake(sessionId)` 恢复。
  抄：DeepSeek key 只许出现在模型接入层；工具只返回字符串，不直接写库。
- **Codex —— Thread / Turn / Item 事件模型**：一次对话是一个 Thread，里面是 Turn，
  每个 Turn 由 Item（`item/started` → delta → `item/completed`）组成；前端和引擎之间
  是稳定的 JSON-RPC 协议，引擎换壳（CLI/IDE/Web）不用改。
  抄：给我们的聊天前端定一个稳定的事件结构，以后换前端不用改后端。
- **pi-agent —— 模型接入层只做四件事**：`stream()/complete()` 一个入口覆盖 20+ 厂商，
  只有 4 个手写的线协议（OpenAI/Anthropic/Google 等），工具 schema 用 TypeBox，
  每次响应都记 token/成本（作者自己提醒：这个计量是 best-effort，**不能用来向终端用户准确计费**）。
  抄：模型接入层一个入口 + 每次调用记用量；计费级精度以后再说。
- **pi-agent —— 核心循环用依赖注入**：`AgentLoopConfig` 里传 `beforeToolCall`（可拦截）、
  `afterToolCall`（可改写结果）、steering/follow-ups/stop，循环本身不知道任何具体工具。
  抄：我们的 LangGraph 图也改成只认一个 config/hooks 对象，具体工具从外面注进去。

### 对我们方案的具体修正

1. **chat_log 不再删除**：摘要后旧行改成标记归档（或迁到 `chat_archive` 表），
   并记一条“已压缩”事件；上下文从“摘要 + 最近 N 轮”派生，历史本身永远可查。
   这是本次调研里最值得立即改的一点。
2. **权限管线显式化**：工具执行前过一道 deny-first 的检查（deny > ask > allow），
   沙箱（容器隔离）和审批（要不要问用户）分开；现在只有路径白名单，P3 拆工具层时补上。
3. **每轮记一条 Turn 上下文记录**：工作目录、审批/沙箱策略、用的模型、本轮 token，
   以后查问题不用翻日志猜。
4. **token/成本计量放在模型接入层唯一调用点**：每次模型调用记一笔，先做到“每租户可查”，
   不承诺计费级精度。
5. **前端协议先定下来**：聊天、工具调用、提醒送达都用统一的事件结构推给前端，
   别再让前端猜回复文本。

### 不抄的（这个规模属于过度设计）

- 三平台的 OS 级沙箱（Seatbelt/bubblewrap/Windows 沙箱）：我们一租户一容器已经是边界，够了。
- 完整的 OTEL 可观测、Agent 团队/多智能体编排、pi-durable 的持久任务状态机、
  Codex 那种多端 App Server、分类器审批（Guardian 类）。
- 为跑分做的 eval harness：先把 46+29 个回归测试守住就行。

调研原始报告：`~/workspace/research_notes/mature-agent-architectures-20261127-0055/report.md`。

## 六、待你定的事

1. **做到哪一步**：只出方案（本文档）先放着，还是按 P0→P3 动手重构？
2. **技术选型**：保持 stdlib `http.server` + SQLite（改动最小，够用），
   还是趁重构换 FastAPI + Postgres（更像生产项目，但改动大、部署变重）？
3. **P4 做不做**：浏览器按租户隔离、Postgres、checkpointer 三项里，有没有现在就想做的？
4. **提醒那两个遗留 bug**（答完上下午还要再确认一次、重复提醒没去重）：
   在重构里顺手修，还是重构完再说？

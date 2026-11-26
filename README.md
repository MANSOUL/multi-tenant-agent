# 多租户 Agent · 单机 + Docker 原型

> 由 `~/workspace/pi-agent-starter/`（单租户）改来，2026-11-26 建。
> 目标：单机 + Docker 上每个租户一台隔离的“虚拟电脑”。
> **2026-11-26 重构：一租户一容器**——每个租户单独一个 agent 容器
> （`mt-tenant-<租户>`），数据在该租户的专属 Docker volume
> （`mt-data-<租户>`）里；以前是所有租户共用一个 agent-api 容器、
> 只靠 `tenant_id` 做逻辑隔离，那版已废弃，见下文“架构”和 VERIFY.md。
> 以后可再换成 Firecracker 微虚拟机 / 完整 KVM 虚拟机，见“隔离路线图”。
> 现有 starter 完全没动，这个项目是独立的新目录。

## 项目结构

```
multi-tenant-agent/
├── docker-compose.yml      # 常驻服务：gateway / browser / postgres·redis（可选）
│                           # 租户 agent 容器不在 compose 里，由网关按需 docker run
├── Dockerfile              # 租户 agent 镜像：装 agent/requirements.txt，CMD python3 agent/app.py
├── .env.example            # 含 TENANT_CPU / TENANT_MEMORY / TENANT_IDLE_MINUTES /
│                           # DOCKER_NETWORK / AGENT_IMAGE 等租户容器配置
├── agent/                  # agent（Python 包，可 python3 agent/app.py 或 python -m agent.app 启动）
│   ├── app.py              # 薄入口：只转发到 server.main()（20 行）
│   ├── config.py           # 环境变量和常量（PORT/DATA_DIR/LLM_*/CONTEXT_*/TOKEN_SECRET/
│   │                       # TENANT_ID / 提示词）
│   ├── tenant.py           # 租户校验：tenant_dirs / tenant_db / safe_path / token 校验 /
│   │                       # is_own_tenant（单租户容器只认自己的租户）
│   ├── memory.py           # 记忆与上下文：facts/chat_log/summaries、load_context、摘要/截断
│   ├── tools.py            # 10 个工具：make_tools(tenant)，浏览器实现 browse_page_impl
│   ├── graph.py            # LangGraph 图：AgentState、build_graph、chat()
│   ├── server.py           # HTTP 服务：页面 + /api/health、/api/chat、/api/me
│   ├── sandbox.py          # 每会话沙箱：docker 模式 / local 回退（docker 模式支持挂卷）
│   └── requirements.txt    # langgraph / langchain-core / langchain-openai
├── gateway/                # 网关：登录 + 发 token + 租户容器管理 + 反向代理
└── browser/                # 浏览器服务：Playwright Chromium（所有租户先共用一个）
```

各模块只通过明确的 import 单向依赖：`server → graph → tools/memory → tenant → config`，
`sandbox` 只被 `tools` 调。隔离逻辑只有 `tenant.py` 一份，工具和记忆都不各自重复实现。

## 架构（文字图）——一租户一容器

```
                    ┌──────────────────────────────────────────────┐
                    │  用户浏览器                                   │
                    └───────────┬──────────────────────────────────┘
                                │  http://<宿主机>:8080
                                ▼
                    ┌───────────────────────┐
                    │  gateway 网关 (8080)   │  登录 → 发 token（HMAC 签名）
                    │  演示账号密码，见 .env  │  按租户管容器：run / start /
                    │  挂 docker.sock        │  stop（闲置回收），见下节
                    └───────────┬───────────┘
                                │  内网 only（租户容器不对外发布端口）
                                ▼
              ┌─────────────────────────────────┐
              │  mt-tenant-demo (8081，容器内)  │  ← 每个租户一个
              │  mt-tenant-tenant-a (8081)      │  ← 容器名就是 DNS 名
              │  LangGraph 图：recall → llm →    │     网关按名代理过去
              │  tools 循环（见下节）            │
              │  记忆：本容器专属卷里的 SQLite   │
              │  命令：每会话一个沙箱容器        │
              └──────┬──────────────┬───────────┘
                     │              │
        ┌────────────┴───┐  ┌───────┴────────────┐
        ▼                ▼  ▼                    ▼
   沙箱容器          browser 服务   postgres / redis（可选）
   每会话一个        Playwright     postgres：以后做共享元数据
   用完即销毁        Chromium       redis：以后做限流/缓存
   (docker run，      开网页/截图     —— 当前版本 agent 默认用
    直接挂租户卷)     返回标题/正文   每租户 SQLite，不依赖它俩
```

数据隔离（进程级 + 卷级，不再只靠 `tenant_id` 分）：

| 东西 | 位置 | 说明 |
|---|---|---|
| 租户容器 | `mt-tenant-<租户>` | 一个租户一个进程/容器，挂了也不影响别的租户；容器只认自己的租户（`TENANT_ID` 校验，见 `agent/tenant.py` 的 `is_own_tenant()`） |
| 工作区文件 | 卷 `mt-data-<租户>` 内的 `<租户>/workspace/` | 只能读写自己的目录，`..` 穿越会被挡掉；卷在宿主机上由 Docker 管理，容器删了数据还在 |
| 记忆 | 同卷内的 `<租户>/memory.db` | 每租户一个 SQLite，`facts` / `chat_log` / `summaries` 三张表，物理上就不在别的容器里 |
| 浏览器 profile | 同卷内的 `<租户>/browser-profile/` | 每租户独立，Cookie/登录态不串 |
| 浏览器截图 | 共享 browser 服务写到宿主机 `./data`（已知限制） | 租户卷共享 browser 容器看不到，截图暂时落不到租户卷里，见“各服务” |

### 租户容器生命周期（网关管，不用手工起）

```
首次登录 / 聊天
  → 网关 docker inspect mt-tenant-<租户>
      ├─ 不存在 → docker run -d --name mt-tenant-<租户> --network <DOCKER_NETWORK>
      │           --cpus <TENANT_CPU> --memory <TENANT_MEMORY>
      │           -v mt-data-<租户>:/app/data -e TENANT_ID=<租户> -e LLM_*=…
      │           <AGENT_IMAGE>
      ├─ 已停止 → docker start mt-tenant-<租户>
      └─ 在跑   → 直接用
  → 轮询 http://mt-tenant-<租户>:8081/api/health 直到通（最多 60 秒）
  → 代理请求到该容器，成功后更新最后活跃时间
闲置回收（网关后台线程，每 60 秒检查一次）
  → 最后活跃超过 TENANT_IDLE_MINUTES（默认 30 分钟）→ docker stop
  → 只停不停删：volume 保留，下次登录/聊天时再 docker start，数据都在
手动管理（调试用）：
  docker ps --filter name=mt-tenant-        # 看哪些租户容器在跑
  docker volume ls | grep mt-data-          # 看各租户的数据卷
  docker stop mt-tenant-demo                 # 手工停一个租户（网关下次会再起）
  docker rm mt-tenant-demo                   # 删容器（数据在卷里，还在）
  docker volume rm mt-data-demo              # ⚠️ 删卷 = 删该租户全部数据，别手滑
```

### 资源估算

| 项 | 开销 | 说明 |
|---|---|---|
| 镜像 | 共享一层 | 所有租户容器都用同一个 `AGENT_IMAGE`，镜像只存一份（约 300MB 级，含 langgraph 依赖）；浏览器镜像另算，约 1GB+，也是所有租户共用一个服务 |
| 每个活跃租户容器 | 约 512MB 内存上限（`TENANT_MEMORY`，可调）+ 0.5 核（`TENANT_CPU`） | Python + LangGraph 进程本身常驻约 100–200MB，跑工具/调模型时再涨，512m 是上限不是常驻占用；不活跃会被回收停掉，不占内存 |
| 每个租户数据卷 | 按实际用量 | SQLite + 工作区文件，刚开始只有几十 KB |
| 2 核 / 3.5G 内存的服务器（当前部署机） | 约 4–6 个同时活跃的租户 | 3.5G 减去系统 + 网关 + 浏览器（Chromium 约 500MB–1GB）后，剩约 2–2.5G 给租户容器；租户多了就排队等回收，或调小 `TENANT_MEMORY`、调短 `TENANT_IDLE_MINUTES` |
| 100 个租户 | 不等于 100 个容器同时跑 | 注册 100 个、同时在线 4–6 个是常态；闲置回收就是干这个的 |

### 从老模型迁移（共享 agent-api → 一租户一容器）

老模型的数据在宿主机 `./data/<租户>/` 下，新模型各租户在自己的卷里。
迁移某个租户的数据（以 `demo` 为例，停掉旧服务后跑一次）：

```bash
docker run --rm -v mt-data-demo:/to -v ./data/demo:/from alpine sh -c 'cp -r /from/. /to/'
# 之后 demo 登录时网关起的容器就能看到原来的记忆和工作区文件
```

不迁移也行：新租户第一次登录时网关会自动建空卷，从零开始。

## LangGraph 图怎么跑（2026-11-26 改成 LangGraph 之后）

上一版是手写 `for` 循环调模型、解析 JSON 再调工具；现在改成 LangGraph 的图，
编排逻辑由框架接管，工具和隔离逻辑没动。图长这样：

```
START → recall_memory → llm ──┬─ tools → llm ──┐
                              │                 │（循环，最多 3 轮工具）
                              ├─ final_llm ─────┤（到上限后不带工具再答一次）
                              └─ save_memory → END
```

- **recall_memory**：读记忆节点。把本租户 SQLite 里 `facts` 表的长期事实读出来，
  拼进后面 llm 节点的系统提示里。只读本租户的库。
- **llm**：调模型节点。用 `ChatOpenAI`（`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`，
  任何 OpenAI 兼容接口）绑定本租户的那套工具后调用，返回一条 `AIMessage`。
  每调一次 `steps + 1`。
- **tools**：工具节点（`ToolNode`）。执行的 10 个工具：
  `run_command`（沙箱白名单命令）、`read_file` / `write_file`（只限本租户工作区）、
  `list_workspace` / `search_file`（本租户工作区内列文件、搜内容）、
  `remember_fact` / `recall_fact` / `search_facts` / `list_facts`（本租户记忆）、
  `browse_page`（浏览器服务）。每个工具函数都闭包了 `tenant`。
- **final_llm**：工具轮数到上限（默认 3 轮，`MAX_TOOL_ROUNDS` 可调）后还想调工具时走这里：
  这次不带工具，强制模型直接给回答，和上一版“循环 3 次后再调一次模型”一致。
- **save_memory**：存记忆节点。把本轮的一问一答写进该租户 `memory.db` 的 `chat_log` 表。

## 上下文怎么流转（2026-11-26 改进后）

```
summaries 表（旧对话摘要）
      ↓
chat_log 最近 CONTEXT_MAX_TURNS 轮原文
      ↓
facts 表（长期事实）
      ↓  全部拼进系统提示（按 CONTEXT_MAX_CHARS 预算截断）
START → recall_memory → llm → tools → llm → save_memory → END
```

- **recall_memory 现在会读三样东西**：最新一条 `summaries` 摘要、最近
  `CONTEXT_MAX_TURNS`（默认 10）轮 `chat_log` 原文、`facts` 表的长期事实，
  拼成系统提示发给模型。所以第二轮能看到第一轮聊了什么，不用再靠模型自己猜。
- **旧对话变摘要**：`chat_log` 轮数超过 `SUMMARY_THRESHOLD_TURNS`（默认 20）
  时，`recall_memory` 会把最旧的、超出最近 10 轮的那些轮拿去用模型总结
  （不超过 200 字），存进 `summaries` 表，再删掉这些旧行；没填 key 或总结
  失败时改成直接截断删掉，不会让请求报错。摘要是累积的：新摘要会接在旧摘要后面。
- **预算管理**：`estimate_tokens()` 按“约 4 字符 ≈ 1 token”粗估；
  摘要 + 事实 + 最近对话超过 `CONTEXT_MAX_CHARS`（默认 12000 字符，约 3000 token）
  时，从最旧一轮开始截，至少保留最后一轮；事实实在太多时再截事实文本。
  系统提示和最新一条用户消息永远不截。
- **新工具**：`search_facts(keyword)`（在 facts 的 key/value 里模糊搜）、
  `list_workspace()`（列本租户工作区文件）、`search_file(keyword)`（在工作区
  文本文件里搜内容，单个文件限 200KB、最多 20 条匹配）。三个都只走本租户
  的库/目录，`search_file` 不会跟出工作区。
- **系统提示也改了**：用户说“记住……”“别忘了……”时，模型会被提示去调
  `remember_fact`；问“你记得我……吗”时，先调 `recall_fact` / `search_facts` 查。

**还没做的（明确说出来）**：没有向量/语义检索（`search_facts` 只是 SQL `LIKE`
模糊匹配，搜不到同义词）；没有 LangGraph checkpointer——图本身仍是无状态的，
每次请求现建图，跨轮记忆全靠每租户 SQLite 的 `facts` / `chat_log` / `summaries`
三张表，不是靠图的 checkpoint；也没有按 token 精确计费（只有字符粗估）。

## 提醒与记忆是怎么做的（2026-11-26 改进后）

背景：之前用户跟部署版 agent 说“8 点提醒我喝水”，它口头说“记下了”其
实没写；再问“你记到哪里了”，它答“记忆还是空的”；还说没法主动在 8 点提醒。
根因是记忆全靠模型自觉调 `remember_fact`，且系统没有任何定时器/调度器/
推送通道。下面是改完之后的做法：

- **自动落盘，不依赖模型自觉。** `save_memory` 每轮对用户消息跑确定性抽取
  （`memory.extract_explicit`）：匹配到“记住…/别忘了…”就写 `facts` 表
  （同一内容去重，不重复写）；“待办：买菜、喝水”写 `todos` 表；
  “提醒我早上 8 点喝水”“晚上 8 点提醒我喝水”写 `reminders` 表。
  抽取失败或没匹配到时静默跳过，绝不报错、绝不阻塞聊天。
- **待办和提醒是每租户自己的表。** `todos`（id, text, status pending/done,
  created_at）、`reminders`（id, text, due_at, repeat once/daily, status
  pending/due/notified, created_at），都在各租户自己的 SQLite 库里，工具
  `add_todo` / `list_todos` / `complete_todo` / `add_reminder` /
  `update_reminder` / `list_reminders` 全部只读写本租户。工具成功返回里含
  写进去的内容和时间，供模型回显和核对。
- **时间解析（`memory.parse_reminder_time`，时区 `REMINDER_TZ`，默认
  Asia/Shanghai）。** 早上/上午 8 点→08:00，晚上/下午 8 点→20:00，
  “明天早上 8 点”算次日，“每天早上 8 点”→每天重复；分钟都认：
  七点二十分→:20、7点20分/7:20→:20、八点半→:30、八点一刻→:15、
  三刻→:45。解析、设提醒工具、自动抽取三处走同一份解析逻辑。
  顺延只有一种情况：用户说的那个时间在今天已经过去了，才顺延到明天，
  回显里会说清楚“今天的 X 点已过，设到明天 X 点”；时间还没过就设今天。
- **歧义时间不落盘。** 裸“8点”没说上下午时，`add_reminder` 和自动抽取
  都不写表，只返回/触发一次追问“早上还是晚上”，问到答案立刻按完整时间设。
- **改提醒直接改，不反复确认。** `update_reminder` 按 id 或按内容匹配最近
  一条改时间；改完必须把工具返回的时间跟用户要的时间核对一遍，对不上换
  一种写法再试一次，两次都对不上才如实说没改成功，绝不编“系统只认整点”
  这类解释。
- **到点提醒 = 下次聊天时当面提醒，只出现一次。** agent 容器启动时起一个
  调度器线程，每 30 秒扫一次：到点的 reminder 置为 due，写进
  `pending_notifications` 表；下次用户来聊天，`recall_memory` 会把未送达
  的提醒以【待提醒】放在系统提示最前面，模型先当面告诉用户，那轮结束
  后标记已送达，同时把提醒行从 due 翻成 notified——不翻的话，提醒行会
  一直留在提醒列表里，模型每轮都再说一遍（2026-11-26 实测到的 Bug 4）。
- **接地确认。** 系统提示里写死了硬规则：用户说记住/保存/提醒我/待办，
  必须调相应工具，只有工具返回成功后才能说“已记下/已设好”并回显内容和时间；
  没调工具不许认。被问“你记到哪里了”时，必须先调 `list_facts` /
  `list_todos` / `list_reminders` 查，照实列出来；只有三张表真的都空，
  才能说“记忆还是空的”。系统提示和工具文档里的例子都明确标注为格式示例，
  不是用户说过的话。

**能力边界（实话实说）：**

- **没有手机推送。** 这个 agent 只有网页聊天框，没有向用户手机推送的通道，
  所以“8 点提醒”实际是“到点后，你下次来聊天时我当面提醒你”。不要在任何
  地方假装能推送；真要推送，得另外接 Telegram/邮件等通道。
- **裸“8 点”会追问，不猜。** 只说“8 点”没说早上还是晚上时，提醒先记下
  （时间未定），模型必须追问“早上 8 点还是晚上 8 点？”，等回答再定时间。
  只有“20 点”这种本身就明确的才不用追问。
- **抽取只认显式指令。** 抽取靠正则匹配“记住/别忘了/提醒我/待办”这类明确
  说法，绕着弯子说的事（比如“我明天要早起”）不会自动落盘，还是得靠模型
  调工具——这时接地规则兜底：模型没调工具就不许说记下了。
- **checkpointer 还是没有。** 多轮对话仍靠 SQLite 文本拼进系统提示，
  不是原生多轮消息；待提醒只出现一次的保证靠 `delivered` 标记，不是靠图的状态。

**和上一版比，变了什么、没变什么：**

- 每次请求现建一张图：`build_graph(tenant)`，`tenant` 同时进两处——
  图的 `state["tenant_id"]`，和每个工具函数的闭包。工具内部走的还是原来的
  `tenant_dirs` / `safe_path` / `sandbox_run`，所以即使模型想读别的租户的文件，
  路径前缀检查也会挡掉。
- 图本身是无状态的（没有 checkpointer），跨轮次的记忆不靠图的状态，
  靠每租户 SQLite 的 `facts`（长期事实）和 `chat_log`（对话记录）两张表。
- 一张图只服务一个租户的一次请求，请求结束图就扔掉，不会把 A 租户的状态带到 B 租户。

**和上一版比，变了什么、没变什么：**

| | 上一版（手写循环） | 这一版（LangGraph） |
|---|---|---|
| 编排 | `for` 循环 + 手工解析模型返回的 JSON | LangGraph 图 + `ToolNode`，工具调用由框架解析 |
| 模型调用 | `urllib` 直接调 `/chat/completions` | `langchain-openai` 的 `ChatOpenAI` |
| 工具 | 同名同行为 | 同名同行为，只是改成 `@tool` 定义、闭包 tenant |
| 记忆 | `facts` 表 | `facts` 表 + 新增 `chat_log` 表（save_memory 节点写） |
| 鉴权 / 隔离 / 沙箱 / 浏览器 | — | 一点没变，见 VERIFY.md 的复测记录 |

## 快速开始

```bash
cd multi-tenant-agent
cp .env.example .env
nano .env        # 1) 填 LLM_API_KEY（任何 OpenAI 兼容接口）
                 # 2) 改 GATEWAY_SECRET 和 GATEWAY_USERS 的密码（演示账号！）
                 # 3) 按机器调 TENANT_CPU / TENANT_MEMORY / TENANT_IDLE_MINUTES
# 先 build 租户镜像（网关 docker run 时不会自动 build，镜像必须先存在）：
docker compose --profile build build agent-image
# 再起常驻服务（网关 + 共享浏览器；postgres/redis 默认不起）：
docker compose up -d --build
# 打开 http://<宿主机IP>:8080 ，用 .env 里 GATEWAY_USERS 的账号登录
# 首次登录会等几秒等网关起你的租户容器（mt-tenant-<你>）
# 试试：df / 记住：我喜欢喝美式 / 帮我打开 https://example.com 看看标题是什么
docker compose down        # 停掉常驻服务；租户容器和卷不受影响，
                           # 下次登录时网关会把你的容器再 start 起来
```

没有 Docker 的机器上也可以先跑通聊天逻辑（见 `VERIFY.md`），但要先装依赖：

```bash
python3 -m venv .venv && .venv/bin/pip install -r agent/requirements.txt
cp .env.example .env   # LLM_API_KEY 先留空也能起服务，聊天时会提示缺 key
# 单租户容器模式等价于：TENANT_ID=demo PORT=8081 DATA_DIR=./data
PORT=8081 DATA_DIR=./data TENANT_ID=demo .venv/bin/python agent/app.py
```

## 各服务

| 服务 | 镜像/构建 | 端口 | 说明 |
|---|---|---|---|
| gateway | `gateway/Dockerfile`（python:3.12-slim，标准库 + docker CLI） | 8080（唯一对外端口） | 登录 + 发 token + 租户容器管理（run/start/stop/闲置回收）+ 按租户反向代理 |
| mt-tenant-\<租户\> | 根目录 `Dockerfile`（python:3.12-slim，装 `agent/requirements.txt`：langgraph 等） | 8081（仅容器内网，不对外发布） | 每个租户一个：LangGraph 图编排、工具、记忆、沙箱、调浏览器服务；数据在卷 `mt-data-<租户>` 里；由网关按需起停，不在 compose 里 |
| browser | `browser/Dockerfile`（Playwright Python 镜像） | 8001（仅容器内网） | Chromium 无头浏览：打开页面、取标题/正文、截图；**所有租户先共用一个**，后续可按租户拆（截图落盘问题见架构图下的表） |
| postgres | `postgres:16-alpine` | 不发布 | 可选（`--profile optional` 才起）。当前版本 agent 不依赖它，留给以后做共享元数据/计量 |
| redis | `redis:7-alpine` | 不发布 | 可选（`--profile optional` 才起）。当前版本 agent 不依赖它，留给以后做限流/缓存 |

## 隔离路线图（按这个顺序升级，每步都能单独验证）

| 阶段 | 隔离 | 状态 |
|---|---|---|
| 0. 一租户一容器（本项目，2026-11-26 重构） | 每个租户一个常驻 agent 容器 + 专属数据卷，进程和数据物理隔离；闲置 30 分钟自动停 | 已实现（代码）；`docker compose up`、真实容器启动/路由/回收还没跑过，见 VERIFY.md |
| 1. 容器沙箱 | 每会话一个 `docker run --rm` 容器：`--read-only` 根文件系统、禁 `--privileged`、无网络（`--network none`）、CPU 0.5 核 / 内存 256MB 上限，只挂载该租户的工作区（租户容器里改成直接挂卷，见 `agent/sandbox.py`） | 已实现；宿主机必须有 Docker 且 agent 容器挂了 `/var/run/docker.sock` |
| 2. 回退模式 | 宿主机没有 Docker 时，命令在 agent 本地跑白名单命令（仍限工作区内），输出会明确标注“本地回退：未隔离，仅开发用” | 已实现，仅用于开发调试 |
| 3. Firecracker 微虚拟机 | 把第 0 步的租户容器换成微虚拟机启动，其他层不动 | 未实现，待定（需要宿主机有 `/dev/kvm`，见下文待讨论问题） |
| 4. 完整 KVM 虚拟机 | 每租户一台常驻虚拟机 | 未实现，只在租户少、隔离要求最高时用 |

### 容量参考（来自 `pi-agent-starter/MULTI_TENANT_VM_PLAN.md`，已按本项目实测口径更新）

| 层次 | 每租户额外开销 | 单机大概能同时跑 |
|---|---|---|
| Docker 容器（本项目） | 约 512MB 内存上限 + 0.5 核（LangGraph 进程常驻约 100–200MB） | 2 核 / 3.5G 的机器约 4–6 个活跃租户；32GB / 8 核约几十个轻度租户 |
| Firecracker 微虚拟机 | 约几 MB–几十 MB + 应用内存 | 几十个，按需启动可更多 |
| 完整 KVM 虚拟机 | 至少 1 核 / 2GB 内存 / 20GB 磁盘 | 约 10 台左右 |

通用规则：**不活跃就关机/快照，活跃才启动**。100 个租户 ≠ 100 台同时在线的机器。

## 安全提醒（重要，请先读）

- **网关的账号密码是演示用的**，只适合局域网原型：密码以明文写在 `.env` 的 `GATEWAY_USERS` 里，
  token 是 HMAC 签名的简单令牌，没有刷新、没有防爆破。生产用请换成 Keycloak / Authentik（见 `.env.example` 注释）。
- **不要把 8080 暴露到公网**，只在局域网用；想在外面用，先走自己的 VPN / 内网穿透。
- `.env` 只放占位符和你自己的 key，**不要提交、不要发给别人**；`.gitignore` 已覆盖 `.env` 和 `data/`。
- LLM API Key 只通过环境变量 `LLM_API_KEY` 传入，代码里没有任何真实 key。
  注意：网关起租户容器时会把 `LLM_API_KEY` 等以 `docker run -e` 传进去，
  key 会短暂出现在宿主机的进程列表里，和其他任何 `docker run -e` 传 key 的做法一样，
  只能在自己可信的单机上用。
- 沙箱容器禁 privileged、只读根文件系统、无网络；但容器共用宿主机内核，理论上仍存在容器逃逸风险，
  隔离强度低于 Firecracker / 完整虚拟机，做不到 Muse 同级。
- 浏览器服务跑的是无头 Chromium：能开网页、登录、截图、抓正文；但网站可能检测无头浏览器而弹验证码，
  视频/音频/DRM 支持差。需要“用户直接看画面操作”时，要再加 Xvfb + noVNC，本项目暂未做。
- **docker.sock 是最大的高权限点**：网关挂了宿主机的 `/var/run/docker.sock`
  才能起/停租户容器，每个租户容器也挂了它才能起每会话沙箱容器——
  拿到这个 sock 就等于拿到宿主机 root。只能在自己可信的单机上用，
  不要把这个 compose 文件原样搬到共享宿主机上；生产要换成 Docker API
  代理（如 socket-proxy）做权限收紧，或把沙箱改成不用宿主机 Docker 的方案。
- 租户容器之间是进程隔离，但共用宿主机内核；一个租户容器被攻破后，
  理论上仍可能通过 docker.sock / 内核漏洞影响宿主机和其他租户，
  这是容器方案的固有上限，不是本项目的 bug。

## 待讨论的问题（下次细聊时定）

1. **机器配置**：核数 / 内存 / 磁盘 / 有没有 `/dev/kvm` / x86 还是 ARM？
   直接决定走容器、Firecracker 还是完整虚拟机。检查命令：
   `nproc; free -h; df -h /; ls /dev/kvm 2>&1; egrep -c '(vmx|svm)' /proc/cpuinfo; uname -m`
2. **租户规模**：总共多少租户？同时在线多少？每个租户要跑什么工具（只聊天，还是跑命令、读写文件、上网）？
3. **登录方式**：演示账号密码先用着，还是直接上 Keycloak / Authentik？要不要微信/邮箱登录？
4. **浏览器要做到哪一步**：无头自动化就够，还是要用户能在网页上看到并操作浏览器画面（noVNC）？
5. **记忆与数据**：每租户 SQLite 先用着，还是现在就上 Postgres（共享库 + `tenant_id` 行级隔离）？
6. **计量与限流**：要不要按租户记 token 用量、做调用次数上限？以后收费的话按什么算？
7. **部署位置**：放家里单机 / 树莓派，还是租一台云服务器？要不要域名 + HTTPS？

## 验证情况

见 `VERIFY.md` 顶部（2026-11-26 一租户一容器重构）：本地只验证了
py_compile、容器名/路由/闲置回收的纯函数逻辑和 fake docker 的 ensure
调用序列；这台 VM 上没有 Docker，所以 `docker compose up`、真实租户容器
的创建/启动/路由/回收、沙箱挂卷模式都没跑过，第一次在有 Docker 的机器上
部署时按 VERIFY.md 逐项验——先 `docker compose --profile build build
agent-image`，再 `docker compose up -d`，登录 `demo` 看 `mt-tenant-demo`
是否起来、`/api/health` 是否通、30 分钟不用是否自动停。

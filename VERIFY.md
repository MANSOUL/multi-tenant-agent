## 2026-11-26 提醒与记忆改进（新）

背景：用户跟部署版 agent 说“8 点提醒我喝水”，它口头说“记下了”其实没写；
再问“你记到哪里了”，答“记忆还是空的”；还说没法主动在 8 点提醒。

改动（只改工作树，未提交、未推送、未部署）：
- `agent/memory.py`：新增 `parse_reminder_time()`（早上/上午→08:00，
  晚上/下午→20:00，明天/每天/8点半/8:30，裸“8点”返回歧义标志不猜）、
  `extract_explicit()`（确定性抽取：记住/别忘了→facts 去重写，待办→todos，
  提醒我/…点提醒我→reminders，失败静默跳过）、`check_due_reminders()`
  （到点置 due + 写 pending_notifications，daily 自动生成次日提醒）、
  `mark_notifications_delivered()`；`load_context()` 增加 todos / reminders /
  due_notifications 三项返回；时区 `REMINDER_TZ`（默认 Asia/Shanghai，zoneinfo）。
- `agent/tenant.py`：`tenant_db()` 新建 `todos` / `reminders` /
  `pending_notifications` 三张表（每租户独立库）。
- `agent/tools.py`：新增 5 个工具 `add_todo` / `list_todos` /
  `complete_todo` / `add_reminder` / `list_reminders`（工具 10 个→15 个，
  全部只读写本租户；成功返回含内容和时间；裸“8点”返回追问上下午）。
- `agent/server.py`：启动时起提醒调度器守护线程（每 30 秒扫一次，
  单租户容器只扫自己）；`start_reminder_scheduler()` 可调 interval 供测试。
- `agent/graph.py`：`AgentState` 增加 todos / reminders / due_notifications；
  `_system_prompt()` 把【待提醒】放最前面、再拼待办和提醒；`save_memory()`
  每轮跑 `extract_explicit()` 自动落盘，并把本轮用过的待提醒标记已送达。
- `agent/config.py`：`SYSTEM_PROMPT` 增加接地硬规则（记住/提醒/待办必须调
  工具，工具成功后才能说“已记下/已设好”并回显；问“记到哪里了”必须先查
  三张表列出来，三张表真空才能说空；裸“8点”必须追问上下午）；
  `TOOLS_DOC` 增加 5 个新工具。
- `README.md`：新增“提醒与记忆是怎么做的”一节，如实写清能力边界
  （自动落盘、待办、到点后下次聊天时提醒、无手机推送、裸“8点”追问）。
- `.env.example`：新增 `REMINDER_TZ=Asia/Shanghai`。

已验证（本机，无 Docker、无真实 LLM key，只做 stub）：
- `python3 -m py_compile agent/*.py gateway/gateway.py browser/browser.py`：全部通过。
- Stub 测试 `/tmp/test_reminders.py`（`~/workspace/tools/mtvenv/bin/python` 跑，
  数据在 `/tmp/mt-reminder-data`，每次运行先清空，没碰项目 `data/`，没联网装包，
  没写任何真实 key、没创建 `.env`），**37 项全部通过，0 失败**：
  - 时间解析 10 项：早上/上午 8 点→08:00，晚上 8 点→20:00，下午 3 点→15:00，
    裸“8点”歧义且 due_at 为空，明天早上 8 点→次日 08:00，每天早上 8 点→
    repeat=daily，早上 8 点半→08:30，20 点→20:00 不歧义，已过时间顺延明天；
  - 确定性抽取 8 项：记住/别忘了→facts、同一内容去重不重复写、待办一次拆 3 条、
    “提醒我早上 8 点喝水”→08:00、“晚上 8 点提醒我喝水”→20:00、裸“8点提醒”
    →due_at 留空、无指令/空消息静默全 0 不报错；
  - 工具与隔离 7 项：add_todo 返回含内容、list_todos、tenant-b 看不到
    tenant-a 的待办、complete_todo 成功与不存在时不报错、add_reminder 返回含
    “08:00”、裸 8 点返回追问“早上还是晚上”、list_reminders；
  - 调度与送达 6 项：到点变 due、写 pending_notifications 未送达、recall 里
    出现且 system prompt 里【待提醒】放最前面、标记已送达后下一轮不再出现、
    重复调度不重复写通知、daily 到点后生成次日提醒；
  - 调度器线程 1 项：`start_reminder_scheduler(interval_seconds=1)` 在进程内
    跑起来，1 秒间隔扫到到点提醒并写入待送达。
  - 模型未调工具时抽取仍落盘：抽取测试全程没调任何模型/工具调用，只调
    `extract_explicit()`，提醒和事实照样进了表（这正是自动落盘要证明的）。
- 测试中发现并修掉一个测试本身的问题（不是代码问题）：`add_reminder` 工具用
  真实当前时间解析，19 点测“早上 8 点”会算到明天，导致 08:01 的调度断言失败；
  改为用注入时间 `NOW=07:00` 再建一条当天 08:00 的提醒后通过。

未验证（必须在服务器上实测）：
- 真实 DeepSeek 下模型是否按新系统提示调工具：说“记住/提醒我/待办”时是否
  真调 `remember_fact` / `add_todo` / `add_reminder`，工具成功后是否回显内容
  和时间；被问“你记到哪里了”时是否先查三张表再回答；裸“8点”是否追问上下午。
- 调度器线程在租户容器里是否正常跑：容器启动后 30 秒间隔扫描是否生效，
  到点后 `pending_notifications` 是否写入，下次聊天时【待提醒】是否出现且只出现一次。
- 网关到租户容器的提醒透传：登录后租户容器是否正常启动、提醒数据是否落在
  该租户的专属 volume 里、两个租户的提醒是否互不干扰。
- 端到端真人对话：用“8 点提醒我喝水”完整走一遍（设提醒→等到点→下次聊天收到提醒）。

## 2026-11-26 一租户一容器重构之后（新）

重构内容：共享 agent-api 拆成一租户一容器。改了 `gateway/gateway.py`
（租户容器管理：ensure/run/start/健康轮询/闲置回收 + 按租户代理）、
`gateway/Dockerfile`（装 docker CLI）、`docker-compose.yml`（去掉共享
agent-api，gateway 挂 docker.sock，browser 保留共享，postgres/redis 改成
`profiles: optional`，新增 `agent-image` 构建服务）、`agent/config.py`
（`TENANT_ID` / `SINGLE_TENANT_MODE`）、`agent/tenant.py`
（`is_own_tenant()`，单租户容器只认自己的租户）、`agent/graph.py`
（`chat()` 开头挡非本租户）、`agent/server.py`（`/api/health` 返回
`tenant` / `single_tenant`）、`agent/sandbox.py`（`SANDBOX_VOLUME`
时沙箱直接挂卷）、`.env.example`（新增 `TENANT_CPU` / `TENANT_MEMORY` /
`TENANT_IDLE_MINUTES` / `DOCKER_NETWORK` / `AGENT_IMAGE`）、`README.md`
（新架构图、生命周期、资源估算、迁移说明）。`pi-agent-starter/` 没动。

- `python3 -m py_compile agent/*.py gateway/gateway.py browser/browser.py`：
  全部通过。
- `docker-compose.yml` 用 PyYAML 解析通过：服务为 gateway / agent-image
  （profile build）/ browser / postgres / redis（profile optional）；
  共享 agent-api 已不在；gateway 挂了 docker.sock、环境变量含
  `DOCKER_NETWORK` / `AGENT_IMAGE` / `TENANT_*`。
- Stub 测试（`/tmp/test_per_tenant.py`，不调真实 Docker/LLM，用 fake
  docker + monkeypatch；数据在 `/tmp/mt-per-tenant-data`，没碰项目 `data/`），
  **全部通过**：
  - (a) 容器名/卷名/路由：`demo` → 容器 `mt-tenant-demo`、卷 `mt-data-demo`、
    代理目标 `http://mt-tenant-demo:8081`；`tenant-a` 同理；
  - (b) 租户 id 校验：`demo` / `tenant-a` / `user_1` 通过；`../demo`、
    `a b`、`demo;rm -rf /`、`demo$(id)`、空串、65 个字符均被拒；
  - (c) ensure 调用序列（fake docker）：容器 missing → 调 `docker run`
    创建（argv 含 `--name mt-tenant-demo`、`--network`、`--cpus 0.5`、
    `--memory 512m`、`-v mt-data-demo:/app/data`、`-e TENANT_ID=demo`、
    `-e SANDBOX_VOLUME=mt-data-demo`、镜像名，且不含 shell 注入字符）；
    容器 stopped → 只调 `docker start`，不重复 run；容器 running →
    不调 run/start，直接等健康检查；
  - (d) 健康轮询：前两次不通、第三次通 → ensure 返回成功；
    一直不通 → 超时抛错；
  - (e) 闲置回收：最后活跃 31 分钟前、阈值 30 分钟 → 该停；
    10 分钟前 → 不停；`reap_idle` 只停超时的租户容器（`docker stop`
    收到一次），不停删，volume 保留；
  - (f) agent 端单租户校验：`TENANT_ID=demo` 时 `is_own_tenant("demo")`
    为真、`is_own_tenant("tenant-a")` 为假；`get_tenant` 拿 tenant-a 的
    token 时返回 None；`chat()` 对非本租户抛错；
  - (g) 沙箱挂卷：`SANDBOX_VOLUME=mt-data-demo` 时沙箱挂载为
    `-v mt-data-demo:/data:rw`、工作目录 `/data/demo/workspace`；
    没设时仍挂宿主机 workspace 路径。
- HTTP 实测（本机起真服务，无 Docker、无真实 key）：
  - 网关 `GET /` → 200 登录页；`POST /api/login`（demo/demo123）→
    502“租户容器启动失败：docker 命令执行失败（本机没装 docker）”，
    说明登录→ensure 的链路已接通，失败时报错清晰、网关不崩。
  - agent（`TENANT_ID=demo`）`GET /api/health` →
    `{"ok": true, "tenant": "demo", "single_tenant": true, ...}`；
    `/api/me` 带 demo 的 token → 200 `{"tenant": "demo"}`，
    带 tenant-a 的 token → 401。单租户校验在 HTTP 层也生效。
    测试用的 `GATEWAY_SECRET=test-secret` 只在测试进程的环境变量里，
    没写进任何文件；测完两个服务都已停掉，无残留进程。
- 无硬编码密钥：grep 检查过 py 文件和 compose（`.env.example` 里只有
  占位符和演示账号密码）。

## 未验证（一租户一容器重构后，必须在有 Docker 的机器上跑）

- **`docker compose up` / `docker compose --profile build build agent-image`：
  本机无 Docker，根本没跑过。** 首次部署必须先 build 租户镜像再起网关，
  否则网关 `docker run` 会报镜像不存在。
- **真实租户容器的创建/启动/路由/回收：都没跑过。** 包括：首次登录
  `demo` 是否真的起了 `mt-tenant-demo`、`/api/health` 是否通、聊天
  是否走到该容器、30 分钟不用是否真的 `docker stop`、再登录是否
  `docker start` 恢复且数据还在（卷里）、两个租户同时在线是否各走
  各的容器、租户容器里 `TENANT_ID` 校验是否挡得住错发的请求。
- **沙箱挂卷模式没跑过**：`SANDBOX_VOLUME` 下沙箱 `docker run -v
  mt-data-demo:/data` 是否真能读写到租户卷里的工作区、git/df 等白名单
  命令是否通；没跑过之前，沙箱 docker 模式按未验证处理。
- **网关的 docker CLI 没跑过**：gateway 镜像里 `apt-get install docker.io`
  是否装得上、网关容器里 `docker` 命令是否真能连上宿主机 Docker、
  `DOCKER_NETWORK` 默认值 `multi-tenant-agent_default` 是否和实际
  compose 网络名一致（用 `-p` 改过项目名时必须改 `.env`）。
- **浏览器截图落盘问题没验证**：共享 browser 容器看不到租户卷，
  截图是否如 README 所说落在宿主机 `./data` 而不是租户卷里，
  以及是否影响 `browse_page` 返回的路径，首次部署时要实测一次。
- **资源估算没实测**：2 核 3.5G 上 4–6 个活跃租户、每租户 512m
  上限只是按进程常驻内存估算的，没有在该配置上同时起多个租户容器压过。
- **真实 LLM 端到端对话**：同前，stub 没调真实接口，填 key 后再验。

## 2026-11-26 项目结构重构之后（上一版记录，保留）

- `agent/app.py` 从约 700 行拆成 7 个模块 + 1 个薄入口（见 README“项目结构”）：
  `config.py`（配置）/`tenant.py`（租户隔离）/`memory.py`（记忆与上下文）/
  `tools.py`（10 个工具）/`graph.py`（LangGraph 图）/`server.py`（HTTP 服务）/
  `sandbox.py`（未动）/`app.py`（20 行薄入口）。行为未改：同一份逻辑原样搬移，
  `tenant.py` 是隔离逻辑的唯一来源，`tools`/`memory` 不再重复实现。
- `python3 -m py_compile agent/*.py gateway/gateway.py browser/browser.py`：全部通过。
- `docker-compose.yml` 用 PyYAML 解析通过，5 个服务都在；根 `Dockerfile` 的
  `CMD ["python3", "agent/app.py"]` 不用改。
- Stub 模型端到端复测（`/tmp/test_refactor.py`，用假模型替掉 `ChatOpenAI`，
  不调真实接口；数据在 `/tmp/mt-refactor-data`，没碰项目 `data/`），**全部通过**：
  - (a) 多轮上下文：第 1 轮“记住，我在上海”→ 返回“记好了，你在上海。”；
    第 2 轮“我上次说我在哪个城市？”→ 系统提示里含第 1 轮原文，返回“你在上海。”；
    `facts` 表有（城市，上海），`chat_log` ≥ 4 行；
  - (c) 租户隔离：tenant-b 的 facts/chat_log 均为空，`recall_fact` 返回“没记过”，
    `search_facts("上海")` 返回“没找到”；
  - (d) 新工具：tenant-a 的 `list_workspace` 能看到 `notes/todo.md`，
    `search_file("牛奶")` 能找到、`search_file("不存在的词xyz")` 返回没找到；
    路径穿越 `../tenant-b/memory.db` 被挡；`rm` 被白名单拒绝；
    `df` 本地回退能跑；工具共 10 个。
- HTTP 入口复测（venv 启动，无真实 key）：`python3 agent/app.py` 和
  `python -m agent.app` 两种方式都能起；`/api/health` → `{"ok": true, ...}`；
  无身份 `/api/me` → 401；带 `X-Tenant-Id: demo`（`TRUST_TENANT_HEADER=1`）→
  `{"tenant": "demo"}`；`/api/chat` 无 key → “还没填 LLM_API_KEY”报错透传、服务不崩。
  测试后两个服务都已停掉，无残留进程。
- 无硬编码密钥：grep 检查过 py 文件。

## 未验证（重构后仍需在用户机器上跑）

- `docker compose up --build`、沙箱 docker 模式、browser 服务运行时、
  postgres / redis 启动：本机无 Docker / 没装 Playwright，**都没跑过**。
  重构只改了 agent 包内部分工，`Dockerfile`/`docker-compose.yml` 没变，
  但首次在有 Docker 的机器上跑时仍要按老步骤逐项验。
- 真实 LLM 端到端对话：stub 只验证了图流转和隔离，真实模型行为同前，仍未验证。

# 验证记录 · 2026-11-26

验证环境：Muse 工作区 VM（Ubuntu 24.04.5，Python 3.12.3）。**这台机器上没有 Docker**，
所以下面凡是标“未验证”的，都是因为缺 Docker / 缺 Playwright，不是代码写错。

## 2026-11-26 上下文/记忆/工具改进之后（新）

- `python3 -m py_compile agent/app.py agent/sandbox.py gateway/gateway.py browser/browser.py`：全部通过。
- 依赖不变（`agent/requirements.txt` 没改），全新 venv（`/tmp/mt-venv`）里
  `pip install -r agent/requirements.txt` 一次成功，`langgraph` 等导入通过。
- Stub 模型端到端测试（`/tmp/test_context_v2.py`，用假模型替掉 `ChatOpenAI`，
  不调真实接口；测试时 `CONTEXT_MAX_TURNS=2`、`SUMMARY_THRESHOLD_TURNS=3`，
  数据在 `/tmp/mt-test2-data`，没碰项目 `data/`），**全部通过**：
  - (a) 多轮上下文：第 2 轮的系统提示里包含第 1 轮原文（“我在上海”），
    `chat_log` 里有 2 轮问答；
  - (b) 旧对话变摘要：6 轮时 `load_context` 把最旧 4 轮用模型总结进
    `summaries` 表（摘要含“在上海/喜欢喝美式”），`chat_log` 只剩最近 2 轮
    （4 行），历史文本里不再有第 1 轮原文；
  - (b2) 无模型回退：`llm=None` 时不写摘要、直接截断最旧轮，只剩最近 2 轮；
  - (b3) 字符预算：`max_chars=40` 时从最旧一轮开始截，只保留最后一轮，
    `estimate_tokens("abcd")==1`、`("abcdefgh")==2`（约 4 字符 ≈ 1 token）；
  - (c) 租户隔离：tenant-b 的 `search_facts("上海")` 返回“没找到”，
    `load_context("tenant-b")` 的 facts/history/summary 全为空；
  - (d) 新工具：tenant-a 的 `list_workspace` 能看到 `notes/todo.md`，
    `search_file("牛奶")` 能找到、`search_file("不存在的词xyz")` 返回没找到，
    tenant-b 的工作区是空的、搜不到 tenant-a 的文件；`read_file("../tenant-b/…")`
    仍被路径前缀检查挡掉。
  - 测试中还发现并修了一个真问题：`load_context` 原先把 `max_chars` 下限
    钳在 500，导致小预算测不出来，现改成下限 10（只防 0/负数）。
- 无硬编码密钥：grep 检查过 py 文件（`.env.example` 里只有占位符和演示账号密码）。

## 未验证（改进后仍需在用户机器上跑）

- `docker compose up --build`、沙箱 docker 模式、browser 服务运行时、
  postgres / redis 启动：本机无 Docker / 没装 Playwright，**都没跑过**。
- **真实 LLM 的摘要质量没跑过**：stub 只验证了“会调总结、会存摘要、会截断”，
  真实模型总结的摘要是否准确、是否超过 200 字、非 OpenAI 兼容接口的兼容性，
  填真实 key 后要先聊 20 轮以上（默认 `SUMMARY_THRESHOLD_TURNS=20`）再看
  `summaries` 表。
- `CONTEXT_MAX_CHARS` 的默认值 12000（约 3000 token）是按字符粗估的，
  不是精确 token 计费；模型上下文上限不同时要在 `.env` 里调这个值。

## 2026-11-26 改成 LangGraph 之后（上一版记录，保留）

- 依赖可装：`pip install langgraph langchain-core langchain-openai` 在全新 venv 里一次成功，
  解析出的版本是 **langgraph 1.2.24 / langchain-core 1.8.5 / langchain-openai 1.8.5 /
  openai 3.59.0 / pydantic 2.14.4**，已按此 pin 在 `agent/requirements.txt`；
  `from langgraph.graph import StateGraph`、`ToolNode`、`langchain_openai.ChatOpenAI` 导入全部通过。
- `python3 -m py_compile agent/app.py agent/sandbox.py gateway/gateway.py browser/browser.py`：全部通过。
- `docker-compose.yml` 用 PyYAML 解析通过，5 个服务都在（compose 文件本身没改，
  只改了根 `Dockerfile` 加装 requirements）。
- 图结构实测：`build_graph("tenant-a")` 编译通过，节点齐全
  `recall_memory / llm / tools / final_llm / save_memory`，7 个工具齐全。
- 租户隔离复测（和上一版同一标准）：tenant-a 用 `remember_fact` 记住“喜好=喜欢喝美式”后，
  tenant-a 能查到，tenant-b 查同一 key 返回“没记过这件事”，`list_facts` 也看不到；
  路径穿越 `../tenant-b/memory.db` 仍被前缀检查挡掉；`rm` 仍不在白名单。
- stub 模型端到端（用假模型替掉 `ChatOpenAI`，不调真实接口）：
  `chat("tenant-a", "记住，我在上海")` 完整走了
  recall→llm→tools（remember_fact）→llm→save_memory，返回“记好了，你在上海。”；
  `facts` 表里有（城市，上海），`chat_log` 表里有一问一答。说明图循环、工具执行、
  存记忆三个节点都通了。
- agent-api + gateway 联调（本地 venv 的 `python` 启动两个进程，无 LLM key）：
  - `GET /api/health` → `{"ok": true, "model": "gpt-4o-mini", "engine": "langgraph", "sandbox_mode": "local"}`。
  - `GET /api/me` 无身份 → 401；经网关不带 token → 401；错误密码登录 → 401；正确登录 → 发 token，带 token 经网关 → `{"tenant": "demo"}`。
  - 经网关调 `/api/chat` → 缺 key 的报错正确透传（“还没填 LLM_API_KEY”）。
  - **只认 HMAC token 的路径复测过**：agent 不开 `TRUST_TENANT_HEADER` 时，
    直接拿 `X-Tenant-Id: demo` 调 `/api/me` 和 `/api/chat`（无 token）→ 都是 401，
    伪造租户头进不去，和上一版行为一致。
- 沙箱本地回退复测：`df` 能跑且带“本地回退：未隔离，仅开发用”标注。
- 无硬编码密钥：grep 检查过 py 文件（`.env.example` 里只有占位符和演示账号密码）。

## 未验证（改成 LangGraph 后仍需在用户机器上跑）

- `docker compose config` / `docker compose up --build`：本机无 Docker，**没跑过**。
  注意根 `Dockerfile` 现在多了 `pip install -r agent/requirements.txt` 一步，
  首次跑时确认这步能在镜像里成功（需要联网拉 PyPI）。
- 沙箱 docker 模式、browser 服务运行时、postgres / redis 启动：同上一版，均未验证。
- **LangGraph + 真实 LLM 的端到端对话：没跑过。** stub 模型只验证了图的流转，
  真实模型调工具时可能出现：模型不按工具格式返回、连续调工具超 3 轮（会走 final_llm
  强制回答）、`ChatOpenAI` 对非 OpenAI 兼容接口的兼容性问题。填真实 key 后先试一句
  “记住，我在上海”再试一句“帮我看看 df”，确认工具调用和记忆都通。
- LangGraph 的 `recursion_limit` 设的是 30，3 轮工具循环（每轮 llm+tools 约 2 步）
  加 recall/save 节点远到不了上限，只是防死循环的安全网。

## 以下是上一版（手写循环）的验证记录，保留作历史


## 已验证（本地实测通过）

- `python3 -m py_compile agent/app.py agent/sandbox.py gateway/gateway.py browser/browser.py`：全部通过。
- `docker-compose.yml` 用 PyYAML 解析通过，5 个服务都在：`gateway / agent-api / browser / postgres / redis`。
- agent-api 单独启动（`SANDBOX_MODE=local`，无 LLM key）：
  - `GET /api/health` → `{"ok": true, "model": "gpt-4o-mini", "sandbox_mode": "local"}`。
  - `GET /api/me` 无身份 → 401；带 `X-Tenant-Id: demo`（`TRUST_TENANT_HEADER=1` 时）→ `{"tenant": "demo"}`。
  - `POST /api/chat` 无 key → 返回“还没填 LLM_API_KEY”，服务不崩。
- gateway + agent-api 联调（本地 `python3` 启动两个进程）：
  - 登录页 `GET /` → 200；错误密码登录 → 401“用户名或密码不对”；正确登录 → 发 token。
  - 不带 token 访问 `/api/me` → 401；带 token → `{"tenant": "demo"}`。
  - 经网关调 `/api/chat` → 缺 key 的报错正确透传。
  - **只认 HMAC token 的路径也测过**：agent 不开 `TRUST_TENANT_HEADER` 时，
    经网关带 token 能进；直接拿 `X-Tenant-Id` 调 agent-api（无 token）→ 401。
    也就是说 compose 里（gateway 和 agent 共用 `GATEWAY_SECRET`）的鉴权链是通的。
- 沙箱（本地回退模式）：`df` 能跑，返回 `[本地回退：未隔离，仅开发用]` 标注；
  `rm` 不在白名单 → 被拒，返回可用命令列表。
- 路径穿越检查：`workspace/../tenant-b/secret.txt` 解析后不在本租户工作区内，被前缀检查挡掉。
- 无硬编码密钥：grep 检查过，`.env.example` 里只有占位符和演示账号密码（首次使用务必改掉）。

## 未验证（需要用户的机器 / Docker）

- `docker compose config` / `docker compose up --build`：本机无 Docker，**没跑过**。
  首次在有 Docker 的机器上跑时，先跑 `docker compose config` 检查，再 `up`。
- 沙箱 docker 模式（`docker run --rm --read-only --network none …`）：本机无 Docker，
  只验证了本地回退模式。docker 模式的代码路径在 `agent/sandbox.py`，等有 Docker 后再实测。
- browser 服务运行时：本机没装 Playwright，只做了 `py_compile`。
  首次跑时要在容器里确认 `mcr.microsoft.com/playwright/python:v1.49.0-jammy`
  镜像能拉到、`pip install -r requirements.txt` 能装上，再调一次 `/browse`。
- LLM 真实对话：没填真实 key，没跑过端到端聊天。填 key 后按 README 的步骤试一句即可。
- postgres / redis：compose 里有这两个服务，但当前版本 agent 默认用每租户 SQLite，
  不依赖它俩；它俩的启动本身也没在这台机器上验证过。

## pi-agent-starter 是否动过

没动。`~/workspace/pi-agent-starter/` 只有原有文件（多了一个 2026-11-26 上午的
`MULTI_TENANT_VM_PLAN.md`，那是之前那轮对话里生成的计划文件，本次任务没有改它）。

# 多租户 Agent · 单机 + Docker 原型

> 由 `~/workspace/pi-agent-starter/`（单租户）改来，2026-11-26 建。
> 目标：单机 + Docker 上每个租户一台隔离的“虚拟电脑”（先用每会话一个容器实现，
> 以后可换成 Firecracker 微虚拟机 / 完整 KVM 虚拟机，见下文“隔离路线图”）。
> 现有 starter 完全没动，这个项目是独立的新目录。

## 项目结构

```
multi-tenant-agent/
├── docker-compose.yml      # 5 个服务：gateway / agent-api / browser / postgres / redis
├── Dockerfile              # agent-api 镜像：装 agent/requirements.txt，CMD python3 agent/app.py
├── .env.example
├── agent/                  # agent-api（Python 包，可 python3 agent/app.py 或 python -m agent.app 启动）
│   ├── app.py              # 薄入口：只转发到 server.main()（20 行）
│   ├── config.py           # 环境变量和常量（PORT/DATA_DIR/LLM_*/CONTEXT_*/TOKEN_SECRET/提示词）
│   ├── tenant.py           # 租户隔离：tenant_dirs / tenant_db / safe_path / token 校验
│   ├── memory.py           # 记忆与上下文：facts/chat_log/summaries、load_context、摘要/截断
│   ├── tools.py            # 10 个工具：make_tools(tenant)，浏览器实现 browse_page_impl
│   ├── graph.py            # LangGraph 图：AgentState、build_graph、chat()
│   ├── server.py           # HTTP 服务：页面 + /api/health、/api/chat、/api/me
│   ├── sandbox.py          # 每会话沙箱：docker 模式 / local 回退
│   └── requirements.txt    # langgraph / langchain-core / langchain-openai
├── gateway/                # 网关：登录 + 发 token + 反向代理
└── browser/                # 浏览器服务：Playwright Chromium
```

各模块只通过明确的 import 单向依赖：`server → graph → tools/memory → tenant → config`，
`sandbox` 只被 `tools` 调。隔离逻辑只有 `tenant.py` 一份，工具和记忆都不各自重复实现。

## 架构（文字图）

```
                    ┌──────────────────────────────────────────────┐
                    │  用户浏览器                                   │
                    └───────────┬──────────────────────────────────┘
                                │  http://<宿主机>:8080
                                ▼
                    ┌───────────────────────┐
                    │  gateway 网关 (8080)   │  登录 → 发 token（HMAC 签名）
                    │  演示账号密码，见 .env  │  每个请求带上 X-Tenant-Id
                    └───────────┬───────────┘
                                │  内网 only（agent-api 不对外发布端口）
                                ▼
              ┌─────────────────────────────────┐
              │  agent-api (8081，容器内)       │
              │  LangGraph 图：recall → llm →    │
              │  tools 循环（见下节）            │
              │  记忆：每租户一个 SQLite         │
              │  命令：每会话一个沙箱容器        │
              └──────┬──────────────┬───────────┘
                     │              │
        ┌────────────┴───┐  ┌───────┴────────────┐
        ▼                ▼  ▼                    ▼
   沙箱容器          browser 服务   postgres / redis（可选）
   每会话一个        Playwright     postgres：以后做共享元数据
   用完即销毁        Chromium       redis：以后做限流/缓存
   (docker run)      开网页/截图     —— 当前版本 agent 默认用
                     返回标题/正文   每租户 SQLite，不依赖它俩
```

数据隔离（每一步都按 `tenant_id` 分）：

| 东西 | 位置 | 说明 |
|---|---|---|
| 工作区文件 | `data/<tenant>/workspace/` | 只能读写自己的目录，`..` 穿越会被挡掉 |
| 记忆 | `data/<tenant>/memory.db` | 每租户一个 SQLite，`facts` / `chat_log` / `summaries` 三张表，不跨租户查 |
| 浏览器 profile | `data/<tenant>/browser-profile/` | 每租户独立，Cookie/登录态不串 |
| 浏览器截图 | `data/<tenant>/workspace/browser-*.png` | 由 browser 服务写到共享卷 |

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
docker compose up -d --build
# 打开 http://<宿主机IP>:8080 ，用 .env 里 GATEWAY_USERS 的账号登录
# 试试：df / 记住：我喜欢喝美式 / 帮我打开 https://example.com 看看标题是什么
docker compose down        # 停掉；data/ 会保留各租户的工作区和记忆
```

没有 Docker 的机器上也可以先跑通聊天逻辑（见 `VERIFY.md`），但要先装依赖：

```bash
python3 -m venv .venv && .venv/bin/pip install -r agent/requirements.txt
cp .env.example .env   # LLM_API_KEY 先留空也能起服务，聊天时会提示缺 key
PORT=8081 DATA_DIR=./data .venv/bin/python agent/app.py
```

## 各服务

| 服务 | 镜像/构建 | 端口 | 说明 |
|---|---|---|---|
| gateway | `gateway/Dockerfile`（python:3.12-slim，标准库） | 8080（唯一对外端口） | 登录 + 发 token + 反向代理到 agent-api |
| agent-api | 根目录 `Dockerfile`（python:3.12-slim，装 `agent/requirements.txt`：langgraph 等） | 8081（仅容器内网） | LangGraph 图编排、工具、记忆、沙箱、调浏览器服务 |
| browser | `browser/Dockerfile`（Playwright Python 镜像） | 8001（仅容器内网） | Chromium 无头浏览：打开页面、取标题/正文、截图 |
| postgres | `postgres:16-alpine` | 不发布 | 可选。当前版本 agent 不依赖它，留给以后做共享元数据/计量 |
| redis | `redis:7-alpine` | 不发布 | 可选。当前版本 agent 不依赖它，留给以后做限流/缓存 |

## 隔离路线图（按这个顺序升级，每步都能单独验证）

| 阶段 | 隔离 | 状态 |
|---|---|---|
| 1. 容器沙箱（本项目） | 每会话一个 `docker run --rm` 容器：`--read-only` 根文件系统、禁 `--privileged`、无网络（`--network none`）、CPU 0.5 核 / 内存 256MB 上限，只挂载该租户的工作区 | 已实现；宿主机必须有 Docker 且 agent-api 挂了 `/var/run/docker.sock` |
| 2. 回退模式 | 宿主机没有 Docker 时，命令在 agent-api 本地跑白名单命令（仍限工作区内），输出会明确标注“本地回退：未隔离，仅开发用” | 已实现，仅用于开发调试 |
| 3. Firecracker 微虚拟机 | 把第 1 步的 `docker run` 换成微虚拟机启动，其他层不动 | 未实现，待定（需要宿主机有 `/dev/kvm`，见下文待讨论问题） |
| 4. 完整 KVM 虚拟机 | 每租户一台常驻虚拟机 | 未实现，只在租户少、隔离要求最高时用 |

### 容量参考（来自 `pi-agent-starter/MULTI_TENANT_VM_PLAN.md`）

| 层次 | 每租户额外开销 | 单机 32GB / 8 核大概能同时跑 |
|---|---|---|
| Docker 容器 | 约 50–100MB 内存 | 几十个轻度租户 |
| Firecracker 微虚拟机 | 约几 MB–几十 MB + 应用内存 | 几十个，按需启动可更多 |
| 完整 KVM 虚拟机 | 至少 1 核 / 2GB 内存 / 20GB 磁盘 | 约 10 台左右 |

通用规则：**不活跃就关机/快照，活跃才启动**。100 个租户 ≠ 100 台同时在线的机器。

## 安全提醒（重要，请先读）

- **网关的账号密码是演示用的**，只适合局域网原型：密码以明文写在 `.env` 的 `GATEWAY_USERS` 里，
  token 是 HMAC 签名的简单令牌，没有刷新、没有防爆破。生产用请换成 Keycloak / Authentik（见 `.env.example` 注释）。
- **不要把 8080 暴露到公网**，只在局域网用；想在外面用，先走自己的 VPN / 内网穿透。
- `.env` 只放占位符和你自己的 key，**不要提交、不要发给别人**；`.gitignore` 已覆盖 `.env` 和 `data/`。
- LLM API Key 只通过环境变量 `LLM_API_KEY` 传入，代码里没有任何真实 key。
- 沙箱容器禁 privileged、只读根文件系统、无网络；但容器共用宿主机内核，理论上仍存在容器逃逸风险，
  隔离强度低于 Firecracker / 完整虚拟机，做不到 Muse 同级。
- 浏览器服务跑的是无头 Chromium：能开网页、登录、截图、抓正文；但网站可能检测无头浏览器而弹验证码，
  视频/音频/DRM 支持差。需要“用户直接看画面操作”时，要再加 Xvfb + noVNC，本项目暂未做。
- `agent-api` 挂载了宿主机的 `/var/run/docker.sock` 才能起沙箱容器——这本身就是一个高权限点，
  只能在自己可信的单机上用，不要把这个 compose 文件原样搬到共享宿主机上。

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

见 `VERIFY.md`：2026-11-26 改成 LangGraph 后，在本机 venv 里实测通过图编译、
租户隔离、stub 模型端到端循环和网关联调；但这台 VM 上没有 Docker，
所以 `docker compose up`、沙箱 docker 模式、浏览器运行时和真实 LLM 对话都没跑过，
第一次在有 Docker 的机器上跑时按 VERIFY.md 逐项验。

# 回归测试

2026-11-27 重建：原来 `/tmp/test_reminders.py`（37 项）、
`/tmp/test_reminder_fixes.py`（46 项）、`/tmp/test_files.py`（29 项）
都在一次 VM 更换里丢了（`/tmp` 是易失的）。本目录按 VERIFY.md
的用例清单重建，不凭记忆编用例；凡是 VERIFY.md 没逐条写清的，
下面“对账”一节逐条说明。

## 怎么跑

```bash
cd ~/workspace/multi-tenant-agent
~/workspace/tools/mtvenv/bin/python -m unittest discover -s tests -t .
```

只用标准库 unittest（venv 里没有 pytest，不新装依赖）；不联网、
不用真实 LLM key（e2e 用剧本假模型替掉 `ChatOpenAI`）、不用真 Docker。

## 测试数据规则

- 每个测试用 `tests/helpers.py` 的 `fresh_tenant_data()` 拿一个全新
  的临时 DATA_DIR，跑完自动删掉，**绝不碰项目 `data/`**。
- 唯一例外：调度器线程测试（`test_reminder_tools.py` 的
  `TestSchedulerThread`）用包默认的临时 DATA_DIR，因为它起的守护
  线程在测试方法返回后还在跑；它只扫一个别的测试都不会用的
  `sched-tenant` 租户，见该测试的说明。
- `tests/__init__.py` 在 import  agent/gateway 之前先把环境变量定死：
  `TENANT_ID=demo`、测试密钥、假 `LLM_API_KEY`、`DATA_DIR` 强制指向
  临时目录。

## 覆盖（共 136 项，全过）

| 文件 | 项数 | 覆盖 |
|---|---|---|
| `unit/test_reminder_parsing.py` | 33 | 新解析 11 + 新抽取 6 + 旧解析 10 + 旧抽取 6 |
| `unit/test_reminder_rules.py` | 10 | 顺延/核对 6 + 例子/记忆 3 + TOOLS_DOC 含 update_reminder 1 |
| `unit/test_event_log.py` | 5 | P0 事件日志：截断归档不删行、二次加载不重复归档、摘要路径、返回结构、归档行可查 |
| `integration/test_reminder_tools.py` | 33 | 工具 8 + 附加 3 + Bug 4 8 + 旧工具 7 + 旧调度 6 + 调度器线程 1 |
| `integration/test_files.py` | 29 | agent /files/ 19 + 网关 10 |
| `integration/test_transport.py` | 21 | P1 传输层：路由/错误码结构/静态页/事件/健康/指标/网关（见下） |
| `e2e/test_reminder_dialogue.py` | 5 | 原对话 5 步（假模型走 `chat()` 整条图） |

P0（2026-11-27）新增 `unit/test_event_log.py` 5 项，总数 110 → 115。
P1（2026-11-27）新增 `integration/test_transport.py` 21 项，总数 115 → 136：
路由与静态页 3 项（`/` 返回 chat.html 文件字节、两处 HTML 文件内容与原内嵌
字符串一致）、健康与指标 3 项（health 内容不变、metrics 文本格式、请求计数
+1）、统一错误码 7 项（401/404/403/400/400-JSON/500 的 `{"error": {...}}`
结构）、事件 5 项（tool_call、reminder_delivered、空数组、collect_events
顺序与空结果）、错误码包 1 项、网关 2 项（`/` 返回 login.html 文件内容、
_proxy 未登录 401 结构）。
另注意：P1 后 e2e 的假模型贴在 `model.client.ChatOpenAI` 上
（原来贴在 `agent.graph.ChatOpenAI`，模型创建已收进 model 层）。

## 对账：总数怎么对上 VERIFY.md

- **修复 46 项**：VERIFY.md 逐条列出 41 项（解析 11 + 抽取 6 + 工具 8 +
  顺延/核对 5 + Bug 4 8 + 例子/记忆 3）。另有 5 项是 VERIFY.md
  修复一节的改动文字里明确写了的行为，逐条有出处，不是新编的：
  update_reminder 改完旧通知作废、list_reminders 状态显示中文、
  add_reminder 返回含今天/明天、TOOLS_DOC 增加 update_reminder，
  以及“系统提示含核对规则与顺延规则”拆成核对、顺延 2 个断言。
  41 + 5 = 46。
- **文件 29 项**：VERIFY.md 逐条列出 27 项（agent 17 + 网关 10）。
  另有 2 项按 `_serve_file` 的白名单逻辑补的：css 的 Content-Type
  是 `text/css`、非白名单后缀（如 `.exe`）403。
- **旧套件 37 项**：逐条重建 30 项（解析 10 + 抽取 6 + 工具 7 +
  调度 6 + 线程 1）。其余 7 项见下节。
- **e2e 5 项**：VERIFY.md 修复一节“仍需服务器实测”的原对话 5 步，
  在本机 stub 能测的部分。

## 没法重建 / 已不适用的用例（逐条）

1. 旧抽取“裸 8 点提醒 → due_at 留空落盘”：Bug 5 已改成不落盘，
   对应 VERIFY.md 修复一节 Bug 5。现行测试是
   `test_bare_8_not_saved`（不落盘、进歧义列表）。
2. 旧抽取返回值精确字典比对（全 0）：新返回多了 `ambiguous_reminders`
   键，旧断言按新行为必然失败，对应 VERIFY.md 修复一节末尾
   “旧测试复跑 4 失败”里的第二条。现行测试按新返回值断言。
3. 旧 `list_reminders` 里有歧义那条：歧义不落盘，列表里没有，
   对应同一处复跑记录的第四条。现行测试是
   `test_list_reminders_has_no_ambiguous_row`。
4. 旧抽取“提醒我早上 8 点喝水”与新抽取重复：只保留一份
   （`test_morning_8`）。另注意现行行为：带空格的
   “提醒我早上 8 点喝水”抽取后内容是“早上 8 点喝水”而不是“喝水”
   （时间词剥离正则不认“早上”和数字之间的空格），测试里只断言
   时间和条数，不对内容断言，见 `test_reminder_parsing.py` 的注释。
5. 旧套件总数 37 项，但 VERIFY.md 逐条只列出 32 项
   （10+8+7+6+1），其余 5 项没有逐条展开，无法逐条对应重建；
   上面 1–4 条占了其中 2 项（裸 8 点留空、早上 8 点重复），
   剩下 3 项没有文字可依，不硬凑。

## e2e 的断言边界

假模型（`ScriptedLLM`）只按剧本回应，测试只断言工具调用链和落盘
结果（问没问上下午、行里的时间对不对、重设是不是只改不新增），
**不硬断言模型原话**。“设完不反问明天”“歧义只问一次”这类措辞是
模型行为，假模型测不出来，对应的规则由 `test_reminder_rules.py`
的提示词断言覆盖（核对规则、顺延规则、只问一次）。不要为了凑数
写假通过的断言。

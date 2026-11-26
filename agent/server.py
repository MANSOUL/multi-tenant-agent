"""HTTP 服务：聊天页面 + /api/health、/api/chat、/api/me。

只负责 HTTP 层：鉴权（调 tenant.get_tenant）、把消息交给 graph.chat()、
返回 JSON。业务逻辑（图、工具、记忆、沙箱）都在别的模块。
提醒调度器（2026-11-26）：启动时起一个守护线程，每 30 秒扫一次本租户
（单租户容器）或 DATA_DIR 下所有租户（老的多租户进程模式）的 reminders，
到点的写进 pending_notifications 表；下次用户来聊天时，recall_memory
会把未送达的提醒拼在系统提示最前面当面告诉用户。注意：这个 agent 只有
网页聊天框，没有向用户手机推送的通道，所以“到点提醒”实际是“到点后下次
聊天时提醒”，不要在任何地方假装能推送（见 README 能力边界）。
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from .config import DATA_DIR, MODEL, PORT, SINGLE_TENANT_MODE, TENANT_ID
    from .graph import chat
    from .memory import check_due_reminders
    from .tenant import get_tenant
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时
    from config import DATA_DIR, MODEL, PORT, SINGLE_TENANT_MODE, TENANT_ID
    from graph import chat
    from memory import check_due_reminders
    from tenant import get_tenant


def _reminder_tenants():
    """调度器要扫的租户：单租户容器只扫自己；否则扫 DATA_DIR 下所有租户目录。"""
    if TENANT_ID:
        return [TENANT_ID]
    try:
        return [p.name for p in DATA_DIR.iterdir()
                if p.is_dir() and (p / "memory.db").exists()]
    except OSError:
        return []


def _reminder_loop(interval_seconds: int = 30):
    """调度器后台线程：每 interval 秒扫一次，异常自己吞掉，绝不把服务拖垮。"""
    while True:
        try:
            for tenant in _reminder_tenants():
                check_due_reminders(tenant)
        except Exception:
            pass
        time.sleep(interval_seconds)


def start_reminder_scheduler(interval_seconds: int = 30) -> threading.Thread:
    """启动提醒调度器守护线程，返回线程对象（测试可调小 interval）。"""
    t = threading.Thread(target=_reminder_loop, args=(interval_seconds,),
                         daemon=True, name="reminder-scheduler")
    t.start()
    return t

PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>多租户 Agent</title><style>
body{font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px}
#log{border:1px solid #ccc;border-radius:8px;padding:12px;height:50vh;overflow:auto;white-space:pre-wrap}
.me{color:#2563eb}.pi{color:#16a34a}
form{display:flex;gap:8px;margin-top:12px}
input{flex:1;padding:8px;font-size:16px}button{padding:8px 16px}
</style></head><body><h1>多租户 Agent</h1>
<p>请通过网关登录（http://宿主机:8080），登录后你的租户 id 会显示在这里。</p>
<div id="log"></div>
<form onsubmit="send(event)"><input id="q" autocomplete="off" placeholder="跟助手说点什么…"><button>发送</button></form>
<script>
async function send(e){e.preventDefault();
 const q=document.getElementById('q'); const t=q.value.trim(); if(!t) return; q.value='';
 const log=document.getElementById('log');
 log.innerHTML+=`<div class="me">你：${t}</div>`; log.scrollTop=log.scrollHeight;
 const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:t})});
 const d=await r.json();
 log.innerHTML+=`<div class="ai">助手：${d.reply||d.error||'出错了'}</div>`; log.scrollTop=log.scrollHeight;}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安静一点，别刷屏
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _need_tenant(self):
        tenant = get_tenant(self)
        if not tenant:
            self._send(401, json.dumps({"error": "未登录：请通过网关登录后再访问"},
                                       ensure_ascii=False), "application/json")
            return None
        return tenant

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE)
        elif self.path == "/api/health":
            self._send(200, json.dumps(
                {"ok": True, "model": MODEL, "engine": "langgraph",
                 "tenant": TENANT_ID or None,
                 "single_tenant": SINGLE_TENANT_MODE,
                 "sandbox_mode": os.environ.get(
                     "SANDBOX_MODE", "docker")}), "application/json")
        elif self.path == "/api/me":
            tenant = self._need_tenant()
            if tenant:
                self._send(200, json.dumps({"tenant": tenant},
                                           ensure_ascii=False),
                           "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/chat":
            return self._send(404, "not found", "text/plain")
        tenant = self._need_tenant()
        if not tenant:
            return
        try:
            payload = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0)) or 0))
            text = (payload.get("message") or "").strip()
            if not text:
                return self._send(400, json.dumps({"error": "消息是空的"}),
                                  "application/json")
            self._send(200, json.dumps(
                {"reply": chat(tenant, text), "tenant": tenant},
                ensure_ascii=False), "application/json")
        except Exception as e:  # noqa: BLE001 - 把错误原样告诉用户，方便调试
            self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False),
                       "application/json")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    start_reminder_scheduler()
    print(f"多租户 Agent API 跑起来了：http://localhost:{PORT}  模型={MODEL} 引擎=langgraph")
    print("提醒调度器已启动（每 30 秒扫一次，到点提醒下次聊天时当面送达）")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

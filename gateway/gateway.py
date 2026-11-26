"""网关：演示登录 + 反向代理（只用 Python 标准库）。

流程：
  1. 用户打开 http://<宿主机>:8080 ，看到登录页。
  2. POST /api/login，账号密码对上 GATEWAY_USERS 里的演示账号，
     就发一个 HMAC 签名 token（tenant.过期时间.签名），有效期 12 小时，
     存在 cookie 里，同时返回给前端。
  3. 之后所有 /api/* 请求，网关先校验 token，校验通过才反向代理到
     agent-api，并在转发时带上 X-Tenant-Id 和 X-Tenant-Token。
     校验不过直接 401，不会把请求透传给 agent-api。

⚠️ 这只是演示登录：密码明文写在 .env 的 GATEWAY_USERS 里，没有防爆破、
没有改密码、没有多因素。生产请换成 Keycloak / Authentik（OIDC），
把 GATEWAY_USERS 和这套 token 逻辑整个替换掉，agent-api 不用改——
它只认 X-Tenant-Token / X-Tenant-Id，见 agent/app.py 的 get_tenant。
"""
import hashlib
import hmac
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

PORT = int(os.environ.get("PORT", "8080"))
AGENT_API_URL = os.environ.get("AGENT_API_URL", "http://127.0.0.1:8081").rstrip("/")
SECRET = os.environ.get("GATEWAY_SECRET", "change-me-to-a-long-random-string")


def load_users() -> dict:
    """GATEWAY_USERS=demo:demo123,tenant-a:changeme-a"""
    users = {}
    for pair in os.environ.get("GATEWAY_USERS", "demo:demo123").split(","):
        if ":" in pair:
            name, pw = pair.split(":", 1)
            users[name.strip()] = pw.strip()
    return users


def make_token(tenant: str) -> str:
    exp = int((datetime.now(timezone.utc) + timedelta(hours=12)).timestamp())
    sig = hmac.new(SECRET.encode(), f"{tenant}.{exp}".encode(),
                   hashlib.sha256).hexdigest()
    return f"{tenant}.{exp}.{sig}"


def verify_token(token: str) -> str | None:
    try:
        tenant, exp, sig = token.split(".")
        if int(exp) < int(datetime.now(timezone.utc).timestamp()):
            return None
        expect = hmac.new(SECRET.encode(), f"{tenant}.{exp}".encode(),
                          hashlib.sha256).hexdigest()
        return tenant if hmac.compare_digest(expect, sig) else None
    except (ValueError, TypeError):
        return None


def token_from(handler) -> str:
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    for part in handler.headers.get("Cookie", "").split(";"):
        part = part.strip()
        if part.startswith("mt_token="):
            return part[len("mt_token="):]
    return ""


LOGIN_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · 多租户 Agent</title><style>
body{font-family:system-ui;max-width:420px;margin:80px auto;padding:0 16px}
input{display:block;width:100%;padding:10px;margin:8px 0;font-size:16px;box-sizing:border-box}
button{padding:10px 20px;font-size:16px}
#msg{color:#dc2626;white-space:pre-wrap}
#chat{display:none}
#log{border:1px solid #ccc;border-radius:8px;padding:12px;height:40vh;overflow:auto;white-space:pre-wrap}
.me{color:#2563eb}.ai{color:#16a34a}
form{display:flex;gap:8px;margin-top:12px}
</style></head><body>
<div id="login"><h1>登录</h1>
<p>演示账号见 .env 的 GATEWAY_USERS（生产请换 Keycloak / Authentik）。</p>
<input id="u" placeholder="用户名（即租户 id）" autocomplete="username">
<input id="p" type="password" placeholder="密码" autocomplete="current-password">
<button onclick="login()">登录</button><p id="msg"></p></div>
<div id="chat"><h1>多租户 Agent</h1><p id="who"></p>
<div id="log"></div>
<form onsubmit="send(event)"><input id="q" autocomplete="off" placeholder="跟助手说点什么…"><button>发送</button></form>
<p><button onclick="logout()">退出登录</button></p></div>
<script>
let token='';
async function login(){
 const u=document.getElementById('u').value.trim();
 const p=document.getElementById('p').value;
 const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
 const d=await r.json();
 if(!r.ok){document.getElementById('msg').textContent=d.error||'登录失败';return;}
 token=d.token; document.cookie='mt_token='+token+'; path=/';
 document.getElementById('login').style.display='none';
 document.getElementById('chat').style.display='block';
 document.getElementById('who').textContent='已登录，租户：'+d.tenant;}
async function send(e){e.preventDefault();
 const q=document.getElementById('q'); const t=q.value.trim(); if(!t) return; q.value='';
 const log=document.getElementById('log');
 log.innerHTML+=`<div class="me">你：${t}</div>`; log.scrollTop=log.scrollHeight;
 const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify({message:t})});
 const d=await r.json();
 log.innerHTML+=`<div class="ai">助手：${d.reply||d.error||'出错了'}</div>`; log.scrollTop=log.scrollHeight;}
function logout(){document.cookie='mt_token=; path=/; max-age=0';location.reload();}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        try:
            return json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, LOGIN_PAGE)
        if self.path.startswith("/api/"):
            return self._proxy()
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/login":
            payload = self._read_json()
            users = load_users()
            name = (payload.get("username") or "").strip()
            if name in users and users[name] == (payload.get("password") or ""):
                token = make_token(name)
                return self._send(200, json.dumps(
                    {"token": token, "tenant": name}, ensure_ascii=False),
                    "application/json",
                    {"Set-Cookie": f"mt_token={token}; Path=/; HttpOnly; SameSite=Lax"})
            return self._send(401, json.dumps(
                {"error": "用户名或密码不对"}, ensure_ascii=False),
                "application/json")
        if self.path.startswith("/api/"):
            return self._proxy()
        return self._send(404, "not found", "text/plain")

    def _proxy(self):
        """校验 token → 透传到 agent-api，带上租户身份。"""
        tenant = verify_token(token_from(self))
        if not tenant:
            return self._send(401, json.dumps(
                {"error": "未登录：请先登录"}, ensure_ascii=False),
                "application/json")
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "X-Tenant-Id": tenant,
            "X-Tenant-Token": token_from(self),
        }
        req = urllib.request.Request(
            AGENT_API_URL + self.path, data=body, headers=headers,
            method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = resp.read()
                return self._send(resp.status, data,
                                  resp.headers.get_content_type()
                                  + "; charset=utf-8")
        except HTTPError as e:
            return self._send(e.code, e.read(),
                              e.headers.get_content_type() + "; charset=utf-8")
        except Exception as e:  # noqa: BLE001 - 代理失败时把原因告诉前端
            return self._send(502, json.dumps(
                {"error": f"连不上 agent-api：{e}"}, ensure_ascii=False),
                "application/json")


if __name__ == "__main__":
    print(f"网关跑起来了：http://localhost:{PORT}  → agent-api {AGENT_API_URL}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

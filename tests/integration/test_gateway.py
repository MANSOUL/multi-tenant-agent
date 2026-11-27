"""P2 网关拆分（integration，不用真 Docker、不用真 key）。

覆盖：edge/auth 的 token 签发/校验（过期、篡改、cookie/Bearer 两路）、
edge/proxy 的 Content-Type 透传、infra/runtime 的镜像 digest 比对
（相同不重建、不同重建、docker 不可用时 fail-open 不拦登录）、
control/tenants 的开通/删除/改密码/配额/用量汇总（fake runtime）、
管理 API 的鉴权与开通/删除流程（真起 127.0.0.1 随机端口）。
测试数据只用临时目录，不碰项目 data/，不写真实 .env。
"""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest import mock

from tests.helpers import TEST_SECRET, make_token
from gateway import main as gateway_mod
from gateway.control import tenants as control_mod
from gateway.edge import auth as auth_mod
from gateway.infra import runtime as runtime_mod

HTML = b"<!DOCTYPE html><html><body>calc</body></html>"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


class _FakeResp:
    def __init__(self, status=200, body=b"", ctype="text/html; charset=utf-8"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": ctype}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_gateway_handler(path="/files/calculator.html", headers=None,
                          command="GET", body=b""):
    h = object.__new__(gateway_mod.Handler)
    h.path = path
    h.headers = dict(headers or {})
    h.command = command
    h.rfile = BytesIO(body)
    h.sent = []
    h._send = lambda code, body_, ctype="text/html; charset=utf-8", extra=None: \
        h.sent.append((code, body_, ctype, extra))
    h._send_error = lambda err: h.sent.append(
        (err.status, json.dumps(err.body(), ensure_ascii=False),
         "application/json", None))
    return h


class TestAuthTokens(unittest.TestCase):
    """edge/auth：token 签发/校验、cookie/Bearer 两路读取。"""

    def test_make_verify_roundtrip(self):
        token = auth_mod.make_token("demo")
        self.assertEqual(auth_mod.verify_token(token), "demo")

    def test_expired_token_rejected(self):
        import time
        token = make_token("demo", exp=int(time.time()) - 10)
        self.assertIsNone(auth_mod.verify_token(token))

    def test_tampered_signature_rejected(self):
        token = auth_mod.make_token("demo")
        head, _, _ = token.rpartition(".")
        self.assertIsNone(auth_mod.verify_token(head + ".0" * 32))

    def test_token_from_cookie(self):
        token = make_token("demo")
        h = _fake_gateway_handler(headers={"Cookie": f"mt_token={token}"})
        self.assertEqual(auth_mod.token_from(h), token)

    def test_token_from_authorization_header(self):
        token = make_token("demo")
        h = _fake_gateway_handler(
            headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(auth_mod.token_from(h), token)


class TestProxyPassthrough(unittest.TestCase):
    """edge/proxy：经 gateway/main.Handler 透传上游 Content-Type。"""

    def _run_proxy(self, fake_resp):
        token = make_token("demo")
        handler = _fake_gateway_handler(
            "/files/calculator.html",
            {"Authorization": f"Bearer {token}"})
        captured = {}

        def fake_urlopen(req, timeout=90):
            captured["url"] = req.full_url
            sent_headers = dict(req.header_items())
            captured["tenant_token"] = sent_headers.get("X-tenant-token")
            captured["tenant_id"] = sent_headers.get("X-tenant-id")
            return fake_resp

        with (mock.patch.object(gateway_mod, "ensure_tenant_container",
                                lambda tenant: None),
              mock.patch("urllib.request.urlopen", fake_urlopen)):
            handler._proxy()
        return handler, captured

    def test_proxy_passthrough_html_content_type(self):
        handler, captured = self._run_proxy(_FakeResp(200, HTML))
        self.assertEqual(handler.sent[0][0], 200)
        self.assertEqual(handler.sent[0][1], HTML)
        self.assertEqual(handler.sent[0][2], "text/html; charset=utf-8")
        self.assertEqual(
            captured["url"], "http://mt-tenant-demo:8081/files/calculator.html")
        self.assertEqual(captured["tenant_id"], "demo")

    def test_proxy_passthrough_png_content_type(self):
        # 图片不许被套上 "; charset=utf-8"。
        handler, _ = self._run_proxy(_FakeResp(200, PNG, "image/png"))
        self.assertEqual(handler.sent[0][2], "image/png")
        self.assertEqual(handler.sent[0][1], PNG)


class TestImageDigest(unittest.TestCase):
    """infra/runtime：镜像 digest 比对（fake docker CLI，不拉镜像）。"""

    def _fake_docker(self, current, used, calls):
        def fake(argv, timeout=120):
            calls.append(list(argv))
            if argv[0] == "image":
                return (0, current, "")
            if argv[0] == "inspect" and argv[2] == "{{.Image}}":
                return (0, used, "")
            if argv[0] == "inspect":
                return (0, "true", "")
            if argv[:2] == ["rm", "-f"]:
                return (0, "", "")
            return (1, "", "unknown docker call")
        return fake

    def test_same_digest_no_rebuild(self):
        calls = []
        fake = self._fake_docker("sha256:new", "sha256:new", calls)
        with mock.patch.object(runtime_mod, "_docker", fake):
            self.assertTrue(runtime_mod.ensure_fresh("demo"))
        self.assertFalse(any(c[:2] == ["rm", "-f"] for c in calls))

    def test_different_digest_rebuilds(self):
        calls = []
        fake = self._fake_docker("sha256:new", "sha256:old", calls)
        with mock.patch.object(runtime_mod, "_docker", fake):
            self.assertTrue(runtime_mod.ensure_fresh("demo"))
        self.assertIn(["rm", "-f", "mt-tenant-demo"], calls)

    def test_docker_unavailable_fail_open_login(self):
        # docker 全挂：ensure_fresh 返回 False，但登录照样成功，
        # 且没有删容器（不拦登录、不破坏现场）。
        calls = []

        def failing_docker(argv, timeout=120):
            calls.append(list(argv))
            return (1, "", "docker: command not found")

        payload = json.dumps({"username": "demo", "password": "demo123"})
        handler = _fake_gateway_handler(
            "/api/login",
            {"Content-Type": "application/json",
             "Content-Length": str(len(payload))},
            command="POST", body=payload.encode())
        with (mock.patch.object(runtime_mod, "_docker", failing_docker),
              mock.patch.object(gateway_mod, "ensure_tenant_container",
                                lambda tenant: None),
              mock.patch.object(gateway_mod, "USERS",
                                {"demo": "demo123"})):
            handler.do_POST()
        self.assertEqual(handler.sent[0][0], 200)
        body = json.loads(handler.sent[0][1])
        self.assertEqual(body["tenant"], "demo")
        self.assertEqual(auth_mod.verify_token(body["token"]), "demo")
        self.assertFalse(any(c[:2] == ["rm", "-f"] for c in calls))


class FakeRuntime:
    """control 测试用的 fake runtime：只记调用，不碰真 docker。"""

    def __init__(self, state="running"):
        self.calls = []
        self.state = state
        self.volumes = set()

    def create_volume(self, tenant):
        self.calls.append(("create_volume", tenant))
        self.volumes.add(tenant)

    def remove_volume(self, tenant):
        self.calls.append(("remove_volume", tenant))
        self.volumes.discard(tenant)

    def stop_container(self, tenant):
        self.calls.append(("stop_container", tenant))

    def remove_container(self, tenant):
        self.calls.append(("remove_container", tenant))

    def container_state(self, tenant):
        return self.state

    def ensure_container(self, tenant):
        self.calls.append(("ensure_container", tenant))

    def ensure_fresh(self, tenant):
        return True


class TestTenantControl(unittest.TestCase):
    """control/tenants：开通/删除/改密码/配额/用量汇总（fake runtime）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mt-test-env-")
        self.env_file = Path(self._tmp.name) / ".env"
        self.env_file.write_text("OTHER=1\nGATEWAY_USERS=demo:demo123\n",
                                 encoding="utf-8")
        self.users = {"demo": "demo123"}
        self.runtime = FakeRuntime()
        self.control = control_mod.TenantControl(
            users=self.users, users_file=self.env_file,
            runtime=self.runtime, cpu="0.5", memory="512m")

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_tenant_with_password(self):
        result = self.control.create_tenant("new-one",
                                            password="InitPass123")
        self.assertEqual(result["tenant"], "new-one")
        self.assertEqual(result["password"], "InitPass123")
        self.assertEqual(result["volume"], "mt-data-new-one")
        self.assertEqual(result["container"], "mt-tenant-new-one")
        self.assertEqual(self.users["new-one"], "InitPass123")
        self.assertIn(("create_volume", "new-one"), self.runtime.calls)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("GATEWAY_USERS=demo:demo123,new-one:InitPass123", text)
        self.assertIn("OTHER=1", text)  # .env 其它行原样保留

    def test_create_tenant_generates_password(self):
        result = self.control.create_tenant("gen-one")
        self.assertTrue(result["password"])
        self.assertEqual(self.users["gen-one"], result["password"])
        self.assertNotIn(":", result["password"])
        self.assertNotIn(",", result["password"])

    def test_create_duplicate_rejected(self):
        with self.assertRaises(ValueError):
            self.control.create_tenant("demo", password="x123456")

    def test_create_invalid_tenant_rejected(self):
        with self.assertRaises(ValueError):
            self.control.create_tenant("bad/tenant")

    def test_delete_requires_explicit_confirm(self):
        with self.assertRaises(ValueError):
            self.control.delete_tenant("demo")
        self.assertIn("demo", self.users)
        self.assertNotIn(("remove_volume", "demo"), self.runtime.calls)

    def test_delete_with_confirm_stops_and_removes(self):
        result = self.control.delete_tenant("demo", confirm=True)
        self.assertEqual(result, {"tenant": "demo", "deleted": True})
        self.assertNotIn("demo", self.users)
        self.assertIn(("stop_container", "demo"), self.runtime.calls)
        self.assertIn(("remove_container", "demo"), self.runtime.calls)
        self.assertIn(("remove_volume", "demo"), self.runtime.calls)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertNotIn("demo:demo123", text)

    def test_change_password(self):
        result = self.control.change_password("demo", "newpass123")
        self.assertEqual(result, {"tenant": "demo", "changed": True})
        self.assertEqual(self.users["demo"], "newpass123")
        self.assertIn("demo:newpass123",
                      self.env_file.read_text(encoding="utf-8"))

    def test_change_password_unknown_tenant(self):
        with self.assertRaises(KeyError):
            self.control.change_password("ghost", "newpass123")

    def test_get_quota(self):
        self.runtime.state = "stopped"
        quota = self.control.get_quota("demo")
        self.assertEqual(quota, {"tenant": "demo", "cpu": "0.5",
                                 "memory": "512m",
                                 "container_state": "stopped"})

    def test_usage_summary_is_empty_pending_p3(self):
        # P2 先留接口：返回空统计，并在返回值里注明 P3 补齐。
        summary = self.control.usage_summary("demo")
        self.assertEqual(summary["model_calls"], 0)
        self.assertEqual(summary["tokens_total"], 0)
        self.assertIsNone(summary["cost_estimate"])
        self.assertEqual(summary["metering"], "not_available")
        self.assertIn("P3", summary["note"])


class TestAdminApi(unittest.TestCase):
    """control/tenants 管理 API：鉴权、开通/删除流程、缺省 token 拒绝服务。"""

    def _start(self, control, token):
        server = control_mod.start_admin_server(
            control, host="127.0.0.1", port=0, admin_token=token)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, server.server_address[1]

    def _req(self, port, method, path, token=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {}
        if token:
            headers["X-Admin-Token"] = token
        raw = None
        if body is not None:
            raw = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = None
        return resp.status, parsed

    def test_admin_requires_token(self):
        control = control_mod.TenantControl(
            users={"demo": "demo123"}, runtime=FakeRuntime())
        server, port = self._start(control, "admin-secret")
        try:
            status, body = self._req(port, "GET", "/admin/tenants")
            self.assertEqual(status, 401)
            self.assertEqual(body["error"]["code"], "unauthenticated")
            status, _ = self._req(port, "GET", "/admin/tenants",
                                  token="wrong")
            self.assertEqual(status, 401)
            status, body = self._req(port, "GET", "/admin/tenants",
                                     token="admin-secret")
            self.assertEqual(status, 200)
            self.assertEqual(body, {"tenants": ["demo"]})
        finally:
            server.shutdown()
            server.server_close()

    def test_admin_create_and_delete_flow(self):
        control = control_mod.TenantControl(
            users={"demo": "demo123"}, runtime=FakeRuntime())
        server, port = self._start(control, "admin-secret")
        try:
            status, body = self._req(
                port, "POST", "/admin/tenants",
                token="admin-secret",
                body={"tenant": "admin-made", "password": "Secret123"})
            self.assertEqual(status, 201)
            self.assertEqual(body["tenant"], "admin-made")
            self.assertEqual(body["password"], "Secret123")
            status, body = self._req(
                port, "DELETE", "/admin/tenants/admin-made",
                token="admin-secret", body={})
            self.assertEqual(status, 400)  # 没带 confirm=true，拒绝删除
            status, body = self._req(
                port, "DELETE", "/admin/tenants/admin-made?confirm=true",
                token="admin-secret")
            self.assertEqual(status, 200)
            self.assertTrue(body["deleted"])
            status, body = self._req(port, "GET", "/admin/tenants",
                                     token="admin-secret")
            self.assertEqual(body, {"tenants": ["demo"]})
        finally:
            server.shutdown()
            server.server_close()

    def test_admin_refuses_service_without_token(self):
        control = control_mod.TenantControl(
            users={"demo": "demo123"}, runtime=FakeRuntime())
        self.assertIsNone(control_mod.start_admin_server(
            control, host="127.0.0.1", port=0, admin_token=None))
        self.assertIsNone(control_mod.start_admin_server(
            control, host="127.0.0.1", port=0, admin_token=""))


if __name__ == "__main__":
    unittest.main()

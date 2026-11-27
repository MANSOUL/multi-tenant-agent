"""P4 浏览器隔离（integration，不用真 Docker、不用真浏览器）。

覆盖：租户 browser 容器命名/地址解析（infra/browser_service.py
的纯函数 + gateway/infra/runtime.py 的 docker 编排）、browse_page
先试租户容器、失败回退共享、截图路径只返回相对名、网关登录时保障
租户 browser 容器（失败不拦登录）、闲置回收时一起停 browser 容器、
删租户时一起删 browser 容器。
"""
import json
import os
import unittest
from unittest import mock
from urllib.error import URLError

from gateway.control import tenants as control_mod
from gateway.infra import runtime as runtime_mod
from infra import browser_service as browser_svc
from tools.definitions.browser import browse_page_impl

TENANT_URL = "http://mt-browser-demo:8001"
SHARED_URL = "http://browser:8001"
PAYLOAD = {"title": "Example", "text": "hello world",
           "screenshot_path": "browser-123.png"}
PAYLOAD_NO_SHOT = {"title": "Example", "text": "hello world",
                   "screenshot_path": ""}


class _FakeResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen_factory(tenant_behavior, shared_behavior):
    """按请求地址分流的 fake urlopen：behavior 是 payload dict
    或要抛的异常实例。"""
    calls = []

    def fake(req, timeout=60):
        calls.append(req.full_url)
        if req.full_url.startswith(TENANT_URL):
            behavior = tenant_behavior
        elif req.full_url.startswith(SHARED_URL):
            behavior = shared_behavior
        else:  # pragma: no cover - 测试里只调这两个地址
            raise AssertionError(f" unexpected url: {req.full_url}")
        if isinstance(behavior, Exception):
            raise behavior
        return _FakeResp(behavior)

    fake.calls = calls
    return fake


class TestBrowserAddressing(unittest.TestCase):
    """地址解析：租户容器名/地址、共享地址、resolve 决策。"""

    def test_tenant_browser_naming(self):
        self.assertEqual(browser_svc.browser_container_name("demo"),
                         "mt-browser-demo")
        self.assertEqual(browser_svc.browser_url_for_tenant("demo"),
                         TENANT_URL)
        self.assertEqual(runtime_mod.browser_container_name("demo"),
                         "mt-browser-demo")
        self.assertEqual(runtime_mod.browser_url("demo"), TENANT_URL)

    def test_shared_url_default_and_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BROWSER_URL", None)
            self.assertEqual(browser_svc.shared_browser_url(), SHARED_URL)
        with mock.patch.dict(os.environ,
                             {"BROWSER_URL": "http://127.0.0.1:8001/"}):
            self.assertEqual(browser_svc.shared_browser_url(),
                             "http://127.0.0.1:8001")

    def test_resolve_running_uses_tenant(self):
        url, via_tenant = runtime_mod.resolve_browser_url(
            "demo", state="running")
        self.assertEqual((url, via_tenant), (TENANT_URL, True))

    def test_resolve_missing_falls_back_to_shared(self):
        url, via_tenant = runtime_mod.resolve_browser_url(
            "demo", state="missing")
        self.assertEqual(url, SHARED_URL)
        self.assertFalse(via_tenant)

    def test_resolve_not_running_falls_back(self):
        # 容器明确不在跑：不硬试租户地址，直接走共享。
        url, via_tenant = browser_svc.resolve_browser_url(
            "demo", container_running=lambda name: False)
        self.assertEqual(url, SHARED_URL)
        self.assertFalse(via_tenant)

    def test_resolve_unknown_defers_to_trying_tenant(self):
        # 不知道容器状态时默认先认租户容器，让调用方试调、失败再回退。
        url, via_tenant = browser_svc.resolve_browser_url("demo")
        self.assertEqual((url, via_tenant), (TENANT_URL, True))


class TestBrowseFallback(unittest.TestCase):
    """browse_page：先租户、失败回退共享、截图只返回相对名。"""

    def test_prefers_tenant_browser(self):
        fake = _fake_urlopen_factory(PAYLOAD, PAYLOAD)
        result, via_tenant = browser_svc.browse_page(
            "demo", "https://example.com", True, urlopen=fake)
        self.assertTrue(via_tenant)
        self.assertEqual(result["title"], "Example")
        self.assertEqual(fake.calls, [TENANT_URL + "/browse"])

    def test_falls_back_to_shared_on_tenant_failure(self):
        fake = _fake_urlopen_factory(
            URLError("Name or service not known"), PAYLOAD)
        result, via_tenant = browser_svc.browse_page(
            "demo", "https://example.com", False, urlopen=fake)
        self.assertFalse(via_tenant)
        self.assertEqual(result["title"], "Example")
        self.assertEqual(fake.calls, [TENANT_URL + "/browse",
                                      SHARED_URL + "/browse"])

    def test_both_fail_reports_shared_error(self):
        fake = _fake_urlopen_factory(
            URLError("tenant down"), URLError("shared down"))
        with self.assertRaises(RuntimeError) as cm:
            browser_svc.browse_page("demo", "https://example.com", False,
                                    urlopen=fake)
        self.assertIn(SHARED_URL, str(cm.exception))

    def test_invalid_url_rejected_without_http(self):
        fake = _fake_urlopen_factory(PAYLOAD, PAYLOAD)
        out = browse_page_impl("demo", "ftp://example.com/x", False,
                               urlopen=fake)
        self.assertIn("只支持 http:// 或 https://", out)
        self.assertEqual(fake.calls, [])

    def test_screenshot_path_is_relative_name(self):
        fake = _fake_urlopen_factory(PAYLOAD, PAYLOAD)
        out = browse_page_impl("demo", "https://example.com", True,
                               urlopen=fake)
        self.assertIn("截图已保存：browser-123.png", out)
        # 只返回相对名，不带 /data、租户卷路径等绝对路径。
        self.assertNotIn("/data", out)
        self.assertNotIn("mt-data-demo", out)
        # 租户容器成功时不追加 fallback 说明。
        self.assertNotIn("共享浏览器", out)

    def test_fallback_screenshot_notes_not_in_tenant_volume(self):
        fake = _fake_urlopen_factory(
            URLError("tenant down"), PAYLOAD)
        out = browse_page_impl("demo", "https://example.com", True,
                               urlopen=fake)
        self.assertIn("截图已保存：browser-123.png", out)
        self.assertIn("共享浏览器", out)
        self.assertIn("不在租户卷里", out)


class TestBrowserRuntime(unittest.TestCase):
    """gateway/infra/runtime：租户 browser 容器的 docker 编排。"""

    def test_browser_run_argv_mounts_tenant_volume(self):
        argv = runtime_mod.browser_run_argv("demo")
        self.assertIn("mt-browser-demo", argv)
        self.assertIn("mt-data-demo:/data", argv)
        self.assertIn(runtime_mod.BROWSER_IMAGE, argv)
        # Chromium 在容器里跑要共享内存（和 compose 的 shm_size 一致）。
        self.assertIn("--shm-size", argv)
        self.assertNotIn("mt-browser-other", argv)

    def test_ensure_browser_missing_creates_and_waits_health(self):
        calls = []

        def fake_docker(argv, timeout=120):
            calls.append(list(argv))
            if argv[0] == "inspect":
                return (1, "", "no such container")
            if argv[0] == "run":
                return (0, "abc123", "")
            return (1, "", "unknown docker call")

        with (mock.patch.object(runtime_mod, "_docker", fake_docker),
              mock.patch.object(runtime_mod, "_http_ok",
                                lambda url, timeout=2: True)):
            self.assertTrue(runtime_mod.ensure_browser_container("demo"))
        run = [c for c in calls if c[0] == "run"]
        self.assertEqual(len(run), 1)
        self.assertIn("mt-browser-demo", run[0])

    def test_ensure_browser_run_failure_fails_open(self):
        def failing_docker(argv, timeout=120):
            return (1, "", "docker: command not found")

        with mock.patch.object(runtime_mod, "_docker", failing_docker):
            # 起不来返回 False，不抛异常（调用方走共享 fallback）。
            self.assertFalse(runtime_mod.ensure_browser_container("demo"))

    def test_ensure_browser_invalid_tenant_fails_open(self):
        with mock.patch.object(runtime_mod, "_docker") as docker:
            self.assertFalse(
                runtime_mod.ensure_browser_container("bad/tenant"))
            docker.assert_not_called()

    def test_reap_idle_stops_browser_container_too(self):
        calls = []

        def fake_docker(argv, timeout=120):
            calls.append(list(argv))
            return (0, "", "")

        runtime_mod.touch("demo", now=0.0)
        try:
            with mock.patch.object(runtime_mod, "_docker", fake_docker):
                stopped = runtime_mod.reap_idle(
                    now=31 * 60)  # 超过默认 30 分钟闲置
            self.assertEqual(stopped, ["demo"])
            stops = [c for c in calls if c[0] == "stop"]
            self.assertIn(["stop", "mt-tenant-demo"], stops)
            self.assertIn(["stop", "mt-browser-demo"], stops)
        finally:
            runtime_mod._last_active.pop("demo", None)

    def test_docker_runtime_adapter_has_browser_methods(self):
        rt = runtime_mod.DockerRuntime()
        self.assertTrue(callable(rt.ensure_browser_container))
        self.assertTrue(callable(rt.resolve_browser_url))
        self.assertTrue(callable(rt.remove_browser_container))
        self.assertTrue(callable(rt.browser_container_state))


class TestBrowserLifecycleInControl(unittest.TestCase):
    """删租户时一并删 browser 容器（control/tenants.py）。"""

    def test_delete_tenant_removes_browser_container(self):
        calls = []

        class _Rt:
            def container_state(self, tenant):
                return "running"

            def stop_container(self, tenant):
                calls.append(("stop_container", tenant))

            def remove_container(self, tenant):
                calls.append(("remove_container", tenant))

            def remove_browser_container(self, tenant):
                calls.append(("remove_browser_container", tenant))

            def remove_volume(self, tenant):
                calls.append(("remove_volume", tenant))

        control = control_mod.TenantControl(
            users={"demo": "demo123"}, runtime=_Rt())
        result = control.delete_tenant("demo", confirm=True)
        self.assertEqual(result, {"tenant": "demo", "deleted": True})
        self.assertIn(("remove_browser_container", "demo"), calls)
        # browser 容器在删卷之前删（截图在卷里，删卷才是最后一步）。
        self.assertLess(calls.index(("remove_browser_container", "demo")),
                        calls.index(("remove_volume", "demo")))

    def test_delete_tenant_without_browser_method_still_works(self):
        # 旧 fake runtime 没有 remove_browser_container：照样删得掉。
        calls = []

        class _OldRt:
            def container_state(self, tenant):
                return "running"

            def stop_container(self, tenant):
                calls.append(("stop_container", tenant))

            def remove_container(self, tenant):
                calls.append(("remove_container", tenant))

            def remove_volume(self, tenant):
                calls.append(("remove_volume", tenant))

        control = control_mod.TenantControl(
            users={"demo": "demo123"}, runtime=_OldRt())
        result = control.delete_tenant("demo", confirm=True)
        self.assertEqual(result, {"tenant": "demo", "deleted": True})
        self.assertIn(("remove_volume", "demo"), calls)


if __name__ == "__main__":
    unittest.main()

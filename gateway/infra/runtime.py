"""Infra 层（网关进程）：Docker 编排——租户容器起/停、闲置回收、镜像 digest 比对。

只用标准库 + subprocess 调 docker CLI，不引入新依赖。网关容器需要
挂载 /var/run/docker.sock 才能调宿主机 Docker（见 docker-compose.yml
和 README 安全提醒：拿到 docker.sock 就等于拿到宿主机 root，只能在
自己可信的单机上用）。

职责边界：本模块只懂 Docker（容器/卷/镜像），不懂登录、token、HTTP
路由——那些在 edge/。control/tenants.py 通过下面末尾的 DockerRuntime
适配器（薄对象包装，下面的模块级函数）调本模块，测试时可注入 fake。

P2 新增（2026-11-27）：镜像 digest 比对 ensure_fresh()——网关比对
租户容器所用镜像的 digest 与当前 agent 镜像的 digest，发现变了就
自动重建租户容器（docker rm -f + 让 ensure 逻辑按新镜像重建，数据在
卷里不丢），替代原来“每次部署手动删旧租户容器”的土办法。digest 查
不到（docker 不可用等）时 fail-open：只记日志，不拦登录。
"""
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

# --- 租户容器配置（见 .env.example）---
# compose 默认建的网络名就是 <项目目录名>_default；用 -p 改过项目名时改这里。
DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "multi-tenant-agent_default")
AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "multi-tenant-agent-agent-api:latest")
TENANT_CPU = os.environ.get("TENANT_CPU", "0.5")
TENANT_MEMORY = os.environ.get("TENANT_MEMORY", "512m")
IDLE_MINUTES = int(os.environ.get("TENANT_IDLE_MINUTES", "30") or 30)
# 容器启动后等 /api/health 通的最长秒数（首次 docker run 拉镜像不算，
# 镜像必须事先 build 好，见 README 快速开始）。
HEALTH_WAIT_SECONDS = int(os.environ.get("TENANT_HEALTH_WAIT", "60") or 60)

# 租户 id 只允许字母数字 _ - ，防止路径穿越、容器名注入和奇怪的租户名。
# 容器名/卷名/环境变量都由它拼出来，所以必须先校验再拼命令；docker 调用
# 全部用 argv 列表（不用 shell=True），租户 id 进不了 shell。
TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 转发给租户容器的环境变量：LLM_* 等从网关自己的环境继承（compose 里
# gateway 用了 env_file: .env，所以这些值都在）。只转白名单里的，
# GATEWAY_SECRET 也转过去，agent 容器靠它校验 token（TOKEN_SECRET 为空时
# 回退读 GATEWAY_SECRET，见 agent/config.py）。
FORWARD_ENVS = [
    "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
    "MAX_TOOL_ROUNDS", "CONTEXT_MAX_TURNS", "CONTEXT_MAX_CHARS",
    "SUMMARY_THRESHOLD_TURNS", "GATEWAY_SECRET", "TOKEN_SECRET",
    "SANDBOX_MODE", "SANDBOX_IMAGE", "BROWSER_URL",
]


def is_valid_tenant(tenant: str) -> bool:
    return bool(tenant and TENANT_RE.fullmatch(tenant))


def container_name(tenant: str) -> str:
    return f"mt-tenant-{tenant}"


def volume_name(tenant: str) -> str:
    return f"mt-data-{tenant}"


def agent_url(tenant: str) -> str:
    """租户容器的内网地址：容器名就是 Docker 网络里的 DNS 名。"""
    return f"http://{container_name(tenant)}:8081"


# ---------------------------------------------------------------------------
# 租户容器管理：docker CLI 全部走 subprocess argv 列表，不用 shell。
# _docker() 是唯一调 docker 的地方，测试时 monkeypatch 它就能验证
# ensure 的调用序列，不用真的起容器。
# ---------------------------------------------------------------------------

def _docker(argv, timeout=120):
    """跑一条 docker 命令。返回 (returncode, stdout, stderr)。"""
    try:
        r = subprocess.run(["docker"] + argv, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", f"docker 命令执行失败：{e}"


def _http_ok(url: str, timeout=2) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - 健康检查不通就是没就绪，不报错
        return False


def docker_run_argv(tenant: str) -> list:
    """创建租户容器的 docker run 参数（纯函数，方便测试）。

    - 专属 volume mt-data-<租户> 挂到 /app/data：SQLite 和 workspace 都在里面，
      容器删了数据还在；volume 不存在时 docker 会自动建。
    - 资源上限 --cpus / --memory（TENANT_CPU / TENANT_MEMORY 可配）。
    - 沙箱还要起每会话容器，所以也挂 /var/run/docker.sock（只读），
      见 README 安全提醒；SANDBOX_VOLUME 告诉沙箱直接挂卷而不是挂宿主机路径。
    """
    argv = [
        "run", "-d",
        "--name", container_name(tenant),
        "--network", DOCKER_NETWORK,
        "--restart", "unless-stopped",
        "--cpus", TENANT_CPU,
        "--memory", TENANT_MEMORY,
        "-v", f"{volume_name(tenant)}:/app/data",
        "-v", "/var/run/docker.sock:/var/run/docker.sock:ro",
        "-e", "PORT=8081",
        "-e", "DATA_DIR=/app/data",
        "-e", f"TENANT_ID={tenant}",
        "-e", f"SANDBOX_VOLUME={volume_name(tenant)}",
    ]
    for key in FORWARD_ENVS:
        if key in os.environ:
            argv += ["-e", f"{key}={os.environ[key]}"]
    argv.append(AGENT_IMAGE)
    return argv


def container_state(tenant: str) -> str:
    """租户容器状态：running / stopped / missing。"""
    rc, out, _ = _docker(
        ["inspect", "-f", "{{.State.Running}}", container_name(tenant)],
        timeout=30)
    if rc != 0:
        return "missing"
    return "running" if out.strip() == "true" else "stopped"


# 最后活跃时间：{租户: 时间戳}，每次代理成功后更新；回收线程只看这里。
_last_active: dict = {}
_last_active_lock = threading.Lock()


def touch(tenant: str, now: float | None = None) -> None:
    with _last_active_lock:
        _last_active[tenant] = now if now is not None else time.time()


def should_stop(last_active_ts: float, idle_minutes: int,
                now: float | None = None) -> bool:
    """闲置判断（纯函数，方便测试）：超过 idle 分钟没活跃就该停。"""
    now = now if now is not None else time.time()
    return (now - last_active_ts) > idle_minutes * 60


def ensure_tenant_container(tenant: str) -> None:
    """保证租户容器在运行且 /api/health 已通，否则抛 RuntimeError。

    不存在 → docker run 创建；已停止 → docker start；已在跑 →
    直接等健康检查。启动后轮询 /api/health 直到就绪或超时。
    """
    if not is_valid_tenant(tenant):
        raise RuntimeError(f"租户 id 不合法：{tenant!r}")
    state = container_state(tenant)
    if state == "missing":
        rc, _out, err = _docker(docker_run_argv(tenant), timeout=180)
        if rc != 0:
            raise RuntimeError(f"创建租户容器失败：{err or 'docker run 失败'}")
    elif state == "stopped":
        rc, _out, err = _docker(["start", container_name(tenant)], timeout=60)
        if rc != 0:
            raise RuntimeError(f"启动租户容器失败：{err or 'docker start 失败'}")
    deadline = time.time() + HEALTH_WAIT_SECONDS
    while time.time() < deadline:
        if _http_ok(agent_url(tenant) + "/api/health"):
            touch(tenant)
            return
        time.sleep(1)
    raise RuntimeError(
        f"租户容器 {container_name(tenant)} 启动超时，"
        f"/api/health 在 {HEALTH_WAIT_SECONDS} 秒内一直不通")


def reap_idle(now: float | None = None) -> list:
    """停掉所有超过闲置时长的租户容器（只 stop 不 rm，volume 保留）。

    返回这次停掉的租户列表。只停 _last_active 里记过的租户——
    也就是网关经手过的；网关重启后丢了记录的容器不会被误停，
    下次该租户登录/聊天时 ensure 会重新接管。
    """
    now = now if now is not None else time.time()
    stopped = []
    with _last_active_lock:
        candidates = list(_last_active.items())
    for tenant, ts in candidates:
        if should_stop(ts, IDLE_MINUTES, now):
            _docker(["stop", container_name(tenant)], timeout=60)
            with _last_active_lock:
                _last_active.pop(tenant, None)
            stopped.append(tenant)
    return stopped


def _reaper_loop() -> None:
    while True:
        time.sleep(60)
        try:
            reap_idle()
        except Exception:  # noqa: BLE001 - 回收失败不能把网关拖垮
            pass


# ---------------------------------------------------------------------------
# 镜像 digest 比对（P2）：部署后租户容器自动重建，不再靠人手动删容器。
# 只比对 digest 字符串，不拉镜像、不 build；轻量，可在每次登录前调一次。
# ---------------------------------------------------------------------------

def image_digest(image: str | None = None) -> str | None:
    """当前 agent 镜像的 digest（docker image inspect 的 .Id，如 sha256:...）。

    查不到（docker 不可用、镜像不存在等）返回 None，调用方按 fail-open 处理。
    用 .Id 而不用 RepoDigests：本地 build 的镜像没有推过 registry 时
    RepoDigests 为空，.Id 才是每次 build 都变的可靠标识。
    """
    rc, out, _ = _docker(
        ["image", "inspect", "-f", "{{.Id}}", image or AGENT_IMAGE],
        timeout=30)
    return out.strip() if rc == 0 and out.strip() else None


def container_image_digest(tenant: str) -> str | None:
    """租户容器当前用的镜像 digest（docker inspect 的 .Image）。

    容器不存在或查不到时返回 None——调用方不把它当成“镜像变了”，
    交给 ensure_tenant_container 按新镜像创建即可。
    """
    rc, out, _ = _docker(
        ["inspect", "-f", "{{.Image}}", container_name(tenant)],
        timeout=30)
    return out.strip() if rc == 0 and out.strip() else None


def rebuild_tenant_container(tenant: str) -> bool:
    """删掉旧租户容器（docker rm -f），让 ensure 逻辑按新镜像重建。

    只删容器不删卷：数据在 mt-data-<租户> 卷里，不丢。返回是否删成功。
    """
    rc, _, err = _docker(["rm", "-f", container_name(tenant)], timeout=60)
    if rc != 0:
        logger.warning("重建租户容器失败（rm -f）：tenant=%s err=%s",
                       tenant, err)
        return False
    return True


def ensure_fresh(tenant: str) -> bool:
    """比对租户容器镜像 digest 与当前 agent 镜像 digest，变了就自动重建。

    返回 True 表示容器已是最新镜像（或已重建、或容器尚不存在，等
    ensure_tenant_container 创建）；返回 False 表示比对没做成
    （docker 不可用、查不到 digest 等）——调用方 fail-open 走老路，
    不拦登录，只记日志。任何异常都不向外抛。
    """
    try:
        if not is_valid_tenant(tenant):
            return False
        current = image_digest()
        if not current:
            logger.warning("digest 比对跳过：查不到当前镜像 %s 的 digest，"
                           "fail-open 走老路", AGENT_IMAGE)
            return False
        used = container_image_digest(tenant)
        if used is None:
            # 容器不存在（ensure 会按新镜像建）或查不到：不重建。
            return True
        if used == current:
            return True
        logger.info("租户容器镜像已变，自动重建：tenant=%s old=%s new=%s",
                    tenant, used, current)
        return rebuild_tenant_container(tenant)
    except Exception:  # noqa: BLE001 - digest 比对绝不能拦登录
        logger.warning("digest 比对异常，fail-open 走老路：tenant=%s",
                       tenant, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# 卷 / 容器生命周期操作（供 control/tenants.py 用；本模块仍是唯一调
# docker 的地方，control 只调这里，不自己拼 docker 命令）。
# ---------------------------------------------------------------------------

def create_volume(tenant: str) -> None:
    """建租户专属卷 mt-data-<租户>（已存在时 docker volume create 是幂等的）。"""
    rc, _, err = _docker(["volume", "create", volume_name(tenant)],
                         timeout=60)
    if rc != 0:
        raise RuntimeError(f"创建租户卷失败：{err or 'docker volume create 失败'}")


def remove_volume(tenant: str) -> None:
    """删租户专属卷（删卷 = 删该租户全部数据，调用方必须先拿到显式确认）。"""
    rc, _, err = _docker(["volume", "rm", volume_name(tenant)], timeout=60)
    if rc != 0:
        raise RuntimeError(f"删除租户卷失败：{err or 'docker volume rm 失败'}")


def stop_container(tenant: str) -> None:
    """停租户容器（只停不停删，卷保留）。容器已停/不存在时 docker stop
    也可能返回非 0，调用方按需忽略——这里如实抛 RuntimeError。"""
    rc, _, err = _docker(["stop", container_name(tenant)], timeout=60)
    if rc != 0:
        raise RuntimeError(f"停止租户容器失败：{err or 'docker stop 失败'}")


def remove_container(tenant: str) -> None:
    """删租户容器（docker rm -f；只删容器不删卷）。"""
    rc, _, err = _docker(["rm", "-f", container_name(tenant)], timeout=60)
    if rc != 0:
        raise RuntimeError(f"删除租户容器失败：{err or 'docker rm 失败'}")


class DockerRuntime:
    """docker CLI 的薄对象适配器：把上面的模块级函数包成对象。

    唯一目的：让 control/tenants.py 能注入一个 runtime 接口（默认用
    这个真实现，测试时换 fake），而不用在 control 里直接写 subprocess
    调用。本类不含任何新逻辑，所有方法都直调同名模块级函数。
    """

    def create_volume(self, tenant: str) -> None:
        create_volume(tenant)

    def remove_volume(self, tenant: str) -> None:
        remove_volume(tenant)

    def stop_container(self, tenant: str) -> None:
        stop_container(tenant)

    def remove_container(self, tenant: str) -> None:
        remove_container(tenant)

    def container_state(self, tenant: str) -> str:
        return container_state(tenant)

    def ensure_container(self, tenant: str) -> None:
        ensure_tenant_container(tenant)

    def ensure_fresh(self, tenant: str) -> bool:
        return ensure_fresh(tenant)

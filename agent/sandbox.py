"""每会话沙箱：把白名单命令放进隔离容器里跑。

两种模式（环境变量 SANDBOX_MODE）：
  docker：docker run --rm --read-only --network none --cpus 0.5 --memory 256m，
          只挂载该租户的工作区（读写），用完即销毁。需要宿主机有 Docker，
          且 agent-api 容器挂载了 /var/run/docker.sock。
  local ：开发回退。没有 Docker 时在 agent 本地跑白名单命令（仍限工作区，
          但和 agent 同进程、未隔离），输出会明确标注，仅开发调试用。

注意：沙箱镜像（如 python:3.12-slim）里不一定有 git，git status 在沙箱里
跑不通时会返回命令自身的报错，这是预期行为，不是 bug。
"""
import os
import shutil
import subprocess

SANDBOX_MODE = os.environ.get("SANDBOX_MODE", "docker")
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "python:3.12-slim")

# 白名单：只读命令，不用 shell=True 解析，管道/重定向/分号都进不来。
# rm 等危险命令不在这里，永远跑不了。
ALLOWED_COMMANDS = {
    "ls": ["ls", "-la"],
    "pwd": ["pwd"],
    "date": ["date"],
    "df": ["df", "-h"],
    "free": ["free", "-h"],
    "uptime": ["uptime"],
    "whoami": ["whoami"],
    "git": ["git", "status", "--short", "--branch"],
}


def _docker_available() -> bool:
    return shutil.which("docker") is not None and (
        os.path.exists("/var/run/docker.sock")
        or os.environ.get("DOCKER_HOST", "")
    )


def run_command(command: str, workspace: str) -> tuple[str, str]:
    """跑一条白名单命令。返回 (输出, 模式)，模式是 docker / local / rejected。"""
    if command not in ALLOWED_COMMANDS:
        return (
            f"命令不在白名单内，可用的有：{', '.join(sorted(ALLOWED_COMMANDS))}",
            "rejected",
        )
    argv = ALLOWED_COMMANDS[command]

    if SANDBOX_MODE == "docker" and _docker_available():
        # 每会话一个短命容器：只读根文件系统、无网络、禁 privileged、
        # CPU 0.5 核 / 内存 256MB 上限，只挂该租户的工作区。
        docker_cmd = [
            "docker", "run", "--rm",
            "--read-only",
            "--network", "none",
            "--cpus", "0.5",
            "--memory", "256m",
            "--cap-drop", "ALL",
            "-v", f"{workspace}:/workspace:rw",
            "-w", "/workspace",
            SANDBOX_IMAGE,
        ] + argv
        try:
            out = subprocess.run(
                docker_cmd, capture_output=True, text=True, timeout=15
            )
            return (
                (out.stdout or out.stderr or "(无输出)").strip()[:4000],
                "docker",
            )
        except Exception as e:  # noqa: BLE001 - 沙箱起不来就回退并说清楚
            return (f"沙箱容器启动失败（{e}），改用本地回退：\n"
                    + _run_local(argv), "local")

    # 本地回退：没有 Docker 时的开发模式，未隔离，输出里会标注。
    return (_run_local(argv), "local")


def _run_local(argv: list) -> str:
    out = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    return (out.stdout or out.stderr or "(无输出)").strip()[:4000]

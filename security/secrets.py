"""密钥管理（安全横切）：DeepSeek key 只许从环境变量读，只在
model/ 层出现。

规则：
- 读 key 的代码只许在 model/client.py（get_api_key / create_client）；
  其它层要判断“有没有 key”只调 model.client.has_api_key()，
  拿不到 key 本身。
- assert_key_isolation() 是启动时检查：grep 级别的自查，扫
  agent/、gateway/、tools/、app/、loop/、session/ 六个目录的
  .py 文件，看有没有出现 key 的名字。出现就是违规（本文件自己
  除外——检查函数本身必须写出这个名字才能查它）。
- 例外（不是违规，是设计如此）：gateway/infra/runtime.py 的
  FORWARD_ENVS 白名单——网关把环境变量的“名字”转发给租户容器
  （docker run -e），让租户容器里的 model 层能读到 key；网关
  代码本身不读 key 的值。assert_key_isolation() 会把这一处
  单独列成“转发白名单”，不算违规，但汇报里要说清楚。
"""
import re
from pathlib import Path

# key 的环境变量名。注意：这个字符串只许出现在这里（检查函数本身）
# 和 model/client.py（真正读它的地方）；其它目录出现就是违规，
# 见 assert_key_isolation。
KEY_ENV_NAME = "LLM_API_KEY"

# 扫哪些目录（相对项目根目录）。
_SCANNED_DIRS = ("agent", "gateway", "tools", "app", "loop", "session")

# 允许出现的非读取位置：(相对路径, 说明)。目前只有网关转发白名单
# 这一处——它只转发环境变量名，不读值。
_ALLOWED_NON_READS = {
    "gateway/infra/runtime.py": "FORWARD_ENVS 转发环境变量名给租户容器，不读值",
}


def _project_root(start: Path | None = None) -> Path:
    """项目根目录：从本文件往上找（security/ 的父目录就是根目录）。"""
    return (start or Path(__file__).resolve()).parent.parent


def assert_key_isolation(root=None) -> list:
    """自查密钥隔离：扫六个目录的 .py 文件，找 key 名字的出现。

    返回违规列表 [{"file": 相对路径, "line": 行号, "text": 该行}]；
    没有违规返回 []。调用方（启动时）拿返回值非空就报错——
    本函数自己不 raise，方便测试断言。
    本文件（security/secrets.py）和 gateway/infra/runtime.py 的
    转发白名单不算违规。
    """
    root = Path(root) if root is not None else _project_root()
    violations = []
    for dirname in _SCANNED_DIRS:
        d = root / dirname
        if not d.is_dir():
            continue
        for path in sorted(d.rglob("*.py")):
            rel = str(path.relative_to(root))
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                if KEY_ENV_NAME not in line:
                    continue
                if rel in _ALLOWED_NON_READS:
                    continue
                violations.append({"file": rel, "line": i,
                                   "text": line.strip()})
    return violations

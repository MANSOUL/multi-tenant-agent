"""测试共用工具：临时租户数据目录、测试 token 签发、提醒行读取。

数据隔离规则：每个测试用 fresh_tenant_data() 拿到一个全新的临时
DATA_DIR（patch agent.tenant.DATA_DIR 和 agent.config.DATA_DIR，
tenant_db / tenant_dirs 都走这两个），跑完自动删掉，绝不碰项目 data/。
唯一的例外是调度器线程测试（见 test_reminder_tools.py 的说明），
它用包默认的临时 DATA_DIR，因为后台线程在测试方法返回后还在跑。
"""
import hashlib
import hmac
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest import mock

TEST_SECRET = "test-secret-do-not-use"


def make_token(tenant: str, secret: str = TEST_SECRET, exp: int | None = None) -> str:
    """签一个 agent 能认的测试 token（tenant.过期时间.hmac），和网关同算法。"""
    if exp is None:
        exp = int(datetime.now().timestamp()) + 3600
    sig = hmac.new(secret.encode(), f"{tenant}.{exp}".encode(),
                   hashlib.sha256).hexdigest()
    return f"{tenant}.{exp}.{sig}"


@contextmanager
def fresh_tenant_data():
    """临时 DATA_DIR 上下文：进出时 patch 掉 tenant/config 的 DATA_DIR。"""
    import agent.config as config_mod
    import agent.tenant as tenant_mod
    with tempfile.TemporaryDirectory(prefix="mt-test-data-") as d:
        p = Path(d)
        with (mock.patch.object(tenant_mod, "DATA_DIR", p),
              mock.patch.object(config_mod, "DATA_DIR", p)):
            yield p


def reminder_rows(tenant: str):
    """读某租户 reminders 表全部行：(id, text, due_at, repeat, status)。"""
    from agent.tenant import tenant_db
    with tenant_db(tenant) as conn:
        return conn.execute(
            "SELECT id, text, due_at, repeat, status FROM reminders"
            " ORDER BY id").fetchall()


def hm_of(due_at: str):
    """due_at ISO 串 → (日期, 时, 分)，跨午夜比较时只比这三项，不比秒。"""
    dt = datetime.fromisoformat(due_at)
    return dt.date(), dt.hour, dt.minute

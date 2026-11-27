"""基础设施层：LangGraph checkpointer——图内原生多轮消息状态（P4）。

图以前是无状态的：每次请求现建图，多轮记忆全靠“历史拼进系统
提示”（context/assembler 从 chat_log 派生摘要 + 最近对话）。
现在给图挂一个 SqliteSaver（langgraph-checkpoint-sqlite）：
- 以会话 id 为 thread_id，同一会话连续两轮对话时，第二轮的
  state["messages"] 里自带第一轮的消息（含工具调用和工具结果），
  模型不用再靠提示词里的历史摘要去猜上一轮发生了什么；
- checkpointer 只负责图内多轮消息状态：摘要 + 长期事实的派生
  上下文还在（P0 的 event_log / chat_log 不动，仍是事实来源），
  澄清捷径、Turn 记录、计量也不走这里；
- 每个租户一个 checkpoint.db 文件，和 memory.db 分开（路径见
  agent/tenant.py 的 checkpoint_db_path），避免互相干扰；
- 每次 get_checkpointer 都新开一个 sqlite3 连接（不跨会话复用
  同一个连接）：ThreadingHTTPServer 下两个会话并发写同一文件时，
  SQLite 靠 busy timeout 串行化，比共享一个连接安全；
  连接 check_same_thread=False、timeout=30 秒；
- CHECKPOINTER_ENABLED=0 可以关掉（关掉后 build_graph 回到
  无 checkpointer 的老行为）；依赖缺失时同样 fail-open 返回 None，
  绝不拦聊天。
"""
import os
import sqlite3

try:
    from langgraph.checkpoint.sqlite import SqliteSaver
except ImportError:  # pragma: no cover - 依赖缺失时 fail-open
    SqliteSaver = None


def is_enabled() -> bool:
    """checkpointer 开关：CHECKPOINTER_ENABLED=0 关掉，其它都开。"""
    return os.environ.get("CHECKPOINTER_ENABLED", "1") != "0"


def get_checkpointer(db_path):
    """给这个租户建一个 SqliteSaver（新开一个连接）。

    未启用或 langgraph-checkpoint-sqlite 没装时返回 None，
    调用方按无 checkpointer 的老路走。db_path 不存在时 sqlite3
    会建出来（父目录由 agent/tenant.checkpoint_db_path 保证）。
    """
    if not is_enabled() or SqliteSaver is None:
        return None
    conn = sqlite3.connect(str(db_path), check_same_thread=False,
                           timeout=30.0)
    return SqliteSaver(conn)


def thread_config(thread_id: str) -> dict:
    """一次图调用的 config：thread_id 就是会话 id。"""
    return {"configurable": {"thread_id": thread_id}}


def close_checkpointer(checkpointer) -> None:
    """关掉一个 get_checkpointer 建出来的 saver 底下的连接。

    图实例自己持有的 saver 不用调这个（图缓存丢掉时连接随对象回收）；
    只给“临时开一个 saver 读个快照就走”的调用方（chat_service 读
    本轮之前的消息条数）用，免得每次聊天漏一个连接。
    """
    if checkpointer is None:
        return
    try:
        conn = getattr(checkpointer, "conn", None)
        if conn is not None:
            conn.close()
    except Exception:  # noqa: BLE001 - 关连接失败不阻塞聊天
        pass


def get_message_count(checkpointer, thread_id: str) -> int:
    """该会话在 checkpointer 里已存的消息条数（没有返回 0）。

    chat_service 在 invoke 之前读这个数，用来给 collect_events
    切片：只把本轮新产生的工具调用事件推给前端，不然第二轮会把
    第一轮的工具调用事件再推一遍。读不到 / 结构不对时返回 0
    （退化成老行为：全量事件），不抛异常。
    """
    if checkpointer is None:
        return 0
    try:
        tup = checkpointer.get_tuple(thread_config(thread_id))
        if tup is None:
            return 0
        messages = (tup.checkpoint.get("channel_values") or {}).get(
            "messages") or []
        return len(messages)
    except Exception:  # noqa: BLE001 - 读快照失败不阻塞聊天
        return 0

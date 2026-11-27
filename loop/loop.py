"""循环驱动（循环控制层）：调模型 → 执行工具 → 再调模型，
直到答完/到上限。

两条路，共用同一份停止策略（AgentLoopConfig）：
1. decide_next(state, config)：给 LangGraph 用的纯路由函数——
   原来 agent/graph.py 的 route()，原样搬过来（有工具调用且没到
   上限 → tools；有工具调用但已到上限 → final_llm；否则 →
   save_memory）。infra/graph.py 只做接线，判断全在这里。
2. AgentLoop：不依赖 LangGraph 的纯驱动（消息用 plain dict，
   不 import langchain——循环层只认 config 和钩子），给单测和
   以后换引擎用；生产路径仍走 LangGraph 图（见 run_graph）。
3. run_graph(graph, initial_state, config)：调一张编译好的
   LangGraph 图，recursion_limit 和超时都走 config；超时时用
   daemon 线程跑图，超时直接报 TimeoutError，不挂死调用方
   （跑飞的那条线程是 daemon 的，不会拖住进程退出）。

重试只在 AgentLoop 里做（只重试模型调用本身）；run_graph 不重试
整图——整图重跑会把已经执行过的工具再跑一遍。
"""
import queue
import threading
import time
from typing import Any

from .config import AgentLoopConfig
from .hooks import LoopHooks


def decide_next(state: dict, config: AgentLoopConfig) -> str:
    """llm 节点之后的分支（纯函数）：有工具调用且没到上限 → tools；
    有工具调用但已到上限 → final_llm；否则 → save_memory。
    state 只要有 messages（最后一条带 tool_calls）和 steps 两个字段。
    """
    messages = state.get("messages") or []
    if not messages:
        return "save_memory"
    last = messages[-1]
    tool_calls = last.get("tool_calls") if isinstance(last, dict) \
        else getattr(last, "tool_calls", None)
    if tool_calls:
        return ("tools" if state.get("steps", 0) < config.max_tool_rounds
                else "final_llm")
    return "save_memory"


def _call_with_timeout(fn, timeout_seconds: float):
    """在 daemon 线程里跑 fn，超时报 TimeoutError。

    超时后那条线程还在跑（停不掉），但它是 daemon 的，不会拖住
    进程退出；调用方拿到 TimeoutError 就能继续，不挂死。
    """
    q: queue.Queue = queue.Queue(maxsize=1)

    def _target():
        try:
            q.put(("ok", fn()))
        except Exception as e:  # noqa: BLE001 - 原样带回调用方
            q.put(("error", e))

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    try:
        kind, value = q.get(timeout=timeout_seconds)
    except queue.Empty:
        raise TimeoutError(f"调用超时（{timeout_seconds} 秒）")
    if kind == "error":
        raise value
    return value


def run_graph(graph, initial_state: dict, config: AgentLoopConfig):
    """调一张编译好的 LangGraph 图：recursion_limit 走 config，
    整轮超时走 config.timeout_seconds。"""
    return _call_with_timeout(
        lambda: graph.invoke(
            initial_state, config={"recursion_limit": config.recursion_limit}),
        config.timeout_seconds)


class AgentLoop:
    """纯循环驱动（零 I/O，不认识任何具体工具）。

    model_fn(messages) -> dict：调一次模型，返回 {"content": str,
      "tool_calls": [{"name": ..., "args": {...}, "id": ...}]}（没工具
    调用时 tool_calls 为空列表）。
    tool_fn(name, args) -> str：执行一个工具调用，返回字符串结果。
    每轮：beforeLLM → 调模型（失败按 config 重试）→ afterLLM →
    没工具调用就结束；有工具调用且没到上限就逐个执行
    （beforeToolCall 可拦截、afterToolCall 可改写结果）→ 再调模型；
    到上限就强制收尾（最后一次调模型时不再接受工具调用的结果，
    直接返回那次的文字）。
    """

    def __init__(self, config: AgentLoopConfig | None = None,
                 hooks: LoopHooks | None = None,
                 model_fn=None, tool_fn=None):
        self.config = config or AgentLoopConfig()
        self.hooks = hooks or LoopHooks()
        self.model_fn = model_fn
        self.tool_fn = tool_fn

    def _call_model(self, messages) -> dict:
        """调一次模型：超时用 config.timeout_seconds；失败按
        config.retry_attempts 重试（只重试模型调用本身）。"""
        attempts = 0
        while True:
            try:
                return _call_with_timeout(
                    lambda: self.model_fn(messages),
                    self.config.timeout_seconds)
            except TimeoutError:
                raise
            except Exception:
                attempts += 1
                if attempts > self.config.retry_attempts:
                    raise
                if self.config.retry_backoff_seconds > 0:
                    time.sleep(self.config.retry_backoff_seconds)

    def run(self, messages) -> dict:
        """跑完一轮对话。返回 {"messages": [...], "rounds": 调了几轮模型,
        "stopped": "done"/"max_rounds"/"timeout", "reply": 最后的文字}。"""
        config = self.config
        hooks = self.hooks
        history = [dict(m) for m in (messages or [])]
        rounds = 0
        try:
            while True:
                hooks.fire_before_llm(history)
                reply = self._call_model(history)
                reply = hooks.fire_after_llm(reply)
                rounds += 1
                history.append(dict(reply))
                tool_calls = reply.get("tool_calls") or []
                if not tool_calls:
                    hooks.fire_on_stop("done", history)
                    return {"messages": history, "rounds": rounds,
                            "stopped": "done",
                            "reply": reply.get("content", "")}
                if rounds >= config.max_tool_rounds:
                    # 轮数到上限：强制收尾，再调一次模型但不再执行工具。
                    hooks.fire_before_llm(history)
                    final = self._call_model(history)
                    final = hooks.fire_after_llm(final)
                    rounds += 1
                    history.append(dict(final))
                    hooks.fire_on_stop("max_rounds", history)
                    return {"messages": history, "rounds": rounds,
                            "stopped": "max_rounds",
                            "reply": final.get("content", "")}
                for tc in tool_calls:
                    name = tc.get("name", "")
                    args = tc.get("args") or {}
                    intercepted = hooks.fire_before_tool_call(name, args)
                    if intercepted is not None:
                        result = intercepted
                    elif self.tool_fn is None:
                        result = f"没有可用的工具执行器：{name}"
                    else:
                        result = self.tool_fn(name, args)
                    result = hooks.fire_after_tool_call(name, args, result)
                    history.append({"role": "tool", "name": name,
                                    "tool_call_id": tc.get("id", ""),
                                    "content": result})
        except TimeoutError:
            hooks.fire_on_stop("timeout", history)
            raise

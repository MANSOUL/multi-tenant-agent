"""循环钩子（循环控制层）：beforeToolCall / afterToolCall /
beforeLLM / afterLLM / onStop。

照 pi-agent 的 AgentLoopConfig：循环本身不知道任何具体工具，
钩子是循环和外面世界的唯一接触点。默认全部空实现（什么都不做，
不拦截、不改写）；要用的地方传一个带同名可调用属性的对象，
或子类化 LoopHooks 重写对应方法。

钩子约定：
- before_llm(messages)：调模型前，返回 None（不改消息）。
- after_llm(reply)：调模型后，返回 None（不改回复）或新回复。
- before_tool_call(name, args)：执行工具前，返回 None（正常执行）
  或一个字符串（拦截：不执行工具，直接拿这个字符串当工具结果）。
- after_tool_call(name, args, result)：执行工具后，返回 None
  （不改结果）或新字符串（改写工具结果）。
- on_stop(reason, messages)：循环结束时，reason 是 done（答完）/
  max_rounds（轮数到上限强制收尾）/ timeout（超时）/ error（异常）。
钩子抛异常时循环不吞：直接往上抛，调用方看得到（钩子是观测点，
不是兜底）。
"""
from typing import Any


class LoopHooks:
    """钩子点容器：五个钩子全是可选的，没传就是空实现。"""

    def __init__(self, before_llm=None, after_llm=None,
                 before_tool_call=None, after_tool_call=None,
                 on_stop=None):
        self.before_llm = before_llm
        self.after_llm = after_llm
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.on_stop = on_stop

    # -- 触发点：没传钩子就是空实现 --------------------------------------
    def fire_before_llm(self, messages) -> None:
        if self.before_llm is not None:
            self.before_llm(messages)

    def fire_after_llm(self, reply) -> Any:
        if self.after_llm is not None:
            rewritten = self.after_llm(reply)
            return reply if rewritten is None else rewritten
        return reply

    def fire_before_tool_call(self, name: str, args: dict) -> str | None:
        if self.before_tool_call is not None:
            return self.before_tool_call(name, args)
        return None

    def fire_after_tool_call(self, name: str, args: dict,
                             result: str) -> str:
        if self.after_tool_call is not None:
            rewritten = self.after_tool_call(name, args, result)
            return result if rewritten is None else rewritten
        return result

    def fire_on_stop(self, reason: str, messages) -> None:
        if self.on_stop is not None:
            self.on_stop(reason, messages)

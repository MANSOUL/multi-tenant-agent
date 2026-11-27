"""模型客户端（P0：只做封装；P3：密钥隔离；P6：换成 ChatDeepSeek）。

原来 graph.py 里直接 `ChatOpenAI(model=..., api_key=...,
base_url=..., temperature=0.7)` 再 `.invoke(...)`；现在创建和调用
都收进这里，graph.py 只调这两个入口。
不加路由（不选模型）、不加降级——那些是 P3 / 以后的事。

P6：从 ChatOpenAI 换成 langchain-deepseek 的 ChatDeepSeek——
同属 BaseChatOpenAI，构造参数、bind_tools、invoke/stream 都一样，
但它会把 deepseek-reasoner 的 reasoning_content 原样放进
additional_kwargs（ChatOpenAI 1.8.5 的 chunk 转换直接丢掉它，
流式思考过程拿不到推理文本）；同时打开 stream_usage，让流的
最后一个 chunk 带上真实用量（见 model/metering.extract_usage）。

密钥隔离（P3）：DeepSeek key 只许在这一层出现、只从环境变量读。
agent/config.py 不再读它（之前 API_KEY 是从 config 转手过来的，
那等于 key 在 agent 层也出现了一份）；其它层要判断“有没有 key”
只调 has_api_key()，拿不到 key 本身。
"""
import os

try:
    from agent.config import BASE_URL, MODEL
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时，项目根目录不在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.config import BASE_URL, MODEL

from langchain_deepseek import ChatDeepSeek

# 密钥只在这里读环境变量：其它模块一律不许出现这个名字
#（security/secrets.py 的自查函数 assert_key_isolation 会 grep 全仓，
# 除了它自己和网关往租户容器转发环境变量名的白名单外不许出现）。
API_KEY = os.environ.get("LLM_API_KEY", "")


def get_api_key() -> str:
    """返回模型 key（只供本层 create_client 用；其它层不要调）。"""
    return API_KEY


def has_api_key() -> bool:
    """有没有配 key：其它层只问这一句，拿不到 key 本身。"""
    return bool(API_KEY)


def create_client(streaming: bool = True, stream_usage: bool = True):
    """建一个模型客户端：配置（模型/key/地址/temperature）原样照搬 graph.py。

    streaming=True 时客户端内部按 token 流式拉取：invoke() 仍返回
    完整消息（行为不变），但图以 stream_mode="messages" 跑时，
    LangGraph 能通过回调拿到逐 token 的增量——流式聊天（/api/chat/stream）
    就靠它。stream_usage=True 时流的最后一个 chunk 带真实用量，
    不开它流式就拿不到 usage（非流式 invoke 本来就有 usage_metadata）。
    图里统一建这个客户端，流式和非流式两条路共用同一张图。
    """
    return ChatDeepSeek(
        model=MODEL, api_key=API_KEY, base_url=BASE_URL, temperature=0.7,
        streaming=streaming, stream_usage=stream_usage,
    )


def invoke(client, messages):
    """调一次模型（client 可以是绑过工具的，也可以是没绑的）。"""
    return client.invoke(messages)

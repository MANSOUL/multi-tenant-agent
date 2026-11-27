"""模型客户端（P0：只做封装）。

原来 graph.py 里直接 `ChatOpenAI(model=..., api_key=...,
base_url=..., temperature=0.7)` 再 `.invoke(...)`；现在创建和调用
都收进这里，graph.py 只调这两个入口。
不加路由（不选模型）、不加计量、不加降级——那些是 P3 / 以后的事。
"""
try:
    from agent.config import API_KEY, BASE_URL, MODEL
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时，项目根目录不在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.config import API_KEY, BASE_URL, MODEL

from langchain_openai import ChatOpenAI


def create_client():
    """建一个模型客户端：配置（模型/key/地址/temperature）原样照搬 graph.py。"""
    return ChatOpenAI(
        model=MODEL, api_key=API_KEY, base_url=BASE_URL, temperature=0.7
    )


def invoke(client, messages):
    """调一次模型（client 可以是绑过工具的，也可以是没绑的）。"""
    return client.invoke(messages)

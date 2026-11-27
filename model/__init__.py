"""模型接入层：模型调用的唯一入口。

P0 只做封装，不路由、不计量、不降级：
- client.py：create_client() / invoke()，把 ChatOpenAI 的创建和调用收进来
- 应用层和工具层只认这一层，不直接碰模型 SDK；
  DeepSeek key 只在这一层出现（经由 agent/config.py 的环境变量读入）。
"""

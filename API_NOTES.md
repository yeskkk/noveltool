# 接口适配说明

NovelTool 面向兼容 Chat Completions 的服务，不要求 OpenAI 官方托管服务或 SDK。
只有 noveltool/llm.py 发送外部模型请求；不使用响应工具调用、严格 JSON mode、Responses API。
参数按本地兼容服务常见的 messages/model/temperature/max_tokens/stream=false 实现。
某个服务不支持这些字段时明确报告错误，后续应通过单独适配项支持，不盲目重发请求。

实现时核对的主要接口文档（2026-09-05）：

- Chat Completions： https://developers.openai.com/api/reference/resources/chat
- HTTPX transport 与 MockTransport： https://www.python-httpx.org/advanced/transports/
- Python sqlite3 连接/事务/备份： https://docs.python.org/3/library/sqlite3.html
- Pydantic strict mode： https://docs.pydantic.dev/latest/concepts/strict_mode/

文档介绍的是相应项目的接口，不表示所有“兼容”服务器都完全支持。版本范围在 pyproject.toml，
本轮实际依赖版本在 requirements-tested.txt；测试使用模拟响应，未冒充真实小模型质量验收。

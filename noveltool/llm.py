"""Single OpenAI-compatible chat/completions HTTP boundary, no vendor SDK.

No automatic retry or redirect. The optional transport is only an injection seam
for deterministic tests. Secrets are read from the environment, never stored.
"""
from __future__ import annotations
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
import json
import os
import sqlite3
import time
from uuid import uuid4
import httpx

from .domain import ProjectConfig, utc_now

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class LLMError(RuntimeError):
    def __init__(self, code: str, message: str, *, run_id: str | None = None,
                 partial_text: str = "", raw_response: str = ""):
        super().__init__(message)
        self.code, self.run_id = code, run_id
        self.partial_text, self.raw_response = partial_text, raw_response


@dataclass(frozen=True, slots=True)
class LLMRun:
    id: str
    purpose: str
    model: str
    status: str
    request_json: str
    raw_response: str
    error_code: str | None
    error_message: str | None
    finish_reason: str | None
    usage_json: str
    retained: int
    started_at: str
    finished_at: str
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    raw_response: str
    run_id: str
    finish_reason: str
    usage: dict[str, int]


def request_token_estimate(messages: list[dict[str, str]]) -> int:
    # Deliberately conservative byte-count fallback, not a model tokenizer.
    return 64 + sum(32 + len(m["content"].encode("utf-8")) for m in messages)


class LLMClient:
    def __init__(self, config: ProjectConfig, *,
                 on_run: Callable[[LLMRun], Awaitable[None]] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.config, self.on_run, self.transport = config, on_run, transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=self.config.api_timeout_seconds,
                                         follow_redirects=False, trust_env=False,
                                         transport=self.transport)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()
        self._client = None

    async def complete(self, *, messages: list[dict[str, str]], model: str,
                       temperature: float = 0.7, max_tokens: int = 512,
                       purpose: str = "text") -> Completion:
        if self._client is None:
            raise RuntimeError("LLMClient 必须在 async with 中使用")
        if not isinstance(model, str) or not model.strip():
            raise LLMError("configuration", "请先填写并应用相应的模型名")
        if (not messages or any(not isinstance(m, dict) or set(m) != {"role", "content"} or m["role"] not in {"system","user","assistant"}
                               or not isinstance(m["content"], str) for m in messages)):
            raise LLMError("request", "模型 messages 结构无效")
        if type(max_tokens) is not int or max_tokens < 1:
            raise LLMError("request", "输出 token 预算必须是正整数")
        try:
            estimate = request_token_estimate(messages)
        except UnicodeError as exc:
            raise LLMError("request", "请求包含无效 Unicode 字符") from exc
        if estimate + max_tokens > int(self.config.context_window * self.config.context_safety_ratio):
            raise LLMError("context_budget", "请求按保守估计超出安全上下文预算；请缩短文本或调整输出预留")
        secret = os.environ.get(self.config.api_key_env, "")
        if secret and (not secret.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in secret)):
            raise LLMError("configuration", "密钥环境变量包含不支持的字符；不能包含换行或控制字符")
        def scrub(text: str) -> str:
            return text.replace(secret, "[REDACTED]") if secret else text
        payload = {"model": model, "messages": messages, "temperature": temperature,
                   "max_tokens": max_tokens, "stream": False}
        headers = {"Accept": "application/json"}
        if secret:
            headers["Authorization"] = "Bearer " + secret
        started, clock, rid = utc_now(), time.monotonic(), uuid4().hex
        raw, finish, usage = "", None, {}
        status, error, completion = "failed", None, None
        try:
            async with asyncio.timeout(self.config.api_timeout_seconds):
                async with self._client.stream("POST", self.config.api_base_url + "/chat/completions",
                                                json=payload, headers=headers) as response:
                    parts, total = [], 0
                    async for part in response.aiter_bytes():
                        total += len(part)
                        if total > MAX_RESPONSE_BYTES:
                            raise LLMError("response_too_large", "模型响应超过 2 MiB，已中止读取")
                        parts.append(part)
                    raw = scrub(b"".join(parts).decode("utf-8", errors="replace"))
                    if not 200 <= response.status_code < 300:
                        raise LLMError("http_error", f"模型服务返回 HTTP {response.status_code}；未自动重试或跟随重定向")
            try:
                data = json.loads(raw)
                if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
                    raise ValueError("choices")
                choice = data["choices"][0]
                msg = choice["message"]
                if not isinstance(msg, dict):
                    raise ValueError("message")
                if msg.get("refusal"):
                    raise LLMError("refusal", "模型拒绝了请求")
                text, finish = msg.get("content"), choice.get("finish_reason")
                if not isinstance(text, str):
                    raise ValueError("content")
                if finish != "stop":
                    code = "truncated" if finish == "length" else "unfinished"
                    raise LLMError(code, "模型未完整结束文本（finish_reason 不是 stop）；不把截断内容当成功",
                                   partial_text=text)
                if not text.strip():
                    raise LLMError("empty", "模型返回空正文")
                supplied = data.get("usage")
                if isinstance(supplied, dict):
                    usage = {k: v for k, v in supplied.items()
                             if k in {"prompt_tokens","completion_tokens","total_tokens"} and type(v) is int and v >= 0}
                completion = Completion(text, raw, rid, finish, usage)
                status = "ok"
            except (KeyError, IndexError, TypeError, ValueError, RecursionError) as exc:
                raise LLMError("protocol", "服务响应不是完整的 Chat Completions 格式") from exc
        except asyncio.CancelledError:
            status = "cancelled"
            error = LLMError("cancelled", "请求已取消", run_id=rid)
            raise
        except (httpx.TimeoutException, TimeoutError):
            error = LLMError("timeout", "模型请求超时；服务端可能仍在计算，未自动重试", run_id=rid)
        except httpx.HTTPError:
            error = LLMError("connection", "无法完成模型 HTTP 请求；请检查地址、服务与网络", run_id=rid)
        except LLMError as exc:
            error = LLMError(exc.code, scrub(str(exc)), run_id=rid, partial_text=scrub(exc.partial_text), raw_response=raw)
        finally:
            if self.on_run:
                keep = self.config.retain_llm_logs
                run = LLMRun(rid,purpose,model,status,
                             scrub(json.dumps(payload,ensure_ascii=False)) if keep else "",
                             raw if keep else "", error.code if error else None,
                             str(error) if error else None, finish if isinstance(finish,str) else None,
                             json.dumps(usage),int(keep),started,utc_now(),max(0,int((time.monotonic()-clock)*1000)))
                await self.on_run(run)
        if error:
            raise error
        if completion is None:
            raise LLMError("unknown", "模型调用未产生结果", run_id=rid)
        return completion


def persist_runs(conn: sqlite3.Connection, runs: list[LLMRun]) -> None:
    for run in runs:
        row=asdict(run)
        conn.execute(f"INSERT INTO llm_runs ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",list(row.values()))

"""
novelai.ai_client
统一的 AI 调用层。支持 OpenAI 协议、Anthropic、以及任何 OpenAI 兼容服务
（DeepSeek、Moonshot、智谱 GLM、Ollama 等都可走 openai_compatible）。

设计原则：
- 同步阻塞调用，CLI 直接用；GUI 可放进线程。
- 不缓存，由上层 writer/retriever 决定上下文。
- 错误尽量抛出有意义的异常。
"""
from __future__ import annotations
import os
import json
import time
from typing import Any, Iterator
from .config import AIConfig


class AICallError(RuntimeError):
    pass


def _chat_messages_to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI 风格 messages -> (system_text, anthropic_messages)"""
    sys_text = ""
    msgs: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            if sys_text:
                sys_text += "\n\n" + content
            else:
                sys_text = content
        else:
            msgs.append({"role": role, "content": content})
    return sys_text, msgs


class AIClient:
    def __init__(self, cfg: AIConfig | None = None):
        from .config import CONFIG
        self.cfg = cfg or CONFIG.ai
        # 最近一次 AI 调用的计量（token/延迟/模型/供应商）。咽喉点插桩写入，调用方按需读取落库。
        # 形如 {prompt_tokens, completion_tokens, total_tokens, latency_ms, model, provider}，无数据时为 None。
        self.last_usage: dict | None = None
        if not self.cfg.api_key:
            # 不强制报错：让用户能先做非 AI 的工作（搭建知识库等）
            self._ready = False
        else:
            self._ready = True

    @property
    def ready(self) -> bool:
        return self._ready

    def chat(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        json_mode: bool = False,
    ) -> str:
        """
        messages: OpenAI 风格 [{role, content}, ...]
        返回模型纯文本输出。
        """
        if not self._ready:
            raise AICallError(
                "AI 客户端未配置 API key。请设置环境变量 NOVELAI_API_KEY 或在 .env 中配置。"
            )
        provider = self.cfg.provider
        if provider == "anthropic":
            return self._chat_anthropic(messages, temperature, max_tokens, model, json_mode)
        else:
            # openai / openai_compatible
            return self._chat_openai(messages, temperature, max_tokens, model, json_mode)

    # ---------- OpenAI 协议 ----------

    def _chat_openai(
        self,
        messages: list[dict],
        temperature: float | None,
        max_tokens: int | None,
        model: str | None,
        json_mode: bool,
    ) -> str:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise AICallError("缺少 openai 库，请 pip install openai>=1.0") from e

        client = OpenAI(
            api_key=self.cfg.api_key,
            base_url=self.cfg.base_url,
            max_retries=0,  # 禁用 SDK 自动重试（避免超时后重复请求浪费 token）
        )
        kwargs: dict[str, Any] = {
            "model": model or self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "timeout": max(self.cfg.timeout, 300),  # 至少 5 分钟（大纲生成等长任务需要）
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        t0 = time.perf_counter()
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                               "latency_ms": int((time.perf_counter() - t0) * 1000),
                               "model": kwargs["model"], "provider": "openai"}
            raise AICallError(f"OpenAI 调用失败: {e}") from e
        latency_ms = int((time.perf_counter() - t0) * 1000)
        # 捕获之前被丢弃的 usage（OpenAI 兼容服务可能不返回，getattr 防御）
        usage = getattr(resp, "usage", None)
        self.last_usage = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            "latency_ms": latency_ms,
            "model": kwargs["model"],
            "provider": "openai",
        }
        if not resp.choices:
            raise AICallError(f"模型返回空 choices: {resp}")
        return resp.choices[0].message.content or ""

    # ---------- Anthropic ----------

    def _chat_anthropic(
        self,
        messages: list[dict],
        temperature: float | None,
        max_tokens: int | None,
        model: str | None,
        json_mode: bool,
    ) -> str:
        try:
            import anthropic
        except ImportError as e:
            raise AICallError("缺少 anthropic 库，请 pip install anthropic") from e

        client = anthropic.Anthropic(api_key=self.cfg.api_key)
        sys_text, amsgs = _chat_messages_to_anthropic(messages)
        kwargs: dict[str, Any] = {
            "model": model or self.cfg.model,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "messages": amsgs,
            "temperature": self.cfg.temperature if temperature is None else temperature,
        }
        if sys_text:
            kwargs["system"] = sys_text
        t0 = time.perf_counter()
        try:
            resp = client.messages.create(**kwargs)
        except Exception as e:
            self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                               "latency_ms": int((time.perf_counter() - t0) * 1000),
                               "model": kwargs["model"], "provider": "anthropic"}
            raise AICallError(f"Anthropic 调用失败: {e}") from e
        latency_ms = int((time.perf_counter() - t0) * 1000)
        # Anthropic usage: input_tokens / output_tokens
        usage = getattr(resp, "usage", None)
        pt = getattr(usage, "input_tokens", 0) or 0
        ct = getattr(usage, "output_tokens", 0) or 0
        self.last_usage = {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
            "latency_ms": latency_ms,
            "model": kwargs["model"],
            "provider": "anthropic",
        }
        # 提取 text
        parts = []
        for b in resp.content:
            if getattr(b, "type", None) == "text":
                parts.append(b.text)
        if not parts:
            raise AICallError(f"Anthropic 返回无 text 块: {resp}")
        return "".join(parts)

    # ---------- Embeddings（语义检索用） ----------

    def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        """调用 embeddings 接口，返回向量列表。

        model 留空时自动按 provider 选默认：
        - openai: text-embedding-3-small
        - openai_compatible (SiliconFlow): BAAI/bge-m3
        - anthropic: 不支持，抛 NotImplementedError
        用户可在 .env 配 NOVELAI_EMBEDDING_MODEL 覆盖。
        """
        # 检查全局开关
        if not self.cfg.enable_embedding:
            raise NotImplementedError("embedding 已禁用（NOVELAI_ENABLE_EMBEDDING=false）")
        # 自动选模型
        if not model:
            model = self.cfg.embedding_model or ""
        if not model:
            if self.cfg.base_url and "siliconflow" in self.cfg.base_url:
                model = "BAAI/bge-m3"
            elif self.cfg.base_url and "deepseek" in self.cfg.base_url:
                raise NotImplementedError("DeepSeek 无 embeddings 接口，已降级为关键词匹配")
            else:
                model = "text-embedding-3-small"
        if not self._ready:
            raise AICallError("AI 客户端未配置 API key，无法生成 embedding。")
        provider = self.cfg.provider
        if provider == "anthropic":
            raise NotImplementedError("Anthropic 暂不支持 embeddings，已降级为关键词匹配。")
        try:
            from openai import OpenAI
        except ImportError as e:
            raise AICallError("缺少 openai 库") from e
        client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url, max_retries=0)
        # 过滤空文本（embeddings 接口不接受空串）
        clean = [t.strip() for t in texts if t and t.strip()]
        if not clean:
            return []
        t0 = time.perf_counter()
        try:
            resp = client.embeddings.create(model=model, input=clean)
        except Exception as e:
            # openai_compatible provider 可能没 embeddings 接口 → 抛出，让上层降级
            raise NotImplementedError(f"embeddings 调用失败（provider 可能不支持）: {e}") from e
        latency_ms = int((time.perf_counter() - t0) * 1000)
        usage = getattr(resp, "usage", None)
        # 记一笔计量（embed 不算 completion_tokens）
        self.last_usage = {
            "prompt_tokens": getattr(usage, "prompt_tokens", len(clean)) or len(clean),
            "completion_tokens": 0,
            "total_tokens": getattr(usage, "total_tokens", len(clean)) or len(clean),
            "latency_ms": latency_ms,
            "model": model,
            "provider": "openai",
        }
        return [list(d.embedding) for d in sorted(resp.data, key=lambda x: x.index)]

    # ---------- 高层便捷方法 ----------

    def chat_json(
        self,
        messages: list[dict],
        temperature: float | None = None,
        model: str | None = None,
    ) -> Any:
        """返回解析后的 JSON。"""
        text = self.chat(
            messages,
            temperature=temperature,
            model=model or self.cfg.mini_model,
            json_mode=(self.cfg.provider != "anthropic"),
        )
        # 尝试提取 ```json ... ``` 块
        if "```" in text:
            segs = text.split("```")
            for i in range(1, len(segs), 2):
                block = segs[i]
                if block.startswith("json"):
                    block = block[4:]
                try:
                    return json.loads(block.strip())
                except Exception:
                    continue
            # B-新1: 走过代码块循环都没成功, text 仍含 ```, 剥掉所有 ``` 块再试
            import re as _re
            stripped = _re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", text, flags=_re.DOTALL).strip()
            try:
                return json.loads(stripped)
            except Exception:
                pass  # 落入下面最后兜底
        # 尝试整体解析
        try:
            return json.loads(text)
        except Exception as e:
            raise AICallError(f"模型未返回合法 JSON: {e}\n原文: {text[:500]}") from e

    # ---------- 工具调用（function calling，仅 openai/openai_compatible） ----------

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> dict:
        """带工具声明的非流式调用。返回 {text, tool_calls, usage}。

        tool_calls 为 list[{name, arguments(dict)}]；无工具调用时为空列表。
        anthropic provider 不支持（守卫跳过 tools，走纯文本，保证不报错）。
        """
        if not self._ready:
            raise AICallError("AI 客户端未配置 API key")
        provider = self.cfg.provider
        # anthropic 工具格式不同，守卫：不挂工具走纯文本（保证跨 provider 不报错）
        if provider == "anthropic":
            text = self.chat(messages, temperature=temperature, max_tokens=max_tokens, model=model)
            return {"text": text, "tool_calls": [], "usage": self.last_usage}

        try:
            from openai import OpenAI
        except ImportError as e:
            raise AICallError("缺少 openai 库") from e
        client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url, max_retries=0)
        kwargs: dict[str, Any] = {
            "model": model or self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "timeout": self.cfg.timeout,
            "tools": tools,
            "tool_choice": "auto",
        }
        t0 = time.perf_counter()
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            # 某些 openai_compatible 不支持 tools → 降级为纯文本
            try:
                kwargs.pop("tools")
                kwargs.pop("tool_choice")
                resp = client.chat.completions.create(**kwargs)
            except Exception as e2:
                raise AICallError(f"工具调用失败且降级也失败: {e2}") from e2
        latency_ms = int((time.perf_counter() - t0) * 1000)
        usage = getattr(resp, "usage", None)
        self.last_usage = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            "latency_ms": latency_ms,
            "model": kwargs["model"],
            "provider": "openai",
        }
        if not resp.choices:
            return {"text": "", "tool_calls": [], "usage": self.last_usage}
        msg = resp.choices[0].message
        text = msg.content or ""
        tool_calls = []
        if getattr(msg, "tool_calls", None):
            import json as _json
            for tc in msg.tool_calls:
                fn = getattr(tc, "function", None)
                if not fn:
                    continue
                try:
                    args = _json.loads(fn.arguments) if fn.arguments else {}
                except (ValueError, TypeError):
                    args = {}
                tool_calls.append({"name": fn.name, "arguments": args, "id": getattr(tc, "id", "")})
        return {"text": text, "tool_calls": tool_calls, "usage": self.last_usage}

    def chat_stream(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        chunk_size: int = 4,
        delay: float = 0.0,
    ) -> Iterator[str]:
        """
        流式输出：优先使用 OpenAI/Anthropic 原生 streaming API；
        如果 provider 不支持流式或出错，回退到伪流式。
        """
        if not self._ready:
            raise AICallError("AI 客户端未配置 API key")
        provider = self.cfg.provider

        # OpenAI / OpenAI-compatible 原生 streaming
        if provider in ("openai", "openai_compatible"):
            try:
                from openai import OpenAI
                client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url, max_retries=0)
                t0 = time.perf_counter()
                stream = client.chat.completions.create(
                    model=model or self.cfg.model,
                    messages=messages,
                    temperature=self.cfg.temperature if temperature is None else temperature,
                    max_tokens=self.cfg.max_tokens if max_tokens is None else max_tokens,
                    timeout=max(self.cfg.timeout, 300),  # 流式至少 5 分钟（长章节生成）
                    stream=True,
                    stream_options={"include_usage": True},  # 让最后 chunk 带 usage
                )
                buffer = ""
                last_usage_seen = None
                for chunk in stream:
                    # usage 出现在最后一个 chunk（choices 为空）
                    cu = getattr(chunk, "usage", None)
                    if cu:
                        last_usage_seen = cu
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        buffer += delta.content
                        if len(buffer) >= chunk_size:
                            yield buffer
                            buffer = ""
                            if delay > 0:
                                time.sleep(delay)
                if buffer:
                    yield buffer
                latency_ms = int((time.perf_counter() - t0) * 1000)
                self.last_usage = {
                    "prompt_tokens": getattr(last_usage_seen, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(last_usage_seen, "completion_tokens", 0) or 0,
                    "total_tokens": getattr(last_usage_seen, "total_tokens", 0) or 0,
                    "latency_ms": latency_ms,
                    "model": model or self.cfg.model,
                    "provider": "openai",
                }
                return
            except Exception:
                pass  # 回退到伪流式

        # Anthropic streaming（Anthropic SDK 也支持 stream）
        if provider == "anthropic":
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=self.cfg.api_key)
                sys_text, amsgs = _chat_messages_to_anthropic(messages)
                kwargs: dict[str, Any] = {
                    "model": model or self.cfg.model,
                    "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
                    "messages": amsgs,
                    "temperature": self.cfg.temperature if temperature is None else temperature,
                }
                if sys_text:
                    kwargs["system"] = sys_text
                t0 = time.perf_counter()
                with client.messages.stream(**kwargs) as stream:
                    buffer = ""
                    for text in stream.text_stream:
                        buffer += text
                        if len(buffer) >= chunk_size:
                            yield buffer
                            buffer = ""
                            if delay > 0:
                                time.sleep(delay)
                    if buffer:
                        yield buffer
                    # 流结束后从 final_message 读 usage
                    final_msg = stream.get_final_message() if hasattr(stream, "get_final_message") else None
                latency_ms = int((time.perf_counter() - t0) * 1000)
                usage = getattr(final_msg, "usage", None) if final_msg else None
                pt = getattr(usage, "input_tokens", 0) or 0
                ct = getattr(usage, "output_tokens", 0) or 0
                self.last_usage = {
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "total_tokens": pt + ct,
                    "latency_ms": latency_ms,
                    "model": kwargs["model"],
                    "provider": "anthropic",
                }
                return
            except Exception:
                pass

        # 回退：伪流式（走 self.chat → last_usage 已被 _chat_* 填好，无需重复）
        full = self.chat(messages, temperature=temperature, max_tokens=max_tokens, model=model)
        if not full:
            return
        for i in range(0, len(full), chunk_size):
            yield full[i:i + chunk_size]
            if delay > 0:
                time.sleep(delay)

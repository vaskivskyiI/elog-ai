"""LLM and embedding providers over plain HTTP (no SDKs): Ollama, OpenAI-compatible
(OpenAI, vLLM, LM Studio, llama.cpp server, OpenRouter, ...), Anthropic and Gemini.

chat() returns the text, or an iterator of text pieces with stream=True.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterable, Iterator

UsageHook = Callable[[dict], None]


class ProviderError(Exception):
    pass


def _post(url: str, body: dict, headers: dict, timeout: float, stream: bool = False):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise ProviderError(f"{e.code} from {urllib.parse.urlsplit(url).netloc}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"cannot reach {urllib.parse.urlsplit(url).netloc}: {getattr(e, 'reason', e)}") from None
    if stream:
        return resp
    with resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url: str, headers: dict, timeout: float = 10):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ProviderError(f"{e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"cannot reach {urllib.parse.urlsplit(url).netloc}: {getattr(e, 'reason', e)}") from None


def _lines(resp) -> Iterator[str]:
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                yield line


def _sse(resp) -> Iterator[dict]:
    for line in _lines(resp):
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except ValueError:
            continue


def mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


THINK = re.compile(r"<think>.*?</think>\s*", re.S)


def strip_think(text: str) -> str:
    return THINK.sub("", text).strip()


class _ThinkFilter:
    """Drops a leading <think>...</think> block from a token stream."""

    def __init__(self):
        self.buf = ""
        self.state = "start"     # start | think | pass

    def feed(self, piece: str) -> str:
        if self.state == "after":       # drop the blank space between </think> and the answer
            piece = piece.lstrip()
            if piece:
                self.state = "pass"
            return piece
        if self.state == "pass":
            return piece
        self.buf += piece
        if self.state == "start":
            s = self.buf.lstrip()
            if not s:
                return ""
            if "<think>".startswith(s[:7]) and len(s) < 7:
                return ""
            if s.startswith("<think>"):
                self.state = "think"
            else:
                self.state = "pass"
                out, self.buf = self.buf, ""
                return out
        if self.state == "think" and "</think>" in self.buf:
            out = self.buf.split("</think>", 1)[1].lstrip()
            self.state, self.buf = ("pass" if out else "after"), ""
            return out
        return ""


class Provider:
    kind = "none"

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "", timeout: float = 300,
                 on_usage: UsageHook | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.on_usage = on_usage

    @property
    def local(self) -> bool:
        host = urllib.parse.urlsplit(self.base_url).hostname or ""
        return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local") or \
            re.match(r"^(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.", host) is not None

    def _usage(self, task: str, model: str, t0: float, tin: int, tout: int, ok: bool = True):
        if self.on_usage:
            self.on_usage({"task": task, "model": model, "tokens_in": tin or 0, "tokens_out": tout or 0,
                           "ms": int((time.time() - t0) * 1000), "ok": ok})

    # chat: messages = [{"role": "user"|"assistant", "content": str, "images": [bytes]}]
    def chat(self, messages: list[dict], system: str = "", model: str = "", json_mode: bool = False,
             stream: bool = False, max_tokens: int = 2048, temperature: float = 0.2, task: str = "chat"):
        model = model or self.model
        t0 = time.time()
        if not stream:
            text, tin, tout = self._chat(messages, system, model, json_mode, max_tokens, temperature)
            self._usage(task, model, t0, tin, tout)
            return strip_think(text)
        return self._stream_wrap(self._chat_stream(messages, system, model, max_tokens, temperature),
                                 task, model, t0)

    def _stream_wrap(self, it, task, model, t0):
        counts = {"in": 0, "out": 0}
        flt = _ThinkFilter()
        ok = False
        try:
            for piece in it:
                if isinstance(piece, dict):
                    counts["in"] = piece.get("in", counts["in"])
                    counts["out"] = piece.get("out", counts["out"])
                    continue
                out = flt.feed(piece)
                if out:
                    yield out
            ok = True
        finally:
            self._usage(task, model, t0, counts["in"], counts["out"], ok)

    def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        raise ProviderError(f"{self.kind} has no embeddings")

    def models(self) -> list[str]:
        return []

    def _chat(self, messages, system, model, json_mode, max_tokens, temperature) -> tuple[str, int, int]:
        raise ProviderError("no AI provider configured (AI_PROVIDER=none)")

    def _chat_stream(self, messages, system, model, max_tokens, temperature) -> Iterable:
        raise ProviderError("no AI provider configured (AI_PROVIDER=none)")


# ---- Ollama ------------------------------------------------------------------------------------

class Ollama(Provider):
    kind = "ollama"
    think_ok = True

    def _headers(self):
        # a remote Ollama is usually behind a reverse proxy that wants a token
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    @property
    def local(self) -> bool:
        return True          # self-hosted by definition, wherever it runs

    def _msgs(self, messages, system):
        out = [{"role": "system", "content": system}] if system else []
        for m in messages:
            x = {"role": m["role"], "content": m["content"]}
            if m.get("images"):
                x["images"] = [base64.b64encode(b).decode() for b in m["images"]]
            out.append(x)
        return out

    def _body(self, messages, system, model, max_tokens, temperature, stream):
        body = {"model": model, "messages": self._msgs(messages, system), "stream": stream,
                "options": {"temperature": temperature, "num_predict": max_tokens,
                            "num_ctx": int(os.environ.get("AI_NUM_CTX") or 16384)},
                "keep_alive": "30m"}
        if self.think_ok:
            body["think"] = False
        return body

    def _call(self, body, stream):
        try:
            return _post(self.base_url + "/api/chat", body, self._headers(), self.timeout, stream)
        except ProviderError as e:
            if "think" in str(e).lower() and "think" in body:
                # models or servers without the thinking switch
                self.think_ok = False
                body.pop("think")
                return _post(self.base_url + "/api/chat", body, self._headers(), self.timeout, stream)
            raise

    def _chat(self, messages, system, model, json_mode, max_tokens, temperature):
        body = self._body(messages, system, model, max_tokens, temperature, False)
        if json_mode:
            body["format"] = "json"
        r = self._call(body, False)
        return r.get("message", {}).get("content", ""), r.get("prompt_eval_count", 0), r.get("eval_count", 0)

    def _chat_stream(self, messages, system, model, max_tokens, temperature):
        resp = self._call(self._body(messages, system, model, max_tokens, temperature, True), True)
        for line in _lines(resp):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("error"):
                raise ProviderError(d["error"])
            piece = d.get("message", {}).get("content")
            if piece:
                yield piece
            if d.get("done"):
                yield {"in": d.get("prompt_eval_count", 0), "out": d.get("eval_count", 0)}

    def embed(self, texts, model=""):
        r = _post(self.base_url + "/api/embed", {"model": model or self.model, "input": texts,
                                                  "keep_alive": "30m"}, self._headers(), self.timeout)
        return r["embeddings"]

    def models(self):
        return [m["name"] for m in _get(self.base_url + "/api/tags", self._headers()).get("models", [])]


# ---- OpenAI-compatible -------------------------------------------------------------------------

class OpenAI(Provider):
    kind = "openai"

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _msgs(self, messages, system):
        out = [{"role": "system", "content": system}] if system else []
        for m in messages:
            if m.get("images"):
                parts = [{"type": "text", "text": m["content"]}]
                for b in m["images"]:
                    parts.append({"type": "image_url", "image_url": {
                        "url": f"data:{mime(b)};base64," + base64.b64encode(b).decode()}})
                out.append({"role": m["role"], "content": parts})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def _body(self, messages, system, model, max_tokens, temperature, stream):
        body = {"model": model, "messages": self._msgs(messages, system), "stream": stream,
                "max_completion_tokens": max_tokens}
        if not re.match(r"^(o\d|gpt-5)", model):     # reasoning models only accept the default
            body["temperature"] = temperature
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def _chat(self, messages, system, model, json_mode, max_tokens, temperature):
        body = self._body(messages, system, model, max_tokens, temperature, False)
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = _post(self.base_url + "/chat/completions", body, self._headers(), self.timeout)
        u = r.get("usage") or {}
        return (r["choices"][0]["message"].get("content") or ""), u.get("prompt_tokens", 0), u.get("completion_tokens", 0)

    def _chat_stream(self, messages, system, model, max_tokens, temperature):
        resp = _post(self.base_url + "/chat/completions",
                     self._body(messages, system, model, max_tokens, temperature, True),
                     self._headers(), self.timeout, stream=True)
        for d in _sse(resp):
            for ch in d.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    yield piece
            if d.get("usage"):
                yield {"in": d["usage"].get("prompt_tokens", 0), "out": d["usage"].get("completion_tokens", 0)}

    def embed(self, texts, model=""):
        r = _post(self.base_url + "/embeddings", {"model": model or self.model, "input": texts},
                  self._headers(), self.timeout)
        return [d["embedding"] for d in sorted(r["data"], key=lambda d: d["index"])]

    def models(self):
        return sorted(m["id"] for m in _get(self.base_url + "/models", self._headers()).get("data", []))


# ---- Anthropic ---------------------------------------------------------------------------------

class Anthropic(Provider):
    kind = "anthropic"

    def _headers(self):
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    def _msgs(self, messages):
        out = []
        for m in messages:
            if m.get("images"):
                parts = [{"type": "image", "source": {"type": "base64", "media_type": mime(b),
                                                      "data": base64.b64encode(b).decode()}} for b in m["images"]]
                parts.append({"type": "text", "text": m["content"]})
                out.append({"role": m["role"], "content": parts})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def _body(self, messages, system, model, max_tokens, temperature, stream):
        body = {"model": model, "messages": self._msgs(messages), "max_tokens": max_tokens,
                "temperature": temperature, "stream": stream}
        if system:
            # the system prompt is the same for every call of a task: let the API cache it
            body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        return body

    def _chat(self, messages, system, model, json_mode, max_tokens, temperature):
        if json_mode:
            system = (system + "\n\nAnswer with one JSON object only.").strip()
        r = _post(self.base_url + "/v1/messages", self._body(messages, system, model, max_tokens, temperature, False),
                  self._headers(), self.timeout)
        text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
        u = r.get("usage") or {}
        return text, u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0)

    def _chat_stream(self, messages, system, model, max_tokens, temperature):
        resp = _post(self.base_url + "/v1/messages", self._body(messages, system, model, max_tokens, temperature, True),
                     self._headers(), self.timeout, stream=True)
        tin = 0
        for d in _sse(resp):
            t = d.get("type")
            if t == "message_start":
                tin = (d.get("message") or {}).get("usage", {}).get("input_tokens", 0)
            elif t == "content_block_delta" and d.get("delta", {}).get("type") == "text_delta":
                yield d["delta"]["text"]
            elif t == "message_delta":
                yield {"in": tin, "out": (d.get("usage") or {}).get("output_tokens", 0)}
            elif t == "error":
                raise ProviderError(json.dumps(d.get("error")))


# ---- Gemini ------------------------------------------------------------------------------------

class Gemini(Provider):
    kind = "gemini"

    def _headers(self):
        return {"x-goog-api-key": self.api_key}

    def _body(self, messages, system, max_tokens, temperature, json_mode=False):
        contents = []
        for m in messages:
            parts = [{"inline_data": {"mime_type": mime(b), "data": base64.b64encode(b).decode()}}
                     for b in m.get("images") or []]
            parts.append({"text": m["content"]})
            contents.append({"role": "model" if m["role"] == "assistant" else "user", "parts": parts})
        body = {"contents": contents,
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature}}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    @staticmethod
    def _text(d):
        return "".join(p.get("text", "") for c in d.get("candidates") or []
                       for p in (c.get("content") or {}).get("parts") or [])

    def _chat(self, messages, system, model, json_mode, max_tokens, temperature):
        r = _post(f"{self.base_url}/models/{model}:generateContent",
                  self._body(messages, system, max_tokens, temperature, json_mode), self._headers(), self.timeout)
        u = r.get("usageMetadata") or {}
        return self._text(r), u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0)

    def _chat_stream(self, messages, system, model, max_tokens, temperature):
        resp = _post(f"{self.base_url}/models/{model}:streamGenerateContent?alt=sse",
                     self._body(messages, system, max_tokens, temperature), self._headers(), self.timeout, stream=True)
        for d in _sse(resp):
            piece = self._text(d)
            if piece:
                yield piece
            u = d.get("usageMetadata")
            if u:
                yield {"in": u.get("promptTokenCount", 0), "out": u.get("candidatesTokenCount", 0)}

    def embed(self, texts, model=""):
        model = model or self.model
        r = _post(f"{self.base_url}/models/{model}:batchEmbedContents",
                  {"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]}} for t in texts]},
                  self._headers(), self.timeout)
        return [e["values"] for e in r["embeddings"]]


KINDS = {"ollama": Ollama, "openai": OpenAI, "anthropic": Anthropic, "gemini": Gemini, "none": Provider}


def make(conf: dict, model_key: str = "model", timeout: float = 300, on_usage: UsageHook | None = None) -> Provider:
    return KINDS[conf["provider"]](conf["base_url"], conf["api_key"], conf.get(model_key) or conf.get("model", ""),
                                   timeout, on_usage)


def parse_json(text: str):
    """The first JSON object or array in a model answer (models like to add prose or fences)."""
    text = strip_think(text)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except ValueError:
        pass
    for open_, close in (("{", "}"), ("[", "]")):
        start = text.find(open_)
        while start >= 0:
            depth, instr, esc = 0, False, False
            for i in range(start, len(text)):
                ch = text[i]
                if instr:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        instr = False
                elif ch == '"':
                    instr = True
                elif ch == open_:
                    depth += 1
                elif ch == close:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except ValueError:
                            break
            start = text.find(open_, start + 1)
    raise ValueError("no JSON in the answer")

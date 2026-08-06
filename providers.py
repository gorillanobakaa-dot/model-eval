"""Provider adapters, so one sweep can cross every backend you have.

Ten or twelve providers is not ten or twelve integrations. Nearly all of them
speak one of three wire formats, and the differences that remain are auth and
tool-schema shape:

  openai     POST {base}/chat/completions       tools=[{type:function,...}]
             NVIDIA NIM, Cloudflare, Groq, Cerebras, xAI, Together, Fireworks,
             DeepSeek, Mistral, OpenRouter, Ollama, LM Studio, vLLM, llama.cpp
  anthropic  POST {base}/v1/messages            tools=[{name,input_schema}]
  gemini     POST {base}/models/{m}:generateContent
                                                tools=[{functionDeclarations}]

So the adapter layer is small, and adding provider eleven is usually one line in
`registry.json` rather than any code at all.

Everything is normalised to one canonical shape borrowed from OpenAI, because
the eval only needs three things from a reply: the text, the tool calls, and a
way to feed results back.

    {"content": str, "tool_calls": [{"id", "name", "arguments": dict}]}

Auth styles supported: bearer token, x-api-key header, and a query parameter.
OAuth-backed providers (gemini-oauth, antigravity) hand out short-lived access
tokens; put the current token in the registry entry or export it as the named
environment variable. Tokens are never written to a result file.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "registry.json"
GORILLA_CONFIG = Path.home() / ".config" / "gorilla-opencode" / "config.json"

RETRY_CODES = (408, 409, 425, 429, 500, 502, 503, 504)


@dataclass
class Provider:
    name: str
    kind: str  # openai | anthropic | gemini
    base: str
    key: str = ""
    auth: str = "bearer"  # bearer | x-api-key | query | none
    extra_headers: dict = None

    # -- auth -------------------------------------------------------------

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        h.update(self.extra_headers or {})
        if self.auth == "bearer" and self.key:
            h["Authorization"] = f"Bearer {self.key}"
        elif self.auth == "x-api-key" and self.key:
            h["x-api-key"] = self.key
            h.setdefault("anthropic-version", "2023-06-01")
        return h

    def _url(self, model: str) -> str:
        if self.kind == "gemini":
            url = f"{self.base}/models/{model}:generateContent"
        elif self.kind == "anthropic":
            url = f"{self.base}/v1/messages"
        else:
            url = f"{self.base}/chat/completions"
        if self.auth == "query" and self.key:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"key": self.key})
        return url

    # -- request shaping --------------------------------------------------

    def _body(self, model, messages, tools, max_tokens) -> dict:
        if self.kind == "anthropic":
            system = " ".join(m["content"] for m in messages if m["role"] == "system")
            return {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": 0,
                **({"system": system} if system else {}),
                "messages": _to_anthropic(messages),
                "tools": [
                    {
                        "name": t["function"]["name"],
                        "description": t["function"].get("description", ""),
                        "input_schema": t["function"]["parameters"],
                    }
                    for t in tools
                ],
            }
        if self.kind == "gemini":
            system = " ".join(m["content"] for m in messages if m["role"] == "system")
            return {
                "contents": _to_gemini(messages),
                **({"systemInstruction": {"parts": [{"text": system}]}} if system else {}),
                "tools": [
                    {
                        "functionDeclarations": [
                            {
                                "name": t["function"]["name"],
                                "description": t["function"].get("description", ""),
                                "parameters": t["function"]["parameters"],
                            }
                            for t in tools
                        ]
                    }
                ],
                "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens},
            }
        return {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": max_tokens,
            "temperature": 0,
        }

    # -- response normalising ---------------------------------------------

    @staticmethod
    def _normalise(kind: str, data: dict) -> dict:
        if kind == "anthropic":
            text, calls = "", []
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")
                elif block.get("type") == "tool_use":
                    calls.append({"id": block.get("id", ""),
                                  "name": block.get("name", ""),
                                  "arguments": block.get("input", {}) or {}})
            return {"content": text, "tool_calls": calls}

        if kind == "gemini":
            text, calls = "", []
            cands = data.get("candidates") or [{}]
            for part in (cands[0].get("content", {}) or {}).get("parts", []) or []:
                if "text" in part:
                    text += part["text"]
                if "functionCall" in part:
                    fc = part["functionCall"]
                    calls.append({"id": fc.get("name", ""), "name": fc.get("name", ""),
                                  "arguments": fc.get("args", {}) or {}})
            return {"content": text, "tool_calls": calls}

        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc.get("id", ""), "name": fn.get("name", ""),
                          "arguments": args if isinstance(args, dict) else {}})
        return {"content": msg.get("content") or "", "tool_calls": calls}

    # -- the one call the runner makes ------------------------------------

    def chat(self, model, messages, tools, max_tokens=2048,
             timeout=90, attempts=3) -> dict:
        """Retry budget is deliberately small.

        A dead model costs attempts x timeout before it is declared dead, and a
        sweep pays that for every dead entry. At 5 x 180s one unreachable model
        burned 961 seconds; across a 31-model sweep that is hours of waiting to
        learn nothing. 3 x 90s still rides out a transient 503 while capping the
        worst case near three minutes.
        """
        body = self._body(model, messages, tools, max_tokens)
        for attempt in range(attempts):
            req = urllib.request.Request(
                self._url(model), data=json.dumps(body).encode(), headers=self._headers()
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return self._normalise(self.kind, json.load(resp))
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRY_CODES or attempt == attempts - 1:
                    raise
                time.sleep(min(60, 4 * (2 ** attempt)))
            except (TimeoutError, urllib.error.URLError):
                if attempt == attempts - 1:
                    raise
                time.sleep(min(60, 4 * (2 ** attempt)))
        raise RuntimeError("unreachable")


# --- message translation ------------------------------------------------

def _to_anthropic(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "tool":
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                 "content": m.get("content", "")}]})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                fn = tc.get("function", tc)
                raw = fn.get("arguments", {})
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except json.JSONDecodeError:
                        raw = {}
                blocks.append({"type": "tool_use", "id": tc.get("id", ""),
                               "name": fn.get("name", ""), "input": raw})
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


def _to_gemini(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "tool":
            out.append({"role": "user", "parts": [{"functionResponse": {
                "name": m.get("name") or m.get("tool_call_id", ""),
                "response": {"result": m.get("content", "")}}}]})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            parts = []
            if m.get("content"):
                parts.append({"text": m["content"]})
            for tc in m["tool_calls"]:
                fn = tc.get("function", tc)
                raw = fn.get("arguments", {})
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except json.JSONDecodeError:
                        raw = {}
                parts.append({"functionCall": {"name": fn.get("name", ""), "args": raw}})
            out.append({"role": "model", "parts": parts})
        else:
            role = "model" if m["role"] == "assistant" else "user"
            out.append({"role": role, "parts": [{"text": m.get("content") or ""}]})
    return out


# --- loading ------------------------------------------------------------

def _resolve_key(entry: dict) -> str:
    """Prefer an env var, so rotating a token never means editing a file."""
    env = entry.get("key_env")
    if env and os.environ.get(env):
        return os.environ[env]
    return entry.get("key", "") or ""


def load_providers() -> dict[str, Provider]:
    """registry.json if present, otherwise auto-discover from gorilla-opencode."""
    found: dict[str, Provider] = {}

    if REGISTRY.exists():
        for entry in json.loads(REGISTRY.read_text()).get("providers", []):
            if entry.get("disabled"):
                continue
            found[entry["name"]] = Provider(
                name=entry["name"],
                kind=entry.get("kind", "openai"),
                base=entry["base"].rstrip("/"),
                key=_resolve_key(entry),
                auth=entry.get("auth", "bearer"),
                extra_headers=entry.get("headers") or {},
            )

    if GORILLA_CONFIG.exists():
        cfg = json.loads(GORILLA_CONFIG.read_text())
        for e in cfg.get("localEndpoints", []):
            if e.get("name") in found or not e.get("baseURL"):
                continue
            found[e["name"]] = Provider(
                name=e["name"], kind="openai",
                base=e["baseURL"].rstrip("/"), key=e.get("apiKey", "") or "",
                auth="bearer" if e.get("apiKey") else "none",
            )
    return found


def describe(providers: dict[str, Provider]) -> str:
    lines = []
    for p in providers.values():
        lines.append(f"  {p.name:<28} kind={p.kind:<9} auth={p.auth:<9} "
                     f"key={'set (' + str(len(p.key)) + ' chars)' if p.key else 'none'}")
    return "\n".join(lines)

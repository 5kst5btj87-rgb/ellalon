"""The AI engines: Claude (Anthropic API) and a local model through Ollama.

Conversations are kept in Anthropic's message format (content blocks: text, image, document,
tool_use, tool_result). For Ollama they are translated to its /api/chat format on each call.
Both return the same thing: {"text": str, "tool_calls": [{"id", "name", "input"}], "stop": str}.
"""
import json
import uuid

import httpx

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"
DEFAULT_OLLAMA_MODEL = "qwen2.5vl:7b"
BUILTIN_OLLAMA = "http://ellalon-ollama:11434"      # the "Local AI (Ollama)" app


class LLMError(Exception):
    pass


async def call(settings: dict, system: str, messages: list[dict], tools: list[dict], max_tokens=4096) -> dict:
    provider = settings.get("provider") or ""
    if provider == "anthropic":
        return await _anthropic(settings, system, messages, tools, max_tokens)
    if provider == "ollama":
        return await _ollama(settings, system, messages, tools)
    raise LLMError("The assistant is not set up yet - an administrator chooses the AI engine in ⚙ Settings.")


# ---------------------------------------------------------------- Claude
async def _anthropic(st, system, messages, tools, max_tokens):
    key = st.get("anthropic_key") or ""
    if not key:
        raise LLMError("No Anthropic API key - add it in ⚙ Settings.")
    body = {"model": st.get("anthropic_model") or DEFAULT_CLAUDE_MODEL, "max_tokens": max_tokens, "system": system,
            "messages": messages}
    if tools:
        body["tools"] = tools
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15)) as c:
            r = await c.post(ANTHROPIC_URL, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise LLMError(f"Cannot reach the Anthropic API ({e.__class__.__name__}) - does the server have internet access?")
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message") or r.text[:300]
        except ValueError:
            msg = r.text[:300]
        if r.status_code == 401:
            msg = "the API key was refused - check it in ⚙ Settings"
        raise LLMError(f"Anthropic API error {r.status_code}: {msg}")
    d = r.json()
    text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    calls = [{"id": b["id"], "name": b["name"], "input": b.get("input") or {}} for b in d.get("content", []) if b.get("type") == "tool_use"]
    return {"text": text, "tool_calls": calls, "stop": d.get("stop_reason"), "raw_content": d.get("content", []),
            "usage": d.get("usage", {})}


# ---------------------------------------------------------------- Ollama
def _to_ollama(system, messages):
    out = [{"role": "system", "content": system}]
    for m in messages:
        blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        if m["role"] == "assistant":
            text = "".join(b.get("text", "") for b in blocks if b["type"] == "text")
            calls = [{"function": {"name": b["name"], "arguments": b.get("input") or {}}} for b in blocks if b["type"] == "tool_use"]
            msg = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        texts, images = [], []
        for b in blocks:
            if b["type"] == "tool_result":
                content = b.get("content")
                if isinstance(content, list):
                    content = "\n".join(x.get("text", "") for x in content if x.get("type") == "text")
                out.append({"role": "tool", "content": str(content)})
            elif b["type"] == "text":
                texts.append(b["text"])
            elif b["type"] == "image":
                images.append(b["source"]["data"])
            elif b["type"] == "document":
                texts.append(b.get("_text") or "(a PDF was attached - its pages are given as pictures)")
        if texts or images:
            msg = {"role": "user", "content": "\n".join(texts)}
            if images:
                msg["images"] = images
            out.append(msg)
    return out


def _ollama_tools(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools]


async def _ollama(st, system, messages, tools):
    base = (st.get("ollama_url") or BUILTIN_OLLAMA).rstrip("/")
    body = {"model": st.get("ollama_model") or DEFAULT_OLLAMA_MODEL, "messages": _to_ollama(system, messages),
            "stream": False, "options": {"num_ctx": int(st.get("ollama_ctx") or 16384)}}
    if tools:
        body["tools"] = _ollama_tools(tools)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as c:
            r = await c.post(f"{base}/api/chat", json=body)
    except httpx.HTTPError as e:
        if base == BUILTIN_OLLAMA:
            raise LLMError("The Local AI (Ollama) app is not running - start it in ⚙ Settings (or the System tab).")
        raise LLMError(f"Cannot reach Ollama at {base} ({e.__class__.__name__}). Is Ollama installed there and listening on the "
                       "network (OLLAMA_HOST=0.0.0.0)? Or leave the address empty to use Ellalon's own Local AI app.")
    if r.status_code != 200:
        txt = r.text[:300]
        if "does not support tools" in txt:
            txt += " - choose a model with tool support (e.g. qwen2.5vl, qwen3, llama3.1)"
        if "not found" in txt:
            txt += f" - run on the Ollama machine: ollama pull {body['model']}"
        raise LLMError(f"Ollama error {r.status_code}: {txt}")
    msg = r.json().get("message") or {}
    calls = []
    for tc in msg.get("tool_calls") or []:
        f = tc.get("function") or {}
        args = f.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        calls.append({"id": "call_" + uuid.uuid4().hex[:10], "name": f.get("name", ""), "input": args})
    raw = ([{"type": "text", "text": msg.get("content", "")}] if msg.get("content") else []) + \
          [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]} for c in calls]
    return {"text": msg.get("content", ""), "tool_calls": calls, "stop": "tool_use" if calls else "end_turn",
            "raw_content": raw, "usage": {}}


async def test(settings: dict) -> str:
    r = await call(settings, "Answer with one short sentence.", [{"role": "user", "content": "Say hello in English and in Hebrew."}], [], 100)
    return r["text"].strip()

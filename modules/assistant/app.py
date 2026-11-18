"""Ellalon AI assistant.

The browser sends a message (text, optional attachments) -> a background task runs the model with
the tools in tools.py until it answers -> the browser polls the conversation and shows the answer,
proposal cards (approve/reject), screen actions and reports.
"""
import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

import llm
import tools as T
from ellalon_sdk import EllalonModule

DATA = Path(os.environ.get("ASSISTANT_DATA", "/data"))
MAX_STEPS = 14
m = EllalonModule(version="0.1.1")
app = m.app


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, root: Path):
        self.root = root
        for d in ("files", "reports", "chats"):
            (root / d).mkdir(parents=True, exist_ok=True)

    # settings
    def settings(self) -> dict:
        try:
            return json.loads((self.root / "settings.json").read_text())
        except (OSError, ValueError):
            return {}

    def save_settings(self, s: dict):
        p = self.root / "settings.json"
        p.write_text(json.dumps(s))
        os.chmod(p, 0o600)

    # files
    def file(self, fid: str) -> dict | None:
        if not re.fullmatch(r"[a-f0-9]{12}", fid or ""):
            return None
        try:
            return json.loads((self.root / "files" / fid / "info.json").read_text())
        except (OSError, ValueError):
            return None

    # reports
    def save_report(self, rep: dict):
        (self.root / "reports" / f"{rep['id']}.json").write_text(json.dumps(rep, ensure_ascii=False))

    def report(self, rid: str) -> dict | None:
        if not re.fullmatch(r"[a-f0-9]{10}", rid or ""):
            return None
        try:
            return json.loads((self.root / "reports" / f"{rid}.json").read_text())
        except (OSError, ValueError):
            return None

    def reports(self) -> list[dict]:
        out = []
        for f in (self.root / "reports").glob("*.json"):
            try:
                r = json.loads(f.read_text())
                out.append({k: r.get(k) for k in ("id", "title", "period", "created", "user")})
            except (OSError, ValueError):
                pass
        return sorted(out, key=lambda r: r.get("created") or "", reverse=True)

    # conversations
    def chat_path(self, cid: str) -> Path:
        return self.root / "chats" / f"{cid}.json"

    def load_chat(self, cid: str) -> dict | None:
        if not re.fullmatch(r"[a-f0-9]{12}", cid or ""):
            return None
        try:
            return json.loads(self.chat_path(cid).read_text())
        except (OSError, ValueError):
            return None

    def save_chat(self, c: dict):
        self.chat_path(c["id"]).write_text(json.dumps(c, ensure_ascii=False))

    def chats(self, user: str) -> list[dict]:
        out = []
        for f in (self.root / "chats").glob("*.json"):
            try:
                c = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if c.get("user") == user:
                out.append({"id": c["id"], "title": c.get("title"), "updated": c.get("updated")})
        return sorted(out, key=lambda c: c.get("updated") or 0, reverse=True)[:40]


store = Store(DATA)
busy: dict[str, dict] = {}            # conversation id -> {"step": text}


def who(request: Request) -> tuple[str, str]:
    return request.headers.get("x-ellalon-user") or "user", request.headers.get("x-ellalon-role") or "viewer"


def need_admin(request: Request):
    if who(request)[1] not in ("admin", "module"):
        raise HTTPException(403, "Only administrators can change the assistant settings")


# ---------------------------------------------------------------- settings
class SettingsIn(BaseModel):
    provider: str = ""                   # anthropic | ollama | "" (off)
    anthropic_key: str | None = None     # None / "" = keep the stored key
    anthropic_model: str = llm.DEFAULT_CLAUDE_MODEL
    ollama_url: str = ""
    ollama_model: str = llm.DEFAULT_OLLAMA_MODEL
    ollama_ctx: int = 16384
    speak: bool = False


def public_settings(s: dict) -> dict:
    out = {k: v for k, v in s.items() if k != "anthropic_key"}
    out["has_anthropic_key"] = bool(s.get("anthropic_key"))
    out.setdefault("provider", "")
    out.setdefault("anthropic_model", llm.DEFAULT_CLAUDE_MODEL)
    out.setdefault("ollama_model", llm.DEFAULT_OLLAMA_MODEL)
    return out


@app.get("/settings")
async def get_settings():
    return public_settings(store.settings())


@app.put("/settings")
async def put_settings(body: SettingsIn, request: Request):
    need_admin(request)
    if body.provider not in ("", "anthropic", "ollama"):
        raise HTTPException(400, "Unknown AI engine")
    old = store.settings()
    new = body.model_dump()
    new["anthropic_key"] = (body.anthropic_key or "").strip() or old.get("anthropic_key", "")
    new["ollama_url"] = body.ollama_url.strip().rstrip("/")
    store.save_settings(new)
    m.status["engine"] = new["provider"] or "off"
    return public_settings(new)


@app.post("/settings/test")
async def test_settings(request: Request):
    need_admin(request)
    t0 = time.time()
    try:
        text = await llm.test(store.settings())
    except llm.LLMError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "answer": text, "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- Ollama models
pull_state: dict = {}


def ollama_base() -> str:
    return (store.settings().get("ollama_url") or llm.BUILTIN_OLLAMA).rstrip("/")


@app.get("/ollama/models")
async def ollama_models(request: Request):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(ollama_base() + "/api/tags")
            r.raise_for_status()
            return {"reachable": True, "url": ollama_base(), "builtin": ollama_base() == llm.BUILTIN_OLLAMA,
                    "models": [{"name": x["name"], "gb": round(x.get("size", 0) / 1e9, 1)} for x in r.json().get("models", [])],
                    "pull": pull_state}
    except Exception as e:
        return {"reachable": False, "url": ollama_base(), "builtin": ollama_base() == llm.BUILTIN_OLLAMA,
                "error": e.__class__.__name__, "models": [], "pull": pull_state}


class PullIn(BaseModel):
    model: str


@app.post("/ollama/pull")
async def ollama_pull(body: PullIn, request: Request):
    """Download a model into Ollama (several GB - takes a while); progress in /ollama/models -> pull."""
    need_admin(request)
    if not re.fullmatch(r"[\w.\-/:]{2,80}", body.model):
        raise HTTPException(400, "Unusual model name")
    if pull_state.get("running"):
        raise HTTPException(409, "A download is already running")
    pull_state.clear()
    pull_state.update(model=body.model, running=True, status="starting", pct=0, error="")

    async def go():
        import httpx
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10)) as c:
                async with c.stream("POST", ollama_base() + "/api/pull", json={"model": body.model, "stream": True}) as r:
                    async for line in r.aiter_lines():
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if d.get("error"):
                            pull_state["error"] = d["error"]
                        pull_state["status"] = d.get("status", pull_state["status"])
                        if d.get("total"):
                            pull_state["pct"] = round(100 * d.get("completed", 0) / d["total"], 1)
        except Exception as e:
            pull_state["error"] = f"{e.__class__.__name__}: {str(e)[:200]}"
        pull_state["running"] = False
        if not pull_state["error"]:
            pull_state["status"], pull_state["pct"] = "done", 100
    asyncio.create_task(go())
    return pull_state


# ---------------------------------------------------------------- attachments
@app.post("/files")
async def upload(file: UploadFile = File(...)):
    """A PDF (up to 8 pages are turned into pictures + its text) or a picture."""
    data = await file.read(40 * 1024 * 1024 + 1)
    if len(data) > 40 * 1024 * 1024:
        raise HTTPException(413, "The file is bigger than 40 MB")
    fid = uuid.uuid4().hex[:12]
    d = DATA / "files" / fid
    d.mkdir(parents=True)
    name = Path(file.filename or "file").name[:120]
    is_pdf = data[:5] == b"%PDF-" or name.lower().endswith(".pdf")
    info = {"id": fid, "name": name, "type": "pdf" if is_pdf else "image", "pages": [], "text": ""}
    try:
        if is_pdf:
            (d / "file.pdf").write_bytes(data)
            r = subprocess.run(["pdftoppm", "-png", "-r", "110", "-f", "1", "-l", "8", "-scale-to", "2000",
                                str(d / "file.pdf"), str(d / "page")], capture_output=True, timeout=120)
            pages = sorted(d.glob("page-*.png"), key=lambda p: int(re.findall(r"\d+", p.stem)[-1]))
            if not pages:
                raise HTTPException(400, "Could not read this PDF: " + r.stderr.decode(errors="replace")[-200:])
            for i, p in enumerate(pages, 1):
                p.rename(d / f"{i}.png")
                info["pages"].append(i)
            t = subprocess.run(["pdftotext", "-layout", "-l", "8", str(d / "file.pdf"), "-"], capture_output=True, timeout=60)
            info["text"] = t.stdout.decode(errors="replace")[:20000]
        else:
            kind = "png" if data.startswith(b"\x89PNG") else "jpeg" if data.startswith(b"\xff\xd8\xff") else None
            if not kind:
                raise HTTPException(400, "Attach a PDF, PNG or JPG")
            (d / "1.png").write_bytes(data)         # served with its real type (mime) below
            info["pages"], info["mime"] = [1], f"image/{kind}"
    except HTTPException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    (d / "info.json").write_text(json.dumps(info))
    return {k: info[k] for k in ("id", "name", "type", "pages")} | {"preview": f"/modules/assistant/files/{fid}/page/1.png"}


@app.get("/files/{fid}/page/{n}.png")
async def page(fid: str, n: int):
    f = store.file(fid)
    p = DATA / "files" / fid / f"{n}.png"
    if not f or n not in f["pages"] or not p.exists():
        raise HTTPException(404)
    media = f.get("mime") or "image/png"
    return FileResponse(p.resolve(), media_type=media)


def attachment_blocks(fids: list[str], provider: str) -> list[dict]:
    blocks = []
    for fid in fids[:4]:
        f = store.file(fid)
        if not f:
            continue
        d = DATA / "files" / fid
        blocks.append({"type": "text", "text": f"[Attached file '{f['name']}' - file_id {fid}, {f['type']}, pages {f['pages']}]"})
        if f["type"] == "pdf" and provider == "anthropic" and (d / "file.pdf").stat().st_size < 20 * 1024 * 1024:
            blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                          "data": base64.b64encode((d / "file.pdf").read_bytes()).decode()},
                           "_text": f["text"][:8000]})
            pages = f["pages"][:3]           # plus pictures of the first pages for placing things on a map
        else:
            pages = f["pages"][:6]
            if f.get("text"):
                blocks.append({"type": "text", "text": "Text of the PDF:\n" + f["text"][:8000]})
        for n in pages:
            img = (d / f"{n}.png").resolve()
            blocks.append({"type": "text", "text": f"Page {n} of file {fid} (picture, use x/y fractions of it):"})
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": f.get("mime") or "image/png",
                                                       "data": base64.b64encode(img.read_bytes()).decode()}})
    return blocks


# ---------------------------------------------------------------- the conversation
SYSTEM = """You are the assistant built into Ellalon, a security management platform (VMS, video wall, video analytics,
people tagging and search, face recognition, maps, access control (Apollo APACS), intrusion (RISCO), rules, Alarm manager).
You help the operators of this site: find information in the system, explain how to do things, and build things for them.

Language: answer in the language the user wrote in (Hebrew or English). Be short and practical - this is a control room.
Use the tools to look things up; never invent cameras, devices, ids or numbers. Times: the user's time zone is {tz}; now is {now}.

Changing things ALWAYS needs the user's approval:
- New rules -> call get_rule_options, look up camera/device ids, then propose_rules (the card shows a demo of how often each
  rule would have fired in the last 7 days). Build exactly what was asked; if something is unclear, make a sensible choice and say it.
- A map from a floor plan -> the user attaches the PDF/picture; look at the page picture, match each symbol/label on the
  plan to Ellalon cameras and devices by name/number (list_cameras, list_devices), and call propose_map with x,y as fractions
  of the page picture (0,0 = top-left). Things you cannot match: type label. Cameras: estimate the direction (angle).
- Doors, relays, arming, door modes -> propose_commands. Never claim a door was opened: the user must approve the card.
After a proposal, tell the user briefly what the card contains and that it waits for their approval.
Showing things (tabs, layouts, cameras, recordings, maps) needs no approval: use show_on_screen.
Reports: gather numbers with event_statistics / search_events / get_system_status / list_alarms, then create_report with a
clear summary, tables and charts. The user role is {role}; only admins can create rules and maps (others can still ask)."""


class SendIn(BaseModel):
    text: str = ""
    files: list[str] = []
    tz: str = "UTC"


def new_chat(user: str) -> dict:
    return {"id": uuid.uuid4().hex[:12], "user": user, "title": "", "created": time.time(), "updated": time.time(),
            "messages": [], "display": []}


@app.get("/chats")
async def list_chats(request: Request):
    return store.chats(who(request)[0])


@app.post("/chats")
async def create_chat(request: Request):
    c = new_chat(who(request)[0])
    store.save_chat(c)
    return {"id": c["id"]}


def own_chat(cid: str, request: Request) -> dict:
    c = store.load_chat(cid)
    if not c or c["user"] != who(request)[0]:
        raise HTTPException(404, "No such conversation")
    return c


@app.get("/chats/{cid}")
async def get_chat(cid: str, request: Request):
    c = own_chat(cid, request)
    return {"id": c["id"], "title": c["title"], "display": c["display"], "busy": busy.get(cid)}


@app.delete("/chats/{cid}")
async def delete_chat(cid: str, request: Request):
    own_chat(cid, request)
    store.chat_path(cid).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/chats/{cid}/send")
async def send(cid: str, body: SendIn, request: Request):
    c = own_chat(cid, request)
    if cid in busy:
        raise HTTPException(409, "Still working on the previous message")
    text = body.text.strip()[:8000]
    if not text and not body.files:
        raise HTTPException(400, "Type a message")
    user, role = who(request)
    st = store.settings()
    files = [f for f in body.files if store.file(f)]
    blocks = attachment_blocks(files, st.get("provider", "")) + ([{"type": "text", "text": text}] if text else [])
    c["messages"].append({"role": "user", "content": blocks})
    c["display"].append({"role": "user", "text": text, "files": [{"id": f, "name": store.file(f)["name"]} for f in files],
                         "time": time.time()})
    if not c["title"]:
        c["title"] = (text or store.file(files[0])["name"])[:60]
    c["updated"] = time.time()
    store.save_chat(c)
    busy[cid] = {"step": "thinking"}
    asyncio.create_task(work(c, user, role, body.tz))
    return {"ok": True}


def strip_private(blocks):
    """Our own helper keys must not go to the API."""
    out = []
    for b in blocks:
        out.append({k: v for k, v in b.items() if not k.startswith("_")} if isinstance(b, dict) else b)
    return out


def api_messages(msgs: list[dict], provider: str) -> list[dict]:
    if provider != "anthropic":
        return msgs
    return [{"role": x["role"], "content": strip_private(x["content"]) if isinstance(x["content"], list) else x["content"]} for x in msgs]


async def work(c: dict, user: str, role: str, tz: str):
    cid = c["id"]
    ctx = T.Context(user, role, tz)
    entry = {"role": "assistant", "text": "", "proposals": [], "screen": [], "reports": [], "steps": [], "time": 0}
    try:
        st = store.settings()
        now = datetime.now(ctx.zone).strftime("%A %Y-%m-%d %H:%M")
        system = SYSTEM.format(tz=tz, now=now, role=role)
        texts = []
        for _ in range(MAX_STEPS):
            res = await llm.call(st, system, api_messages(c["messages"], st.get("provider")), T.TOOLS)
            c["messages"].append({"role": "assistant", "content": res["raw_content"] or [{"type": "text", "text": res["text"] or "…"}]})
            if res["text"].strip():
                texts.append(res["text"].strip())
            if not res["tool_calls"]:
                break
            results = []
            for call in res["tool_calls"]:
                busy[cid] = {"step": STEP_TEXT.get(call["name"], call["name"])}
                entry["steps"].append(call["name"])
                try:
                    out = await T.run(call["name"], call["input"], ctx, store)
                except Exception as e:
                    out = f"Tool failed: {e.__class__.__name__}: {str(e)[:200]}"
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": out})
            c["messages"].append({"role": "user", "content": results})
        else:
            texts.append("(I stopped after many steps - ask me to continue if needed.)")
        entry["text"] = "\n\n".join(texts) or "Done."
    except llm.LLMError as e:
        entry["text"], entry["error"] = str(e), True
        if c["messages"] and c["messages"][-1]["role"] == "user":
            pass
    except Exception as e:
        m.log.exception("assistant failed")
        entry["text"], entry["error"] = f"Something went wrong: {e.__class__.__name__}: {str(e)[:200]}", True
    entry["proposals"], entry["screen"], entry["reports"] = ctx.proposals, ctx.screen, ctx.reports
    entry["time"] = time.time()
    _repair(c["messages"])
    c["display"].append(entry)
    c["updated"] = time.time()
    c["messages"] = c["messages"][-60:]
    while c["messages"] and not (c["messages"][0]["role"] == "user" and not _has_tool_result(c["messages"][0])):
        c["messages"].pop(0)                     # never start with an orphan tool result
    store.save_chat(c)
    busy.pop(cid, None)


def _has_tool_result(msg):
    return isinstance(msg["content"], list) and any(b.get("type") == "tool_result" for b in msg["content"])


def _repair(msgs):
    """After an error the history may end with tool calls that have no results - close them."""
    if msgs and msgs[-1]["role"] == "assistant" and isinstance(msgs[-1]["content"], list):
        pend = [b["id"] for b in msgs[-1]["content"] if b.get("type") == "tool_use"]
        if pend:
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": "not run"} for i in pend]})
    if msgs and msgs[-1]["role"] == "user":
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "(no answer)"}]})


STEP_TEXT = {"get_system_status": "checking the system", "list_cameras": "looking at the cameras", "list_devices": "looking at doors and devices",
             "list_alarms": "reading the alarms", "search_events": "searching events", "event_statistics": "counting events",
             "list_rules": "reading the rules", "get_rule_options": "reading the rule options", "list_maps": "reading the maps",
             "list_layouts": "reading the layouts", "search_people": "searching people", "get_people_tags": "reading people tags",
             "propose_rules": "preparing the rules demo", "propose_map": "preparing the map", "propose_commands": "preparing the commands",
             "show_on_screen": "showing it", "create_report": "writing the report"}


class Decision(BaseModel):
    status: str                      # approved | rejected | failed
    result: str = ""


@app.post("/chats/{cid}/proposals/{pid}")
async def decide(cid: str, pid: str, body: Decision, request: Request):
    """The browser carried out (or rejected) a proposal - record it so the assistant knows next time."""
    c = own_chat(cid, request)
    for e in c["display"]:
        for p in e.get("proposals") or []:
            if p["id"] == pid:
                p["status"], p["result"] = body.status, body.result[:500]
                note = f"[The user {body.status} proposal {pid}. {body.result[:300]}]"
                if c["messages"] and c["messages"][-1]["role"] == "assistant":
                    c["messages"].append({"role": "user", "content": [{"type": "text", "text": note}]})
                    c["messages"].append({"role": "assistant", "content": [{"type": "text", "text": "Noted."}]})
                store.save_chat(c)
                return {"ok": True}
    raise HTTPException(404, "No such proposal")


# ---------------------------------------------------------------- reports
@app.get("/reports")
async def list_reports():
    return store.reports()


@app.get("/reports/{rid}")
async def get_report(rid: str):
    r = store.report(rid)
    if not r:
        raise HTTPException(404)
    return r


@app.delete("/reports/{rid}")
async def delete_report(rid: str):
    if store.report(rid):
        (DATA / "reports" / f"{rid}.json").unlink(missing_ok=True)
    return {"ok": True}


@app.get("/status")
async def status():
    s = store.settings()
    return {"engine": s.get("provider") or "", "ready": bool(s.get("provider")), "busy": len(busy)}


@m.on_startup
async def _start():
    m.status["engine"] = store.settings().get("provider") or "off"
    for f in (DATA / "files").iterdir():                   # attachments older than 30 days
        try:
            if time.time() - f.stat().st_mtime > 30 * 86400:
                shutil.rmtree(f, ignore_errors=True)
        except OSError:
            pass

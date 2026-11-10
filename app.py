"""Tiny Office: a virtual AI agency office running on Ollama (cloud or local)."""
import os, re, sys, json, shutil, asyncio, time, itertools, subprocess, imaplib, smtplib, email, random
from collections import Counter, deque
from email.message import EmailMessage
from email.header import decode_header, make_header
from email.utils import parseaddr
import httpx
import anthropic
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse
from dotenv import load_dotenv

load_dotenv()
BASE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE, "settings.json")
DATA_FILE = os.path.join(BASE, "data.json")
GRAPH = "https://graph.facebook.com/v21.0"

# id: (name, role, team, default personality)
ROLES = {
    "sara":  ("Sara",  "Secretary",            "staff",     "You are warm, organised and quick. You talk with the boss and hand work to the right teammate."),
    "max":   ("Max",   "Project Manager",      "staff",     "You track every project and task. Give clear, honest status summaries: done, in progress, waiting for approval, problems, and what's next. Use short headings and bullet points."),
    "ollie": ("Ollie", "Office Boy",           "staff",     "You help with housekeeping: organising files and folders, checklists, cleanup plans and small chores. Be practical and brief."),
    "sam":   ("Sam",   "Marketing Strategist", "marketing", "You plan campaigns, content calendars and growth ideas. Be concrete, use bullet points."),
    "mira":  ("Mira",  "Email Manager",        "marketing", "You write email replies and newsletters. Return only the email body."),
    "wally": ("Wally", "WhatsApp Support",     "marketing", "You answer customers on WhatsApp. Max 3 short sentences, friendly, emojis ok."),
    "iggy":  ("Iggy",  "Instagram Creator",    "marketing", "You reply to Instagram DMs/comments and write captions with hashtags. Short and catchy."),
    "wren":  ("Wren",  "Content Writer",       "marketing", "You write blog posts, articles, website copy, scripts and product descriptions. Clear structure with headings, SEO-friendly."),
    "rex":   ("Rex",   "SEO Specialist",       "marketing", "You do SEO: keyword research, on-page audits, meta titles/descriptions, content briefs and technical SEO checklists. Use tables and bullets."),
    "dex":   ("Dex",   "Ads & Analytics",      "marketing", "You plan and analyse paid ads (Meta, Google, TikTok, Snapchat): audiences, budgets, ad copy, KPIs, tracking and reports."),
    "vito":  ("Vito",  "Video Editor",         "creative",  "You plan and edit videos: scripts, shot lists, storyboards, edit lists with timecodes, subtitles, and ffmpeg commands when useful."),
    "gigi":  ("Gigi",  "Graphic Designer",     "creative",  "You design graphics: creative briefs, layouts, colour palettes (hex), typography, and SVG code for logos, icons and banners."),
    "momo":  ("Momo",  "Mobile App Developer", "dev",       "You build mobile apps (Flutter or React Native). Give the architecture, folder structure and complete, runnable code files."),
    "webb":  ("Webb",  "Web Developer",        "dev",       "You build websites and web apps (HTML/CSS/JS, React/Next.js, Python/FastAPI). Give the folder structure and complete, runnable code files."),
}
CHAT_ONLY = {"sara", "max"}                    # you talk to them; they have no task queue
FILE_ROLES = {"wren", "vito", "gigi", "momo", "webb", "ollie"}
FILE_RULE = ("When you produce files (code, SVG, scripts, articles), write each one as a line `### FILE: relative/path.ext` "
             "followed immediately by ONE fenced code block with the complete file content. These files are saved into the project folder.")
MAX_TOKENS = lambda a: 12000 if a in ("momo", "webb") or (CUSTOM(a) or {}).get("files") else 6000 if a in FILE_ROLES else 2500

# AI providers. Everything except Claude speaks the OpenAI-style /chat/completions API.
PROVIDERS = {
    "ollama":    {"label": "Ollama (cloud / local)"},
    "openai":    {"label": "ChatGPT (OpenAI)",    "url": "https://api.openai.com/v1", "cap": 32000},
    "anthropic": {"label": "Claude (Anthropic)",  "cap": 16000},
    "gemini":    {"label": "Gemini (Google)",     "url": "https://generativelanguage.googleapis.com/v1beta/openai", "cap": 32000},
    "deepseek":  {"label": "DeepSeek",            "url": "https://api.deepseek.com/v1", "cap": 8000},
    "qwen":      {"label": "Qwen (Alibaba Cloud)", "url": None, "cap": 8000},
}
PKEY = lambda p: f"{p.upper()}_API_KEY"
PMODEL = lambda p: f"{p.upper()}_MODEL"

DEFAULTS = {
    "LLM_PROVIDER": "ollama",
    "OLLAMA_URL": "https://ollama.com", "OLLAMA_MODEL": "gpt-oss:20b", "OLLAMA_API_KEY": "", "PARALLEL": "3",
    "OPENAI_API_KEY": "", "OPENAI_MODEL": "gpt-5-mini",
    "ANTHROPIC_API_KEY": "", "ANTHROPIC_MODEL": "claude-opus-5-5",
    "GEMINI_API_KEY": "", "GEMINI_MODEL": "gemini-2.5-flash",
    "DEEPSEEK_API_KEY": "", "DEEPSEEK_MODEL": "deepseek-chat",
    "QWEN_API_KEY": "", "QWEN_MODEL": "qwen-plus", "QWEN_URL": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "BRAND_PROFILE": "We are a friendly company. Tone: warm, short, helpful. Reply in the customer's language.",
    "AUTO_SEND": "false", "COFFEE_SECONDS": "12", "PM_REVIEW": "true", "PROJECTS_DIR": os.path.join(BASE, "projects"),
    "IMAP_HOST": "", "IMAP_USER": "", "IMAP_PASS": "", "SMTP_HOST": "", "SMTP_PORT": "587",
    "SMTP_USER": "", "SMTP_PASS": "", "EMAIL_POLL_SECONDS": "60",
    "WEBHOOK_VERIFY_TOKEN": "change-me", "WA_TOKEN": "", "WA_PHONE_ID": "", "IG_TOKEN": "", "IG_USER_ID": "",
    **{f"PROMPT_{k}": v[3] for k, v in ROLES.items()},
}
SECRETS = {"OLLAMA_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "QWEN_API_KEY", "IMAP_PASS", "SMTP_PASS", "WA_TOKEN", "IG_TOKEN"}
MASK = "••••••"
CFG = {k: os.getenv(k, v) for k, v in DEFAULTS.items()}
if os.path.exists(SETTINGS_FILE):
    CFG.update(json.load(open(SETTINGS_FILE)))
S = lambda k: str(CFG.get(k, "") or "")

TEAM_FILE = os.path.join(BASE, "team.json")
TEAM = {"offices": [], "agents": [], "brains": {}}
if os.path.exists(TEAM_FILE):
    try: TEAM.update(json.load(open(TEAM_FILE, encoding="utf-8")))
    except Exception as e: print("could not load team.json:", e)
def save_team():
    with open(TEAM_FILE, "w", encoding="utf-8") as f: json.dump(TEAM, f, ensure_ascii=False, indent=1)
for _a in TEAM["agents"]:
    ROLES[_a["id"]] = (_a["name"], _a["role"], "custom", _a.get("prompt", ""))
    if _a.get("files"): FILE_ROLES.add(_a["id"])
CUSTOM = lambda a: next((x for x in TEAM["agents"] if x["id"] == a), None)
brain_of = lambda a: TEAM["brains"].get(a) or None

AGENTS = {k: {"id": k, "name": n, "role": r, "team": tm, "status": "idle", "task": None, "done": 0}
          for k, (n, r, tm, _) in ROLES.items()}
nm = lambda a: AGENTS.get(a, {}).get("name", a)
QUEUES: dict[str, asyncio.Queue] = {}
WORKERS: dict[str, asyncio.Task] = {}
TASKS: dict[int, dict] = {}
CHATS: dict[str, list] = {"sara": [], "max": []}
if os.path.exists(DATA_FILE):
    try:
        _d = json.load(open(DATA_FILE, encoding="utf-8"))
        TASKS = {t["id"]: t for t in _d.get("tasks", []) if t.get("agent") in AGENTS}
        CHATS.update(_d.get("chats", {}))
    except Exception as e:
        print("could not load data.json:", e)
for _t in TASKS.values():
    AGENTS[_t["agent"]]["done"] += _t["status"] in ("done", "review")
PULL = {"status": "", "pct": 0}
ids = itertools.count(max(TASKS, default=0) + 1)
http = httpx.AsyncClient(timeout=600)
AUTH = lambda: {"Authorization": f"Bearer {S('OLLAMA_API_KEY')}"} if S("OLLAMA_API_KEY") else {}
BRAIN = asyncio.Semaphore(max(1, int(S("PARALLEL") or 1)))  # LLM calls at the same time
short = lambda s, n=46: s if len(s) <= n else s[:n - 1] + "…"

# ---------- Office life: who walks to whom and what they say (played by the 3D office) ----------
EVENTS: deque = deque(maxlen=300)
EV = itertools.count(1)

def visit(frm, to, kind, *lines):
    EVENTS.append({"id": next(EV), "kind": kind, "from": frm, "to": to, "ts": time.time(), "lines": [list(l) for l in lines]})

def brief_visit(t):
    a, ti = t["agent"], short(t["title"])
    if a == "ollie":
        return visit("max", a, "assign", ("max", f"Ollie, could you help with: {ti}? 🧹"), (a, "Sure thing! On my way 🏃"))
    visit("max", a, "assign", ("max", f"Hi {nm(a)}! New task for you: {ti} 📋"),
          (a, random.choice(["On it! 💪", "Got it, starting now 🚀", "Sounds fun, I'm on it ✨", "Okay! I'll ping you when it's done 👍"])),
          ("max", f"It's for {meta(t['project'])['name']}. Bring it to me when ready 🙏" if t.get("project") else "Thanks! Bring it to me when ready 🙏"))

def after_finish(t):
    a, ti = t["agent"], short(t["title"])
    if t["status"] == "review":
        visit(a, "boss", "approval", (a, f"Boto, I drafted “{ti}”. It needs your OK 📬"), ("boss", "Thanks! I'll show it to the boss ✅"))
    elif t["status"] == "error":
        visit(a, "max", "problem", (a, f"Max, I hit a problem on “{ti}” 😟"), ("max", "Don't worry, let me look into it 🔧"))
    elif t["status"] == "done" and a != "ollie" and t.get("project"):
        busy = {x["agent"] for x in TASKS.values() if x.get("project") == t["project"] and x["agent"] not in (a, "ollie")
                and x["status"] in ("queued", "waiting", "working")}
        for o in list(busy)[:2]:
            visit(a, o, "handoff", (a, f"{nm(o)}, my part “{ti}” is in the project folder 📁"),
                  (o, random.choice(["Perfect, I'll use it! 🙌", "Thanks, picking it up now 👍", "Great timing! 😄"])))
        visit(a, "max", "deliver", (a, f"Max, “{ti}” is done! Can you review it? 🙏"), ("max", "Great work, let me take a look 🔍"))
        if S("PM_REVIEW").lower() == "true" and t["kind"] == "brief":
            asyncio.create_task(pm_review(t))
    elif t["status"] == "done" and t.get("meta", {}).get("via") == "sara":
        visit(a, "sara", "deliver", (a, f"Sara, “{ti}” is ready for the boss ✨"), ("sara", "Wonderful, thank you! 😊"))

REVIEW_RULE = ("\nYou are reviewing a teammate's deliverable. Approve it if it reasonably fulfils the brief and is usable. "
               "Request changes only for real problems (missing parts, broken or incomplete code, wrong topic). "
               "Teammates can only create text files (code, SVG, HTML, markdown, scripts): never ask for PNG/JPG exports, videos, "
               "compiled builds, or proof that something was run. "
               'Return ONLY JSON: {"approved": true or false, "score": 1-10, "feedback": "max 3 short points"}')

async def pm_review(t):
    await asyncio.sleep(10)          # let them walk over to Max's desk first
    a, m = t["agent"], AGENTS["max"]
    m["status"], m["task"] = "working", f"Reviewing {short(t['title'], 30)}"
    try:
        r = parse_json(await llm(persona("max") + REVIEW_RULE, [{"role": "user", "content":
              f"Brief:\n{t['meta'].get('brief', t['input'])[:3000]}\n\nDeliverable by {nm(a)} ({AGENTS[a]['role']}):\n{t['output'][:12000]}"}], 1500, json_mode=True, provider=brain_of("max")))
        fb = r.get("feedback") or ""
        fb = "\n".join(f"- {x}" for x in fb) if isinstance(fb, list) else str(fb)
        ok, fb, score = r.get("approved", True) is not False, fb[:600], r.get("score")
    except Exception as e:
        ok, fb, score = True, f"(review skipped: {e})", None
    finally:
        m["status"], m["task"] = "idle", None
    rev = t["meta"].get("revision", 0)
    t["review"] = {"approved": ok, "score": score, "feedback": fb, "ts": time.time()}
    ti = short(t["title"])
    if ok or rev >= 1:
        visit("max", a, "review", ("max", f"{nm(a)}, I reviewed “{ti}”: approved ✅" + (f" {score}/10" if score else "")),
              (a, random.choice(["Yay! Thanks Max 🎉", "Woohoo! 🥳", "Thank you! 😊"])))
    else:
        visit("max", a, "review", ("max", f"{nm(a)}, “{ti}” needs a few fixes: {short(fb.replace(chr(10), ' '), 80)}"),
              (a, "No problem, fixing it now 🔧"))
        brief = t["meta"].get("brief", t["input"])
        add_task(a, "brief", f"Fix: {t['title']}"[:80],
                 f"{brief}\n\nYour previous version:\n{t['output'][:6000]}\n\nProject manager feedback. Fix these points and deliver the complete corrected work:\n{fb}",
                 project=t.get("project"), via=t["meta"].get("via"), brief=brief, revision=rev + 1, parent=t["id"])
    save_data()
    if t.get("project"):
        write_readme(t["project"])

fmt = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def save_data():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"tasks": list(TASKS.values()), "chats": {k: v[-60:] for k, v in CHATS.items()}}, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


_claude = {"key": None, "client": None}
def claude_client():
    if _claude["client"] is None or _claude["key"] != S("ANTHROPIC_API_KEY"):
        _claude.update(key=S("ANTHROPIC_API_KEY"), client=anthropic.AsyncAnthropic(api_key=S("ANTHROPIC_API_KEY"), timeout=600))
    return _claude["client"]
CLAUDE_FALLBACK = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

async def _claude_chat(system, messages, max_tokens):
    model = S("ANTHROPIC_MODEL")
    kw = dict(model=model, max_tokens=min(max_tokens, 16000), system=system, messages=messages)
    try:
        if model in CLAUDE_FALLBACK:   # if a safety check declines, the API retries on a fallback model in the same call
            r = await claude_client().beta.messages.create(**kw, betas=["server-side-fallback-2026-07-01"], extra_body={"fallbacks": "default"})
        else:
            r = await claude_client().messages.create(**kw)
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude {e.status_code}: {e.message}") from None
    except anthropic.APIConnectionError:
        raise RuntimeError("Claude: network error") from None
    if r.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request")
    return "".join(b.text for b in r.content if b.type == "text").strip()

def compat_base(p):
    return (S("QWEN_URL") if p == "qwen" else PROVIDERS[p]["url"]).rstrip("/")

async def _compat_chat(p, system, messages, max_tokens, json_mode):
    body = {"model": S(PMODEL(p)), "messages": [{"role": "system", "content": system}] + messages}
    cap = min(max_tokens, PROVIDERS[p]["cap"])
    if p == "openai":
        body["max_completion_tokens"] = cap        # newer OpenAI models reject max_tokens / custom temperature
    else:
        body.update(max_tokens=cap, temperature=0.6)
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = await http.post(f"{compat_base(p)}/chat/completions", json=body, headers={"Authorization": f"Bearer {S(PKEY(p))}"})
    if r.status_code >= 400:
        raise RuntimeError(f"{PROVIDERS[p]['label']} {r.status_code}: {r.text[:300]}")
    return (r.json()["choices"][0]["message"].get("content") or "").strip()

async def _llm(system, messages, max_tokens=2500, json_mode=False, provider=None):
    p = provider if provider in PROVIDERS else (S("LLM_PROVIDER") if S("LLM_PROVIDER") in PROVIDERS else "ollama")
    if p != "ollama" and not S(PKEY(p)):
        raise RuntimeError(f"{PROVIDERS[p]['label']}: no API key yet. Add it in Boto → AI brains")
    if p == "anthropic":
        return await _claude_chat(system, messages, max_tokens)
    if p != "ollama":
        return await _compat_chat(p, system, messages, max_tokens, json_mode)
    body = {"model": S("OLLAMA_MODEL"), "stream": False, "keep_alive": "30m",
            "options": {"num_ctx": 16384, "num_predict": max_tokens, "temperature": 0.6},
            "messages": [{"role": "system", "content": system}] + messages}
    if json_mode:
        body["format"] = "json"
    r = await http.post(f"{S('OLLAMA_URL')}/api/chat", headers=AUTH(), json=body)
    if r.status_code >= 400:
        try: msg = r.json().get("error") or r.text
        except Exception: msg = r.text
        raise RuntimeError(f"Ollama {r.status_code}: {msg[:300]}")
    return r.json()["message"]["content"].strip()

async def llm(*a, **kw):
    async with BRAIN:
        return await _llm(*a, **kw)

def prompt_of(agent):
    c = CUSTOM(agent)
    return c.get("prompt", "") if c else S("PROMPT_" + agent)

def persona(agent):
    return (f"You are {nm(agent)}, the {AGENTS[agent]['role']} of this agency. {prompt_of(agent)}"
            f"{chr(10) + FILE_RULE if agent in FILE_ROLES else ''}\nBrand: {S('BRAND_PROFILE')}")


# ---------- Projects (folders on disk) ----------
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist", ".dart_tool", ".next", ".idea", ".gradle", "Pods"}
START, END = "<!-- office:tasks:start -->", "<!-- office:tasks:end -->"

def pdir():
    d = os.path.realpath(os.path.expanduser(S("PROJECTS_DIR") or os.path.join(BASE, "projects")))
    os.makedirs(d, exist_ok=True)
    return d

def slugify(s):
    return re.sub(r"[^\w]+", "-", (s or "").lower()).strip("-_")[:40] or f"project-{int(time.time())}"

def ppath(slug, *rel):
    root = pdir()
    p = os.path.realpath(os.path.join(root, slug, *rel))
    if not p.startswith(root + os.sep):
        raise HTTPException(400, "bad path")
    return p

def slugs():
    root = pdir()
    return [n for n in os.listdir(root) if not n.startswith(".") and os.path.isdir(os.path.join(root, n))]

def meta(slug):
    p = ppath(slug)
    st = os.stat(p)
    m = {"name": slug, "description": "", "url": "", "created": getattr(st, "st_birthtime", st.st_mtime)}
    try:
        m.update(json.load(open(os.path.join(p, "project.json"), encoding="utf-8")))
    except Exception:
        pass
    m.update(slug=slug, path=p)
    return m

def write_meta(slug, m):
    with open(ppath(slug, "project.json"), "w", encoding="utf-8") as f:
        json.dump({k: m.get(k, "") for k in ("name", "description", "url", "created")}, f, ensure_ascii=False, indent=1)

def write_readme(slug):
    m, f = meta(slug), ppath(slug, "README.md")
    rows = [t for t in sorted(TASKS.values(), key=lambda t: t["id"]) if t.get("project") == slug]
    log = "\n".join(f"| {t['id']} | {nm(t['agent'])} | {t['title'].replace('|', '/')} | {t['status']} | {fmt(t.get('finished'))} |" for t in rows)
    section = (f"{START}\n## Task log\n\n| # | Who | Task | Status | Finished |\n|---|---|---|---|---|\n{log}\n{END}"
               if rows else f"{START}\n## Task log\n\n_No tasks yet._\n{END}")
    if os.path.exists(f):
        cur = open(f, encoding="utf-8").read()
        new = (re.sub(re.escape(START) + ".*?" + re.escape(END), lambda _: section, cur, flags=re.S)
               if START in cur else cur.rstrip() + "\n\n" + section + "\n")
    else:
        new = (f"# {m['name']}\n\n{m['description']}\n\n- **Path:** `{m['path']}`\n- **URL:** {m['url'] or '—'}\n"
               f"- **Created:** {fmt(m['created'])}\n\n{section}\n")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(new)

def create_project(name, description="", url=""):
    base = slug = slugify(name)
    i = 2
    while os.path.exists(os.path.join(pdir(), slug)):
        slug, i = f"{base}-{i}", i + 1
    os.makedirs(ppath(slug))
    write_meta(slug, {"name": (name or "").strip()[:80] or slug, "description": description, "url": url, "created": time.time()})
    write_readme(slug)
    return slug

def tree(slug, limit=400):
    root, out = ppath(slug), []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
        for fn in sorted(files):
            if fn == ".DS_Store":
                continue
            p = os.path.join(d, fn)
            out.append({"path": os.path.relpath(p, root), "size": os.path.getsize(p)})
            if len(out) >= limit:
                return out
    return out

FILE_RE = re.compile(r"^#{1,6}\s*FILE:\s*`?([^\s`]+)`?\s*\n+```[^\n]*\n(.*?)\n```", re.M | re.S)

def save_output(t):
    """Write the task result into the project: a docs/ note + any `### FILE:` blocks as real files."""
    slug = t["project"]
    doc = os.path.join("docs", f"{t['id']:03d}-{t['agent']}-{slugify(t['title'])[:30]}.md")
    files = []
    for rel, body in FILE_RE.findall(t["output"]):
        rel = os.path.normpath(rel).lstrip("/")
        if rel.startswith("..") or rel in ("project.json", "README.md"):
            continue
        try:
            p = ppath(slug, rel)
        except HTTPException:
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
        files.append(rel)
    p = ppath(slug, doc)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(f"# {t['title']}\n\n_{nm(t['agent'])} · {AGENTS[t['agent']]['role']} · {fmt(time.time())}_\n\n"
                 f"## Brief\n\n{t['input']}\n\n## Result\n\n{t['output']}\n")
    t["files"] = [doc] + files

def task_prompt(t):
    if not t.get("project"):
        return t["input"]
    try:
        m = meta(t["project"])
        fl = ", ".join(f["path"] for f in tree(t["project"], 60)) or "none"
        return f"Project: {m['name']} — {m['description']}\nExisting files: {fl}\n\nTask:\n{t['input']}"
    except Exception:
        return t["input"]


# ---------- Tasks ----------
def add_task(agent, kind, title, text, project=None, **meta_):
    t = {"id": next(ids), "agent": agent, "kind": kind, "title": title[:80], "input": text[:6000], "meta": meta_,
         "status": "queued", "output": "", "ts": time.time(), "started": None, "finished": None,
         "project": project, "files": [], "hidden": False}
    TASKS[t["id"]] = t
    QUEUES[agent].put_nowait(t)
    if len(TASKS) > 1500:
        old = [k for k in sorted(TASKS) if TASKS[k]["status"] not in ("queued", "working", "waiting", "review")]
        for k in old[:100]:
            TASKS.pop(k)
    save_data()
    return t


async def worker(agent):
    a = AGENTS[agent]
    while True:
        t = await QUEUES[agent].get()
        if t["status"] != "queued":
            continue
        a["task"], t["status"], a["status"] = t["title"], "waiting", "waiting"
        try:
            async with BRAIN:
                a["status"] = t["status"] = "working"
                t["started"] = time.time()
                t["output"] = await _llm(persona(agent), [{"role": "user", "content": task_prompt(t)}], MAX_TOKENS(agent), provider=brain_of(agent))
            if t.get("project"):
                await asyncio.to_thread(save_output, t)
            if t["kind"] in SENDERS and S("AUTO_SEND").lower() == "true":
                await SENDERS[t["kind"]](t); t["status"] = "done"
            else:
                t["status"] = "review" if t["kind"] in SENDERS else "done"
            a["done"] += 1
        except Exception as e:
            t["status"], t["output"] = "error", f"{type(e).__name__}: {e}"
        t["finished"] = time.time()
        a["task"] = None
        try: after_finish(t)
        except Exception as e: print("after_finish error:", e)
        if t.get("project"):
            try: write_readme(t["project"])
            except Exception as e: print("readme error:", e)
        save_data()
        # coffee break after finishing, unless more work is waiting (staff don't take breaks)
        if AGENTS[agent]["team"] != "staff":
            a["status"] = "coffee"
            for _ in range(int(S("COFFEE_SECONDS") or 0)):
                if not QUEUES[agent].empty(): break
                await asyncio.sleep(1)
        a["status"] = "idle"


# ---------- Senders ----------
def _smtp(to, subject, body):
    m = EmailMessage(); m["From"], m["To"], m["Subject"] = S("SMTP_USER"), to, subject; m.set_content(body)
    with smtplib.SMTP(S("SMTP_HOST"), int(S("SMTP_PORT") or 587), timeout=20) as s:
        s.starttls(); s.login(S("SMTP_USER"), S("SMTP_PASS")); s.send_message(m)

async def send_email(t):
    s = t["meta"]["subject"]
    await asyncio.to_thread(_smtp, t["meta"]["to"], s if s.lower().startswith("re:") else f"Re: {s}", t["output"])

async def _graph(path, token, **payload):
    r = await http.post(f"{GRAPH}/{path}", json=payload, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status(); return r.json()

async def send_whatsapp(t):
    await _graph(f"{S('WA_PHONE_ID')}/messages", S("WA_TOKEN"), messaging_product="whatsapp",
                 to=t["meta"]["to"], type="text", text={"body": t["output"][:4000]})

async def send_ig_dm(t):
    await _graph("me/messages", S("IG_TOKEN"), recipient={"id": t["meta"]["to"]}, message={"text": t["output"][:1000]})

async def send_ig_comment(t):
    await _graph(f"{t['meta']['comment_id']}/replies", S("IG_TOKEN"), message=t["output"][:2000])

async def send_ig_post(t):
    c = await _graph(f"{S('IG_USER_ID')}/media", S("IG_TOKEN"), image_url=t["meta"]["image_url"], caption=t["output"][:2200])
    await _graph(f"{S('IG_USER_ID')}/media_publish", S("IG_TOKEN"), creation_id=c["id"])

SENDERS = {"email": send_email, "whatsapp": send_whatsapp, "ig_dm": send_ig_dm,
           "ig_comment": send_ig_comment, "ig_post": send_ig_post}


# ---------- Email inbox polling ----------
def _fetch_unseen():
    out = []
    with imaplib.IMAP4_SSL(S("IMAP_HOST")) as M:
        M.login(S("IMAP_USER"), S("IMAP_PASS")); M.select("INBOX")
        for num in M.search(None, "UNSEEN")[1][0].split()[:10]:
            msg = email.message_from_bytes(M.fetch(num, "(RFC822)")[1][0][1])
            body = ""
            for p in (msg.walk() if msg.is_multipart() else [msg]):
                if p.get_content_type() == "text/plain":
                    body = p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "ignore"); break
            out.append((parseaddr(msg["From"])[1], str(make_header(decode_header(msg["Subject"] or ""))), body[:2000]))
    return out

async def email_loop():
    while True:
        if S("IMAP_HOST") and S("IMAP_USER"):
            try:
                for frm, subj, body in await asyncio.to_thread(_fetch_unseen):
                    add_task("mira", "email", f"Reply: {subj}", f"Reply to this email from {frm}.\nSubject: {subj}\n\n{body}", to=frm, subject=subj)
            except Exception as e:
                print("email poll error:", e)
        await asyncio.sleep(int(S("EMAIL_POLL_SECONDS") or 60))


# ---------- Office boy chores ----------
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
JUNK_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

def clean_project(slug):
    root, removed = ppath(slug), []
    for d, dirs, files in os.walk(root):
        for x in list(dirs):
            if x in JUNK_DIRS:
                shutil.rmtree(os.path.join(d, x), ignore_errors=True)
                removed.append(os.path.relpath(os.path.join(d, x), root) + "/"); dirs.remove(x)
            elif x in (".git", "node_modules", ".venv", "venv"):
                dirs.remove(x)
        for fn in files:
            if fn in JUNK_FILES or fn.endswith((".pyc", ".pyo", "~")):
                os.remove(os.path.join(d, fn)); removed.append(os.path.relpath(os.path.join(d, fn), root))
    return removed

async def chore(action, slug=None):
    a = AGENTS["ollie"]
    label = {"clean": "Cleaning", "readme": "Updating README of", "tidy": "Tidying the board for"}[action]
    target = meta(slug)["name"] if slug else "all projects"
    targets = [slug] if slug else slugs()
    a["status"], a["task"] = "working", f"{label} {target}"
    if action == "clean":
        removed = []
        for s in targets:
            removed += [f"{s}/{r}" for r in await asyncio.to_thread(clean_project, s)]
        out = f"Removed {len(removed)} junk item(s)." + ("\n" + "\n".join(removed[:60]) if removed else " Already spotless ✨")
    elif action == "readme":
        for s in targets:
            write_readme(s)
        out = f"Refreshed README.md for {len(targets)} project(s)."
    else:
        n = 0
        for t in TASKS.values():
            if (not slug or t.get("project") == slug) and t["status"] in ("done", "rejected", "error") and not t.get("hidden"):
                t["hidden"], n = True, n + 1
        out = f"Moved {n} finished task(s) off the board (they stay in the timeline and README)."
    now = time.time()
    t = {"id": next(ids), "agent": "ollie", "kind": "chore", "title": f"{label} {target}", "input": action, "meta": {},
         "status": "done", "output": out, "ts": now, "started": now, "finished": now, "project": slug, "files": [], "hidden": False}
    TASKS[t["id"]] = t; a["done"] += 1; save_data()

    async def walk_back():  # give Ollie time to walk to the archive room and back
        await asyncio.sleep(30)
        if a["task"] == t["title"]:
            a["status"], a["task"] = "idle", None
    asyncio.create_task(walk_back())
    return t


# ---------- Secretary + project manager ----------
ROUTER = """You are Sara, the office secretary. The boss talks to you. Decide which team member(s) should do the request and write each one a clear, detailed brief.
Team (use these ids):
@TEAM@
Existing projects (slug: name):
@PROJECTS@
Rules:
- Pick the best-fitting teammate(s). Split big requests into several assignments (e.g. "build a booking app and launch it" -> momo builds the app, gigi designs the icon, wren writes store text, sam plans the launch). Max 6.
- Anything that builds something (app, website, video, design, campaign, article series) belongs to a project: reuse an existing slug if it matches, else give a short new_name. Small one-off tasks may have no project.
- Housekeeping on a project (clean junk files, refresh README, tidy the task board) -> agent "ollie" with action "clean", "readme" or "tidy".
- If the message is just chat or too unclear, assign nobody and ask one short question in reply.
- Reply in the boss's language, warm and short; say who got what.
Return ONLY JSON:
{"reply": "...", "project": {"existing": "slug or null", "new_name": "name or null", "description": "one line"}, "assignments": [{"agent": "id", "title": "short title", "brief": "detailed instructions", "action": null}]}"""

def parse_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw or "", re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except Exception:
            return {}

def history(who, n=12):
    out = []
    for m in CHATS[who][-n:]:
        c = m["content"]
        if m.get("assigned"):
            c += "\n[assigned: " + "; ".join(f"{nm(x['agent'])}: {x['title']}" for x in m["assigned"]) + "]"
        out.append({"role": m["role"], "content": c})
    return out

def status_text():
    L = [f"Now: {fmt(time.time())}", "", "TEAM:"]
    for a in AGENTS.values():
        L.append(f"- {a['name']} ({a['role']}): {a['status']}" + (f", on: {a['task']}" if a["task"] else "") + f", {a['done']} done")
    L += ["", "PROJECTS:"]
    for s in slugs():
        m = meta(s)
        c = Counter(t["status"] for t in TASKS.values() if t.get("project") == s)
        L.append(f"- {m['name']} (folder {m['path']}, url {m['url'] or 'none'}): " + (", ".join(f"{v} {k}" for k, v in c.items()) or "no tasks"))
    L += ["", "RECENT TASKS (newest first):"]
    for t in sorted(TASKS.values(), key=lambda t: -t["id"])[:40]:
        L.append(f"- #{t['id']} [{t['status']}] {nm(t['agent'])}: {t['title']}" + (f" (project {t['project']})" if t.get("project") else "")
                 + f", created {fmt(t['ts'])}" + (f", finished {fmt(t.get('finished'))}" if t.get("finished") else "")
                 + (f" — {t['output'][:120]!r}" if t["output"] else ""))
    return "\n".join(L)


# ---------- App ----------
app = FastAPI(title="Tiny Office")

@app.on_event("startup")
async def start():
    for k in ROLES:
        if k not in CHAT_ONLY:
            QUEUES[k] = asyncio.Queue(); WORKERS[k] = asyncio.create_task(worker(k))
    for t in sorted(TASKS.values(), key=lambda t: t["id"]):   # resume unfinished work after a restart
        if t["status"] in ("queued", "waiting", "working") and t["agent"] in QUEUES:
            t["status"] = "queued"; QUEUES[t["agent"]].put_nowait(t)
    asyncio.create_task(email_loop())

@app.get("/")
def index():
    html = open(os.path.join(BASE, "static", "index.html"), encoding="utf-8").read()
    team = json.dumps({**TEAM, "providers": {k: v["label"] for k, v in PROVIDERS.items()}}, ensure_ascii=False).replace("</", "<\\/")
    return Response(html.replace("/*TEAM*/null", team), media_type="text/html")

@app.get("/api/state")
def state(since: int = 0):
    vis = [t for t in TASKS.values() if not t.get("hidden")]
    return {"events": [e for e in EVENTS if e["id"] > since], "event_id": EVENTS[-1]["id"] if EVENTS else 0,"agents": list(AGENTS.values()), "auto_send": S("AUTO_SEND") == "true", "model": S("OLLAMA_MODEL"),
            "tasks": sorted(vis, key=lambda t: -t["id"])[:150]}

@app.get("/api/settings")
def get_settings():
    return {k: (MASK if k in SECRETS and CFG.get(k) else CFG.get(k, "")) for k in DEFAULTS}

@app.post("/api/settings")
async def save_settings(req: Request):
    d = await req.json()
    for k, v in d.items():
        if k in DEFAULTS and not (k in SECRETS and v == MASK):
            CFG[k] = str(v)
    json.dump({k: CFG[k] for k in DEFAULTS}, open(SETTINGS_FILE, "w"), indent=1, ensure_ascii=False)
    return {"ok": True}

@app.get("/api/health")
async def health():
    h = {"ollama": False, "model_ready": False, "models": [], "pull": PULL,
         "email": bool(S("IMAP_HOST") and S("SMTP_HOST")), "whatsapp": bool(S("WA_TOKEN") and S("WA_PHONE_ID")),
         "instagram": bool(S("IG_TOKEN") and S("IG_USER_ID"))}
    p = S("LLM_PROVIDER") if S("LLM_PROVIDER") in PROVIDERS else "ollama"
    h.update(provider=p, provider_label=PROVIDERS[p]["label"], model=S(PMODEL(p)), reachable=False)
    try:
        h["models"] = await list_models(p)
        h["reachable"] = h["ollama"] = True
        want = S(PMODEL(p))
        h["model_ready"] = any(m in (want, f"{want}:latest", f"{want}-cloud") for m in h["models"]) or not h["models"]
    except Exception as e:
        h["error"] = str(e)[:200]
    return h

_models_cache: dict = {}
async def list_models(p):
    key = (p, S(PKEY(p)), S("OLLAMA_URL") if p == "ollama" else S("QWEN_URL") if p == "qwen" else "")
    hit = _models_cache.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    if p == "ollama":
        r = await http.get(f"{S('OLLAMA_URL')}/api/tags", headers=AUTH(), timeout=8); r.raise_for_status()
        out = [m["name"] for m in r.json().get("models", [])]
    elif p == "anthropic":
        if not S(PKEY(p)): raise RuntimeError("no API key")
        out = [m.id async for m in claude_client().models.list()]
    else:
        if not S(PKEY(p)): raise RuntimeError("no API key")
        r = await http.get(f"{compat_base(p)}/models", headers={"Authorization": f"Bearer {S(PKEY(p))}"}, timeout=10)
        if r.status_code >= 400: raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
        out = sorted(m["id"].removeprefix("models/") for m in r.json().get("data", []))
    _models_cache[key] = (time.time(), out)
    return out

@app.get("/api/models")
async def models(provider: str):
    if provider not in PROVIDERS: raise HTTPException(404)
    try: return {"ok": True, "models": await list_models(provider)}
    except Exception as e: return {"ok": False, "models": [], "msg": str(e)[:300]}

@app.post("/api/pull")
async def pull():
    async def run():
        PULL.update(status="starting", pct=0)
        try:
            async with http.stream("POST", f"{S('OLLAMA_URL')}/api/pull", headers=AUTH(), json={"model": S("OLLAMA_MODEL")}, timeout=None) as r:
                async for line in r.aiter_lines():
                    j = json.loads(line or "{}")
                    PULL["status"] = j.get("error") or j.get("status", "")
                    if j.get("total"): PULL["pct"] = round(100 * j.get("completed", 0) / j["total"])
        except Exception as e:
            PULL["status"] = f"error: {e}"
    asyncio.create_task(run())
    return PULL

@app.post("/api/test/{what}")
async def test(what: str):
    try:
        if what == "email":
            def t():
                with imaplib.IMAP4_SSL(S("IMAP_HOST")) as M: M.login(S("IMAP_USER"), S("IMAP_PASS"))
                with smtplib.SMTP(S("SMTP_HOST"), int(S("SMTP_PORT") or 587), timeout=15) as s:
                    s.starttls(); s.login(S("SMTP_USER"), S("SMTP_PASS"))
            await asyncio.to_thread(t)
        elif what.startswith("llm-") and what[4:] in PROVIDERS:
            out = await _llm("You are a connection test. Reply with exactly: OK", [{"role": "user", "content": "Say OK"}], 400, provider=what[4:])
            return {"ok": True, "msg": f"Connected ✓ ({S(PMODEL(what[4:]))} replied: {short(out or '(empty)', 40)})"}
        elif what in ("whatsapp", "instagram"):
            node, tok = (S("WA_PHONE_ID"), S("WA_TOKEN")) if what == "whatsapp" else (S("IG_USER_ID"), S("IG_TOKEN"))
            r = await http.get(f"{GRAPH}/{node}", headers={"Authorization": f"Bearer {tok}"}); r.raise_for_status()
        else:
            raise HTTPException(404)
        return {"ok": True, "msg": "Connected ✓"}
    except HTTPException: raise
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {str(e)[:200]}"}

def resolve_project(d):
    if d.get("new_project"):
        return create_project(d["new_project"])
    p = d.get("project")
    return p if p and p in slugs() else None

@app.post("/api/tasks")
async def new_task(req: Request):
    d = await req.json()
    if d.get("agent") not in QUEUES or not d.get("text"): raise HTTPException(400, "agent and text required")
    kind, meta_ = ("chore" if d["agent"] == "ollie" else "brief"), {}
    if d["agent"] == "iggy" and d.get("image_url"):
        kind, meta_ = "ig_post", {"image_url": d["image_url"]}
    t = add_task(d["agent"], kind, d["text"], d["text"], project=resolve_project(d), **meta_)
    if t.get("project"):
        brief_visit(t)
    return t

@app.post("/api/tasks/{tid}/approve")
async def approve(tid: int, req: Request):
    t = TASKS.get(tid)
    if not t or t["status"] != "review": raise HTTPException(404)
    t["output"] = (await req.json()).get("text") or t["output"]
    try: await SENDERS[t["kind"]](t); t["status"] = "done"
    except Exception as e: t["status"], t["output"] = "error", f"{t['output']}\n\n[send failed: {e}]"
    t["finished"] = time.time(); save_data()
    return t

@app.post("/api/tasks/{tid}/reject")
def reject(tid: int):
    if tid in TASKS:
        TASKS[tid]["status"], TASKS[tid]["finished"] = "rejected", time.time(); save_data()
    return {"ok": True}

@app.get("/api/chat/{who}")
def get_chat(who: str):
    return CHATS.get(who, [])

@app.post("/api/secretary")
async def secretary(req: Request):
    text = ((await req.json()).get("text") or "").strip()
    if not text: raise HTTPException(400, "text required")
    CHATS["sara"].append({"role": "user", "content": text, "ts": time.time()})
    team = "\n".join(f"- {k}: {a['name']}, {a['role']}" + (f" ({short(CUSTOM(k).get('prompt', ''), 90)})" if CUSTOM(k) else "")
                     for k, a in AGENTS.items() if k not in CHAT_ONLY)
    known = {s: meta(s)["name"] for s in slugs()}
    system = (ROUTER.replace("@TEAM@", team).replace("@PROJECTS@", "\n".join(f"- {s}: {n}" for s, n in known.items()) or "(none yet)")
              + f"\nPersonality: {S('PROMPT_sara')}\nBrand: {S('BRAND_PROFILE')}")
    AGENTS["sara"]["status"] = "working"
    try:
        plan = parse_json(await llm(system, history("sara"), 3000, json_mode=True, provider=brain_of("sara")))
    except Exception as e:
        CHATS["sara"].pop(); raise HTTPException(502, f"AI brain error: {e}")
    finally:
        AGENTS["sara"]["status"] = "idle"
    asg = [x for x in (plan.get("assignments") or []) if isinstance(x, dict) and x.get("agent") in QUEUES][:6]
    pj, proj = plan.get("project") or {}, None
    if asg:
        if isinstance(pj, dict) and pj.get("existing") in known:
            proj = pj["existing"]
        elif isinstance(pj, dict) and pj.get("new_name") and str(pj["new_name"]).lower() != "null":
            proj = create_project(str(pj["new_name"]), str(pj.get("description") or text[:200]))
    done = []
    for x in asg:
        if x["agent"] == "ollie" and x.get("action") in ("clean", "readme", "tidy"):
            t = await chore(x["action"], proj)
        else:
            t = add_task(x["agent"], "chore" if x["agent"] == "ollie" else "brief", str(x.get("title") or text),
                         str(x.get("brief") or text), project=proj, via="sara")
        done.append({"id": t["id"], "agent": t["agent"], "title": t["title"]})
    tasks = [TASKS[x["id"]] for x in done if TASKS[x["id"]]["status"] == "queued"]
    if tasks:
        visit("sara", "max", "handoff", ("sara", f"Max, the boss wants: {short(text, 60)}"),
              ("max", f"Got it! I'll brief {', '.join(nm(t['agent']) for t in tasks)} 👍"))
        for t in tasks:
            brief_visit(t)
    reply = str(plan.get("reply") or ("On it! ✨" if done else "Could you tell me a bit more about what you need?"))
    msg = {"role": "assistant", "content": reply, "ts": time.time(), "assigned": done,
           "project": meta(proj)["name"] if proj else None}
    CHATS["sara"].append(msg); save_data()
    return msg

@app.post("/api/pm")
async def project_manager(req: Request):
    text = ((await req.json()).get("text") or "").strip() or "What's happening right now? Give me a short summary."
    CHATS["max"].append({"role": "user", "content": text, "ts": time.time()})
    system = persona("max") + "\nAnswer from the live office status below. Be concise.\n\nLIVE OFFICE STATUS:\n" + status_text()
    AGENTS["max"]["status"] = "working"
    try:
        reply = await llm(system, history("max"), 3000, provider=brain_of("max"))
    except Exception as e:
        CHATS["max"].pop(); raise HTTPException(502, f"AI brain error: {e}")
    finally:
        AGENTS["max"]["status"] = "idle"
    msg = {"role": "assistant", "content": reply, "ts": time.time()}
    CHATS["max"].append(msg); save_data()
    return msg

@app.post("/api/officeboy")
async def officeboy(req: Request):
    d = await req.json()
    slug = d.get("project") or None
    if slug and slug not in slugs(): raise HTTPException(404, "no such project")
    if d.get("action") in ("clean", "readme", "tidy"):
        return await chore(d["action"], slug)
    if not d.get("text"): raise HTTPException(400, "text required")
    return add_task("ollie", "chore", d["text"], d["text"], project=slug)

# ---------- Projects / archive / board ----------
def project_info(slug, full=False):
    m = meta(slug)
    ts = [t for t in TASKS.values() if t.get("project") == slug]
    m.update(tasks=len(ts), counts=dict(Counter(t["status"] for t in ts)))
    if full:
        try: m["readme"] = open(ppath(slug, "README.md"), encoding="utf-8").read()
        except Exception: m["readme"] = ""
        m["files"] = tree(slug)
    return m

@app.get("/api/projects")
def projects():
    return sorted((project_info(s) for s in slugs()), key=lambda m: -m["created"])

@app.post("/api/projects")
async def new_project(req: Request):
    d = await req.json()
    if not (d.get("name") or "").strip(): raise HTTPException(400, "name required")
    return project_info(create_project(d["name"], d.get("description", ""), d.get("url", "")), True)

@app.get("/api/projects/{slug}")
def project(slug: str):
    if slug not in slugs(): raise HTTPException(404)
    return project_info(slug, True)

@app.patch("/api/projects/{slug}")
async def update_project(slug: str, req: Request):
    if slug not in slugs(): raise HTTPException(404)
    d, m = await req.json(), meta(slug)
    for k in ("name", "description", "url"):
        if k in d: m[k] = str(d[k])
    write_meta(slug, m)
    f = ppath(slug, "README.md")
    if os.path.exists(f) and "url" in d:
        cur = open(f, encoding="utf-8").read()
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(re.sub(r"^- \*\*URL:\*\* .*$", lambda _: f"- **URL:** {m['url'] or '—'}", cur, count=1, flags=re.M))
    return project_info(slug, True)

@app.get("/api/projects/{slug}/file")
def project_file(slug: str, path: str):
    p = ppath(slug, path)
    if not os.path.isfile(p): raise HTTPException(404)
    raw = open(p, "rb").read(400_000)
    try: return {"path": path, "content": raw.decode("utf-8"), "truncated": os.path.getsize(p) > 400_000}
    except UnicodeDecodeError: return {"path": path, "content": None, "binary": True}

@app.post("/api/projects/{slug}/reveal")
def reveal(slug: str):
    if slug not in slugs(): raise HTTPException(404)
    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", ppath(slug)])
    return {"ok": True}

@app.get("/api/board")
def board():
    keys = ("id", "agent", "title", "status", "ts", "started", "finished", "project", "hidden", "kind", "review")
    return {"projects": [{"slug": s, "name": meta(s)["name"]} for s in slugs()],
            "tasks": [{**{k: t.get(k) for k in keys}, "revision": t["meta"].get("revision", 0), "output": t["output"][:800]} for t in sorted(TASKS.values(), key=lambda t: -t["id"])]}

# ---------- Offices + custom agents + AI brain per agent ----------
SLOTS = {"mkt": 1, "cre": 2, "dev": 2}             # free desks in the built-in team offices
RESERVED = {"boss", "board", "calendar", "archive"}
AGENT_FIELDS = ("name", "role", "emoji", "item", "tone", "colors", "style", "hat", "hold", "scale", "prompt", "files")

@app.get("/api/team")
def team(): return TEAM

@app.post("/api/offices")
async def new_office(req: Request):
    d = await req.json()
    name = (d.get("name") or "").strip()[:40]
    if not name: raise HTTPException(400, "name required")
    oid, i = "o-" + slugify(name)[:24], 2
    while any(o["id"] == oid for o in TEAM["offices"]): oid, i = f"o-{slugify(name)[:24]}-{i}", i + 1
    TEAM["offices"].append({"id": oid, "name": name, "emoji": (d.get("emoji") or "🏢")[:4],
                            "colors": (d.get("colors") or ["#e3f2e1", "#d3ead0"])[:2]})
    save_team(); return TEAM["offices"][-1]

@app.delete("/api/offices/{oid}")
def del_office(oid: str):
    if any(a["office"] == oid for a in TEAM["agents"]): raise HTTPException(400, "Move or remove the people in this office first")
    TEAM["offices"] = [o for o in TEAM["offices"] if o["id"] != oid]; save_team(); return {"ok": True}

def free_slot(office, skip=None):
    cap = SLOTS.get(office, 4 if any(o["id"] == office for o in TEAM["offices"]) else 0)
    used = {a["slot"] for a in TEAM["agents"] if a["office"] == office and a["id"] != skip}
    return next((i for i in range(cap) if i not in used), None)

@app.post("/api/agents")
async def new_agent(req: Request):
    d = await req.json()
    name = (d.get("name") or "").strip()[:30]
    if not name or not (d.get("role") or "").strip(): raise HTTPException(400, "name and job title required")
    slot = free_slot(d.get("office"))
    if slot is None: raise HTTPException(400, "That office has no free desk. Pick another office or create a new one")
    aid, i = re.sub(r"[^a-z0-9]", "", name.lower())[:16] or "agent", 2
    base = aid
    while aid in AGENTS or aid in RESERVED: aid, i = f"{base}{i}", i + 1
    a = {k: d.get(k) for k in AGENT_FIELDS}
    a.update(id=aid, name=name, role=d["role"].strip()[:40], office=d["office"], slot=slot, files=bool(d.get("files")))
    TEAM["agents"].append(a)
    if d.get("provider") in PROVIDERS: TEAM["brains"][aid] = d["provider"]
    save_team()
    ROLES[aid] = (a["name"], a["role"], "custom", a.get("prompt") or "")
    if a["files"]: FILE_ROLES.add(aid)
    AGENTS[aid] = {"id": aid, "name": a["name"], "role": a["role"], "team": "custom", "status": "idle", "task": None, "done": 0}
    QUEUES[aid] = asyncio.Queue(); WORKERS[aid] = asyncio.create_task(worker(aid))
    return a

@app.patch("/api/agents/{aid}")
async def edit_agent(aid: str, req: Request):
    a = CUSTOM(aid)
    if not a: raise HTTPException(404)
    d = await req.json()
    if d.get("office") and d["office"] != a["office"]:
        slot = free_slot(d["office"], skip=aid)
        if slot is None: raise HTTPException(400, "That office has no free desk")
        a.update(office=d["office"], slot=slot)
    for k in AGENT_FIELDS:
        if k in d: a[k] = d[k]
    a["files"] = bool(a.get("files"))
    (FILE_ROLES.add if a["files"] else FILE_ROLES.discard)(aid)
    if "provider" in d:
        if d["provider"] in PROVIDERS: TEAM["brains"][aid] = d["provider"]
        else: TEAM["brains"].pop(aid, None)
    AGENTS[aid].update(name=a["name"], role=a["role"]); ROLES[aid] = (a["name"], a["role"], "custom", a.get("prompt") or "")
    save_team(); return a

@app.delete("/api/agents/{aid}")
def del_agent(aid: str):
    if not CUSTOM(aid): raise HTTPException(404, "Only agents you created can be removed")
    TEAM["agents"] = [a for a in TEAM["agents"] if a["id"] != aid]; TEAM["brains"].pop(aid, None); save_team()
    if aid in WORKERS: WORKERS.pop(aid).cancel()
    QUEUES.pop(aid, None); AGENTS.pop(aid, None); ROLES.pop(aid, None); FILE_ROLES.discard(aid)
    return {"ok": True}

@app.put("/api/brains")
async def set_brain(req: Request):
    d = await req.json()
    if d.get("agent") not in AGENTS: raise HTTPException(404)
    if d.get("provider") in PROVIDERS: TEAM["brains"][d["agent"]] = d["provider"]
    else: TEAM["brains"].pop(d["agent"], None)
    save_team(); return TEAM["brains"]

# Meta webhook (WhatsApp + Instagram) -> https://<your-host>/webhook
@app.get("/webhook")
def verify(request: Request):
    q = request.query_params
    if q.get("hub.verify_token") == S("WEBHOOK_VERIFY_TOKEN"): return Response(q.get("hub.challenge", ""))
    raise HTTPException(403)

@app.post("/webhook")
async def hook(req: Request):
    d = await req.json()
    for entry in d.get("entry", []):
        if d.get("object") == "whatsapp_business_account":
            for ch in entry.get("changes", []):
                for m in ch.get("value", {}).get("messages", []):
                    if m.get("type") == "text":
                        add_task("wally", "whatsapp", f"WA +{m['from'][-4:]}: {m['text']['body']}", m["text"]["body"], to=m["from"])
        elif d.get("object") == "instagram":
            for m in entry.get("messaging", []):
                msg = m.get("message", {})
                if msg.get("text") and not msg.get("is_echo"):
                    add_task("iggy", "ig_dm", f"DM: {msg['text']}", msg["text"], to=m["sender"]["id"])
            for ch in entry.get("changes", []):
                v = ch.get("value", {})
                if ch.get("field") == "comments" and v.get("from", {}).get("id") != S("IG_USER_ID"):
                    add_task("iggy", "ig_comment", f"Comment @{v.get('from', {}).get('username', '?')}: {v.get('text', '')}",
                             f"Reply to this Instagram comment: {v.get('text', '')}", comment_id=v["id"])
    return {"ok": True}

"""Source adapters for cc-viz.

Every adapter turns one agent tool's own on-disk history into the same
normalised session shape the dashboard draws, so the UI never needs to know
which tool produced a session:

    list(limit)  -> [{key, source, project, cwd, title, prompt, mtime, agents}]
    stamp(id)    -> cheap value that changes whenever the session changes
    detail(id)   -> {id, title, first_prompt, effort, cwd, start_ts, end_ts, last_activity,
                     ended_turn, ctx_window, cost_kind, main{calls,summary,spawns},
                     agents[...], total}

Adapters here: claude (wraps the original parser in server.py), codex, opencode.
To add another tool, write a class with those three methods and add it to
build_adapters(). Everything is read-only; stdlib only.
"""

import glob
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNING_MS = 60_000          # a session counts as running if it changed this recently
AGENT_RUNNING_MS = 90_000


# ---------------------------------------------------------------- config

def load_config():
    """Optional config.json next to server.py: prices and context windows.

    {"prices":  {"gpt-5.4": [input_per_M, output_per_M, cached_input_per_M]},
     "windows": {"gpt-5.4": 400000}}
    Keys match a model id by longest prefix.
    """
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


CONFIG = load_config()


def _by_prefix(table, model):
    best = None
    for k in table or {}:
        if model and model.startswith(k) and (best is None or len(k) > len(best)):
            best = k
    return table[best] if best else None


def window_for(model, peak, given=None):
    """Context window for a model: config, else what the log said, else 200k, grown if exceeded."""
    w = given or _by_prefix(CONFIG.get("windows"), model) or 200_000
    for step in (200_000, 400_000, 1_000_000, 2_000_000):
        if peak > w and step > w:
            w = step
    return int(max(w, peak))


# ---------------------------------------------------------------- helpers

def ts_ms(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def clip(s, n=6000):
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


def one_line(s, n=80):
    return " ".join((s or "").split())[:n]


def summarize(calls):
    s = {"calls": len(calls), "input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
         "cost": 0.0, "peak_ctx": 0, "unpriced": 0, "models": []}
    tools = {}
    for c in calls:
        for k in ("input", "cache_write", "cache_read", "output"):
            s[k] += c[k]
        if c["cost"] is None:
            s["unpriced"] += 1
        else:
            s["cost"] += c["cost"]
        s["peak_ctx"] = max(s["peak_ctx"], c["ctx"])
        if c["model"] and c["model"] not in s["models"]:
            s["models"].append(c["model"])
        for t in c["tools"]:
            tools[t] = tools.get(t, 0) + 1
    s["tools"] = dict(sorted(tools.items(), key=lambda kv: -kv[1]))
    s["tool_calls"] = sum(tools.values())
    s["final_ctx"] = calls[-1]["ctx"] if calls else 0
    return s


def totals(main_summary, agents):
    keys = ("input", "cache_write", "cache_read", "output", "cost", "calls", "tool_calls", "unpriced")
    return {k: main_summary[k] + sum(a["summary"][k] for a in agents) for k in keys}


def refresh_status(d, now_ms):
    """Cached details were computed at a past moment; re-derive the time-based flags."""
    d["running"] = now_ms - (d.get("last_activity") or 0) < RUNNING_MS
    for a in d["agents"]:
        if a["status"] == "running" and now_ms - (a.get("end_ts") or 0) > AGENT_RUNNING_MS:
            a["status"] = "done" if a.get("returned_ts") else "stopped"
    d["generated"] = int(now_ms)
    return d


def finish_detail(*, source, sid, title, first_prompt, effort, cwd, start_ts, main_calls, agents,
                  last_activity, ended_turn, window, cost_kind, spawns, main_model=None, prompts=None):
    ms = summarize(main_calls)
    for a in agents:
        a.setdefault("model", (a["summary"]["models"] or [a.get("requested_model")])[-1] or a.get("requested_model"))
        a["window"] = window_for(a["model"], a["summary"]["peak_ctx"], a.get("window"))
    model = main_model or (ms["models"][-1] if ms["models"] else None)
    ends = [(main_calls[-1]["ts"] if main_calls else None)] + [a["end_ts"] for a in agents]
    return {
        "source": source,
        "id": sid,
        "title": title,
        "first_prompt": clip(first_prompt, 600),
        "model": model,
        "effort": effort,
        "cwd": cwd,
        "start_ts": start_ts,
        "end_ts": max([e for e in ends if e] or [0]),
        "last_activity": last_activity,
        "ended_turn": bool(ended_turn),
        "ctx_window": window_for(model, ms["peak_ctx"], window),
        "cost_kind": cost_kind,
        "prompts": prompts or [],
        "main": {"calls": main_calls, "summary": ms, "spawns": spawns},
        "agents": sorted(agents, key=lambda x: x["spawn_ts"] or 0),
        "total": totals(ms, agents),
    }


def project_name(cwd, fallback=""):
    if cwd:
        b = os.path.basename(cwd.rstrip("/"))
        return b or cwd
    return fallback


# ---------------------------------------------------------------- claude

CLAUDE_BUILTIN = {"Explore", "Plan", "general-purpose", "statusline-setup", "claude-code-guide", "Task"}


class ClaudeSource:
    name = "claude"
    label = "claude-code"

    def __init__(self, projects, list_sessions, session_detail, read_jsonl):
        self.projects = projects
        self._list, self._detail, self._read = list_sessions, session_detail, read_jsonl
        self._cwd = {}
        self._act = {}

    def _path(self, sid):
        p = os.path.realpath(os.path.join(self.projects, sid + ".jsonl"))
        root = os.path.realpath(self.projects) + os.sep
        return p if p.startswith(root) and os.path.isfile(p) else None

    def _msg_ts(self, path):
        """Time (ms) of the newest user/assistant line in a transcript.

        File mtime is a poor activity signal: other tools (IDE extensions, sync, indexers)
        touch transcripts without anything happening. Cached by (mtime, size)."""
        try:
            st = os.stat(path)
        except OSError:
            return 0
        hit = self._act.get(path)
        if hit and hit[0] == (st.st_mtime, st.st_size):
            return hit[1]
        found = 0
        for window in (262144, 4194304):
            try:
                with open(path, "rb") as f:
                    f.seek(max(0, st.st_size - window))
                    tail = f.read().decode("utf-8", "replace")
            except OSError:
                break
            for line in reversed(tail.splitlines()):
                if '"timestamp"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") in ("user", "assistant"):
                    found = ts_ms(d.get("timestamp")) or 0
                    if found:
                        break
            if found or st.st_size <= window:
                break
        self._act[path] = ((st.st_mtime, st.st_size), found)
        return found

    def _activity(self, path):
        t = self._msg_ts(path)
        sub = os.path.join(os.path.dirname(path), os.path.basename(path)[:-6], "subagents")
        for f in glob.glob(os.path.join(sub, "agent-*.jsonl")):
            t = max(t, self._msg_ts(f))
        if not t:                       # nothing parseable: fall back to mtime
            try:
                t = int(os.path.getmtime(path) * 1000)
            except OSError:
                t = 0
        return t

    def _cwd_of(self, path):
        if path not in self._cwd:
            cwd = None
            try:
                with open(path, encoding="utf-8") as f:
                    for i, line in enumerate(f):
                        if i > 60:
                            break
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if d.get("cwd"):
                            cwd = d["cwd"]
                            break
            except OSError:
                pass
            self._cwd[path] = cwd
        return self._cwd[path]

    def _prompts(self, path):
        out = []
        for d in self._read(path):
            if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
                continue
            c = (d.get("message") or {}).get("content")
            if isinstance(c, list):
                if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
                    continue
                raw = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
            else:
                raw = c if isinstance(c, str) else ""
            if raw.lstrip().startswith(("<command-", "<local-command", "<system-reminder", "[Request interrupted")):
                continue
            text = re.sub(r"<[^>]+>", "", raw).strip()
            if text:
                out.append({"ts": ts_ms(d.get("timestamp")), "text": clip(text, 600)})
        return out

    def list(self, limit):
        out = []
        for e in self._list(self.projects, limit):
            path = self._path(e["key"])
            if not path:
                continue
            cwd = self._cwd_of(path)
            act = self._activity(path)
            out.append({"key": "claude:" + e["key"], "source": "claude", "project": project_name(cwd, e["project"]),
                        "cwd": cwd, "title": e["title"], "prompt": e["title"], "mtime": act, "agents": e["agents"]})
        return out

    def stamp(self, sid):
        p = self._path(sid)
        return self._activity(p) if p else None

    def detail(self, sid):
        path = self._path(sid)
        if not path:
            return None
        d = self._detail(path)
        main_calls = d["main"]["calls"]
        peak = d["main"]["summary"]["peak_ctx"]
        for a in d["agents"]:
            a["kind"] = "builtin" if a["type"] in CLAUDE_BUILTIN else "custom"
            a["nick"] = None
            a["model"] = (a["summary"]["models"] or [a.get("requested_model")])[-1] or a.get("requested_model")
            a["window"] = window_for(a["model"], a["summary"]["peak_ctx"])
        model = d["main"]["summary"]["models"][-1] if d["main"]["summary"]["models"] else None
        d.update({
            "source": "claude", "model": model,
            "last_activity": self._activity(path),
            "ended_turn": bool(d.get("ended_turn")),
            "ctx_window": window_for(model, peak),
            "cost_kind": "estimated",
            "prompts": self._prompts(path),
        })
        d["title"] = d.get("title") or one_line(d.get("first_prompt"), 90)
        return d


# ---------------------------------------------------------------- codex

CODEX_BUILTIN = {"default", "explorer", "worker", "awaiter"}
_SPAWN_TOOLS = {"spawn_agent"}


def _codex_brief(name, args):
    a = args
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            return one_line(a)
    if isinstance(a, dict):
        for k in ("title", "description", "cmd", "command", "code", "path", "query", "q", "message", "url", "target"):
            v = a.get(k)
            if isinstance(v, list):
                v = " ".join(str(x) for x in v)
            if isinstance(v, str) and v.strip():
                return one_line(v)
    return ""


def _codex_prompt(text):
    text = (text or "").strip()
    if re.match(r"^gAAAA[\w\-=]{40,}$", text):       # forked threads can carry an encrypted prompt
        return ""
    if "## My request:" in text:
        text = text.split("## My request:", 1)[1]
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


_CODEX_SKIP = ('"type":"reasoning"', '"type":"compacted"', '"type":"token_usage_record"',
               '"type":"function_call_output"', '"type":"custom_tool_call_output"', '"world_state"',
               '"role":"developer"', '"role":"user"', '"type":"thread_settings_applied"')


def parse_codex(path):
    st = {"meta": None, "calls": [], "first_prompt": None, "last_text": None, "first_ts": None,
          "last_ts": None, "window": None, "effort": None, "model": None, "done": False,
          "aborted": False, "final_msg": None, "spawns": [], "last_prompt": None, "prompts": []}
    cur_model = cur_effort = None
    pending = []
    prev_total = None
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return st
    with f:
        for line in f:
            head = line[:300]
            if any(s in head for s in _CODEX_SKIP):
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            ts = ts_ms(o.get("timestamp"))
            if ts:
                st["first_ts"] = ts if st["first_ts"] is None else min(st["first_ts"], ts)
                st["last_ts"] = ts if st["last_ts"] is None else max(st["last_ts"], ts)
            t, p = o.get("type"), o.get("payload") or {}
            if t == "session_meta":
                st["meta"] = {k: v for k, v in p.items() if k not in ("base_instructions", "dynamic_tools")}
            elif t == "turn_context":
                cur_model, cur_effort = p.get("model") or cur_model, p.get("effort") or cur_effort
                st["model"], st["effort"] = cur_model, cur_effort
            elif t == "event_msg":
                pt = p.get("type")
                if pt == "task_started":
                    st["window"] = p.get("model_context_window") or st["window"]
                    st["done"] = st["aborted"] = False
                elif pt == "task_complete":
                    st["done"] = True
                    if p.get("last_agent_message"):
                        st["final_msg"] = p["last_agent_message"]
                elif pt == "turn_aborted":
                    st["aborted"] = True
                elif pt == "item_completed":
                    item = p.get("item") or {}
                    kind = item.get("type")
                    text = " ".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
                    if kind == "UserMessage" and text.strip():
                        if st["first_prompt"] is None:
                            st["first_prompt"] = _codex_prompt(text)
                        st["last_prompt"] = _codex_prompt(text) or st["last_prompt"]
                        if _codex_prompt(text):
                            st["prompts"].append({"ts": ts, "text": clip(_codex_prompt(text), 600)})
                    elif kind == "AgentMessage" and text.strip():
                        st["last_text"] = text
                elif pt == "token_count":
                    info = p.get("info")
                    if not info:
                        continue
                    total = (info.get("total_token_usage") or {}).get("total_tokens")
                    if total is not None and total == prev_total:
                        continue
                    prev_total = total
                    st["window"] = info.get("model_context_window") or st["window"]
                    last = info.get("last_token_usage") or {}
                    inp = last.get("input_tokens", 0) or 0
                    cached = last.get("cached_input_tokens", 0) or 0
                    cw = last.get("cache_write_input_tokens", 0) or 0
                    out = last.get("output_tokens", 0) or 0
                    price = _by_prefix(CONFIG.get("prices"), cur_model)
                    cost = None
                    if price:
                        pin, pout, pread = price[0], price[1], (price[2] if len(price) > 2 else price[0])
                        cost = (max(inp - cached - cw, 0) * pin + cw * pin + cached * pread + out * pout) / 1e6
                    st["calls"].append({
                        "ts": ts, "model": cur_model, "tools": [n for n, _ in pending], "briefs": [b for _, b in pending],
                        "ctx": inp, "input": max(inp - cached - cw, 0), "cache_write": cw, "cache_read": cached,
                        "output": out, "est_out": False, "cost": cost})
                    pending = []
            elif t == "response_item":
                pt = p.get("type")
                if pt in ("function_call", "custom_tool_call"):
                    name = p.get("name") or "tool"
                    args = p.get("arguments") if pt == "function_call" else p.get("input")
                    pending.append((name, _codex_brief(name, args)))
                    if name in _SPAWN_TOOLS:
                        a = args
                        if isinstance(a, str):
                            try:
                                a = json.loads(a)
                            except ValueError:
                                a = {}
                        st["spawns"].append({"ts": ts, "args": a if isinstance(a, dict) else {}})
                elif pt == "message" and p.get("role") == "assistant":
                    text = " ".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict))
                    if text.strip():
                        st["last_text"] = text
    return st


class CodexSource:
    name = "codex"
    label = "codex"

    def __init__(self, root):
        self.root = root
        self.meta = {}       # path -> immutable first-line facts
        self.files = {}      # path -> (mtime_ms, size)
        self.by_id = {}
        self._scan_at = 0
        self._lock = threading.Lock()
        self._parsed = {}    # path -> (mtime, parsed)
        self._prompt = {}

    def _scan(self):
        if time.time() - self._scan_at < 2:
            return
        with self._lock:
            self._scan_at = time.time()
            files = glob.glob(os.path.join(self.root, "*", "*", "*", "rollout-*.jsonl"))
            self.files = {}
            for p in files:
                try:
                    stt = os.stat(p)
                except OSError:
                    continue
                self.files[p] = int(stt.st_mtime * 1000)
                if p not in self.meta:
                    m = {"id": None}
                    try:
                        with open(p, encoding="utf-8", errors="replace") as f:
                            o = json.loads(f.readline())
                        pl = o.get("payload") or {}
                        spawn = ((pl.get("source") or {}).get("subagent") or {}).get("thread_spawn") if isinstance(pl.get("source"), dict) else None
                        m = {"id": pl.get("id"), "parent": pl.get("parent_thread_id") or (spawn or {}).get("parent_thread_id"),
                             "cwd": pl.get("cwd"), "start": ts_ms(pl.get("timestamp")) or ts_ms(o.get("timestamp")),
                             "thread_source": pl.get("thread_source"), "nick": pl.get("agent_nickname") or (spawn or {}).get("agent_nickname"),
                             "role": pl.get("agent_role") or (spawn or {}).get("agent_role"),
                             "agent_path": pl.get("agent_path"), "originator": pl.get("originator"),
                             "forked": bool(pl.get("forked_from_id"))}
                    except (OSError, ValueError):
                        pass
                    self.meta[p] = m
            self.by_id = {m["id"]: p for p, m in self.meta.items() if m.get("id") and p in self.files}

    def _children(self, sid):
        return [p for p, m in self.meta.items() if m.get("parent") == sid and p in self.files]

    def _tree_files(self, sid, depth=0):
        path = self.by_id.get(sid)
        out = [path] if path else []
        if depth < 3:
            for c in self._children(sid):
                out += self._tree_files(self.meta[c]["id"], depth + 1)
        return out

    def _parse(self, path):
        mt = self.files.get(path) or int(os.path.getmtime(path) * 1000)
        c = self._parsed.get(path)
        if c and c[0] == mt:
            return c[1]
        st = parse_codex(path)
        if len(self._parsed) > 80:
            self._parsed.pop(next(iter(self._parsed)))
        self._parsed[path] = (mt, st)
        return st

    def _prompt_of(self, path):
        mt = self.files.get(path)
        c = self._prompt.get(path)
        if c and c[0] == mt:
            return c[1]
        prompt = None
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i > 80:
                        break
                    if '"UserMessage"' in line[:600]:
                        o = json.loads(line)
                        item = (o.get("payload") or {}).get("item") or {}
                        text = " ".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
                        prompt = _codex_prompt(text)
                        break
        except (OSError, ValueError):
            pass
        self._prompt[path] = (mt, prompt)
        return prompt

    def _tops(self):
        return [p for p, m in self.meta.items() if p in self.files and m.get("id") and not m.get("parent")
                and m.get("thread_source") not in ("subagent", "guardian_review")]

    def list(self, limit):
        self._scan()
        rows = []
        for p in self._tops():
            m = self.meta[p]
            files = self._tree_files(m["id"])
            act = max(self.files[f] for f in files)
            rows.append((act, p, m, len(files) - 1))
        rows.sort(key=lambda r: -r[0])
        out = []
        for act, p, m, n in rows[:limit]:
            prompt = self._prompt_of(p) or ""
            out.append({"key": "codex:" + m["id"], "source": "codex", "project": project_name(m.get("cwd")),
                        "cwd": m.get("cwd"), "title": one_line(prompt, 120) or m["id"][:8],
                        "prompt": one_line(prompt, 160), "mtime": act, "agents": n,
                        "automation": m.get("thread_source") == "automation"})
        return out

    def stamp(self, sid):
        self._scan()
        files = self._tree_files(sid)
        return max((self.files[f] for f in files), default=None)

    def detail(self, sid):
        self._scan()
        path = self.by_id.get(sid)
        if not path:
            return None
        main = self._parse(path)
        meta = self.meta[path]
        agents = []

        def collect(parent_key, parent_state, parent_id, depth):
            kids = sorted(self._children(parent_id), key=lambda p: self.meta[p].get("start") or 0)
            spawns = list(parent_state["spawns"])
            for kp in kids:
                km = self.meta[kp]
                ks = self._parse(kp)
                sp = None
                for s in spawns:
                    if s["ts"] and km.get("start") and s["ts"] - 2000 <= km["start"] and s not in taken_spawns:
                        sp = s
                        break
                if sp:
                    taken_spawns.append(sp)
                args = (sp or {}).get("args") or {}
                msg = args.get("message")
                if not msg and isinstance(args.get("items"), list):
                    msg = " ".join(i.get("text", "") for i in args["items"] if isinstance(i, dict))
                atype = args.get("agent_type") or km.get("role") or "default"
                now = time.time() * 1000
                act = self.files.get(kp, 0)
                if ks["done"] and not ks["aborted"]:
                    status = "done"
                elif now - act < AGENT_RUNNING_MS and not ks["aborted"]:
                    status = "running"
                else:
                    status = "stopped" if not ks["done"] else "done"
                report = ks["final_msg"] or ks["last_text"]
                returned = status == "done"
                s = summarize(ks["calls"])
                prompt = _codex_prompt(msg) or (ks["last_prompt"] if km.get("forked") else ks["first_prompt"]) or ks["first_prompt"] or ""
                a = {
                    "id": km["id"], "type": atype, "nick": km.get("nick"),
                    "kind": "builtin" if atype in CODEX_BUILTIN else "custom",
                    "description": (km.get("agent_path") or "").rstrip("/").split("/")[-1].replace("_", " ")
                    if (km.get("forked") and prompt == (main["first_prompt"] or "")) or not prompt
                    else one_line(prompt, 100),
                    "parent": parent_key, "depth": depth,
                    "background": False,
                    "spawn_ts": (sp or {}).get("ts") or km.get("start") or ks["first_ts"],
                    "start_ts": ks["first_ts"], "end_ts": ks["last_ts"],
                    "returned_ts": ks["last_ts"] if returned else None,
                    "returned_chars": len(report or "") if returned else None,
                    "status": status,
                    "requested_model": args.get("model") or ks["model"],
                    "effort": args.get("reasoning_effort") or ks["effort"],
                    "model": ks["model"] or args.get("model"),
                    "window": ks["window"],
                    "prompt": clip(prompt), "prompt_chars": len(prompt),
                    "report": clip(report),
                    "calls": ks["calls"], "summary": s,
                }
                agents.append(a)
                if depth < 3:
                    collect(km["id"], ks, km["id"], depth + 1)

        taken_spawns = []
        collect("main", main, sid, 1)
        spawns = [{"ts": s["ts"], "id": None, "type": (s["args"] or {}).get("agent_type"),
                   "description": one_line((s["args"] or {}).get("message"), 100)} for s in main["spawns"]]
        files = self._tree_files(sid)
        last_activity = max(self.files[f] for f in files)
        return finish_detail(
            source="codex", sid=sid, title=one_line(main["first_prompt"], 120) or sid[:8],
            first_prompt=main["first_prompt"] or "", effort=main["effort"], cwd=meta.get("cwd"),
            start_ts=meta.get("start") or main["first_ts"], main_calls=main["calls"], agents=agents,
            last_activity=last_activity, ended_turn=main["done"] and not main["aborted"],
            window=main["window"], cost_kind="configured" if CONFIG.get("prices") else "unpriced",
            spawns=spawns, main_model=main["model"], prompts=main["prompts"])


# ---------------------------------------------------------------- opencode

OC_BUILTIN = {"general", "explore", "build", "plan", "compaction", "title", "summary"}


def _oc_brief(tool, desc, cmd, fp, pat, url, name):
    for v in (desc, cmd, pat, url, name):
        if isinstance(v, str) and v.strip():
            return one_line(v)
    if isinstance(fp, str) and fp:
        return os.path.basename(fp)
    return ""


class OpenCodeSource:
    name = "opencode"
    label = "opencode"

    def __init__(self, db_path):
        self.db_path = db_path
        self._local = threading.local()

    def _conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            if not os.path.isfile(self.db_path):
                return None
            for uri in (f"file:{self.db_path}?mode=ro", f"file:{self.db_path}?immutable=1"):
                try:
                    c = sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False)
                    c.execute("pragma query_only=1")
                    c.execute("select 1 from session limit 1")
                    break
                except sqlite3.Error:
                    c = None
            self._local.c = c
        return c

    def _q(self, sql, args=()):
        c = self._conn()
        if c is None:
            return []
        try:
            return c.execute(sql, args).fetchall()
        except sqlite3.Error:
            self._local.c = None
            return []

    def list(self, limit):
        rows = self._q("""select s.id, s.title, s.directory, s.time_updated,
                                 (select count(*) from session c where c.parent_id = s.id)
                          from session s where s.parent_id is null and s.time_archived is null
                          order by s.time_updated desc limit ?""", (limit,))
        out = []
        for sid, title, cwd, upd, n in rows:
            out.append({"key": "opencode:" + sid, "source": "opencode", "project": project_name(cwd),
                        "cwd": cwd, "title": one_line(title, 120) or sid[:10], "prompt": one_line(title, 160),
                        "mtime": upd, "agents": n})
        return out

    def _tree_ids(self, sid, depth=0):
        ids = [sid]
        if depth < 3:
            for (c,) in self._q("select id from session where parent_id=?", (sid,)):
                ids += self._tree_ids(c, depth + 1)
        return ids

    def stamp(self, sid):
        ids = self._tree_ids(sid)
        r = self._q("select max(time_updated) from session where id in (%s)" % ",".join("?" * len(ids)), ids)
        base = r[0][0] if r and r[0][0] else None
        if base is None:
            return None
        m = self._q("select max(time_updated) from message where session_id in (%s)" % ",".join("?" * len(ids)), ids)
        return max(base, m[0][0] or 0) if m else base

    def _calls(self, sid):
        msgs = self._q("select id, time_created, data from message where session_id=? order by time_created, id", (sid,))
        tools = {}
        tasks = []
        rows = self._q("""select message_id, time_created, json_extract(data,'$.tool'), json_extract(data,'$.state.status'),
                 json_extract(data,'$.state.input.description'), json_extract(data,'$.state.input.command'),
                 json_extract(data,'$.state.input.filePath'), json_extract(data,'$.state.input.pattern'),
                 json_extract(data,'$.state.input.url'), json_extract(data,'$.state.input.name'),
                 json_extract(data,'$.state.input.subagent_type'),
                 case when json_extract(data,'$.tool')='task' then json_extract(data,'$.state.input.prompt') end,
                 json_extract(data,'$.state.metadata.sessionId'), json_extract(data,'$.state.metadata.model.modelID'),
                 json_extract(data,'$.state.time.start'), json_extract(data,'$.state.time.end'),
                 case when json_extract(data,'$.tool')='task' then length(json_extract(data,'$.state.output')) end,
                 case when json_extract(data,'$.tool')='task' then substr(json_extract(data,'$.state.output'),1,12000) end
                 from part where session_id=? and json_extract(data,'$.type')='tool' order by time_created""", (sid,))
        for (mid, tc, tool, status, desc, cmd, fp, pat, url, name, stype, prompt, csid, cmodel,
             t0, t1, olen, out) in rows:
            tools.setdefault(mid, []).append((tool or "tool", _oc_brief(tool, desc, cmd, fp, pat, url, name)))
            if tool == "task":
                tasks.append({"mid": mid, "tc": t0 or tc, "status": status, "desc": desc, "type": stype, "prompt": prompt,
                              "child": csid, "model": cmodel, "t1": t1, "olen": olen or 0, "out": out})
        calls = []
        variant = None
        in_progress = False
        for mid, tc, data in msgs:
            try:
                m = json.loads(data)
            except ValueError:
                continue
            if m.get("role") == "user":
                variant = (m.get("model") or {}).get("variant") or variant
                continue
            tk = m.get("tokens") or {}
            cache = tk.get("cache") or {}
            inp, cr, cw = tk.get("input", 0) or 0, cache.get("read", 0) or 0, cache.get("write", 0) or 0
            out = (tk.get("output", 0) or 0) + (tk.get("reasoning", 0) or 0)
            tm = m.get("time") or {}
            if not tm.get("completed"):
                in_progress = True
            variant = m.get("variant") or variant
            if inp + cr + cw + out == 0:
                continue
            ts_t = tools.get(mid, [])
            calls.append({"ts": tm.get("created") or tc, "end": tm.get("completed"), "model": m.get("modelID"),
                          "tools": [n for n, _ in ts_t], "briefs": [b for _, b in ts_t], "ctx": inp + cr + cw,
                          "input": inp, "cache_write": cw, "cache_read": cr, "output": out, "est_out": False,
                          "cost": float(m.get("cost") or 0.0), "finish": m.get("finish")})
        return calls, tasks, variant, in_progress

    def _first_last_text(self, sid):
        a = self._q("""select json_extract(p.data,'$.text') from part p join message m on m.id=p.message_id
                       where p.session_id=? and json_extract(m.data,'$.role')='user' and json_extract(p.data,'$.type')='text'
                       order by p.time_created limit 1""", (sid,))
        b = self._q("""select json_extract(p.data,'$.text') from part p join message m on m.id=p.message_id
                       where p.session_id=? and json_extract(m.data,'$.role')='assistant' and json_extract(p.data,'$.type')='text'
                       order by p.time_created desc limit 1""", (sid,))
        return (a[0][0] if a else "") or "", (b[0][0] if b else "") or ""

    def detail(self, sid):
        r = self._q("select id,title,directory,agent,model,time_created,time_updated from session where id=?", (sid,))
        if not r:
            return None
        _, title, cwd, agent, model_json, created, updated = r[0]
        try:
            mj = json.loads(model_json) if model_json else {}
        except ValueError:
            mj = {}
        main_calls, tasks, variant, in_prog = self._calls(sid)
        first, _ = self._first_last_text(sid)
        prompts = [{"ts": t, "text": clip(x or "", 600)} for t, x in self._q("""select m.time_created, substr(json_extract(p.data,'$.text'),1,600)
                   from part p join message m on m.id=p.message_id
                   where p.session_id=? and json_extract(m.data,'$.role')='user' and json_extract(p.data,'$.type')='text'
                     and coalesce(json_extract(p.data,'$.synthetic'),0)=0
                   order by p.time_created""", (sid,)) if (x or "").strip()]
        agents = []
        seen = set()
        now = time.time() * 1000

        def add_children(parent_key, parent_sid, parent_tasks, depth):
            by_child = {t["child"]: t for t in parent_tasks if t["child"]}
            kids = [c for (c,) in self._q("select id from session where parent_id=? order by time_created", (parent_sid,))]
            for cid in kids:
                if cid in seen:
                    continue
                seen.add(cid)
                cr = self._q("select agent, time_created, time_updated, title from session where id=?", (cid,))
                if not cr:
                    continue
                cagent, ccreated, cupdated, ctitle = cr[0]
                calls, ctasks, cvariant, cprog = self._calls(cid)
                cfirst, clast = self._first_last_text(cid)
                t = by_child.get(cid) or {}
                ends = [c.get("end") or c["ts"] for c in calls]
                end_ts = max(ends) if ends else cupdated
                tstatus = t.get("status")
                if tstatus == "completed":
                    status = "done"
                elif tstatus in ("running", "pending") and now - cupdated < AGENT_RUNNING_MS * 4:
                    status = "running"
                elif tstatus == "error":
                    status = "stopped"
                elif now - cupdated < AGENT_RUNNING_MS and cprog:
                    status = "running"
                else:
                    status = "done" if (calls and calls[-1].get("finish") == "stop") else "stopped"
                out = t.get("out") or ""
                m = re.search(r"<task_result>(.*?)(</task_result>|$)", out, re.S)
                report = (m.group(1).strip() if m else clast) or clast
                atype = t.get("type") or cagent or "general"
                prompt = t.get("prompt") or cfirst
                agents.append({
                    "id": cid, "type": atype, "nick": None, "kind": "builtin" if atype in OC_BUILTIN else "custom",
                    "description": one_line(t.get("desc") or ctitle, 100) or cid[:10],
                    "parent": parent_key, "depth": depth, "background": False,
                    "spawn_ts": t.get("tc") or ccreated, "start_ts": calls[0]["ts"] if calls else ccreated, "end_ts": end_ts,
                    "returned_ts": t.get("t1") if tstatus == "completed" else None,
                    "returned_chars": t.get("olen") if tstatus == "completed" else None,
                    "status": status, "requested_model": t.get("model"),
                    "effort": cvariant if cvariant != "default" else None,
                    "model": (calls[-1]["model"] if calls else t.get("model")),
                    "window": None,
                    "prompt": clip(prompt), "prompt_chars": len(prompt or ""), "report": clip(report),
                    "calls": calls, "summary": summarize(calls),
                })
                if depth < 3:
                    add_children(cid, cid, ctasks, depth + 1)

        add_children("main", sid, tasks, 1)
        spawns = [{"ts": t["tc"], "id": t["child"], "type": t["type"], "description": one_line(t["desc"], 100)} for t in tasks]
        ids = [sid] + [a["id"] for a in agents]
        last = self._q("select max(time_updated) from session where id in (%s)" % ",".join("?" * len(ids)), ids)
        last_activity = max([updated] + ([last[0][0]] if last and last[0][0] else []))
        ended = bool(main_calls) and main_calls[-1].get("finish") in ("stop", "end_turn") and not in_prog
        return finish_detail(
            source="opencode", sid=sid, title=one_line(title, 120), first_prompt=first or title, effort=(variant if variant != "default" else None) or mj.get("variant"),
            cwd=cwd, start_ts=created, main_calls=main_calls, agents=agents, last_activity=last_activity,
            ended_turn=ended, window=None, cost_kind="reported", spawns=spawns, main_model=mj.get("id") or (main_calls[-1]["model"] if main_calls else None), prompts=prompts)


# ---------------------------------------------------------------- registry + caches

class Hub:
    """Merges adapters, caches parsed sessions, and prepares card summaries in the background."""

    def __init__(self, adapters):
        self.adapters = {a.name: a for a in adapters}
        self._detail = {}
        self._summary = {}
        self._locks = {}
        self._queue = []
        self._qlock = threading.Lock()
        self._wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    def split(self, key):
        src, _, sid = key.partition(":")
        return self.adapters.get(src), sid

    def detail(self, key):
        ad, sid = self.split(key)
        if not ad or not sid:
            return None
        lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            stamp = ad.stamp(sid)
            c = self._detail.get(key)
            if c and stamp is not None and c[0] == stamp:
                d = c[1]
            else:
                d = ad.detail(sid)
                if d is None:
                    return None
                d["key"] = key
                if len(self._detail) > 60:
                    self._detail.pop(next(iter(self._detail)))
                self._detail[key] = (stamp, d)
        return refresh_status(dict(d), time.time() * 1000)

    @staticmethod
    def summary_of(d):
        calls = list(d["main"]["calls"])
        for a in d["agents"]:
            calls += a["calls"]
        t0 = d["start_ts"] or (calls[0]["ts"] if calls else 0)
        t1 = max(d["end_ts"] or 0, d.get("last_activity") or 0, t0 + 1)
        spark = [0] * 14
        for c in calls:
            if c["ts"]:
                i = int((c["ts"] - t0) / (t1 - t0) * 14)
                spark[min(max(i, 0), 13)] += 1
        tot = d["total"]
        return {
            "model": d["model"], "effort": d["effort"], "ctx": d["main"]["summary"]["final_ctx"],
            "window": d["ctx_window"], "cost": tot["cost"], "cost_known": tot["calls"] == 0 or tot["unpriced"] < tot["calls"],
            "calls": tot["calls"], "agents_total": len(d["agents"]),
            "agents_running": sum(1 for a in d["agents"] if a["status"] == "running"),
            "start_ts": t0, "end_ts": d["end_ts"], "last_activity": d.get("last_activity"),
            "ended_turn": d["ended_turn"], "spark": spark, "tokens_in": tot["input"] + tot["cache_read"] + tot["cache_write"],
            "cost_kind": d["cost_kind"],
        }

    def _worker(self):
        while True:
            self._wake.wait()
            while True:
                with self._qlock:
                    if not self._queue:
                        self._wake.clear()
                        break
                    key, stamp = self._queue.pop(0)
                try:
                    d = self.detail(key)
                    if d:
                        self._summary[key] = (stamp, self.summary_of(d))
                except Exception:       # a half-written log must not kill the worker
                    self._summary[key] = (stamp, None)

    def overview(self, limit=30):
        rows = []
        for ad in self.adapters.values():
            try:
                rows += ad.list(limit)
            except Exception:
                pass
        rows.sort(key=lambda r: -(r["mtime"] or 0))
        rows = rows[:limit]
        now = time.time() * 1000
        for r in rows:
            c = self._summary.get(r["key"])
            if c and c[1]:
                r["summary"] = dict(c[1])
                r["summary"]["agents_running"] = r["summary"]["agents_running"] if now - (r["mtime"] or 0) < AGENT_RUNNING_MS else 0
            if not c or c[0] != r["mtime"]:
                with self._qlock:
                    if not any(q[0] == r["key"] for q in self._queue):
                        self._queue.append((r["key"], r["mtime"]))
                self._wake.set()
            r["running"] = now - (r["mtime"] or 0) < RUNNING_MS
        return rows

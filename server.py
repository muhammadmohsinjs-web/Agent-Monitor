#!/usr/bin/env python3
"""Claude Code agent viewer.

Reads the session transcripts Claude Code writes under ~/.claude/projects and
serves a dashboard showing how the main session delegated work to subagents,
with per-agent tokens, context size, timing and estimated cost.

Usage:  python3 server.py [--port 8765] [--projects ~/.claude/projects]
Stdlib only. Read-only: it never writes to the transcripts.
"""

import argparse
import glob
import json
import os
import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import sources

HERE = os.path.dirname(os.path.abspath(__file__))

# $ per million tokens: (input, output, cache_read). Cache writes are priced
# from input: 1.25x for the 5-minute cache, 2x for the 1-hour cache.
# First-party API list prices; matched by longest model-id prefix.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}


def price_for(model):
    best = None
    for prefix in PRICES:
        if model and model.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return PRICES.get(best)


def cost_of(model, u):
    p = price_for(model)
    if not p:
        return None
    pin, pout, pread = p
    cc = u.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
    w5m = (u.get("cache_creation_input_tokens", 0) or 0) - w1h
    return (
        (u.get("input_tokens", 0) or 0) * pin
        + w5m * pin * 1.25
        + w1h * pin * 2.0
        + (u.get("cache_read_input_tokens", 0) or 0) * pread
        + (u.get("output_tokens", 0) or 0) * pout
    ) / 1e6


def ts_ms(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for c in content or []:
        if isinstance(c, dict):
            if c.get("type") == "text":
                out.append(c.get("text", ""))
            elif c.get("type") == "tool_result":
                out.append(text_of(c.get("content")))
    return "\n".join(out)


def tool_brief(name, inp):
    """A few words describing one tool call, for the session log."""
    for k in ("description", "file_path", "notebook_path", "pattern", "query", "url", "command", "skill", "prompt"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            if k in ("file_path", "notebook_path"):
                v = os.path.basename(v)
            v = " ".join(v.split())
            return v[:80]
    return ""


def read_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass  # a line still being written
    except OSError:
        pass
    return rows


def parse_transcript(path):
    """One transcript -> API calls, tool uses, tool results and summary text."""
    rows = read_jsonl(path)
    calls = {}  # message id -> call
    order = []
    tool_uses = {}
    tool_results = {}
    agent_messages = {}  # async subagent hand-backs, by agent id
    title = None
    first_prompt = None
    last_text = None
    last_stop = None
    first_ts = last_ts = None
    effort = cwd = None

    for d in rows:
        t = d.get("type")
        ts = ts_ms(d.get("timestamp"))
        if ts and not d.get("isMeta"):
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        if d.get("effort"):
            effort = d["effort"]
        if d.get("cwd") and not cwd:
            cwd = d["cwd"]
        if t == "ai-title":
            title = d.get("aiTitle")
        elif t == "queue-operation" and d.get("operation") == "enqueue":
            m = re.search(r'<agent-message from="([^"]+)"', d.get("content") or "")
            if m:
                agent_messages.setdefault(m.group(1), {"ts": ts, "chars": len(d.get("content") or "")})
        elif t == "user":
            msg = d.get("message") or {}
            content = msg.get("content")
            if first_prompt is None and not d.get("isMeta") and not text_of(content).lstrip().startswith(("<command-", "<local-command")) and not (
                isinstance(content, list) and any(c.get("type") == "tool_result" for c in content if isinstance(c, dict))
            ):
                first_prompt = text_of(content)
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        tool_results[c.get("tool_use_id")] = {
                            "ts": ts,
                            "chars": len(text_of(c.get("content"))),
                            "is_error": bool(c.get("is_error")),
                            "meta": d.get("toolUseResult") if isinstance(d.get("toolUseResult"), dict) else None,
                        }
        elif t == "assistant":
            msg = d.get("message") or {}
            model = msg.get("model")
            if model == "<synthetic>":
                continue
            mid = msg.get("id") or d.get("uuid")
            call = calls.get(mid)
            if call is None:
                call = calls[mid] = {"id": mid, "ts": ts, "model": model, "usage": {}, "tools": [], "briefs": [], "chars": 0, "final": False}
                order.append(mid)
            for k, v in (msg.get("usage") or {}).items():
                if isinstance(v, (int, float)):
                    call["usage"][k] = max(call["usage"].get(k, 0), v)
                elif k == "cache_creation" and isinstance(v, dict):
                    cc = call["usage"].setdefault("cache_creation", {})
                    for kk, vv in v.items():
                        cc[kk] = max(cc.get(kk, 0), vv or 0)
            call["end_ts"] = ts
            if msg.get("stop_reason"):
                last_stop = msg["stop_reason"]
                call["final"] = True
            for c in msg.get("content") or []:
                call["chars"] += len(c.get("text") or c.get("thinking") or "") + (len(json.dumps(c.get("input"))) if c.get("type") == "tool_use" else 0)
                if c.get("type") == "tool_use":
                    call["tools"].append(c.get("name"))
                    call["briefs"].append(tool_brief(c.get("name"), c.get("input") or {}))
                    tool_uses[c.get("id")] = {"name": c.get("name"), "input": c.get("input") or {}, "ts": ts}
                elif c.get("type") == "text" and c.get("text", "").strip():
                    last_text = c["text"]

    out_calls = []
    for mid in order:
        c = calls[mid]
        u = dict(c["usage"])
        # Claude Code often stores the usage from the start of the stream, before the
        # final output count arrives. For those calls, estimate output from what was
        # actually written (~3.5 chars per token); hidden thinking can't be counted.
        est_out = False
        if not c["final"]:
            guess = int(c["chars"] / 3.5)
            if guess > (u.get("output_tokens") or 0):
                u["output_tokens"] = guess
                est_out = True
        ctx = (u.get("input_tokens", 0) or 0) + (u.get("cache_creation_input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0)
        out_calls.append({
            "ts": c["ts"], "model": c["model"], "tools": c["tools"], "briefs": c["briefs"], "ctx": ctx,
            "input": u.get("input_tokens", 0) or 0,
            "cache_write": u.get("cache_creation_input_tokens", 0) or 0,
            "cache_read": u.get("cache_read_input_tokens", 0) or 0,
            "output": u.get("output_tokens", 0) or 0,
            "est_out": est_out,
            "cost": cost_of(c["model"], u),
        })
    return {
        "title": title, "calls": out_calls, "tool_uses": tool_uses, "tool_results": tool_results,
        "agent_messages": agent_messages, "first_prompt": first_prompt, "last_text": last_text,
        "last_stop": last_stop, "first_ts": first_ts, "last_ts": last_ts,
        "effort": effort, "cwd": cwd,
    }


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


def clip(s, n=6000):
    if s is None:
        return None
    return s if len(s) <= n else s[:n] + f"\n… [{len(s) - n:,} more characters]"


def session_detail(path):
    session_id = os.path.basename(path)[:-6]
    main = parse_transcript(path)
    sub_dir = os.path.join(os.path.dirname(path), session_id, "subagents")
    now = time.time() * 1000

    agents = []
    for f in sorted(glob.glob(os.path.join(sub_dir, "agent-*.jsonl"))):
        aid = os.path.basename(f)[len("agent-"):-len(".jsonl")]
        meta = {}
        try:
            with open(f[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as mf:
                meta = json.load(mf)
        except (OSError, ValueError):
            pass
        t = parse_transcript(f)
        agents.append({"id": aid, "meta": meta, "t": t, "mtime": os.path.getmtime(f) * 1000})

    # Who spawned whom: find the Agent tool_use that owns each toolUseId.
    owners = {"main": main}
    owners.update({a["id"]: a["t"] for a in agents})

    out_agents = []
    for a in agents:
        meta, t = a["meta"], a["t"]
        tuid = meta.get("toolUseId")
        parent, spawn = "main", None
        for oid, ot in owners.items():
            if tuid and tuid in ot["tool_uses"]:
                parent, spawn = oid, ot["tool_uses"][tuid]
                break
        inp = (spawn or {}).get("input", {})
        ptr = owners[parent]
        result = ptr["tool_results"].get(tuid) if tuid else None
        rmeta = (result or {}).get("meta") or {}
        is_async = rmeta.get("isAsync") or meta.get("requestShape") == "background"

        returned_ts = returned_chars = None
        if rmeta.get("status") == "completed" or (result and not is_async):
            returned_ts, returned_chars = result["ts"], result["chars"]
        elif a["id"] in ptr["agent_messages"]:
            m = ptr["agent_messages"][a["id"]]
            returned_ts, returned_chars = m["ts"], m["chars"]
        else:
            for oid, ot in owners.items():
                if a["id"] in ot["agent_messages"]:
                    m = ot["agent_messages"][a["id"]]
                    returned_ts, returned_chars = m["ts"], m["chars"]

        if returned_ts:
            status = "done"
        elif now - a["mtime"] < 90_000:
            status = "running"
        elif t["last_stop"] == "end_turn":
            status = "done"
        else:
            status = "stopped"

        s = summarize(t["calls"])
        out_agents.append({
            "id": a["id"],
            "type": meta.get("agentType") or inp.get("subagent_type") or "general-purpose",
            "description": meta.get("description") or inp.get("description") or a["id"],
            "parent": parent,
            "depth": meta.get("spawnDepth") or 1,
            "background": bool(is_async),
            "spawn_ts": (spawn or {}).get("ts") or t["first_ts"],
            "start_ts": t["first_ts"],
            "end_ts": t["last_ts"],
            "returned_ts": returned_ts,
            "returned_chars": returned_chars,
            "status": status,
            "requested_model": inp.get("model") or rmeta.get("resolvedModel"),
            "effort": t["effort"],
            "prompt": clip(inp.get("prompt") or t["first_prompt"]),
            "prompt_chars": len(inp.get("prompt") or t["first_prompt"] or ""),
            "report": clip(((rmeta.get("handbackReport") or {}).get("text")) or t["last_text"]),
            "calls": t["calls"],
            "summary": s,
        })

    # Main session's tool calls on the timeline, and its spawns.
    spawns = [{"ts": tu["ts"], "id": tid, "type": tu["input"].get("subagent_type"),
               "description": tu["input"].get("description")}
              for tid, tu in main["tool_uses"].items() if tu["name"] in ("Agent", "Task")]

    ms = summarize(main["calls"])
    total = {k: ms[k] + sum(x["summary"][k] for x in out_agents) for k in ("input", "cache_write", "cache_read", "output", "cost", "calls", "tool_calls", "unpriced")}
    ends = [main["last_ts"]] + [x["end_ts"] for x in out_agents]
    return {
        "id": session_id,
        "title": main["title"],
        "first_prompt": clip(main["first_prompt"], 600),
        "effort": main["effort"],
        "cwd": main["cwd"],
        "running": now - os.path.getmtime(path) * 1000 < 60_000,
        "ended_turn": main["last_stop"] == "end_turn",
        "start_ts": main["first_ts"],
        "end_ts": max([e for e in ends if e] or [0]),
        "main": {"calls": main["calls"], "summary": ms, "spawns": spawns},
        "agents": sorted(out_agents, key=lambda x: x["spawn_ts"] or 0),
        "total": total,
        "generated": int(now),
    }


_list_cache = {}


def list_sessions(projects, limit):
    files = glob.glob(os.path.join(projects, "*", "*.jsonl"))
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    out = []
    for p in files[:limit]:
        mt = os.path.getmtime(p)
        cached = _list_cache.get(p)
        if not cached or cached[0] != mt:
            title, prompt = None, None
            for d in read_jsonl(p):
                if d.get("type") == "ai-title":
                    title = d.get("aiTitle")
                elif prompt is None and d.get("type") == "user" and not d.get("isMeta"):
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
                        continue
                    txt = re.sub(r"<[^>]+>", "", text_of(c)).strip()
                    if txt and not text_of(c).lstrip().startswith(("<command-", "<local-command")):
                        prompt = txt[:120]
            cached = _list_cache[p] = (mt, title, prompt)
        sid = os.path.basename(p)[:-6]
        sub = os.path.join(os.path.dirname(p), sid, "subagents")
        n = len(glob.glob(os.path.join(sub, "agent-*.jsonl")))
        project = os.path.basename(os.path.dirname(p))
        out.append({
            "key": f"{project}/{sid}",
            "project": project,
            "title": cached[1] or cached[2] or sid[:8],
            "mtime": int(mt * 1000),
            "agents": n,
        })
    return out


def make_handler(projects, hub):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def page(self, name):
            with open(os.path.join(HERE, name), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                # old links like /?key=<project>/<session> keep opening the swarm view
                return self.page("index.html" if "key" in q else "dashboard.html")
            pages = {"/swarm": "index.html", "/classic": "classic.html", "/detailed": "detailed.html"}
            if u.path in pages:
                return self.page(pages[u.path])
            if u.path == "/api/health":
                return self.send(200, {"ok": True, "sources": sorted(hub.adapters)})
            if u.path == "/api/overview":
                return self.send(200, {"sessions": hub.overview(int(q.get("limit", ["30"])[0])),
                                       "sources": sorted(hub.adapters), "now": int(time.time() * 1000)})
            if u.path == "/api/v2/session":
                d = hub.detail(q.get("key", [""])[0])
                return self.send(200, d) if d else self.send(404, {"error": "session not found"})
            # original Claude-only API, kept for the classic / detailed / swarm pages
            if u.path == "/api/sessions":
                return self.send(200, list_sessions(projects, int(q.get("limit", ["150"])[0])))
            if u.path == "/api/session":
                key = q.get("key", [""])[0]
                path = os.path.realpath(os.path.join(projects, key + ".jsonl"))
                if not path.startswith(os.path.realpath(projects) + os.sep) or not os.path.isfile(path):
                    return self.send(404, {"error": "session not found"})
                return self.send(200, session_detail(path))
            self.send(404, {"error": "not found"})

    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"), help="Claude Code transcripts")
    ap.add_argument("--codex", default=os.path.expanduser("~/.codex/sessions"), help="Codex rollout logs")
    ap.add_argument("--opencode", default=os.path.expanduser("~/.local/share/opencode/opencode.db"), help="OpenCode database")
    args = ap.parse_args()
    adapters = [sources.ClaudeSource(args.projects, list_sessions, session_detail, read_jsonl)]
    if os.path.isdir(args.codex):
        adapters.append(sources.CodexSource(args.codex))
    if os.path.isfile(args.opencode):
        adapters.append(sources.OpenCodeSource(args.opencode))
    hub = sources.Hub(adapters)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.projects, hub))
    print(f"Agent monitor ({', '.join(a.label for a in adapters)}): http://127.0.0.1:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()

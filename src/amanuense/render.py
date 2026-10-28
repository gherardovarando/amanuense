"""Counts for the summary line, and the JSON output. Readable layouts are templates (templates.py)."""
import json
from collections import Counter
from datetime import datetime


_FIELDS = ("uid", "folder", "folder_label", "messageId", "from", "subject", "date", "attachments", "expires", "handled",
           "outside_window", "skip", "error", "existing", "tag_actions", "task", "earlier", "event_status",
           "event_other")
_WAIT_FIELDS = ("uid", "to", "subject", "date", "days", "task")


def stats(msgs):
    done = [m for m in msgs if "ai" in m]
    return {
        "messages": len(msgs),
        "outside_window": sum(1 for m in msgs if m.get("outside_window")),
        "triaged": len(done),
        "reused": sum(1 for m in done if m.get("ai_reused")),
        "events": sum(1 for m in done if (m["ai"].get("event") or {}).get("date", "") >= f"{datetime.now():%Y-%m-%d}"),
        "events_added": sum(1 for m in msgs if m.get("event_status") == "added"),
        "events_merged": sum(1 for m in msgs if m.get("event_status") == "merged"),
        "todos": sum(1 for m in done if m["ai"]["needs_action"] and not m["handled"]),
        "skipped": sum(1 for m in msgs if m.get("skip")),
        "sensitive": sum(1 for m in msgs if m.get("sensitive")),
        "errors": sum(1 for m in msgs if m.get("error")),
        "tagged": sum(1 for m in done if any(a == "add" for a, _ in m.get("tag_actions") or [])),
        "tasks": sum(1 for m in done if m.get("task") in ("added", "would add")),
        "categories": dict(Counter(m["ai"]["category"] for m in done)),
        "expiring": sum(1 for m in msgs if m.get("expires")),
    }


def to_json(cfg, msgs, opts, elapsed, task_error=None, waits=()):
    out = []
    for m in msgs:
        r = {k: m.get(k) for k in _FIELDS if k in m}
        r.update(m.get("ai") or {})
        if "sensitive" in m:
            r["sensitive"] = m["sensitive"]  # the model's answer or the config rules
        out.append(r)
    return json.dumps({"window": [opts.start.isoformat(), opts.end.isoformat()], "model": cfg["model"]["name"],
                       "unread": opts.unread, "tagging": opts.tag, "dry_run": opts.dry_run,
                       "stats": stats(msgs), "elapsed_s": round(elapsed), "task_error": task_error,
                       "problems": opts.problems, "messages": out,
                       "waiting": [{k: s.get(k) for k in _WAIT_FIELDS}
                                   | (s.get("waiting") or {}) for s in waits]},
                      ensure_ascii=False, indent=1, default=str) + "\n"


def from_json(text, cfg):
    """Read the JSON output back (e.g. after a jq filter), as the saved results of a run.
    Returns a dict like last-digest.json: start, end, unread, tag, dry_run, problems, elapsed,
    task_error, msgs, waits. Raises ValueError if it is not amanuense JSON."""
    data = json.loads(text)
    if not isinstance(data, dict) or "messages" not in data or "window" not in data:
        raise ValueError("no window or messages; make it with -t json")
    msgs = []
    for r in data["messages"]:
        m = {k: r[k] for k in _FIELDS if k in r}
        if "sensitive" in r:
            m["sensitive"] = r["sensitive"]
        ai = {k: v for k, v in r.items() if k not in _FIELDS}
        if ai.get("category"):  # triaged; a category missing from this config is added at the end
            if ai["category"] not in cfg["categories"]:
                cfg["categories"].append(ai["category"])
            m["ai"] = ai
        msgs.append(m)
    waits = []
    for w in data.get("waiting") or []:
        s = {k: w.get(k) for k in _WAIT_FIELDS}
        s["waiting"] = {k: v for k, v in w.items() if k not in _WAIT_FIELDS}
        waits.append(s)
    return {"start": data["window"][0], "end": data["window"][1], "unread": data.get("unread", True),
            "tag": data.get("tagging", False), "dry_run": data.get("dry_run", False),
            "problems": data.get("problems") or [], "elapsed": data.get("elapsed_s") or 0,
            "task_error": data.get("task_error"), "msgs": msgs, "waits": waits}

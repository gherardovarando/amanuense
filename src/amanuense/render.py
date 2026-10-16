"""Counts for the summary line, and the JSON output. Readable layouts are templates (templates.py)."""
import json
from collections import Counter


_FIELDS = ("uid", "folder", "from", "subject", "date", "expires", "handled", "outside_window", "skip", "error",
           "existing", "tag_actions", "task")


def stats(msgs):
    done = [m for m in msgs if "ai" in m]
    return {
        "messages": len(msgs),
        "outside_window": sum(1 for m in msgs if m.get("outside_window")),
        "triaged": len(done),
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
                       "stats": stats(msgs), "elapsed_s": round(elapsed), "task_error": task_error,
                       "problems": opts.problems, "messages": out,
                       "waiting": [{k: s.get(k) for k in ("uid", "to", "subject", "date", "days", "task")}
                                   | (s.get("waiting") or {}) for s in waits]},
                      ensure_ascii=False, indent=1, default=str) + "\n"

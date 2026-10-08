"""Digest output: Markdown for reading, JSON for other tools."""
import json
from collections import Counter
from datetime import datetime

from .triage import parse_date, short_from
from .window import describe

_FIELDS = ("uid", "folder", "from", "subject", "date", "expires", "handled", "outside_window", "skip", "error",
           "existing", "tag_actions", "task")


def days_left(m):
    return (datetime.fromisoformat(m["expires"]) - datetime.now().astimezone()).days


def stats(msgs):
    done = [m for m in msgs if "ai" in m]
    return {
        "messages": len(msgs),
        "outside_window": sum(1 for m in msgs if m.get("outside_window")),
        "triaged": len(done),
        "todos": sum(1 for m in done if m["ai"]["needs_action"] and not m["handled"]),
        "skipped": sum(1 for m in msgs if m.get("skip")),
        "errors": sum(1 for m in msgs if m.get("error")),
        "tagged": sum(1 for m in done if any(a == "add" for a, _ in m.get("tag_actions") or [])),
        "tasks": sum(1 for m in done if m.get("task") in ("added", "would add")),
        "categories": dict(Counter(m["ai"]["category"] for m in done)),
        "expiring": sum(1 for m in msgs if m.get("expires")),
    }


def to_json(cfg, msgs, opts, elapsed, task_error=None):
    out = []
    for m in msgs:
        r = {k: m.get(k) for k in _FIELDS if k in m}
        r.update(m.get("ai") or {})
        out.append(r)
    return json.dumps({"window": [opts.start.isoformat(), opts.end.isoformat()], "model": cfg["model"]["name"],
                       "stats": stats(msgs), "elapsed_s": round(elapsed), "task_error": task_error,
                       "problems": opts.problems, "messages": out},
                      ensure_ascii=False, indent=1, default=str) + "\n"


def to_markdown(cfg, msgs, opts, elapsed, task_error=None):
    cats = cfg["categories"]
    st = stats(msgs)
    done = [m for m in msgs if "ai" in m]
    extra = " plus unread mail outside it" if opts.unread else ""
    lines = [
        f"# Mail digest {opts.end:%Y-%m-%d}", "",
        f"Window {describe(opts.start, opts.end)}{extra}: {st['messages']} messages "
        f"({st['outside_window']} outside the window), {st['triaged']} triaged by {cfg['model']['name']}, "
        f"{st['skipped']} skipped by the skip rules. Took {elapsed / 60:.0f} min.", "",
        "Generated locally; categories and to-dos are the model's guesses, check them.", "",
    ]
    if opts.tag:
        verb = "Would tag" if opts.dry_run else "Tagged"
        lines += [f"{verb} {st['tagged']} messages in the mailbox.", ""]
    if task_error:
        lines += [f"Taskwarrior failed, no tasks were added: {task_error}", ""]
    for p in opts.problems:
        lines += [f"**Problem:** {p}", ""]

    expiring = sorted((m for m in msgs if m.get("expires")), key=lambda m: m["expires"])
    if expiring:
        lines += ["## Deleted soon", "",
                  "These messages are in folders the server empties after a few days. Open or save them in time.", ""]
        for m in expiring:
            d = days_left(m)
            when = f"in about {d} day{'' if d == 1 else 's'}" if d > 0 else "any time now"
            what = f" — {m['ai']['todo'] or m['ai']['summary']}" if "ai" in m else ""
            lines.append(f"- **{m['subject'] or '(no subject)'}** — {short_from(m['from'])}, in {m['folder_label']}, "
                         f"deleted {when} ({datetime.fromisoformat(m['expires']):%d %b}), uid {m['uid']}{what}")
        lines.append("")

    todos = [m for m in done if m["ai"]["needs_action"] and not m["handled"]]
    todos.sort(key=lambda m: (m["ai"]["deadline"] or "9999", cats.index(m["ai"]["category"])))
    lines += ["## Proposed to-dos", ""]
    for m in todos:
        dl = f" **(due {m['ai']['deadline']})**" if m["ai"]["deadline"] else ""
        task = {"added": " · task added", "would add": " · task (dry run)", "exists": " · task exists"}.get(m.get("task"), "")
        lines.append(f"- [ ] {m['ai']['todo']}{dl} — {m['ai']['category']}, {short_from(m['from'])}, uid {m['uid']}{task}")
    if not todos:
        lines.append("- none")
    lines.append("")

    for cat in cats:
        ms = [m for m in done if m["ai"]["category"] == cat]
        if not ms:
            continue
        ms.sort(key=lambda m: (not m["ai"]["needs_action"], m["handled"], m["ai"]["deadline"] or "9999"))
        lines += [f"## {cat} ({len(ms)})", ""]
        for m in ms:
            status = "handled" if m["handled"] else ("needs action" if m["ai"]["needs_action"] else "fyi")
            notes = []
            if m.get("outside_window"):
                notes.append("unread, older")
            if m.get("folder_label"):
                notes.append(f"in {m['folder_label']}")
            if m["ai"].get("category_from_tag"):
                notes.append("category from existing tag")
            note = "".join(f" · {n}" for n in notes)
            att = f" 📎 {', '.join(m['attachments'])}" if m.get("attachments") else ""
            lines.append(f"- **{m['subject'] or '(no subject)'}** — {short_from(m['from'])}, "
                         f"{parse_date(m['date']).astimezone():%a %d %b %H:%M}, uid {m['uid']} · *{status}*{note}{att}  ")
            lines.append(f"  {m['ai']['summary']}")
        lines.append("")

    errors = [m for m in msgs if m.get("error")]
    if errors:
        lines += ["## Not triaged (error)", ""]
        lines += [f"- {m['subject']} — {short_from(m['from'])}, uid {m['uid']} ({m['error']})" for m in errors]
        lines.append("")
    skipped = [m for m in msgs if m.get("skip")]
    if skipped:
        lines += [f"## Skipped ({len(skipped)})", ""]
        lines += [f"- {m['subject']} — {short_from(m['from'])}" for m in skipped]
    return "\n".join(lines) + "\n"

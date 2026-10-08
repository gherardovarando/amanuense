"""Digest output: Markdown for reading, JSON for other tools."""
import json
from collections import Counter
from datetime import datetime

from .triage import parse_date, short_from
from .window import describe, describe_duration, parse_duration

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


def to_json(cfg, msgs, opts, elapsed, task_error=None, waits=()):
    out = []
    for m in msgs:
        r = {k: m.get(k) for k in _FIELDS if k in m}
        r.update(m.get("ai") or {})
        out.append(r)
    return json.dumps({"window": [opts.start.isoformat(), opts.end.isoformat()], "model": cfg["model"]["name"],
                       "stats": stats(msgs), "elapsed_s": round(elapsed), "task_error": task_error,
                       "problems": opts.problems, "messages": out,
                       "waiting": [{k: s.get(k) for k in ("uid", "to", "subject", "date", "days", "task")}
                                   | (s.get("waiting") or {}) for s in waits]},
                      ensure_ascii=False, indent=1, default=str) + "\n"


_RANK = {"high": 0, "normal": 1, "low": 2}


def priority(m, cfg=None):
    """The model's priority; results from before priorities existed count as normal/low.
    Small models call most things urgent, so "high" with a deadline beyond output.urgent_within becomes "normal"."""
    ai = m["ai"]
    p = ai.get("priority") or ("normal" if ai["needs_action"] else "low")
    if p == "high" and cfg and ai.get("deadline"):
        limit = datetime.now() + parse_duration(cfg["output"]["urgent_within"])
        if ai["deadline"] > f"{limit:%Y-%m-%d}":
            return "normal"
    return p


def to_markdown(cfg, msgs, opts, elapsed, task_error=None, waits=()):
    if cfg["output"].get("detail", "brief") == "full":
        return to_markdown_full(cfg, msgs, opts, elapsed, task_error, waits)
    return to_markdown_brief(cfg, msgs, opts, elapsed, task_error, waits)


def _task_note(m, followup=False):
    added = " · follow-up task added" if followup else " · task added"
    return {"added": added, "would add": " · task (dry run)", "exists": " · task exists"}.get(m.get("task"), "")


def _expires(m):
    return f" ⏳ deleted {datetime.fromisoformat(m['expires']):%d %b}" if m.get("expires") else ""


def to_markdown_brief(cfg, msgs, opts, elapsed, task_error=None, waits=()):
    """What needs you first, then a few things worth knowing; the rest is only counted."""
    cats = cfg["categories"]
    st = stats(msgs)
    done = [m for m in msgs if "ai" in m]
    todos = [m for m in done if m["ai"]["needs_action"] and not m["handled"]]
    todos.sort(key=lambda m: (_RANK[priority(m, cfg)], m["ai"]["deadline"] or "9999", cats.index(m["ai"]["category"])))
    fyi = [m for m in done if not m["ai"]["needs_action"] and not m["handled"] and priority(m, cfg) != "low"]
    fyi.sort(key=lambda m: (_RANK[priority(m, cfg)], cats.index(m["ai"]["category"])))
    max_fyi = cfg["output"].get("max_fyi", 10)
    shown_fyi = fyi[:max_fyi]

    lines = [f"# Mail digest {opts.end:%Y-%m-%d}", "",
             f"{describe(opts.start, opts.end)} · {st['messages']} messages · {len(todos)} to-dos"
             f"{f' · {len(waits)} waiting for replies' if waits else ''}", ""]
    if task_error:
        lines += [f"Taskwarrior failed, no tasks were added: {task_error}", ""]
    for p in opts.problems:
        lines += [f"**Problem:** {p}", ""]

    lines += ["## To do", ""]
    if not todos:
        lines.append("- nothing")
    current = None
    for m in todos:
        p = priority(m, cfg)
        if p != current and p == "high":
            lines.append("**Urgent**")
        elif p != current and current == "high":
            lines += ["", "**Then**"]
        current = p
        dl = f" **(due {m['ai']['deadline']})**" if m["ai"]["deadline"] else ""
        lines.append(f"- [ ] {m['ai']['todo'] or m['ai']['summary']}{dl}{_expires(m)} — {m['ai']['category']}, "
                     f"{short_from(m['from'])}, uid {m['uid']}{_task_note(m)}")
    lines.append("")

    if waits:
        lines += ["## Waiting for replies", ""]
        for s in waits:
            to = ", ".join(short_from(t) for t in s.get("to") or [])
            lines.append(f"- **{s['subject'] or '(no subject)'}** — {to}, {s['days']} days: "
                         f"{s['waiting']['waiting_for']}{_task_note(s, True)}")
        lines.append("")

    if shown_fyi:
        lines += ["## Worth knowing", ""]
        lines += [f"- {m['ai']['summary']}{_expires(m)} — {short_from(m['from'])}" for m in shown_fyi]
        lines.append("")

    low = [m for m in done if not m["ai"]["needs_action"] and not m["handled"] and priority(m, cfg) == "low"]
    by_cat = Counter(m["ai"]["category"] for m in low + fyi[max_fyi:])
    handled = sum(1 for m in done if m["handled"])
    left = []
    if by_cat:
        left.append(f"{sum(by_cat.values())} low-priority ({', '.join(f'{c} {n}' for c, n in by_cat.most_common())})")
    if handled:
        left.append(f"{handled} already answered")
    if st["skipped"]:
        left.append(f"{st['skipped']} skipped by the skip rules")
    if st["errors"]:
        left.append(f"{st['errors']} not triaged (error)")
    if left:
        lines += [f"Left out: {', '.join(left)}. `amanuense render --full` shows everything.", ""]
    lines.append(f"_Sorted by {cfg['model']['name']} on this machine in {elapsed / 60:.0f} min; check its guesses._")
    return "\n".join(lines) + "\n"


def to_markdown_full(cfg, msgs, opts, elapsed, task_error=None, waits=()):
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
    todos.sort(key=lambda m: (_RANK[priority(m, cfg)], m["ai"]["deadline"] or "9999", cats.index(m["ai"]["category"])))
    lines += ["## Proposed to-dos", ""]
    for m in todos:
        dl = f" **(due {m['ai']['deadline']})**" if m["ai"]["deadline"] else ""
        task = {"added": " · task added", "would add": " · task (dry run)", "exists": " · task exists"}.get(m.get("task"), "")
        lines.append(f"- [ ] {m['ai']['todo']}{dl} — {m['ai']['category']}, {short_from(m['from'])}, uid {m['uid']}{task}")
    if not todos:
        lines.append("- none")
    lines.append("")

    if waits:
        lines += ["## Waiting for replies", "",
                  f"Mail you sent more than {describe_duration(cfg['waiting']['after'])} ago (looking back "
                  f"{describe_duration(cfg['waiting']['lookback'])}) that asked for something and got no answer yet.",
                  ""]
        for s in waits:
            to = ", ".join(short_from(t) for t in s.get("to") or [])
            task = {"added": " · follow-up task added", "would add": " · follow-up task (dry run)",
                    "exists": " · task exists"}.get(s.get("task"), "")
            lines.append(f"- **{s['subject'] or '(no subject)'}** — to {to}, sent {s['days']} days ago "
                         f"({parse_date(s['date']).astimezone():%a %d %b}) · {s['waiting']['category']}{task}  ")
            lines.append(f"  Waiting for {s['waiting']['waiting_for']}.")
        lines.append("")

    for cat in cats:
        ms = [m for m in done if m["ai"]["category"] == cat]
        if not ms:
            continue
        ms.sort(key=lambda m: (not m["ai"]["needs_action"], m["handled"], m["ai"]["deadline"] or "9999"))
        lines += [f"## {cat} ({len(ms)})", ""]
        for m in ms:
            status = "handled" if m["handled"] else ("needs action" if m["ai"]["needs_action"] else "fyi")
            if not m["handled"]:
                status += f", {priority(m, cfg)}"
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

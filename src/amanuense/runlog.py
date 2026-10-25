"""A log of the runs, one JSON line each, in ~/.local/state/amanuense/runs.jsonl (mode 600).

Counts and ids only, never text from mail: no subjects, senders, titles or summaries. So it can be
read by someone (or an assistant) who must not see the mail, to follow how the runs go over time.
Each event amanuense put in a calendar is logged by its calendar id, date and the IMAP uid of its
email; `amanuense log --events` later asks the calendar whether it was kept, confirmed or deleted.
"""
import json
import os
from datetime import datetime

from . import events, render, window


def path():
    return window.state_file("runs").with_name("runs.jsonl")


def entry(command, cfg, opts, msgs, elapsed, tasks_error=None, replies=0):
    st = render.stats(msgs)
    new = st["triaged"] - st["reused"]
    return {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "command": command,
        "window": [opts.start.isoformat(timespec="seconds"), opts.end.isoformat(timespec="seconds")],
        "model": cfg["model"]["name"],
        "dry_run": bool(opts.dry_run),
        "elapsed_s": round(elapsed),
        "s_per_message": round(elapsed / new, 1) if new else None,
        **{k: st[k] for k in ("messages", "outside_window", "expiring", "triaged", "reused", "skipped", "errors",
                              "sensitive", "todos", "tagged", "tasks", "events", "events_added", "categories")},
        "tasks_noted": sum(1 for m in msgs if m.get("task") == "noted"),
        "replies_noted": replies,
        "task_error": tasks_error,
        "problems": list(opts.problems),
        "calendar_events": [{"id": events.uid(m), "date": m["ai"]["event"]["date"],
                             "start": m["ai"]["event"]["start"], "mail_uid": m.get("uid"),
                             "status": m.get("event_status")} for m in events.upcoming(msgs)],
    }


def write(e):
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def read():
    try:
        lines = path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def line(e):
    """One short line per run, like the run's own summary."""
    ev = e["calendar_events"]
    added = sum(1 for x in ev if x["status"] == "added")
    return (f"{e['time'][:16].replace('T', ' ')} {e['command']}{' (dry run)' if e['dry_run'] else ''}: "
            f"{e['messages']} messages, {e['triaged']} triaged ({e['reused']} reused), {e['todos']} to-dos, "
            f"{e['tasks']} tasks, {e['tagged']} tagged, {e['sensitive']} sensitive, {len(ev)} events ({added} added), "
            f"{e['errors']} errors, {e['elapsed_s'] / 60:.1f} min"
            + (f", {e['s_per_message']} s/message" if e["s_per_message"] else "")
            + (f", PROBLEMS: {len(e['problems'])}" if e["problems"] or e["task_error"] else ""))

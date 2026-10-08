"""Taskwarrior: one task per to-do, tagged with its category and pointing back to the email.

Tasks are created with `task import` (JSON on stdin), so model output never passes
through a command line. Each task gets an annotation like

    2026-10-08 IMAP uid 123, account work, subject: Review invitation (amanuense, message-id <abc@x>)

which is also how amanuense recognises an email that already has a task, in any
status (pending, completed or deleted), so a task is never created twice. Tasks you
made by hand count too, if an annotation has "IMAP uid N, account A" or "msgid:<...>".

Lookups run with rc.gc=off and rc.hooks=off, so they never change or commit the task data.
"""
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone

OLD_REF = re.compile(r"IMAP uid (\d+), account ([^,\s]+)(?:, folder ([^,]+))?")
MSGID = re.compile(r"(?:message-id|msgid)[:\s]\s*(<[^>]+>)", re.I)


class TaskError(RuntimeError):
    pass


def available(cfg):
    return shutil.which(os.path.expanduser(cfg["taskwarrior"]["command"])) is not None


def enabled(cfg):
    """Config value "auto" means: when the task command is installed."""
    e = cfg["taskwarrior"]["enabled"]
    if isinstance(e, str) and e.lower() == "auto":
        return available(cfg)
    return bool(e)


def _task(cfg, *args, stdin=None, read_only=False):
    tw = cfg["taskwarrior"]
    cmd = [os.path.expanduser(tw["command"]), "rc.verbose=nothing", "rc.confirmation=no", "rc.json.array=on"]
    if read_only:  # no garbage collection and no hooks: a lookup must not change or commit anything
        cmd += ["rc.gc=off", "rc.hooks=off"]
    cmd += [f"rc.{k}={os.path.expanduser(str(v))}" for k, v in tw.get("rc", {}).items()]
    try:
        r = subprocess.run(cmd + list(args), input=stdin, capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")  # old task data may not be valid UTF-8
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TaskError(f"task failed: {e}") from None
    if r.returncode != 0:
        raise TaskError(f"task {args[0] if args else ''} failed: {r.stderr.strip()[:200]}")
    return r.stdout


def existing_refs(cfg):
    """Message-IDs and (account, folder, uid) triples that already have a task, in any status.
    An annotation without a folder refers to the inbox."""
    ids, uids = set(), set()
    inbox = cfg["mail"]["inbox"]
    for t in json.loads(_task(cfg, "export", read_only=True) or "[]"):
        for a in t.get("annotations") or []:
            text = a.get("description", "")
            ids.update(MSGID.findall(text))
            uids.update((acc, folder or inbox, int(uid)) for uid, acc, folder in OLD_REF.findall(text))
    return ids, uids


def _utc(d):
    return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build(cfg, m, now=None):
    now = now or datetime.now().astimezone()
    tw = cfg["taskwarrior"]
    ai = m["ai"]
    account = cfg["mail"]["account"] or "default"
    tags = list(tw["tags"])
    if cfg["keywords"].get(ai["category"]):  # categories that are never tagged get no category tag
        tags.insert(0, ai["category"])
    where = "" if m.get("folder", cfg["mail"]["inbox"]) == cfg["mail"]["inbox"] else f", folder {m['folder']}"
    note = (f"{now:%Y-%m-%d} IMAP uid {m['uid']}, account {account}{where}, "
            f"subject: {m.get('subject') or '(no subject)'}"
            f" (amanuense, message-id {m.get('messageId') or 'unknown'})")
    t = {
        "uuid": str(uuid.uuid4()),
        "status": "pending",
        "entry": _utc(now),
        "description": " ".join((ai["todo"] or ai["summary"] or "Answer email").split())[:200],
        "tags": tags,
        "annotations": [{"entry": _utc(now), "description": note}],
    }
    dues = []
    if ai.get("deadline"):
        dues.append(datetime.strptime(ai["deadline"], "%Y-%m-%d").replace(tzinfo=now.tzinfo))  # local midnight
    if m.get("expires"):  # the server deletes the message then: act before
        dues.append(datetime.fromisoformat(m["expires"]).astimezone(now.tzinfo))
    if dues:
        t["due"] = _utc(min(dues))
    if tw.get("project"):
        t["project"] = tw["project"]
    return t


def add_tasks(cfg, msgs, dry_run=False):
    """Create a task for each open to-do that doesn't have one yet. Sets m["task"] on those messages."""
    todo = [m for m in msgs if "ai" in m and m["ai"]["needs_action"] and not m["handled"]]
    if not todo:
        return 0
    ids, uids = existing_refs(cfg)
    account = cfg["mail"]["account"] or "default"
    new = []
    for m in todo:
        if m.get("messageId") in ids or (account, m.get("folder", cfg["mail"]["inbox"]), m["uid"]) in uids:
            m["task"] = "exists"
            continue
        m["task"] = "would add" if dry_run else "added"
        new.append(build(cfg, m))
    if new and not dry_run:
        _task(cfg, "import", "-", stdin=json.dumps(new))
    return len(new)

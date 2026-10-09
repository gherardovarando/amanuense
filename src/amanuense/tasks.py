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
    """Message-IDs and (account, folder, uid) triples that already have a task, in any status."""
    ids, uids = _refs(cfg)
    return set(ids), uids


def _refs(cfg):
    """({Message-ID: task uuid}, {(account, folder, uid)}) from the annotations of all tasks.
    An annotation without a folder refers to the inbox. Free-text notes count too: one that
    names a configured folder (e.g. "/pkg uid 131 + corrected uid 132") covers every uid in it."""
    ids, uids = {}, set()
    inbox = cfg["mail"]["inbox"]
    account = cfg["mail"]["account"] or "default"
    folders = [f["name"] for f in cfg["folders"]]
    for t in json.loads(_task(cfg, "export", read_only=True) or "[]"):
        for a in t.get("annotations") or []:
            text = a.get("description", "")
            for i in MSGID.findall(text):
                ids.setdefault(i, t["uuid"])
            uids.update((acc, folder or inbox, int(uid)) for uid, acc, folder in OLD_REF.findall(text))
            for f in folders:
                if f in text:
                    uids.update((account, f, int(u)) for u in re.findall(r"\buid (\d+)", text))
    return ids, uids


def _utc(d):
    return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build(cfg, m, now=None):
    """Task for an email that needs action."""
    ai = m["ai"]
    now = now or datetime.now().astimezone()
    dues, notes = [], []
    deadline = datetime.strptime(ai["deadline"], "%Y-%m-%d").replace(tzinfo=now.tzinfo) if ai.get("deadline") else None
    if deadline and deadline.date() < now.date():  # old mail: a due date in the past only adds noise
        notes.append(f"The email's deadline, {deadline:%d %b}, has passed: check whether this is still needed.")
    elif deadline:
        dues.append(deadline)
    if m.get("expires"):  # the server deletes the message then: act before
        expires = datetime.fromisoformat(m["expires"]).astimezone(now.tzinfo)
        if expires > now and (not dues or expires < dues[0]):
            dues = [expires]
            notes.append(f"Due {expires:%d %b} because the server deletes the message then"
                         + (f"; the email's own deadline is {deadline:%d %b}." if deadline and deadline > expires else "."))
    return _make(cfg, m, ai["todo"] or ai["summary"] or "Answer email", ai["category"], dues=dues, now=now,
                 notes=notes)


def build_followup(cfg, s, now=None):
    """Task for a sent email still waiting for a reply."""
    w = s["waiting"]
    return _make(cfg, s, f"Follow up: {w['waiting_for'] or s.get('subject') or 'no reply yet'}", w["category"],
                 extra_tags=[cfg["waiting"]["task_tag"]], now=now)


def _make(cfg, m, description, category, extra_tags=(), dues=(), now=None, notes=()):
    now = now or datetime.now().astimezone()
    tw = cfg["taskwarrior"]
    account = cfg["mail"]["account"] or "default"
    tags = list(tw["tags"]) + [t for t in extra_tags if t]
    if cfg["keywords"].get(category):  # categories that are never tagged get no category tag
        tags.insert(0, category)
    where = "" if m.get("folder", cfg["mail"]["inbox"]) == cfg["mail"]["inbox"] else f", folder {m['folder']}"
    note = (f"{now:%Y-%m-%d} IMAP uid {m['uid']}, account {account}{where}, "
            f"subject: {m.get('subject') or '(no subject)'}"
            f" (amanuense, message-id {m.get('messageId') or 'unknown'})")
    t = {
        "uuid": str(uuid.uuid4()),
        "status": "pending",
        "entry": _utc(now),
        "description": " ".join(description.split())[:200],
        "tags": tags,
        "annotations": [{"entry": _utc(now), "description": n} for n in [note, *notes]],
    }
    dues = [d.replace(tzinfo=now.tzinfo) if d.tzinfo is None else d.astimezone(now.tzinfo) for d in dues]
    if dues:
        t["due"] = _utc(min(dues))
    if m.get("project") or tw.get("project"):  # the folder's project, else the default one
        t["project"] = m.get("project") or tw["project"]
    return t


def add_tasks(cfg, msgs, dry_run=False):
    """Create a task for each open to-do that doesn't have one yet. Sets m["task"] on those messages."""
    todo = [m for m in msgs if "ai" in m and m["ai"]["needs_action"] and not m["handled"]]
    return _add(cfg, todo, build, dry_run)


def add_followups(cfg, waits, dry_run=False):
    """Create a follow-up task for each sent email still waiting for a reply."""
    return _add(cfg, [s for s in waits if s.get("waiting", {}).get("expects_reply")], build_followup, dry_run)


def _add(cfg, todo, make, dry_run):
    if not todo:
        return 0
    ids, uids = _refs(cfg)
    account = cfg["mail"]["account"] or "default"
    new, notes = [], []
    for m in todo:
        key = (account, m.get("folder", cfg["mail"]["inbox"]), m["uid"])
        if m.get("messageId") in ids or key in uids:
            m["task"] = "exists"
            continue
        if m.get("inReplyTo") in ids:  # a reply in the thread of a task: note it there, no new task
            m["task"] = "would note" if dry_run else "noted"
            target = ids[m["inReplyTo"]]
            notes.append((target, make(cfg, m)["annotations"][0]["description"]))
        else:
            m["task"] = "would add" if dry_run else "added"
            new.append(make(cfg, m))
            target = new[-1]["uuid"]
        if m.get("messageId"):  # the same email again later in this run (another folder) is a duplicate
            ids[m["messageId"]] = target
        uids.add(key)
    if not dry_run:
        if new:
            _task(cfg, "import", "-", stdin=json.dumps(new))
        for uuid_, text in notes:
            _task(cfg, uuid_, "annotate", "--", f"reply in this thread: {text}")
    return len(new)

"""Taskwarrior: one task per to-do, tagged with its category and pointing back to the email.

Tasks are created with `task import` (JSON on stdin), so model output never passes
through a command line. Each task gets an annotation like

    2026-10-08 IMAP uid 123, account work, subject: Review invitation (amanuense, message-id <abc@x>)

which is also how amanuense recognises an email that already has a task, in any
status (pending, completed or deleted), so a task is never created twice. Tasks you
made by hand count too, if an annotation has "IMAP uid N, account A" or "msgid:<...>".

When you reply to the email of a pending task, the next digest adds a note "You replied on ..."
to the task. amanuense never closes a task: you, or another tool reading the notes, decide.

Lookups run with rc.gc=off and rc.hooks=off, so they never change or commit the task data.
"""
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone

from .triage import parse_date

OLD_REF = re.compile(r"IMAP uid (\d+), account ([^,\s]+)(?:, folder ([^,]+))?")
MSGID = re.compile(r"(?:message-id|msgid)[:\s]\s*(<[^>]+>)", re.I)
SUBJECT = re.compile(r"subject: (.+?)(?: \(amanuense, message-id|, msgid:|$)")
PREFIX = re.compile(r"^(\s*(re|fwd?|rv|res|enc|aw|wg|tr)\s*:\s*|\s*\[[^\]]*\]\s*)+", re.I)
MIN_SUBJECT = 12  # shorter subjects ("Re: question") are too generic to group mail by


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
    ids, uids, _ = _refs(cfg)
    return set(ids), uids


def topic(subject):
    """The subject without Re:/Fwd:/[list] prefixes, lowercased; None if too short to mean anything."""
    t = " ".join(re.sub(r"[^\w]+", " ", PREFIX.sub("", subject or "")).split()).lower()  # words only
    return t if len(t) >= MIN_SUBJECT else None


def _refs(cfg):
    """({Message-ID: task uuid}, {(account, folder, uid)}, {topic: task}) from the annotations of all
    tasks; for topics a pending task wins over a completed or deleted one.
    An annotation without a folder refers to the inbox. Free-text notes count too: one that
    names a configured folder (e.g. "/pkg uid 131 + corrected uid 132") covers every uid in it."""
    ids, uids, topics = {}, set(), {}
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
            for subj in SUBJECT.findall(text):
                if (k := topic(subj)) and (k not in topics or topics[k]["status"] != "pending"):
                    topics[k] = t
    return ids, uids, topics


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


def shorten(text, limit=120):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def project(cfg, m):
    """The folder's project, else the first taskwarrior.projects rule whose pattern matches the
    subject, sender, recipients or folder, else taskwarrior.project."""
    if m.get("project"):
        return m["project"]
    text = "\n".join([m.get("subject") or "", m.get("from") or "", *(m.get("to") or []), *(m.get("cc") or []),
                      m.get("folder") or ""])
    for name, pattern in cfg["taskwarrior"].get("projects", {}).items():
        if re.search(pattern, text, re.I):
            return name
    return cfg["taskwarrior"]["project"]


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
        "description": shorten(description),
        "tags": tags,
        "annotations": [{"entry": _utc(now), "description": n} for n in [note, *notes]],
    }
    dues = [d.replace(tzinfo=now.tzinfo) if d.tzinfo is None else d.astimezone(now.tzinfo) for d in dues]
    if dues:
        t["due"] = _utc(min(dues))
    if p := project(cfg, m):
        t["project"] = p
    return t


def add_tasks(cfg, msgs, dry_run=False):
    """Create a task for each open to-do that doesn't have one yet. Sets m["task"] on those messages.
    Mail in extra folders dated before the window gets none: earlier runs already saw it."""
    todo = [m for m in msgs if "ai" in m and m["ai"]["needs_action"] and not m["handled"] and not m.get("before_window")]
    return _add(cfg, todo, build, dry_run, group=True)


def add_followups(cfg, waits, dry_run=False):
    """Create a follow-up task for each sent email still waiting for a reply."""
    return _add(cfg, [s for s in waits if s.get("waiting", {}).get("expects_reply")], build_followup, dry_run)


def _add(cfg, todo, make, dry_run, group=False):
    """With group, mail on the same topic (subject without Re:/Fwd:) as a pending task, or as an
    earlier email in this run, becomes a note on that task; a completed or deleted task on the
    topic is mentioned on the new one."""
    if not todo:
        return 0
    ids, uids, topics = _refs(cfg)
    account = cfg["mail"]["account"] or "default"
    new, notes = [], []
    for m in todo:
        key = (account, m.get("folder", cfg["mail"]["inbox"]), m["uid"])
        if m.get("messageId") in ids or key in uids:
            m["task"] = "exists"
            continue
        k = topic(m.get("subject")) if group else None
        same = topics.get(k) if k else None
        if m.get("inReplyTo") in ids or (same and same["status"] == "pending"):
            # a reply in the thread of a task, or the same topic: note it there, no new task
            m["task"] = "would note" if dry_run else "noted"
            target = ids.get(m.get("inReplyTo")) or same["uuid"]
            notes.append((target, make(cfg, m)["annotations"][0]["description"]))
        else:
            m["task"] = "would add" if dry_run else "added"
            t = make(cfg, m)
            if same:  # done or dropped earlier: the new task says so, the old one is left alone
                t["annotations"].append({"entry": t["entry"], "description":
                    f"Same subject as an earlier {same['status']} task: {same['description'][:80]}"})
            new.append(t)
            target = t["uuid"]
            if k:
                topics[k] = t
        if m.get("messageId"):  # the same email again later in this run (another folder) is a duplicate
            ids[m["messageId"]] = target
        uids.add(key)
    if not dry_run:
        if new:  # raw UTF-8: Taskwarrior 2.6 stores \ud83d-style escapes as invalid bytes
            _task(cfg, "import", "-", stdin=json.dumps(new, ensure_ascii=False))
        for uuid_, text in notes:
            _task(cfg, uuid_, "annotate", "--", f"same thread: {text}")
    return len(new)


REPLIED = "You replied on"


def note_replies(cfg, sent, dry_run=False):
    """Add a note to each pending task whose email you answered: a message in `sent` replies
    (In-Reply-To) to an email whose Message-ID is in the task's annotations. Follow-up tasks are
    left out, since there the reply is yours to wait for. Each reply is noted once.
    Returns the number of notes."""
    replies = {}
    for s in sent:
        if s.get("inReplyTo") and s.get("messageId"):
            replies.setdefault(s["inReplyTo"], []).append(s)
    if not replies:
        return 0
    notes = []
    for t in json.loads(_task(cfg, "status:pending", "export", read_only=True) or "[]"):
        if cfg["waiting"]["task_tag"] in (t.get("tags") or []):
            continue
        texts = [a.get("description", "") for a in t.get("annotations") or []]
        seen = set()
        for i in {i for x in texts for i in MSGID.findall(x)}:
            for s in replies.get(i, []):
                if s["messageId"] in seen or any(s["messageId"] in x for x in texts):
                    continue  # noted on an earlier run
                seen.add(s["messageId"])
                when = parse_date(s["date"]).astimezone()
                # "reply id", not "message-id": the reply must not count as an email with a task
                notes.append((t["uuid"], f"{REPLIED} {when:%Y-%m-%d %H:%M} (amanuense, reply id {s['messageId']})"))
    if not dry_run:
        for uuid_, text in notes:
            _task(cfg, uuid_, "annotate", "--", text)
    return len(notes)

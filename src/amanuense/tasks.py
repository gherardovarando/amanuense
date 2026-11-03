"""Taskwarrior: one task per to-do, tagged with its category and pointing back to the email.

Tasks are created with `task import` (JSON on stdin), so model output never passes
through a command line. Each task gets an annotation like

    2026-10-08 IMAP uid 123, account work, subject: Review invitation (amanuense, message-id <abc@x>)

which is also how amanuense recognises an email that already has a task, in any
status (pending, completed or deleted), so a task is never created twice. Tasks you
made by hand count too, if an annotation has "IMAP uid N, account A" or "msgid:<...>".

A sensitive email (see the [sensitive] config section) gets a task that names only the sender,
with the sensitive tag and no subject: whoever reads the task list sees nothing of its content.

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

from .triage import parse_date, short_from

OLD_REF = re.compile(r"IMAP uid (\d+), account ([^,\s]+)(?:, folder ([^,]+))?")
MSGID = re.compile(r"(?:message-id|msgid)[:\s]\s*(<[^>]+>)", re.I)
SUBJECT = re.compile(r"subject: (.+?)(?: \(amanuense, message-id|, msgid:|$)")
PREFIX = re.compile(r"^(\s*(re|fwd?|rv|res|enc|aw|wg|tr)\s*:\s*|\s*\[[^\]]*\]\s*)+", re.I)
CLOSED = ("completed", "deleted")
MIN_SUBJECT = 12  # shorter subjects ("Re: question") are too generic to group mail by
# a to-do that only says to attend: the calendar event already reminds you of that
ATTEND = re.compile(r"^\s*(attend|join|participate|take part|go to|be at|be present)\b", re.I)
# "Confirm …" is a real to-do only if the email asks for it (triage sets m["asks_to_confirm"])
CONFIRM = re.compile(r"^\s*confirm\b", re.I)


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
    ids, uids, _, _ = _refs(cfg)
    return set(ids), uids


def references(cfg, *texts):
    """The references (taskwarrior.references patterns, e.g. a manuscript ID) named in the texts."""
    text = "\n".join(t for t in texts if t)
    return {r.upper() for p in cfg["taskwarrior"]["references"] for r in re.findall(p, text)}


def topic(subject):
    """The subject without Re:/Fwd:/[list] prefixes, lowercased; None if too short to mean anything."""
    t = " ".join(re.sub(r"[^\w]+", " ", PREFIX.sub("", subject or "")).split()).lower()  # words only
    return t if len(t) >= MIN_SUBJECT else None


def _refs(cfg):
    """({Message-ID: task}, {(account, folder, uid)}, {topic: task}, {reference: task}) from the
    annotations of all tasks, in any status; for topics and references a pending task wins over a
    completed or deleted one. References come from the description and the annotated subjects.
    An annotation without a folder refers to the inbox. Free-text notes count too: one that
    names a configured folder (e.g. "/pkg uid 131 + corrected uid 132") covers every uid in it."""
    ids, uids, topics, refs = {}, set(), {}, {}
    inbox = cfg["mail"]["inbox"]
    account = cfg["mail"]["account"] or "default"
    folders = [f["name"] for f in cfg["folders"]]

    def keep(table, keys, t):  # an open task wins
        for k in keys:
            if k and (k not in table or table[k]["status"] in CLOSED):
                table[k] = t

    for t in json.loads(_task(cfg, "export", read_only=True) or "[]"):
        for a in t.get("annotations") or []:
            text = a.get("description", "")
            for i in MSGID.findall(text):
                ids.setdefault(i, t)
            uids.update((acc, folder or inbox, int(uid)) for uid, acc, folder in OLD_REF.findall(text))
            for f in folders:
                if f in text:
                    uids.update((account, f, int(u)) for u in re.findall(r"\buid (\d+)", text))
            for subj in SUBJECT.findall(text):
                keep(topics, [topic(subj)], t)
                keep(refs, references(cfg, subj), t)
        keep(refs, references(cfg, t.get("description")), t)
    return ids, uids, topics, refs


def earlier_note(t):
    """The note on a new task whose email is about a task already marked done or deleted."""
    end = t.get("end") or t.get("modified") or ""
    when = f" on {end[:4]}-{end[4:6]}-{end[6:8]}" if len(end) >= 8 else ""
    check = "Check it was really done." if t["status"] == "completed" else "Check whether it is needed after all."
    return f"An earlier task on this was marked {'done' if t['status'] == 'completed' else 'deleted'}{when}: " \
           f"'{t['description'][:80]}'. {check}"


def _utc(d):
    return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build(cfg, m, now=None):
    """Task for an email that needs action."""
    ai = m["ai"]
    now = now or datetime.now().astimezone()
    dues, notes = [], []
    deadline = datetime.strptime(ai["deadline"], "%Y-%m-%d").astimezone() if ai.get("deadline") else None
    if deadline and deadline.date() < now.date():  # old mail: a due date in the past only adds noise
        notes.append(f"The email's deadline, {deadline:%d %b}, has passed: check whether this is still needed.")
    elif deadline:
        dues.append(deadline)
    event = ai.get("event")
    if event and not deadline:  # work for a meeting is due when the meeting starts
        at = datetime.strptime(f"{event['date']} {event['start'] or '00:00'}", "%Y-%m-%d %H:%M").astimezone()
        if at > now:
            dues.append(at)
            notes.append(f"Due at the start of the event{'' if m.get('sensitive') else ' ' + repr(event['title'])}, "
                         f"{at:%a %d %b}{'' if not event['start'] else f' {at:%H:%M}'}: the email states no other deadline.")
    if m.get("expires"):  # the server deletes the message then: act before
        expires = datetime.fromisoformat(m["expires"]).astimezone(now.tzinfo)
        if expires > now and (not dues or expires < dues[0]):
            dues = [expires]
            notes.append(f"Due {expires:%d %b} because the server deletes the message then"
                         + (f"; the email's own deadline is {deadline:%d %b}." if deadline and deadline > expires else "."))
    if m.get("sensitive"):  # the task says who and that it's sensitive, nothing of the content
        return _make(cfg, m, f"Sensitive email from {short_from(m.get('from'))}: needs action", ai["category"],
                     extra_tags=[cfg["sensitive"]["task_tag"]], dues=dues, now=now, notes=notes)
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
            f"subject: {'(sensitive)' if m.get('sensitive') else m.get('subject') or '(no subject)'}"
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


def add_tasks(cfg, msgs, dry_run=False, today=None):
    """Create a task for each open to-do that doesn't have one yet. Sets m["task"] on those messages.
    Mail in extra folders dated before the window gets none: earlier runs already saw it."""
    todo = [m for m in msgs if "ai" in m and m["ai"]["needs_action"] and not m["handled"] and not m.get("before_window")]
    today = today or f"{datetime.now():%Y-%m-%d}"
    for m in todo:
        if in_calendar(m):
            m["task"] = "in calendar"
        elif event_passed(m, today):
            m["task"] = "event passed"
    return _add(cfg, [m for m in todo if m.get("task") not in ("in calendar", "event passed")], build, dry_run, group=True)


def only_event(m):
    """True when the to-do is only to attend the email's event, or to confirm it when the email
    never asks for that (the model sometimes invents it)."""
    todo = m["ai"].get("todo") or ""
    unasked = CONFIRM.match(todo) and m.get("asks_to_confirm") is False
    return bool(m["ai"].get("event") and (ATTEND.match(todo) or unasked))


def in_calendar(m):
    """True when the to-do is only about an event that is in the events calendar: no task then.
    Without the event in a calendar (no events.url, or it failed) the task is kept."""
    return only_event(m) and m.get("event_status") in ("added", "merged", "exists", "yours")


def event_passed(m, today):
    """True when the to-do is only about an event dated before today: too late for a task.
    E.g. a meeting fixed for the same day, read by the next morning's digest."""
    return only_event(m) and m["ai"]["event"]["date"] < today


def add_followups(cfg, waits, dry_run=False):
    """Create a follow-up task for each sent email still waiting for a reply."""
    return _add(cfg, [s for s in waits if s.get("waiting", {}).get("expects_reply")], build_followup, dry_run)


def _add(cfg, todo, make, dry_run, group=False):
    """With group, mail in the thread of a task, about the same meeting as its email (see events.add),
    on the same topic (subject without Re:/Fwd:), or that
    names the same reference (e.g. a manuscript ID, see taskwarrior.references) in its subject or to-do,
    becomes a note on that task if it is pending. If the matching task was marked done or deleted,
    the mail gets a new task with the tag "again" and a note saying so: a reminder about something
    closed by mistake is not lost. Sets m["earlier"] to that task's status."""
    if not todo:
        return 0
    ids, uids, topics, refs = _refs(cfg)
    account = cfg["mail"]["account"] or "default"
    new, notes = [], []
    for m in todo:
        key = (account, m.get("folder", cfg["mail"]["inbox"]), m["uid"])
        if m.get("messageId") in ids or key in uids:
            m["task"] = "exists"
            continue
        k = topic(m.get("subject")) if group else None
        named = references(cfg, m.get("subject"), (m.get("ai") or {}).get("todo")) if group else set()
        matches = []  # (task, label), most specific first
        if m.get("inReplyTo") in ids:
            matches.append((ids[m["inReplyTo"]], "same thread"))
        matches += [(ids[i], "same meeting") for i in m.get("same_meeting", []) if i in ids]
        matches += [(topics[k], "same thread")] if k in topics else []
        matches += [(refs[r], "same item") for r in sorted(named) if r in refs]
        pending = next(((t, label) for t, label in matches if t["status"] not in CLOSED), None)
        closed = next((t for t, _ in matches if t["status"] in CLOSED), None)
        if pending:  # still open (pending or waiting): note it there, no new task
            m["task"] = "would note" if dry_run else "noted"
            t, label = pending
            notes.append((t["uuid"], f"{label}: {make(cfg, m)['annotations'][0]['description']}"))
        else:
            m["task"] = "would add" if dry_run else "added"
            t = make(cfg, m)
            if closed:  # done or dropped earlier: a new task says so; the old one is left alone
                m["earlier"] = closed["status"]
                t["tags"].append("again")
                t["annotations"].append({"entry": t["entry"], "description": earlier_note(closed)})
            new.append(t)
            if k:
                topics[k] = t
            refs.update(dict.fromkeys(named, t))
        if m.get("messageId"):  # the same email again later in this run (another folder) is a duplicate
            ids[m["messageId"]] = t
        uids.add(key)
    if not dry_run:
        if new:  # raw UTF-8: Taskwarrior 2.6 stores \ud83d-style escapes as invalid bytes (3 doesn't mind)
            _task(cfg, "import", "-", stdin=json.dumps(new, ensure_ascii=False))
        for uuid_, text in notes:
            _task(cfg, uuid_, "annotate", "--", text)
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

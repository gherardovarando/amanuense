"""Events the mail fixes: listed in the digest and, with events.url set, added to a CalDAV calendar.

Each event is created TENTATIVE, with a description pointing back to the email. Its id comes
from the date, the start time and the sender, so the same meeting in a later email from the same
person is recognised. A later email about the same meeting from someone else (same time, and
a sender of one on the other, a similar title, the same subject or the same reference) adds its place or link
to amanuense's own event while that is still tentative; one you confirmed or edited is left as
it is. With events.check, your own calendars are read too (never written): if one of your events
starts at that time, nothing is added. amanuense never deletes an event. If only the time matches, a new event is added and
both its notes and the digest say another event starts then.
"""
import hashlib
import re
from datetime import datetime, timedelta, timezone

from . import caldav, tasks
from .triage import addr, short_from

OURS = "amanuense-event-"


def upcoming(msgs, today=None, once=True):
    """Triaged messages with an event today or later, soonest first; with once, each event once
    (same date, start and title), for listing."""
    today = today or f"{datetime.now():%Y-%m-%d}"
    with_event = [m for m in msgs if (m.get("ai") or {}).get("event") and m["ai"]["event"]["date"] >= today]
    out, seen = [], set()
    for m in sorted(with_event, key=lambda m: (m["ai"]["event"]["date"], m["ai"]["event"]["start"] or "")):
        e = m["ai"]["event"]
        key = (e["date"], e["start"], e["title"].lower())
        if not once or key not in seen:
            seen.add(key)
            out.append(m)
    return out


def uid(m):
    e = m["ai"]["event"]
    key = f"{e['date']}|{e['start'] or 'all day'}|{addr(m.get('from'))}"
    return OURS + hashlib.sha1(key.encode()).hexdigest()[:16]


def ics(cfg, m, now=None):
    """A TENTATIVE, private VEVENT. Sensitive mail gets a title that names only the sender."""
    e = m["ai"]["event"]
    now = now or datetime.now().astimezone()
    sender = short_from(m.get("from"))
    utc = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    day = datetime.strptime(e["date"], "%Y-%m-%d")
    if e["start"]:
        start = datetime.strptime(f"{e['date']} {e['start']}", "%Y-%m-%d %H:%M").astimezone(now.tzinfo)
        end = (datetime.strptime(f"{e['date']} {e['end']}", "%Y-%m-%d %H:%M").astimezone(now.tzinfo) if e["end"]
               else start + timedelta(minutes=int(cfg["events"]["minutes"])))
        when = [f"DTSTART:{utc(start)}", f"DTEND:{utc(end)}"]
    else:  # all day
        when = [f"DTSTART;VALUE=DATE:{day:%Y%m%d}", f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}"]
    if m.get("sensitive"):
        title, place = f"Event (sensitive email from {sender})", None
        about = f"a sensitive email from {sender}"
    else:
        title, place = e["title"], e["place"]
        about = f"an email from {sender}, subject: {m.get('subject') or '(no subject)'}"
    why = "" if m.get("sensitive") or not e.get("why") else e["why"].rstrip(".") + ".\n\n"
    notes = (f"{why}Proposed by amanuense from {about} (message-id {m.get('messageId') or 'unknown'}). "
             "It may be wrong: confirm it by editing it, or delete it.")
    if m.get("event_other"):
        notes += f"\n\nAnother event starts at the same time: '{m['event_other']}'. If it is the same meeting, delete one."
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//amanuense//events//EN", "BEGIN:VEVENT",
             f"UID:{uid(m)}", f"DTSTAMP:{utc(now)}", *when, "STATUS:TENTATIVE", "CLASS:PRIVATE",
             f"SUMMARY:{caldav._text(title)}", f"DESCRIPTION:{caldav._text(notes)}",
             f"X-AMANUENSE-FROM:{addr(m.get('from'))}", f"X-AMANUENSE-PEOPLE:{','.join(sorted(people(m)))}"]
    if place:
        lines.append(f"LOCATION:{caldav._text(place)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(caldav._fold(l) for l in lines) + "\r\n"


def people(m):
    """The addresses on an email: sender, To and Cc, lowercased."""
    return {addr(a) for a in [m.get("from"), *(m.get("to") or []), *(m.get("cc") or [])] if addr(a)}


GENERIC = {"meeting", "reunion", "reunión", "call", "with", "about", "online", "zoom", "teams", "link",
           "enlace", "session", "sesión", "and", "the", "for", "para", "sobre", "con", "del"}


def _words(text):
    return {w for w in re.findall(r"\w+", (text or "").lower()) if len(w) >= 3 and w not in GENERIC}


def _subject(notes):
    found = re.search(r"subject: (.*?) \(message-id", notes or "")
    return found.group(1) if found else ""


def same_meeting(cfg, m, e):
    """Why the email's event and the calendar event e (at the same time) look like one meeting:
    a list of reasons, empty if nothing but the time matches."""
    mine = set(cfg["mail"]["addresses"])
    # someone who sent one of the two emails is on the other: a list or a colleague in Cc on both is not enough
    senders = ({addr(m.get("from"))} | e.get("senders", set())) - mine - {""}
    shared = senders & (people(m) - mine) & (e["people"] - mine)
    why = ["shared people"] if shared else []
    a, b = _words(m["ai"]["event"]["title"]), _words(e["title"])
    if a and b and len(a & b) >= min(2, len(a), len(b)):
        why.append("similar title")
    if tasks.topic(m.get("subject")) and tasks.topic(m.get("subject")) == tasks.topic(_subject(e["notes"])):
        why.append("same subject")
    named = tasks.references(cfg, m.get("subject"), m["ai"]["event"]["title"])
    if named & tasks.references(cfg, _subject(e["notes"]), e["title"]):
        why.append("same reference")
    return why


def _at_same_time(m, e):
    ev = m["ai"]["event"]
    if not ev["start"]:
        return e["all_day"] and f"{e['start']:%Y-%m-%d}" == ev["date"]
    return not e["all_day"] and f"{e['start']:%Y-%m-%d %H:%M}" == f"{ev['date']} {ev['start']}"


def merged(cfg, m, e, now=None):
    """The raw iCalendar text of e with this email added: its place or link in LOCATION (if not
    there yet), a line in the description, and its people. Sensitive mail adds only the sender."""
    now = now or datetime.now().astimezone()
    sender = short_from(m.get("from"))
    place = None if m.get("sensitive") else m["ai"]["event"]["place"]
    about = (f"a sensitive email from {sender}" if m.get("sensitive")
             else f"an email from {sender}, subject: {m.get('subject') or '(no subject)'}")
    extra = f"Also in {about} (message-id {m.get('messageId') or 'unknown'})" + (f": {place}" if place else "") + "."
    lines = re.sub(r"\r?\n[ \t]", "", e["raw"]).splitlines()
    out, inside, seen = [], False, set()
    for line in lines:
        name = re.split(r"[;:]", line, 1)[0].upper()
        if line == "BEGIN:VEVENT":
            inside = True
        elif line == "END:VEVENT" and inside:
            if place and "LOCATION" not in seen:
                out.append(f"LOCATION:{caldav._text(place)}")
            if "DESCRIPTION" not in seen:
                out.append(f"DESCRIPTION:{caldav._text(extra)}")
            if "X-AMANUENSE-PEOPLE" not in seen:
                out.append(f"X-AMANUENSE-PEOPLE:{','.join(sorted(people(m)))}")
            if "X-AMANUENSE-FROM" not in seen and addr(m.get("from")):
                out.append(f"X-AMANUENSE-FROM:{addr(m.get('from'))}")
            inside = False
        elif inside and name not in seen:
            seen.add(name)
            if name == "LOCATION" and place and place.lower() not in caldav._untext(line.split(":", 1)[1]).lower():
                line += " · " + caldav._text(place)
            elif name == "DESCRIPTION":
                line += "\\n\\n" + caldav._text(extra)
            elif name == "X-AMANUENSE-PEOPLE":
                line = f"X-AMANUENSE-PEOPLE:{','.join(sorted(e['people'] | people(m)))}"
            elif name == "X-AMANUENSE-FROM":
                line = f"X-AMANUENSE-FROM:{','.join(sorted(e['senders'] | {addr(m.get('from'))} - {''}))}"
            elif name == "DTSTAMP":
                line = f"DTSTAMP:{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
        out.append(line)
    return "\r\n".join(caldav._fold(l) for l in out) + "\r\n"


MSGID = re.compile(r"message-id (<[^>]+>)")


def link(msgs, m, e):
    """Record that m is about the same meeting as the emails named in event e, both ways, in
    m["same_meeting"] (Message-IDs): their to-dos then become one task."""
    others = set(MSGID.findall(e["notes"])) - {m.get("messageId")}
    m["same_meeting"] = sorted(set(m.get("same_meeting", [])) | others)
    for o in msgs:
        if o.get("messageId") in others and m.get("messageId"):
            o["same_meeting"] = sorted(set(o.get("same_meeting", [])) | {m["messageId"]})


def your_calendars(cfg):
    """[(name, url)] of the calendars in events.check (names as `calendar list` shows them, or URLs),
    and a problem for each name not found."""
    wanted = cfg["events"]["check"]
    out, missing = [(u, u) for u in wanted if u.startswith(("http://", "https://"))], []
    names = [n for n in wanted if not n.startswith(("http://", "https://"))]
    if names:
        found = {n.lower(): u for n, u in caldav.discover(cfg["calendar"])}
        out += [(n, found[n.lower()]) for n in names if n.lower() in found]
        missing = [n for n in names if n.lower() not in found]
    return out, (f"events.check: no calendar named {', '.join(map(repr, missing))} (`amanuense calendar list`)"
                 if missing else None)


def add(cfg, msgs, dry_run=False):
    """Create the upcoming events in events.url, or add the email to the same meeting already there.
    Sets m["event_status"] on each: "added", "merged" (into amanuense's tentative event), "yours"
    (an event of yours in an events.check calendar starts then: nothing added),
    "exists" (already there, left as it is), "would add" or "failed"; m["event_other"] is the
    title of the other event at that time, if any; m["same_meeting"] the Message-IDs of the other
    emails about that meeting. Returns (added, problem or None)."""
    url = cfg["events"]["url"]
    added, problem, yours = 0, None, []
    if url and not dry_run and cfg["events"]["check"]:
        try:
            yours, problem = your_calendars(cfg)
        except caldav.CalendarError as err:
            problem = f"events.check: {err}"
    for m in upcoming(msgs, once=False):  # every email: a later one may add its link
        if dry_run or not url:
            m["event_status"] = "would add"
            continue
        try:
            day = datetime.strptime(m["ai"]["event"]["date"], "%Y-%m-%d").astimezone()
            there = [e for e in caldav.items(cfg["calendar"], url, day, day + timedelta(days=1)) if _at_same_time(m, e)]
            same = (next((e for e in there if e["uid"] == uid(m)), None)
                    or next((e for e in there if same_meeting(cfg, m, e)), None))
            if same:
                link(msgs, m, same)
            own = same is not None and same["uid"] == uid(m)  # from this sender, at this time
            if own and f"message-id {m.get('messageId')}" in same["notes"]:
                m["event_status"], m["event_id"] = "exists", uid(m)
                continue
            mine = None if own else next((e for _, u in yours for e in caldav.read_events(
                cfg["calendar"], u, day, day + timedelta(days=1)) if _at_same_time(m, e)), None)
            if mine:  # you put it in your calendar (or have something else then): add nothing
                m["event_status"], m["event_other"] = "yours", mine["title"]
                continue
            if same:
                m["event_other"], m["event_id"] = same["title"], same["uid"]
                ours = same["uid"].startswith(OURS) and same["status"] == "tentative"
                if ours and f"message-id {m.get('messageId')}" in same["notes"]:
                    m["event_status"] = "exists"
                elif ours and caldav.update(cfg["calendar"], same["href"], merged(cfg, m, same), same["etag"]):
                    m["event_status"] = "merged"
                else:
                    m["event_status"] = "exists"
                continue
            if there:
                m["event_other"] = there[0]["title"]
            new = caldav.create(cfg["calendar"], url, uid(m), ics(cfg, m))
        except caldav.CalendarError as err:
            m["event_status"] = "failed"
            problem = str(err)
            continue
        m["event_status"] = "added" if new else "exists"
        added += new
    return added, problem

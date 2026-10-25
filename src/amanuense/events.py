"""Events the mail fixes: listed in the digest and, with events.url set, added to a CalDAV calendar.

Each event is created once, TENTATIVE, with a description pointing back to the email. Its id comes
from the date, the start time and the sender, so the same meeting in a later email from the same
person is recognised. amanuense never replaces, edits or deletes an event: one you confirmed,
moved or deleted stays as you left it.
"""
import hashlib
from datetime import datetime, timedelta, timezone

from . import caldav
from .triage import addr, short_from


def upcoming(msgs, today=None):
    """Triaged messages with an event today or later, soonest first, each event once."""
    today = today or f"{datetime.now():%Y-%m-%d}"
    with_event = [m for m in msgs if (m.get("ai") or {}).get("event") and m["ai"]["event"]["date"] >= today]
    out, seen = [], set()
    for m in sorted(with_event, key=lambda m: (m["ai"]["event"]["date"], m["ai"]["event"]["start"] or "")):
        e = m["ai"]["event"]
        key = (e["date"], e["start"], e["title"].lower())
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


def uid(m):
    e = m["ai"]["event"]
    key = f"{e['date']}|{e['start'] or 'all day'}|{addr(m.get('from'))}"
    return "amanuense-event-" + hashlib.sha1(key.encode()).hexdigest()[:16]


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
    notes = (f"Proposed by amanuense from {about} (message-id {m.get('messageId') or 'unknown'}). "
             "It may be wrong: confirm it by editing it, or delete it.")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//amanuense//events//EN", "BEGIN:VEVENT",
             f"UID:{uid(m)}", f"DTSTAMP:{utc(now)}", *when, "STATUS:TENTATIVE", "CLASS:PRIVATE",
             f"SUMMARY:{caldav._text(title)}", f"DESCRIPTION:{caldav._text(notes)}"]
    if place:
        lines.append(f"LOCATION:{caldav._text(place)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(caldav._fold(l) for l in lines) + "\r\n"


def add(cfg, msgs, dry_run=False):
    """Create the upcoming events in events.url. Sets m["event_status"] ("added", "exists",
    "would add", "failed") on each; returns (added, problem or None)."""
    url = cfg["events"]["url"]
    added, problem = 0, None
    for m in upcoming(msgs):
        if dry_run or not url:
            m["event_status"] = "would add"
            continue
        try:
            new = caldav.create(cfg["calendar"], url, uid(m), ics(cfg, m))
        except caldav.CalendarError as err:
            m["event_status"] = "failed"
            problem = str(err)
            continue
        m["event_status"] = "added" if new else "exists"
        added += new
    return added, problem

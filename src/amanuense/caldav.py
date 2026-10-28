"""Deliver the digest to a CalDAV calendar, as a task (default) or an event, so a phone that syncs
it shows a reminder.

One item per day, with a fixed id (amanuense-YYYY-MM-DD), so a rerun replaces it. The title
comes from one template (by default `oneline`: counts only, which is what a lock screen shows)
and the notes from another (by default `tiny`).
"""
import base64
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from . import config

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}


class CalendarError(RuntimeError):
    pass


def password(c):
    """From password_file (mode 600), password_command (e.g. `pass uv`), or $AMANUENSE_CALDAV_PASSWORD."""
    if c.get("password_file"):
        p = config.expand(c["password_file"])
        if p.stat().st_mode & 0o077:
            raise CalendarError(f"{p} is readable by others; run chmod 600 {p}")
        return p.read_text().strip()
    if c.get("password_command"):
        r = subprocess.run(c["password_command"], shell=True, capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise CalendarError("calendar.password_command failed")
        return r.stdout.strip()
    if os.environ.get("AMANUENSE_CALDAV_PASSWORD"):
        return os.environ["AMANUENSE_CALDAV_PASSWORD"]
    raise CalendarError("no calendar password: set calendar.password_file or calendar.password_command")


def _request(c, method, url, body=None, headers=None):
    auth = base64.b64encode(f"{c['username']}:{password(c)}".encode()).decode()
    req = urllib.request.Request(url, data=body.encode() if isinstance(body, str) else body, method=method,
                                 headers={"Authorization": f"Basic {auth}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), r.geturl()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise CalendarError("the calendar server refused the username or password") from None
        if e.code in (301, 302, 307, 308) and e.headers.get("Location"):
            return e.code, (None, e.headers["Location"]), url
        return e.code, e.read(), url
    except urllib.error.URLError as e:
        raise CalendarError(f"cannot reach {url}: {e.reason}") from None


def _propfind(c, url, props, depth="0"):
    body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
            + "".join(props) + "</d:prop></d:propfind>")
    for _ in range(5):  # urllib follows redirects only for GET, and /.well-known/caldav usually redirects
        status, data, final = _request(c, "PROPFIND", url, body, {"Depth": depth, "Content-Type": "application/xml"})
        if status in (301, 302, 307, 308) and isinstance(data, tuple):
            url = urllib.parse.urljoin(url, data[1])
            continue
        break
    if status == 200:
        raise CalendarError(f"{url.split('?')[0]} answered with a web page, not calendar data (a login page?); "
                            "set calendar.server to the server's DAV address, e.g. https://host/SOGo/dav/")
    if status != 207:
        raise CalendarError(f"PROPFIND {url}: HTTP {status}")
    return ET.fromstring(data), final


def _href(root, path, base):
    el = root.find(path, NS)
    if el is None or not (el.text or "").strip():
        return None
    return urllib.parse.urljoin(base, el.text.strip())


def discover(c):
    """[(name, url)] of the calendars that take events, starting from calendar.server."""
    start = urllib.parse.urljoin(c["server"], "/.well-known/caldav") if urllib.parse.urlparse(c["server"]).path in ("", "/") else c["server"]
    root, base = _propfind(c, start, ["<d:current-user-principal/>"])
    principal = _href(root, ".//d:current-user-principal/d:href", base)
    if not principal:
        raise CalendarError("the server did not say who you are (no current-user-principal)")
    root, base = _propfind(c, principal, ["<c:calendar-home-set/>"])
    home = _href(root, ".//c:calendar-home-set/d:href", base)
    if not home:
        raise CalendarError("the server did not give a calendar home")
    root, base = _propfind(c, home, ["<d:displayname/>", "<d:resourcetype/>", "<c:supported-calendar-component-set/>"], "1")
    found = []
    for r in root.findall("d:response", NS):
        if r.find(".//d:resourcetype/c:calendar", NS) is None:
            continue
        comps = [x.get("name") for x in r.findall(".//c:supported-calendar-component-set/c:comp", NS)]
        if comps and not {"VEVENT", "VTODO"} & set(comps):
            continue
        name = (r.findtext(".//d:displayname", "", NS) or "").strip()
        url = urllib.parse.urljoin(base, r.findtext("d:href", "", NS).strip())
        found.append((name or url.rstrip("/").rsplit("/", 1)[-1], url))
    return found


def _text(s):
    """RFC 5545 TEXT value: escape, and drop Markdown emphasis that calendar apps show literally."""
    s = re.sub(r"\*\*|__", "", s.strip())
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _fold(line):
    """Fold a content line as RFC 5545 section 3.1 asks: at most 75 bytes per line, continuation
    lines start with a space, and a UTF-8 character is never split."""
    out, b = [], line.encode()
    while len(b) > 74:
        cut = 74
        while cut and (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(b[:cut].decode())
        b = b[cut:]
    out.append(b.decode())
    return "\r\n ".join(out)


def ics(uid, start, minutes, title, notes, alarm, kind="task"):
    """A VTODO (kind "task": due at `start`, ticked off once read) or a VEVENT (kind "event")."""
    utc = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    comp = "VTODO" if kind == "task" else "VEVENT"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//amanuense//digest//EN", f"BEGIN:{comp}",
             f"UID:{uid}", f"DTSTAMP:{utc(datetime.now(timezone.utc))}", f"DTSTART:{utc(start)}"]
    if comp == "VTODO":
        lines += [f"DUE:{utc(start + timedelta(minutes=minutes))}", "STATUS:NEEDS-ACTION"]
    else:
        lines += [f"DTEND:{utc(start + timedelta(minutes=minutes))}", "TRANSP:TRANSPARENT"]
    lines += [f"SUMMARY:{_text(title)}", f"DESCRIPTION:{_text(notes)}", "CLASS:PRIVATE"]
    if alarm:  # relative to DTSTART for both kinds
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_text(title)}", "TRIGGER;RELATED=START:PT0S",
                  "END:VALARM"]
    lines += [f"END:{comp}", "END:VCALENDAR"]
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


def event_start(c, day, now=None):
    """calendar.time on the digest's day; if that has passed (a late run, a manual put), in two minutes,
    so the reminder still goes off."""
    now = now or datetime.now().astimezone()
    hh, mm = (int(x) for x in c["time"].split(":"))
    start = day.astimezone(now.tzinfo).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if start <= now:
        start = (now + timedelta(minutes=2)).replace(second=0, microsecond=0)
    return start


def create(c, base_url, uid, body):
    """PUT a new item only if none with that id exists (If-None-Match: *): never replaces one.
    Returns True if created, False if it was already there."""
    url = base_url.rstrip("/") + f"/{uid}.ics"
    status, _, _ = _request(c, "PUT", url, body, {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
    if status == 412:
        return False
    if status not in (200, 201, 204):
        raise CalendarError(f"PUT {url}: HTTP {status}")
    return True


def put(c, day, title, notes):
    """Create or replace the day's event. `day` is an aware datetime; returns the event URL."""
    if not c.get("url"):
        raise CalendarError("set calendar.url (`amanuense calendar list` shows your calendars)")
    start = event_start(c, day)
    uid = f"amanuense-{start:%Y-%m-%d}"
    url = c["url"].rstrip("/") + f"/{uid}.ics"
    status, _, _ = _request(c, "PUT", url, ics(uid, start, c["minutes"], title, notes, c["alarm"], c.get("kind", "task")),
                            {"Content-Type": "text/calendar; charset=utf-8"})
    if status not in (200, 201, 204):
        raise CalendarError(f"PUT {url}: HTTP {status}")
    return start


# Reading events (read-only): `amanuense calendar events` lists what is coming, for you or an assistant.

def read_events(c, url, start, end):
    """VEVENTs of one calendar that overlap [start, end) (aware datetimes), soonest first.
    The server is asked to expand repeating events; one it returns unexpanded keeps its first time
    and is marked repeats=True. Only REPORT is sent: nothing on the server changes."""
    f = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rng = f'start="{f(start)}" end="{f(end)}"'
    body = ('<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
            f'<c:calendar-data><c:expand {rng}/></c:calendar-data></d:prop><c:filter><c:comp-filter name="VCALENDAR">'
            f'<c:comp-filter name="VEVENT"><c:time-range {rng}/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')
    status, data, _ = _request(c, "REPORT", url, body, {"Depth": "1", "Content-Type": "application/xml"})
    if status != 207:
        raise CalendarError(f"REPORT {url}: HTTP {status}")
    out = []
    for el in ET.fromstring(data).iter("{urn:ietf:params:xml:ns:caldav}calendar-data"):
        for e in parse_events(el.text or ""):
            stop = e["end"] or e["start"]
            if e["repeats"] or (e["start"] < end and (stop > start or e["start"] >= start)):
                out.append(e)
    return sorted(out, key=lambda e: e["start"])


def parse_events(text):
    """The VEVENTs of an iCalendar text as dicts: start/end (aware), all_day, title, place, status,
    notes, repeats."""
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    events, cur = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            e = _event(cur)
            if e:
                events.append(e)
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *params = head.split(";")
            cur.setdefault(name.upper(), (dict(p.split("=", 1) for p in params if "=" in p), value))
    return events


def _when(prop):
    if not prop:
        return None, False
    params, v = prop
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        return datetime.strptime(v[:8], "%Y%m%d").astimezone(), True
    d = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
    if v.endswith("Z"):
        return d.replace(tzinfo=timezone.utc).astimezone(), False
    try:
        from zoneinfo import ZoneInfo
        return d.replace(tzinfo=ZoneInfo(params["TZID"])).astimezone(), False
    except Exception:  # no or unknown TZID: local time
        return d.astimezone(), False


def _untext(s):
    return re.sub(r"\\([\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), s)


def _event(p):
    try:
        start, all_day = _when(p.get("DTSTART"))
        end, _ = _when(p.get("DTEND"))
    except ValueError:
        return None
    if not start:
        return None
    get = lambda k: _untext(p[k][1]).strip() if k in p else ""
    return {"start": start, "end": end, "all_day": all_day, "title": get("SUMMARY") or "(no title)",
            "place": get("LOCATION"), "status": get("STATUS").lower(), "notes": get("DESCRIPTION"),
            "repeats": "RRULE" in p, "uid": get("UID"), "senders": {a for a in get("X-AMANUENSE-FROM").lower().split(",") if a},
            "people": {a for a in get("X-AMANUENSE-PEOPLE").lower().split(",") if a}}


def items(c, url, start, end):
    """The events of one calendar in [start, end), unexpanded, each as parse_events gives it plus
    href (full URL), etag and raw (the iCalendar text), so one can be updated with If-Match.
    Only REPORT is sent: nothing on the server changes."""
    f = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body = ('<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
            '<d:getetag/><c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR">'
            f'<c:comp-filter name="VEVENT"><c:time-range start="{f(start)}" end="{f(end)}"/></c:comp-filter>'
            '</c:comp-filter></c:filter></c:calendar-query>')
    status, data, _ = _request(c, "REPORT", url, body, {"Depth": "1", "Content-Type": "application/xml"})
    if status != 207:
        raise CalendarError(f"REPORT {url}: HTTP {status}")
    out = []
    for r in ET.fromstring(data).iter("{DAV:}response"):
        href, etag, raw = r.findtext("d:href", "", NS), r.findtext(".//d:getetag", "", NS), r.findtext(".//c:calendar-data", "", NS)
        for e in parse_events(raw or "")[:1]:
            if e["start"] < end and (e["end"] or e["start"]) >= start:
                out.append({**e, "href": urllib.parse.urljoin(url, href), "etag": etag, "raw": raw})
    return sorted(out, key=lambda e: e["start"])


def update(c, url, body, etag):
    """PUT a changed event only if it is unchanged on the server since it was read (If-Match).
    Returns True if updated, False if it changed meanwhile."""
    status, _, _ = _request(c, "PUT", url, body, {"Content-Type": "text/calendar; charset=utf-8", "If-Match": etag})
    if status == 412:
        return False
    if status not in (200, 201, 204):
        raise CalendarError(f"PUT {url}: HTTP {status}")
    return True


def fetch(c, base_url, uid):
    """The event with this id (as parse_events gives it), or None if it is gone. Read-only (GET)."""
    url = base_url.rstrip("/") + f"/{uid}.ics"
    status, data, _ = _request(c, "GET", url)
    if status in (404, 410):
        return None
    if status != 200:
        raise CalendarError(f"GET {url}: HTTP {status}")
    found = parse_events(data.decode("utf-8", "replace") if isinstance(data, bytes) else "")
    return found[0] if found else None

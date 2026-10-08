"""Deliver the digest as an event in a CalDAV calendar, so a phone that syncs it shows a reminder.

One event per day, with a fixed id (amanuense-YYYY-MM-DD), so a rerun replaces it. The title
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
        if comps and "VEVENT" not in comps:
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
    out, b = [], line.encode()
    while len(b) > 74:
        cut = 74
        while cut and (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(b[:cut].decode())
        b = b[cut:]
    out.append(b.decode())
    return "\r\n ".join(out)


def ics(uid, start, minutes, title, notes, alarm):
    utc = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//amanuense//digest//EN", "BEGIN:VEVENT",
             f"UID:{uid}", f"DTSTAMP:{utc(datetime.now(timezone.utc))}", f"DTSTART:{utc(start)}",
             f"DTEND:{utc(start + timedelta(minutes=minutes))}", f"SUMMARY:{_text(title)}",
             f"DESCRIPTION:{_text(notes)}", "TRANSP:TRANSPARENT", "CLASS:PRIVATE"]
    if alarm:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_text(title)}", "TRIGGER:PT0S", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
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


def put(c, day, title, notes):
    """Create or replace the day's event. `day` is an aware datetime; returns the event URL."""
    if not c.get("url"):
        raise CalendarError("set calendar.url (`amanuense calendar list` shows your calendars)")
    start = event_start(c, day)
    uid = f"amanuense-{start:%Y-%m-%d}"
    url = c["url"].rstrip("/") + f"/{uid}.ics"
    status, _, _ = _request(c, "PUT", url, ics(uid, start, c["minutes"], title, notes, c["alarm"]),
                            {"Content-Type": "text/calendar; charset=utf-8"})
    if status not in (200, 201, 204):
        raise CalendarError(f"PUT {url}: HTTP {status}")
    return start

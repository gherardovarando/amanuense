"""Digest layouts are Jinja templates; this module prepares their data and renders them.

A template is chosen by name (looked up as NAME.j2 or NAME.*.j2, first in the
config folder's templates/ directory, then among the shipped ones) or by path.
The variables a template sees are listed in `context` below and in the README.
"""
import re
from datetime import datetime
from importlib import resources
from pathlib import Path

from jinja2 import ChoiceLoader, select_autoescape, FileSystemLoader, StrictUndefined, TemplateNotFound, TemplateSyntaxError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

from . import config, events
from .triage import parse_date, short_from
from .window import describe, describe_duration, parse_duration

PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}


class TemplateError(RuntimeError):
    pass


def shipped_dir():
    return Path(str(resources.files("amanuense") / "data" / "templates"))


def user_dir():
    return config.config_dir() / "templates"


def available():
    """{name: path} of every template, user ones overriding shipped ones of the same name."""
    found = {}
    for d in (shipped_dir(), user_dir()):
        if d.is_dir():
            for f in sorted(d.glob("*.j2")):
                if not f.name.startswith("_"):  # _macros.j2 and the like are building blocks
                    found[f.name.split(".")[0]] = f
    return found


def find(name):
    """A template name ("brief") or a path to a template file."""
    if "/" in name or name.endswith(".j2"):
        p = config.expand(name)
        if not p.exists():
            raise TemplateError(f"no template file {p}")
        return p
    t = available().get(name)
    if not t:
        raise TemplateError(f"no template {name!r}; available: {', '.join(available())}")
    return t


def description(path):
    """First line of the template's leading {# comment #}."""
    m = re.match(r"\s*\{#\s*(.+?)\s*(?:\n|#\})", Path(path).read_text())
    return m.group(1) if m else ""


def _date(value, fmt="%a %d %b"):
    if not value:
        return ""
    d = value if isinstance(value, datetime) else parse_date(value) if "T" in str(value) else datetime.fromisoformat(str(value))
    return f"{d:{fmt}}"


def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def environment(path):
    env = SandboxedEnvironment(  # sandboxed: a template can format data, not touch the system
        loader=ChoiceLoader([FileSystemLoader(str(path.parent)), FileSystemLoader(str(user_dir())),
                             FileSystemLoader(str(shipped_dir()))]),
        undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
        autoescape=select_autoescape(["html", "html.j2", "htm.j2", "xml.j2"], default_for_string=False, default=False))
    env.filters.update(date=_date, plural=_plural, sender=short_from)
    return env


def priority(m, cfg):
    """The model's priority. Small models call most things urgent, so "high" with a deadline
    beyond output.urgent_within becomes "normal". Results from before priorities count as normal/low."""
    ai = m["ai"]
    p = ai.get("priority") or ("normal" if ai["needs_action"] else "low")
    if p == "high" and ai.get("deadline"):
        limit = datetime.now() + parse_duration(cfg["output"]["urgent_within"])
        if ai["deadline"] > f"{limit:%Y-%m-%d}":
            return "normal"
    return p


def item(m, cfg):
    """One email as templates see it."""
    ai = m.get("ai") or {}
    expires = datetime.fromisoformat(m["expires"]) if m.get("expires") else None
    return {
        "uid": m["uid"], "folder": m.get("folder"), "folder_label": m.get("folder_label"),
        "sender": short_from(m.get("from")), "from": m.get("from"), "subject": m.get("subject") or "(no subject)",
        "date": parse_date(m["date"]) if m.get("date") else None,
        "attachments": m.get("attachments") or [],
        "triaged": bool(ai), "handled": bool(m.get("handled")), "older_unread": bool(m.get("outside_window")),
        "skipped": bool(m.get("skip")), "error": m.get("error"),
        "category": ai.get("category"), "category_from_tag": bool(ai.get("category_from_tag")),
        "needs_action": bool(ai.get("needs_action")), "todo": ai.get("todo") or ai.get("summary") or "",
        "deadline": ai.get("deadline"), "priority": priority(m, cfg) if ai else None,
        "summary": ai.get("summary") or "", "sensitive": bool(m.get("sensitive")),
        "event": ai.get("event"),  # {title, date, start, end, place} or None
        "event_status": m.get("event_status"),  # "added", "merged", "exists", "would add", "failed" or None
        "event_other": m.get("event_other"),  # title of another event at the same time (the same meeting if merged/exists)
        "task": m.get("task"),  # "added", "would add", "noted" (on the thread's task), "would note", "exists" or None
        "earlier": m.get("earlier"),  # "completed" or "deleted": the new task is about a task closed before
        "expires": expires, "days_left": (expires - datetime.now().astimezone()).days if expires else None,
    }


def wait_item(s):
    w = s.get("waiting") or {}
    return {
        "uid": s["uid"], "subject": s.get("subject") or "(no subject)",
        "to": ", ".join(short_from(t) for t in s.get("to") or []), "to_list": [short_from(t) for t in s.get("to") or []],
        "date": parse_date(s["date"]), "days": s.get("days"),
        "waiting_for": w.get("waiting_for") or "", "category": w.get("category"), "task": s.get("task"),
    }


def context(cfg, msgs, opts, elapsed, task_error=None, waits=()):
    cats = cfg["categories"]
    items = [item(m, cfg) for m in msgs]
    triaged = [i for i in items if i["triaged"]]
    open_ = [i for i in triaged if not i["handled"]]
    todos = sorted((i for i in open_ if i["needs_action"]),
                   key=lambda i: (PRIORITY_RANK[i["priority"]], i["deadline"] or "9999", cats.index(i["category"])))
    fyi = sorted((i for i in open_ if not i["needs_action"] and i["priority"] != "low"),
                 key=lambda i: (PRIORITY_RANK[i["priority"]], cats.index(i["category"])))
    low = [i for i in open_ if not i["needs_action"] and i["priority"] == "low"]
    by_category = []
    for c in cats:
        ms = sorted((i for i in triaged if i["category"] == c),
                    key=lambda i: (not i["needs_action"], i["handled"], PRIORITY_RANK[i["priority"]], i["deadline"] or "9999"))
        if ms:
            by_category.append({"name": c, "items": ms})

    upcoming = [item(m, cfg) for m in events.upcoming(msgs)]  # soonest first, each event once

    def count(xs):
        return {c: n for c in cats if (n := sum(1 for i in xs if i["category"] == c))}

    return {
        "now": datetime.now().astimezone(),
        "window": {"start": opts.start, "end": opts.end, "text": describe(opts.start, opts.end)},
        "unread_included": opts.unread, "model": cfg["model"]["name"], "minutes": round(elapsed / 60),
        "categories": cats, "problems": list(opts.problems), "task_error": task_error,
        "tagging": opts.tag, "dry_run": opts.dry_run,
        "messages": items, "triaged": triaged, "todos": todos, "urgent": [i for i in todos if i["priority"] == "high"],
        "fyi": fyi, "low": low, "handled": [i for i in triaged if i["handled"]],
        "skipped": [i for i in items if i["skipped"]], "errors": [i for i in items if i["error"]],
        "expiring": sorted((i for i in items if i["expires"]), key=lambda i: i["expires"]),
        "older_unread": [i for i in items if i["older_unread"]],
        "events": upcoming,
        "events_calendar": bool(cfg["events"]["url"]),
        "by_category": by_category, "count": count,
        "waits": [wait_item(s) for s in waits],
        "waiting_after": describe_duration(cfg["waiting"]["after"]),
        "waiting_lookback": describe_duration(cfg["waiting"]["lookback"]),
        "out": cfg["output"],  # any key you put under [output] (e.g. max_fyi) is out.<key>
    }


def render(cfg, name, msgs, opts, elapsed, task_error=None, waits=()):
    path = find(name)
    try:
        text = environment(path).get_template(path.name).render(context(cfg, msgs, opts, elapsed, task_error, waits))
    except TemplateSyntaxError as e:
        raise TemplateError(f"{path}, line {e.lineno}: {e.message}") from None
    except (UndefinedError, TemplateNotFound) as e:
        raise TemplateError(f"{path}: {e}") from None
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"  # forgive stray blank lines

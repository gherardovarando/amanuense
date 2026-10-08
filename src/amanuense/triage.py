"""Select the messages of a run, ask the model about each one, and optionally tag them."""
import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import config, model


@dataclass
class Options:
    start: datetime
    end: datetime
    unread: bool = True
    max_unread: int = 100
    max_messages: int = 300
    skip: bool = True
    tag: bool = False
    retag: bool = False
    dry_run: bool = False
    triage_tagged: bool = True  # False for the tag command: mail already tagged is left alone


def addr(s):
    m = re.search(r"<([^>]+)>", s or "")
    return (m.group(1) if m else (s or "")).strip().lower()


def parse_date(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.astimezone()


def short_from(s):
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', s or "")
    return m.group(1).strip() if m else (s or "?")


def is_skipped(cfg, m):
    sk = cfg["skip"]
    return bool(re.search(sk["subject"], m.get("subject") or "", re.I)
                or re.search(sk["from"], m.get("from") or "", re.I))


async def collect(cfg, box, opts):
    """Messages in the window, plus unread ones outside it; and the Sent mail needed to spot replies."""
    inbox = cfg["mail"]["inbox"]
    # IMAP SINCE/BEFORE are date-only: search a day wider on each side, then filter exactly.
    found = await box.search(inbox, since=f"{opts.start - timedelta(days=1):%Y-%m-%d}",
                             before=f"{opts.end + timedelta(days=1):%Y-%m-%d}", limit=5000)
    msgs = {m["uid"]: m for m in found if opts.start <= parse_date(m["date"]) < opts.end}
    if opts.unread:
        for m in await box.search(inbox, seen=False, limit=opts.max_unread):
            if m["uid"] not in msgs:
                m["outside_window"] = True
                msgs[m["uid"]] = m
    msgs = sorted(msgs.values(), key=lambda m: parse_date(m["date"]), reverse=True)[:opts.max_messages]
    oldest = min([parse_date(m["date"]) for m in msgs] + [opts.start])
    sent = await box.search(cfg["mail"]["sent"], since=f"{oldest - timedelta(days=1):%Y-%m-%d}", limit=5000)
    return msgs, sent


def mark_handled(msgs, sent):
    replied_ids = {s.get("inReplyTo") for s in sent if s.get("inReplyTo")}
    for m in msgs:
        sender = addr(m["from"])
        d = parse_date(m["date"])
        m["handled"] = (
            m.get("messageId") in replied_ids
            or "\\Answered" in (m.get("flags") or [])
            or any(sender in [addr(t) for t in s.get("to") or []] and parse_date(s["date"]) > d for s in sent)
        )


def tag_actions(cfg, m, retag):
    """Keyword changes for one triaged message: [("add"|"remove", keyword), ...]."""
    have = set(m.get("customKeywords") or [])
    known = config.keyword_categories(cfg)
    want = config.tag_keyword(cfg, m["ai"]["category"])
    actions = []
    if retag:
        actions += [("remove", k) for k in sorted(have) if k in known and k != want]
    if want and want not in have and (retag or not m["existing"]):
        actions.append(("add", want))
    todo = cfg["tag"]["todo_keyword"]
    if todo and m["ai"]["needs_action"] and not m["handled"] and todo not in have:
        actions.append(("add", todo))
    return actions


async def run(cfg, box, opts, log=lambda s: None):
    msgs, sent = await collect(cfg, box, opts)
    mark_handled(msgs, sent)
    known = config.keyword_categories(cfg)
    system = model.system_prompt(cfg, config.categories_text(cfg))
    inbox = cfg["mail"]["inbox"]
    for i, m in enumerate(msgs, 1):
        m["existing"] = [known[k] for k in m.get("customKeywords") or [] if k in known]
        m["skip"] = opts.skip and is_skipped(cfg, m)
        if m["skip"] or (m["existing"] and not opts.retag and not opts.triage_tagged):
            continue
        log(f"triaging {i}/{len(msgs)}")
        try:
            m.update(await box.get(inbox, m["uid"], cfg["model"]["body_chars"]))
            m["ai"] = await asyncio.to_thread(model.classify, cfg, system, m)
        except Exception as e:  # keep going; the message is listed as not triaged
            m["error"] = type(e).__name__
            log(f"message {i}: {type(e).__name__}")
            continue
        finally:
            m.pop("body", None)  # don't keep bodies longer than needed
        if m["existing"] and not opts.retag:
            m["ai"]["category"] = m["existing"][0]  # a tag already on the message wins
            m["ai"]["category_from_tag"] = True
        if opts.tag:
            m["tag_actions"] = tag_actions(cfg, m, opts.retag)
            if not opts.dry_run:
                for action, kw in m["tag_actions"]:
                    await (box.add_keyword if action == "add" else box.remove_keyword)(inbox, m["uid"], kw)
    return msgs

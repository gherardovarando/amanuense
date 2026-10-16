"""Select the messages of a run, ask the model about each one, and optionally tag them."""
import asyncio
import re
from dataclasses import dataclass, field
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
    folders: bool = True        # also read the extra folders of the config
    problems: list = field(default_factory=list)  # filled during the run: folders that could not be read
    sent: list = field(default_factory=list)      # filled during the run: the Sent mail read to spot replies


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


async def in_window(box, folder, opts):
    # IMAP SINCE/BEFORE are date-only: search a day wider on each side, then filter exactly.
    found = await box.search(folder, since=f"{opts.start - timedelta(days=1):%Y-%m-%d}",
                             before=f"{opts.end + timedelta(days=1):%Y-%m-%d}", limit=5000)
    return [m for m in found if opts.start <= parse_date(m["date"]) < opts.end]


async def extra_folders(cfg, box, opts):
    """Messages from the [[folders]] of the config. A folder with all = true (the default) is read
    whole, whatever the window: it's meant for holding areas such as quarantine or oversized mail."""
    out = []
    for f in cfg["folders"] if opts.folders else []:
        name = f["name"]
        try:
            found = (await box.search(name, limit=int(f.get("max_messages", 100))) if f.get("all", True)
                     else await in_window(box, name, opts))
        except Exception as e:  # e.g. the folder doesn't exist: report it, keep going
            opts.problems.append(f"could not read folder {name}: {str(e)[:150]}")
            continue
        for m in found:
            m["folder"] = name
            m["folder_label"] = f.get("label") or name
            # Older mail in a folder read whole was there on earlier runs (or before amanuense ran at
            # all): it shows in the digest but gets no task, so a backlog doesn't become stale tasks.
            m["before_window"] = parse_date(m["date"]) < opts.start
            if f.get("project"):
                m["project"] = f["project"]
            if f.get("expires_days"):
                m["expires"] = (parse_date(m["date"]) + timedelta(days=int(f["expires_days"]))).isoformat()
        out += found
    return out


def dedup(extra, inbox):
    """Drop extra-folder messages already seen (same Message-ID) in the inbox or an earlier folder:
    mail sent to two addresses lands in two folders with different uids. The copy kept gets
    the project of a dropped one if it has none."""
    seen = {m["messageId"]: m for m in inbox if m.get("messageId")}
    out = []
    for m in extra:
        first = seen.get(m.get("messageId")) if m.get("messageId") else None
        if first is None:
            seen[m.get("messageId")] = m
            out.append(m)
        elif m.get("project") and not first.get("project"):
            first["project"] = m["project"]
    return out


async def collect(cfg, box, opts):
    """Messages in the window, plus unread ones outside it, plus the extra folders;
    and the Sent mail needed to spot replies."""
    inbox = cfg["mail"]["inbox"]
    msgs = {m["uid"]: m for m in await in_window(box, inbox, opts)}
    if opts.unread:
        for m in await box.search(inbox, seen=False, limit=opts.max_unread):
            if m["uid"] not in msgs:
                m["outside_window"] = True
                msgs[m["uid"]] = m
    for m in msgs.values():
        m["folder"] = inbox
    extra = dedup(await extra_folders(cfg, box, opts), msgs.values())
    # Extra folders are capped by their own max_messages and always kept: they may be about to expire.
    msgs = extra + sorted(msgs.values(), key=lambda m: parse_date(m["date"]), reverse=True)[
        :max(opts.max_messages - len(extra), 0)]
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


def is_sensitive(cfg, m):
    """By the config rules only; the model's answer is added during triage. Empty rules match nothing."""
    s = cfg["sensitive"]
    return bool((s["subject"] and re.search(s["subject"], m.get("subject") or "", re.I))
                or (s["from"] and re.search(s["from"], m.get("from") or "", re.I)))


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
    sensitive, screened = cfg["sensitive"]["keyword"], cfg["sensitive"]["screened"]
    if sensitive and m.get("sensitive") and sensitive not in have:  # only ever added, never removed
        actions.append(("add", sensitive))
    if screened and screened not in have:  # checked for sensitive material, whatever the answer
        actions.append(("add", screened))
    return actions


async def run(cfg, box, opts, log=lambda s: None):
    msgs, sent = await collect(cfg, box, opts)
    opts.sent = sent
    mark_handled(msgs, sent)
    known = config.keyword_categories(cfg)
    system = model.system_prompt(cfg)
    for i, m in enumerate(msgs, 1):
        m["existing"] = [known[k] for k in m.get("customKeywords") or [] if k in known]
        m["skip"] = opts.skip and is_skipped(cfg, m)
        m["sensitive"] = is_sensitive(cfg, m)
        if m["skip"] or (m["existing"] and not opts.retag and not opts.triage_tagged):
            continue
        log(f"triaging {i}/{len(msgs)}")
        try:
            m.update(await box.get(m["folder"], m["uid"], cfg["model"]["body_chars"]))
            m["ai"] = await asyncio.to_thread(model.classify, cfg, system, m)
            m["sensitive"] = m["sensitive"] or m["ai"]["sensitive"]
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
                    await (box.add_keyword if action == "add" else box.remove_keyword)(m["folder"], m["uid"], kw)
    return msgs

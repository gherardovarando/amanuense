"""Command line: amanuense {digest,render,tag,check,init,config,category}."""
import argparse
import asyncio
import json
import os
import shlex
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path

from . import __version__, caldav, categories, config, events, model, render, runlog, samples, tasks, templates, waiting, window
from .mail import Mailbox, MailError
from .samples import FakeMailbox
from .triage import Options, run


def selection_args():
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("which mail")
    g.add_argument("-w", "--window", metavar="SPEC", help=f"time window (default: window.default in the config, 24h): {window.HELP}")
    g.add_argument("--unread", action=argparse.BooleanOptionalAction, default=None,
                   help="also include unread mail outside the window (default: window.unread in the config, on)")
    g.add_argument("--max-unread", type=int, metavar="N", help="at most N unread messages outside the window")
    g.add_argument("--max-messages", type=int, metavar="N", help="at most N messages per run, newest first")
    g.add_argument("--account", help="account name in the IMAP MCP server")
    g.add_argument("--folder", help="folder to read (default INBOX)")
    g.add_argument("--sent-folder", metavar="FOLDER", help="Sent folder, used to spot replies")
    g.add_argument("--no-skip", action="store_true", help="don't apply the skip rules")
    g.add_argument("--extra-folders", action=argparse.BooleanOptionalAction, default=True,
                   help="also read the [[folders]] of the config (default: on)")

    g = p.add_argument_group("model and mail server")
    g.add_argument("--model", metavar="NAME", help="model name, e.g. qwen3:4b-instruct")
    g.add_argument("--model-url", metavar="URL", help="model server URL (Ollama base URL, or OpenAI-compatible /v1 URL)")
    g.add_argument("--model-api", choices=["ollama", "openai"], help="model server API")
    g.add_argument("--mcp-command", metavar="CMD", help='command that starts the IMAP MCP server, e.g. "node ~/imap-mcp-server/dist/index.js"')
    g.add_argument("--categories", metavar="FILE", help="categories file (Markdown; defines the categories)")

    g = p.add_argument_group("run")
    g.add_argument("--test", action="store_true", help="use made-up emails instead of the mailbox (the model still runs)")
    g.add_argument("--dry-run", action="store_true", help="write no tags, tasks or events; the digest and the phone reminder are still written")
    g.add_argument("-v", "--verbose", action="store_true", help="progress on stderr (counts only, never mail content)")
    return p


def output_args(p):
    p.add_argument("-t", "--template", metavar="NAME|FILE",
                   help="layout: brief (default), long, tiny, todo, counts, oneline, page (HTML), json, "
                        "or your own; see `amanuense template list`")
    p.add_argument("-o", "--output", metavar="PATH",
                   help="output file; {date} and {template} are replaced; '-' prints to stdout")
    p.add_argument("--urgent-within", metavar="DUR",
                   help="urgent = act within this, e.g. 2d (default 3d); a later deadline is never urgent")


def build_parser():
    p = argparse.ArgumentParser(prog="amanuense", description="Private mail digest and tagging with a local language model.")
    p.add_argument("-c", "--config", type=Path, metavar="FILE",
                   help=f"config file (default {config.default_config_path()}, or $AMANUENSE_CONFIG)")
    p.add_argument("-q", "--quiet", action="store_true", help="no summary line")
    p.add_argument("--version", action="version", version=f"amanuense {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    sel = selection_args()

    d = sub.add_parser("digest", parents=[sel], help="write a digest of the mail",
                       description="Triage the mail in the window and write a digest.")
    output_args(d)
    d.add_argument("--tag", action=argparse.BooleanOptionalAction, default=None,
                   help="also tag the messages in the mailbox (default: tag.enabled in the config)")
    d.add_argument("--todo-keyword", metavar="KW", help="with --tag, also set this keyword on to-dos (e.g. $label4)")
    d.add_argument("--waiting", action=argparse.BooleanOptionalAction, default=None,
                   help="list sent mail still waiting for a reply (default: waiting.enabled in the config)")
    d.add_argument("--waiting-after", metavar="DUR",
                   help="waiting = no reply after this long: 36h, 7d, 2w, or a number of days (default 7d)")
    d.add_argument("--waiting-lookback", metavar="DUR",
                   help="how far back to look at sent mail, same format (default 30d)")
    d.add_argument("--waiting-tasks", action=argparse.BooleanOptionalAction, default=None,
                   help="also add a follow-up task for each (default: waiting.tasks in the config)")
    d.add_argument("--calendar", action=argparse.BooleanOptionalAction, default=None,
                   help="also put the digest in the calendar as today's task or event (default: calendar.enabled)")
    d.add_argument("--tasks", action=argparse.BooleanOptionalAction, default=None,
                   help="add a Taskwarrior task for each new to-do (default: taskwarrior.enabled in the config, "
                        "'auto' = when the task command is installed)")

    r = sub.add_parser("render", help="write the last digest again, e.g. with another template (-t)",
                       description="Render the results of the last digest run again, without reading mail or "
                                   "calling the model. Writes to the configured output path unless -o. "
                                   "With FILE, render that JSON output (-t json) instead, to stdout unless -o; '-' reads stdin, "
                                   "so you can filter with jq: amanuense render -t json -o - | jq ... | "
                                   "amanuense render -")
    r.add_argument("file", nargs="?", metavar="FILE",
                   help="a JSON output of amanuense (-t json) to render instead of the last run; '-' = stdin")
    output_args(r)
    r.add_argument("--test", action="store_true", help=argparse.SUPPRESS)

    t = sub.add_parser("tag", parents=[sel], help="tag messages in the mailbox with their category",
                       description="Set an IMAP keyword with the category on each message. Messages that "
                                   "already carry a category keyword keep it unless --retag; they are only "
                                   "checked for sensitive material if they lack the screened keyword. Mail with the "
                                   "screened keyword is skipped unless --retag.")
    t.add_argument("--retag", action="store_true", help="re-triage tagged messages and replace their category keyword")
    t.add_argument("--todo-keyword", metavar="KW", help="also set this keyword on to-dos (e.g. $label4)")

    sub.add_parser("check", parents=[sel], help="check the config, the model server and the mail server")
    i = sub.add_parser("init", help="create a config and a categories file to edit")
    i.add_argument("--force", action="store_true", help="overwrite existing files")
    sub.add_parser("config", parents=[sel], help="print the effective configuration")

    pp = sub.add_parser("prompt", help="show the model's instructions, or copy them to edit",
                        description="The instructions the model gets for each email. A prompt.md next to the config "
                                    "(model.prompt_file) replaces the built-in ones; the categories file is added after.")
    psub = pp.add_subparsers(dest="action", required=True, metavar="ACTION")
    ps = psub.add_parser("show", help="print the prompt in use, with {placeholders}")
    ps.add_argument("--filled", action="store_true", help="as the model gets it: values filled in, categories added")
    pc = psub.add_parser("copy", help="copy the built-in prompt to prompt.md next to the config, to edit it")
    pc.add_argument("--force", action="store_true", help="overwrite an existing prompt.md")

    tp = sub.add_parser("template", help="list, show, copy or preview digest templates",
                        description="Digest layouts are Jinja templates. Shipped ones can be copied to "
                                    f"{templates.user_dir()} and edited; a template there overrides the shipped one "
                                    "with the same name.")
    tsub = tp.add_subparsers(dest="action", required=True, metavar="ACTION")
    tsub.add_parser("list", help="list the templates")
    ts = tsub.add_parser("show", help="print a template's source")
    ts.add_argument("name")
    tc = tsub.add_parser("copy", help="copy a shipped template to your templates folder to edit it")
    tc.add_argument("name")
    tc.add_argument("new_name", nargs="?", help="name of the copy (default: the same name)")
    tc.add_argument("--force", action="store_true", help="overwrite your copy")
    tv = tsub.add_parser("preview", help="render a template on made-up results (no mail, no model)")
    tv.add_argument("name")

    cp = sub.add_parser("calendar", help="find your calendars, list coming events, or put the last digest in the calendar",
                        description="Deliver the digest as a daily event in a CalDAV calendar ([calendar] in the config).")
    csub2 = cp.add_subparsers(dest="action", required=True, metavar="ACTION")
    csub2.add_parser("list", help="list the calendars on calendar.server (to choose calendar.url)")
    csub2.add_parser("put", help="put the last digest in the calendar now (replaces today's event)")
    ce = csub2.add_parser("events", help="list the events of the coming days (read-only)",
                          description="List the events from today on in your CalDAV calendars, soonest first. "
                                      "Read-only: nothing on the server changes.")
    ce.add_argument("--days", type=int, default=7, help="how many days from today (default 7)")
    ce.add_argument("--calendar", action="append", metavar="NAME",
                    help="only this calendar (repeatable); default: all, except the digest's own (calendar.url)")

    lg = sub.add_parser("log", help="the past runs, counts only; or what became of the events added",
                        description="Show the run log (~/.local/state/amanuense/runs.jsonl): counts and ids only, "
                                    "never text from mail.")
    lg.add_argument("-n", type=int, default=14, help="how many runs (default 14)")
    lg.add_argument("--events", action="store_true",
                    help="each event added to events.url, and whether it is still tentative, confirmed, moved or deleted")
    lg.add_argument("--details", action="store_true", help="with --events, also print each event's title (from the calendar)")

    c = sub.add_parser("category", help="list categories, add one, or add an example",
                       description="Categories are the `## name` sections of the categories file; "
                                   "these commands edit that file for you.")
    c.add_argument("--categories", metavar="FILE", help="categories file (default: from the config)")
    csub = c.add_subparsers(dest="action", required=True, metavar="ACTION")
    csub.add_parser("list", help="list the categories with their tag keyword")
    ca = csub.add_parser("add", help="add a category")
    ca.add_argument("name", help="one word, e.g. phd")
    ca.add_argument("-d", "--description", default="", help="what belongs in this category")
    ca.add_argument("-e", "--example", action="append", default=[], metavar="TEXT",
                    help='an example, e.g. "A student asks about thesis enrolment → phd, needs action" (repeatable)')
    kw = ca.add_mutually_exclusive_group()
    kw.add_argument("-k", "--keyword", help="IMAP keyword when tagging (default: the name)")
    kw.add_argument("--no-tag", action="store_true", help="never tag messages with this category")
    ca.add_argument("--fallback", action="store_true", help="use this category when the model's answer is unknown")
    ce = csub.add_parser("example", help="add an example to a category")
    ce.add_argument("name")
    ce.add_argument("text", help='e.g. "A call for papers sent to a list → info, no action"')
    return p


def overrides(a):
    o = {}

    def put(path, value):
        if value is None:
            return
        d = o
        *keys, last = path.split(".")
        for k in keys:
            d = d.setdefault(k, {})
        d[last] = value

    g = lambda name: getattr(a, name, None)
    put("window.default", g("window"))
    put("window.unread", g("unread"))
    put("window.max_unread", g("max_unread"))
    put("window.max_messages", g("max_messages"))
    put("mail.account", g("account"))
    put("mail.inbox", g("folder"))
    put("mail.sent", g("sent_folder"))
    if g("no_skip"):
        put("skip.enabled", False)
    put("model.name", g("model"))
    put("model.url", g("model_url"))
    put("model.api", g("model_api"))
    if g("mcp_command"):
        cmd = shlex.split(a.mcp_command)
        put("mcp.command", cmd[0])
        put("mcp.args", cmd[1:])
    if g("categories"):
        put("categories_file", str(Path(a.categories).expanduser().resolve()))
    put("output.path", g("output"))
    put("output.template", g("template"))
    put("output.urgent_within", g("urgent_within"))
    put("tag.enabled", g("tag"))
    put("tag.todo_keyword", g("todo_keyword"))
    put("taskwarrior.enabled", g("tasks"))
    put("calendar.enabled", g("calendar"))
    put("waiting.enabled", g("waiting"))
    put("waiting.after", g("waiting_after"))
    put("waiting.lookback", g("waiting_lookback"))
    put("waiting.tasks", g("waiting_tasks"))
    return o


def make_options(cfg, a, command):
    w = cfg["window"]
    last = window.read_last_run(command) if w["default"].strip().lower() == "last" else None
    start, end = window.parse_window(w["default"], last_run=last)
    return Options(start=start, end=end, unread=w["unread"], max_unread=w["max_unread"],
                   max_messages=w["max_messages"], skip=cfg["skip"]["enabled"],
                   tag=command == "tag" or cfg["tag"]["enabled"], retag=getattr(a, "retag", False),
                   dry_run=a.dry_run, triage_tagged=command != "tag", folders=a.extra_folders)


def logger(a):
    return (lambda s: print(f"amanuense: {s}", file=sys.stderr)) if a.verbose else (lambda s: None)


def mailbox(cfg, a, write):
    return FakeMailbox(cfg) if a.test else Mailbox(cfg, write=write, verbose=a.verbose)


async def session(cfg, a, opts, write, followups=False):
    """One mailbox session: triage, then (for digest) the sent mail waiting for replies."""
    log = logger(a)
    async with mailbox(cfg, a, write=write) as box:
        msgs = await run(cfg, box, opts, log)
        waits = []
        if followups:
            try:
                waits = await waiting.find(cfg, box, log)
            except Exception as e:  # keep the digest even if this part fails
                opts.problems.append(f"could not check sent mail for missing replies: {type(e).__name__}: {str(e)[:150]}")
        return msgs, waits


def cmd_digest(cfg, a):
    check_templates(cfg, a)
    opts = make_options(cfg, a, "digest")
    if not a.test:  # made-up mail never mixes with real results
        opts.previous = previous_verdicts()
    t0 = time.time()
    msgs, waits = asyncio.run(session(cfg, a, opts, write=opts.tag and not opts.dry_run,
                                      followups=cfg["waiting"]["enabled"]))
    for p in opts.problems:
        print(f"amanuense: warning: {p}", file=sys.stderr)
    try:  # made-up mail never reaches a calendar; first, so tasks know which emails are about one meeting
        _, problem = events.add(cfg, msgs, dry_run=a.dry_run or a.test)
    except (OSError, ValueError) as e:
        problem = f"{type(e).__name__}: {e}"
    if problem:
        opts.problems.append(f"calendar events: {problem}")
        print(f"amanuense: warning: calendar events: {problem}", file=sys.stderr)
    task_error, replies = None, 0
    if tasks.enabled(cfg):
        try:  # made-up mail never becomes real tasks
            tasks.add_tasks(cfg, msgs, dry_run=a.dry_run or a.test)
            replies = tasks.note_replies(cfg, opts.sent, dry_run=a.dry_run or a.test)
            if cfg["waiting"]["tasks"]:
                tasks.add_followups(cfg, waits, dry_run=a.dry_run or a.test)
        except tasks.TaskError as e:
            task_error = str(e)
            print(f"amanuense: warning: {e}", file=sys.stderr)
    elapsed = time.time() - t0
    if not a.test:
        save_results(opts, elapsed, task_error, msgs, waits)
        log_run("digest", cfg, opts, msgs, elapsed, task_error, replies)
    out = write_output(cfg, a, opts, elapsed, task_error, msgs, waits)
    if cfg["calendar"]["enabled"] and not a.test:  # made-up mail never reaches the calendar
        out += ", " + deliver_calendar(cfg, opts, elapsed, task_error, msgs, waits)
    if not a.test and not a.dry_run:  # a trial run must not make `-w last` skip mail next time
        window.write_last_run("digest", opts.end)
    summary(a, msgs, elapsed, f"{len(waits)} waiting for replies, {replies} replies noted on tasks, -> {out}",
            file=sys.stderr if out == "stdout" else sys.stdout)


def results_file():
    return window.state_file("digest").with_name("last-digest.json")


def save_results(opts, elapsed, task_error, msgs, waits):
    """Keep the last run's results (local disk, mode 600) so `render` can lay them out again."""
    p = results_file()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {"start": opts.start.isoformat(), "end": opts.end.isoformat(), "unread": opts.unread, "tag": opts.tag,
            "dry_run": opts.dry_run, "problems": opts.problems, "elapsed": elapsed, "task_error": task_error,
            "msgs": [{k: v for k, v in m.items() if k != "body"} for m in msgs], "waits": waits}
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, default=str)


def outputs(cfg, a):
    """(template, path) pairs to write. -t or -o on the command line mean just that one output."""
    main = (cfg["output"]["template"], cfg["output"]["path"])
    if (getattr(a, "test", False) or getattr(a, "file", None)) and not getattr(a, "output", None):
        return [(main[0], "-")]  # made-up mail, or a JSON file given: print it, never overwrite the digest
    if getattr(a, "template", None) and not getattr(a, "output", None) and main[1] != "-" and "{template}" not in main[1]:
        # another layout of the same run goes next to the usual file, e.g. mail-2026-10-08-long.md
        p = Path(main[1])
        return [(main[0], str(p.with_name(f"{p.stem}-{{template}}{extension(main[0], p.suffix)}")))]
    if getattr(a, "template", None) or getattr(a, "output", None):
        return [main]
    return [main] + [(o["template"], o["path"]) for o in cfg["outputs"]]


def extension(name, default):
    """.html for page.html.j2, .txt for oneline.txt.j2, .json for json."""
    if name == "json":
        return ".json"
    try:
        parts = templates.find(name).name.split(".")
    except templates.TemplateError:
        return default
    return f".{parts[-2]}" if len(parts) > 2 else default


def check_templates(cfg, a):
    """Fail early (before an hour of triage) on a missing or broken template."""
    msgs, waits = samples.results(cfg)
    start, end = window.parse_window("24h")
    for name, _ in outputs(cfg, a):
        if name != "json":
            try:
                templates.render(cfg, name, msgs, Options(start=start, end=end), 60, None, waits)
            except templates.TemplateError as e:
                raise SystemExit(f"amanuense: template: {e}") from None


def write_output(cfg, a, opts, elapsed, task_error, msgs, waits):
    """Write every output; returns where they went."""
    written = []
    for name, path in outputs(cfg, a):
        try:
            if name == "json":
                text = render.to_json(cfg, msgs, opts, elapsed, task_error, waits)
            else:
                text = templates.render(cfg, name, msgs, opts, elapsed, task_error, waits)
        except templates.TemplateError as e:
            print(f"amanuense: template {e}; fix it and run `amanuense render`", file=sys.stderr)
            continue
        if path == "-":
            sys.stdout.write(text)
            written.append("stdout")
            continue
        out = config.expand(path.replace("{date}", f"{opts.end:%Y-%m-%d}").replace("{template}", Path(name).name.split(".")[0]))
        out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        written.append(str(out))
    return ", ".join(written) or "nothing written"


def deliver_calendar(cfg, opts, elapsed, task_error, msgs, waits):
    """Put the day's event in the calendar; returns a short status for the summary line."""
    c = cfg["calendar"]
    try:
        title = templates.render(cfg, c["title_template"], msgs, opts, elapsed, task_error, waits).strip()
        notes = templates.render(cfg, c["template"], msgs, opts, elapsed, task_error, waits)
        start = caldav.put(c, opts.end, title.splitlines()[0] if title else "Mail digest", notes)
        return f"calendar {c['kind']} {start:%a %d %b %H:%M}"
    except (caldav.CalendarError, templates.TemplateError, OSError) as e:
        print(f"amanuense: warning: calendar: {e}", file=sys.stderr)
        return "calendar FAILED"


def previous_verdicts():
    """The model's verdicts in the last digest, by (folder, uid, Message-ID), so held mail read on an
    earlier day keeps its to-do without another model call."""
    try:
        data = json.loads(results_file().read_text())
    except (OSError, ValueError):
        return {}
    return {(m.get("folder"), m.get("uid"), m.get("messageId")): m["ai"] for m in data.get("msgs", []) if m.get("ai")}


def load_results(cfg=None, file=None):
    """The last run's saved results, or with file the JSON output in that file ('-' = stdin)."""
    if file:
        try:
            text = sys.stdin.read() if file == "-" else config.expand(file).read_text()
            data = render.from_json(text, cfg)
        except OSError as e:
            sys.exit(f"amanuense: {file}: {e.strerror}")
        except (ValueError, KeyError, TypeError, IndexError) as e:
            sys.exit(f"amanuense: {'stdin' if file == '-' else file}: not the JSON output of amanuense ({e})")
    else:
        try:
            data = json.loads(results_file().read_text())
        except OSError:
            sys.exit("amanuense: no saved digest yet; run `amanuense digest` first")
    opts = Options(start=datetime.fromisoformat(data["start"]), end=datetime.fromisoformat(data["end"]),
                   unread=data["unread"], tag=data["tag"], dry_run=data["dry_run"], problems=data["problems"])
    return opts, data


def cmd_calendar(cfg, a):
    c = cfg["calendar"]
    try:
        if a.action == "list":
            if not c.get("server") or not c.get("username"):
                sys.exit("amanuense: set calendar.server and calendar.username first")
            for name, url in caldav.discover(c):
                print(f"{name}\n    url = \"{url}\"")
        elif a.action == "events":
            print_events(c, a.days, a.calendar)
        elif a.action == "put":
            opts, data = load_results()
            print(deliver_calendar(cfg, opts, data["elapsed"], data["task_error"], data["msgs"], data["waits"]))
    except caldav.CalendarError as e:
        sys.exit(f"amanuense: calendar: {e}")
    return 0


def print_events(c, days, names=None):
    """Each event on one line: day, time, title, place, calendar and status; under it the first line
    of its notes (for amanuense's events, what the event is for)."""
    if not c.get("server") or not c.get("username"):
        sys.exit("amanuense: set calendar.server and calendar.username first")
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days)
    cals = [(n, u) for n, u in caldav.discover(c)
            if (n in names if names else u.rstrip("/") != (c.get("url") or "").rstrip("/"))]
    if names and len(cals) < len(set(names)):
        sys.exit(f"amanuense: calendar: not found: {', '.join(set(names) - {n for n, _ in cals})} (`amanuense calendar list`)")
    found = [(e, n) for n, u in cals for e in caldav.read_events(c, u, start, end)]
    print(f"{len(found)} events, {start:%a %d %b} to {end - timedelta(days=1):%a %d %b}, "
          f"calendars: {', '.join(n for n, _ in cals)}")
    for e, n in sorted(found, key=lambda x: x[0]["start"]):
        if e["all_day"]:
            when = f"{e['start']:%a %d %b} all day"
        else:
            when = f"{e['start']:%a %d %b %H:%M}" + (f"–{e['end']:%H:%M}" if e["end"] and e["end"].date() == e["start"].date() else "")
        if e["repeats"]:  # the server didn't expand it: only its first time is known
            when = f"repeating, from {when}"
        extra = [x for x in (n, e["status"] if e["status"] not in ("", "confirmed") else "") if x]
        print(f"- {when}  {e['title']}" + (f" @ {e['place']}" if e["place"] else "") + f"  [{', '.join(extra)}]")
        first = e["notes"].strip().splitlines()[0] if e["notes"].strip() else ""
        if first:
            print(f"    {first[:200]}")


def cmd_render(cfg, a):
    opts, data = load_results(cfg, a.file)
    out = write_output(cfg, a, opts, data["elapsed"], data["task_error"], data["msgs"], data["waits"])
    if not a.quiet and out != "stdout":
        print(f"-> {out}")


def cmd_prompt(cfg, a):
    if a.action == "show":
        sys.stdout.write(model.system_prompt(cfg) if a.filled else model.prompt_template(cfg))
        if not a.quiet:
            print(f"(from {cfg['model']['prompt_file'] or 'the built-in prompt'})", file=sys.stderr)
    else:
        dst = Path(cfg["model"]["prompt_file"]) if cfg["model"]["prompt_file"] else \
            (Path(cfg["_path"]).parent if cfg["_path"] else config.config_dir()) / config.DEFAULTS["model"]["prompt_file"]
        if dst.exists() and not a.force:
            sys.exit(f"amanuense: {dst} exists (use --force to overwrite)")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((resources.files("amanuense") / "data" / "prompt.example.md").read_text())
        print(f"wrote {dst}; edit it, and check it with `amanuense prompt show --filled`")
    return 0


def cmd_template(cfg, a):
    try:
        if a.action == "list":
            mine = templates.user_dir()
            for name, path in templates.available().items():
                where = "yours" if path.parent == mine else "shipped"
                print(f"{name:10} {where:8} {templates.description(path)}")
            print(f"{'json':10} {'built-in':8} Every field of every message, for other programs.")
        elif a.action == "show":
            sys.stdout.write(templates.find(a.name).read_text())
        elif a.action == "copy":
            src = templates.find(a.name)
            dst = templates.user_dir() / (src.name if not a.new_name else a.new_name + "." + ".".join(src.name.split(".")[1:]))
            if dst.exists() and not a.force:
                sys.exit(f"amanuense: {dst} exists (use --force to overwrite)")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text())
            if not (dst.parent / "_macros.j2").exists():
                (dst.parent / "_macros.j2").write_text((templates.shipped_dir() / "_macros.j2").read_text())
            print(f"wrote {dst}; use it with -t {dst.name.split('.')[0]} or output.template")
        elif a.action == "preview":
            msgs, waits = samples.results(cfg)
            start, end = window.parse_window("24h")
            sys.stdout.write(templates.render(cfg, a.name, msgs, Options(start=start, end=end), 60, None, waits))
    except templates.TemplateError as e:
        sys.exit(f"amanuense: {e}")
    return 0


def cmd_tag(cfg, a):
    opts = make_options(cfg, a, "tag")
    t0 = time.time()
    msgs, _ = asyncio.run(session(cfg, a, opts, write=not a.dry_run))
    for p in opts.problems:
        print(f"amanuense: warning: {p}", file=sys.stderr)
    if not a.test:
        log_run("tag", cfg, opts, msgs, time.time() - t0)
    if not a.test and not a.dry_run:
        window.write_last_run("tag", opts.end)
    already = sum(1 for m in msgs if "ai" not in m and not m.get("skip") and not m.get("error"))
    summary(a, msgs, time.time() - t0, f"{already} already done{' (dry run)' if a.dry_run else ''}")


def log_run(command, cfg, opts, msgs, elapsed, task_error=None, replies=0):
    try:  # a log that can't be written never stops a run
        runlog.write(runlog.entry(command, cfg, opts, msgs, elapsed, task_error, replies))
    except (OSError, KeyError, TypeError, ValueError) as e:
        print(f"amanuense: warning: run log: {e}", file=sys.stderr)


def cmd_log(cfg, a):
    runs = runlog.read()
    if not runs:
        print(f"no runs logged yet ({runlog.path()})")
        return 0
    if not a.events:
        for e in runs[-a.n:]:
            print(runlog.line(e))
        return 0
    # each event amanuense added, once, and what became of it in the calendar
    added = {}
    for e in runs:
        for x in e["calendar_events"]:
            if x["status"] == "added":
                added[x["id"]] = {**x, "run": e["time"][:10]}
    url, c = cfg["events"]["url"], cfg["calendar"]
    if not url:
        sys.exit("amanuense: set events.url to look the events up")
    today = f"{datetime.now():%Y-%m-%d}"
    counts = {}
    for x in sorted(added.values(), key=lambda x: (x["date"], x["start"] or "")):
        try:
            now = caldav.fetch(c, url, x["id"])
        except caldav.CalendarError as err:
            sys.exit(f"amanuense: calendar: {err}")
        if now is None:
            state = "deleted"
        else:
            moved = f"{now['start']:%Y-%m-%d}" != x["date"] or (not now["all_day"] and f"{now['start']:%H:%M}" != (x["start"] or ""))
            state = ("moved, " if moved else "") + ("tentative" if now["status"] == "tentative" else "confirmed")
        if x["date"] < today:
            state += ", past"
        counts[state] = counts.get(state, 0) + 1
        title = f"  {now['title']}" if a.details and now else ""
        print(f"- {x['date']} {x['start'] or 'all day':7} added {x['run']}, mail uid {x['mail_uid']}: {state}{title}")
    print(f"{len(added)} events added: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    return 0


def summary(a, msgs, elapsed, tail, file=sys.stdout):
    if a.quiet:
        return
    st = render.stats(msgs)
    cats = ", ".join(f"{k} {v}" for k, v in st["categories"].items())
    reused = f" ({st['reused']} from earlier runs)" if st["reused"] else ""
    merged = f", {st['events_merged']} merged" if st.get("events_merged") else ""
    print(f"{st['messages']} messages ({st['outside_window']} unread outside the window, "
          f"{st['expiring']} in expiring folders), {st['triaged']} triaged{reused}"
          f"{f' [{cats}]' if cats else ''}, {st['todos']} to-dos, {st['tasks']} tasks, {st['tagged']} tagged, "
          f"{st['sensitive']} sensitive, {st['events']} events ({st['events_added']} added{merged}), {st['skipped']} skipped, "
          f"{st['errors']} errors, {elapsed / 60:.1f} min, {tail}", file=file)


def cmd_check(cfg, a):
    ok = True

    def report(name, good, detail):
        nonlocal ok
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} {name}: {detail}")

    report("config", True, cfg["_path"])
    try:
        check_templates(cfg, a)
        report("templates", True, ", ".join(f"{t} -> {p}" for t, p in outputs(cfg, a)))
    except SystemExit as e:
        report("templates", False, str(e))
    report("categories", True, f"{cfg['categories_file']}: {', '.join(cfg['categories'])} "
                               f"(fallback {cfg['fallback']})")
    report("prompt", True, cfg["model"]["prompt_file"] or "built-in (`amanuense prompt copy` to edit it)")
    try:
        start, end = window.parse_window(cfg["window"]["default"], last_run=window.read_last_run("digest"))
        report("window", True, f"{cfg['window']['default']}: {window.describe(start, end)}")
    except ValueError as e:
        report("window", False, str(e))

    tw = cfg["taskwarrior"]
    if tasks.enabled(cfg):
        try:
            n_ids, n_uids = (len(x) for x in tasks.existing_refs(cfg))
            report("taskwarrior", True, f"{tw['command']}: on, {n_ids} message-ids and {n_uids} IMAP uids already in task annotations")
        except tasks.TaskError as e:
            report("taskwarrior", False, str(e))
    else:
        found = "found" if tasks.available(cfg) else "not found"
        report("taskwarrior", True, f"off (enabled = {tw['enabled']!r}, {tw['command']} {found})")

    m = cfg["model"]
    try:
        if m["api"] == "ollama":
            with urllib.request.urlopen(m["url"].rstrip("/") + "/api/tags", timeout=10) as r:
                names = [x["name"] for x in json.load(r)["models"]]
        else:
            req = urllib.request.Request(m["url"].rstrip("/") + "/models")
            if m.get("api_key_env"):
                req.add_header("Authorization", f"Bearer {os.environ.get(m['api_key_env'], '')}")
            with urllib.request.urlopen(req, timeout=10) as r:
                names = [x["id"] for x in json.load(r)["data"]]
        found = m["name"] in names or f"{m['name']}:latest" in names
        report("model", found, f"{m['name']} at {m['url']}" + ("" if found else f" not found; available: {', '.join(names)}"))
    except Exception as e:
        report("model", False, f"{m['url']}: {e}")

    if a.test:
        report("calendars", True, "skipped (--test)")
    else:
        for name, good, detail in calendar_checks(cfg):
            report(name, good, detail)

    if a.test:
        report("mail", True, "skipped (--test)")
        for f in cfg["folders"]:
            report(f"folder {f['name']}", True, "skipped (--test)")
    else:
        async def probe():
            results = {}
            async with Mailbox(cfg, verbose=a.verbose) as box:
                await box.search(cfg["mail"]["inbox"], since=f"{datetime.now():%Y-%m-%d}", limit=1)
                for f in cfg["folders"]:
                    try:
                        results[f["name"]] = (True, len(await box.search(f["name"], limit=500)))
                    except Exception as e:
                        results[f["name"]] = (False, f"{type(e).__name__}: {str(e)[:150]}")
            return results
        try:
            results = asyncio.run(probe())
            report("mail", True, f"account {cfg['mail']['account'] or '(default)'}, folder {cfg['mail']['inbox']}")
            for name, (good, info) in results.items():
                report(f"folder {name}", good, f"{info} messages" if good else info)
        except Exception as e:
            report("mail", False, f"{type(e).__name__}: {str(e)[:200]}")
    return 0 if ok else 1


def calendar_checks(cfg):
    """[(name, ok, detail)] for the configured calendars: the digest's and the events'. Read only."""
    c = cfg["calendar"]
    wanted = [(n, u) for n, u in (("calendar", c["url"] if c["enabled"] else ""), ("events calendar", cfg["events"]["url"])) if u]
    if not wanted:
        return []
    try:
        found = {u.rstrip("/"): n for n, u in caldav.discover(c)}
    except (caldav.CalendarError, OSError) as e:
        return [(n, False, str(e)) for n, _ in wanted]
    out = [(n, u.rstrip("/") in found, f'"{found[u.rstrip("/")]}"' if u.rstrip("/") in found
            else f"{u} is not one of your calendars (`amanuense calendar list`)") for n, u in wanted]
    names = {n.lower() for n in found.values()}
    for x in cfg["events"]["check"]:  # your calendars, read before adding an event
        ok = x.rstrip("/") in found if x.startswith(("http://", "https://")) else x.lower() in names
        out.append((f"events.check {x}", ok, "found" if ok else "not one of your calendars (`amanuense calendar list`)"))
    return out


def cmd_category(cfg, a):
    path = Path(cfg["categories_file"])
    if a.action == "list":
        cats = categories.load(path)
        print(f"{path}")
        for c in cats.items:
            tag = c.keyword or "(not tagged)"
            fb = "  fallback" if c.name == cats.fallback else ""
            n = f"{c.examples} example{'' if c.examples == 1 else 's'}"
            print(f"  {c.name:<14} keyword {tag:<14} {n}{fb}")
        return 0
    if not cfg.get("_path") and not a.categories:
        raise SystemExit("amanuense: no config yet; run `amanuense init` first")
    try:
        if a.action == "add":
            keyword = "" if a.no_tag else a.keyword
            categories.add(path, a.name, a.description, a.example, keyword=keyword, fallback=a.fallback)
            print(f"added category {a.name} to {path}")
        else:
            categories.add_example(path, a.name, a.text)
            print(f"added an example to {a.name} in {path}")
    except categories.CategoryError as e:
        raise SystemExit(f"amanuense: {e}") from None
    return 0


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.command == "init":
        d, written = config.init(a.force)
        for f in written:
            print(f"wrote {f}")
        print(f"Edit the files in {d}" if written else f"{d} already has a config (use --force to overwrite)")
        return 0
    cfg = config.load(a.config, overrides(a),
                      require_file=a.command not in ("config", "category", "template", "prompt") and not getattr(a, "test", False)
                      and not getattr(a, "file", None))  # rendering a JSON file needs no mail and no model
    if a.command == "config":
        print(json.dumps({k: v for k, v in cfg.items() if k != "categories_prompt"}, indent=2, ensure_ascii=False))
        return 0
    if a.command == "category":
        return cmd_category(cfg, a)
    if a.command == "template":
        return cmd_template(cfg, a)
    if a.command == "prompt":
        return cmd_prompt(cfg, a)
    if a.command == "calendar":
        return cmd_calendar(cfg, a)
    if a.command == "log":
        return cmd_log(cfg, a)
    try:
        window.parse_window(cfg["window"]["default"])
    except ValueError as e:
        raise SystemExit(f"amanuense: {e}") from None
    try:
        if a.command == "check":
            return cmd_check(cfg, a)
        {"digest": cmd_digest, "render": cmd_render, "tag": cmd_tag}[a.command](cfg, a)
    except MailError as e:
        raise SystemExit(f"amanuense: {e}") from None
    return 0


if __name__ == "__main__":
    sys.exit(main())

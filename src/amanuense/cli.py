"""Command line: amanuense {digest,tag,check,init,config}."""
import argparse
import asyncio
import json
import os
import shlex
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import __version__, categories, config, render, tasks, window
from .mail import Mailbox, MailError
from .samples import FakeMailbox
from .triage import Options, run


def selection_args():
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("which mail")
    g.add_argument("-w", "--window", metavar="SPEC", help=f"time window (default 24h): {window.HELP}")
    g.add_argument("--unread", action=argparse.BooleanOptionalAction, default=None,
                   help="also include unread mail outside the window (default: on)")
    g.add_argument("--max-unread", type=int, metavar="N", help="at most N unread messages outside the window")
    g.add_argument("--max-messages", type=int, metavar="N", help="at most N messages per run, newest first")
    g.add_argument("--account", help="account name in the IMAP MCP server")
    g.add_argument("--folder", help="folder to read (default INBOX)")
    g.add_argument("--sent-folder", metavar="FOLDER", help="Sent folder, used to spot replies")
    g.add_argument("--no-skip", action="store_true", help="don't apply the skip rules")

    g = p.add_argument_group("model and mail server")
    g.add_argument("--model", metavar="NAME", help="model name, e.g. qwen3:4b-instruct")
    g.add_argument("--model-url", metavar="URL", help="model server URL (Ollama base URL, or OpenAI-compatible /v1 URL)")
    g.add_argument("--model-api", choices=["ollama", "openai"], help="model server API")
    g.add_argument("--mcp-command", metavar="CMD", help='command that starts the IMAP MCP server, e.g. "node ~/imap-mcp-server/dist/index.js"')
    g.add_argument("--categories", metavar="FILE", help="categories file (Markdown; defines the categories)")

    g = p.add_argument_group("run")
    g.add_argument("--test", action="store_true", help="use made-up emails instead of the mailbox (the model still runs)")
    g.add_argument("--dry-run", action="store_true", help="decide tags but don't write them to the mailbox")
    g.add_argument("-v", "--verbose", action="store_true", help="progress on stderr (counts only, never mail content)")
    return p


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
    d.add_argument("-o", "--output", metavar="PATH", help="output file; {date} is replaced; '-' prints to stdout")
    d.add_argument("--format", choices=["markdown", "json"], help="output format (default markdown)")
    d.add_argument("--tag", action=argparse.BooleanOptionalAction, default=None,
                   help="also tag the messages in the mailbox (default: tag.enabled in the config)")
    d.add_argument("--todo-keyword", metavar="KW", help="with --tag, also set this keyword on to-dos (e.g. $label4)")
    d.add_argument("--tasks", action=argparse.BooleanOptionalAction, default=None,
                   help="add a Taskwarrior task for each new to-do (default: taskwarrior.enabled in the config, "
                        "'auto' = when the task command is installed)")

    t = sub.add_parser("tag", parents=[sel], help="tag messages in the mailbox with their category",
                       description="Set an IMAP keyword with the category on each message. Messages that "
                                   "already carry a category keyword are left alone unless --retag.")
    t.add_argument("--retag", action="store_true", help="re-triage tagged messages and replace their category keyword")
    t.add_argument("--todo-keyword", metavar="KW", help="also set this keyword on to-dos (e.g. $label4)")

    sub.add_parser("check", parents=[sel], help="check the config, the model server and the mail server")
    i = sub.add_parser("init", help="create a config and a categories file to edit")
    i.add_argument("--force", action="store_true", help="overwrite existing files")
    sub.add_parser("config", parents=[sel], help="print the effective configuration")

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
    put("output.format", g("format"))
    put("tag.enabled", g("tag"))
    put("tag.todo_keyword", g("todo_keyword"))
    put("taskwarrior.enabled", g("tasks"))
    return o


def make_options(cfg, a, command):
    w = cfg["window"]
    last = window.read_last_run(command) if w["default"].strip().lower() == "last" else None
    start, end = window.parse_window(w["default"], last_run=last)
    return Options(start=start, end=end, unread=w["unread"], max_unread=w["max_unread"],
                   max_messages=w["max_messages"], skip=cfg["skip"]["enabled"],
                   tag=command == "tag" or cfg["tag"]["enabled"], retag=getattr(a, "retag", False),
                   dry_run=a.dry_run, triage_tagged=command != "tag")


def logger(a):
    return (lambda s: print(f"amanuense: {s}", file=sys.stderr)) if a.verbose else (lambda s: None)


def mailbox(cfg, a, write):
    return FakeMailbox(cfg) if a.test else Mailbox(cfg, write=write, verbose=a.verbose)


def cmd_digest(cfg, a):
    opts = make_options(cfg, a, "digest")
    t0 = time.time()
    msgs = asyncio.run(run(cfg, mailbox(cfg, a, write=opts.tag and not opts.dry_run), opts, logger(a)))
    task_error = None
    if tasks.enabled(cfg):
        try:  # made-up mail never becomes real tasks
            tasks.add_tasks(cfg, msgs, dry_run=a.dry_run or a.test)
        except tasks.TaskError as e:
            task_error = str(e)
            print(f"amanuense: warning: {e}", file=sys.stderr)
    elapsed = time.time() - t0
    fmt = cfg["output"]["format"]
    text = (render.to_json if fmt == "json" else render.to_markdown)(cfg, msgs, opts, elapsed, task_error)

    path = cfg["output"]["path"]
    if a.test and not a.output:
        path = "-"  # made-up mail: print it
    if path == "-":
        sys.stdout.write(text)
        out = "stdout"
    else:
        out = config.expand(path.replace("{date}", f"{opts.end:%Y-%m-%d}"))
        out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
    if not a.test:
        window.write_last_run("digest", opts.end)
    summary(a, msgs, elapsed, f"-> {out}", file=sys.stderr if path == "-" else sys.stdout)


def cmd_tag(cfg, a):
    opts = make_options(cfg, a, "tag")
    t0 = time.time()
    box = mailbox(cfg, a, write=not a.dry_run)
    msgs = asyncio.run(run(cfg, box, opts, logger(a)))
    if not a.test and not a.dry_run:
        window.write_last_run("tag", opts.end)
    already = sum(1 for m in msgs if m.get("existing") and "ai" not in m)
    summary(a, msgs, time.time() - t0, f"{already} already tagged{' (dry run)' if a.dry_run else ''}")


def summary(a, msgs, elapsed, tail, file=sys.stdout):
    if a.quiet:
        return
    st = render.stats(msgs)
    cats = ", ".join(f"{k} {v}" for k, v in st["categories"].items())
    print(f"{st['messages']} messages ({st['outside_window']} unread outside the window), {st['triaged']} triaged"
          f"{f' [{cats}]' if cats else ''}, {st['todos']} to-dos, {st['tasks']} tasks, {st['tagged']} tagged, "
          f"{st['skipped']} skipped, "
          f"{st['errors']} errors, {elapsed / 60:.1f} min, {tail}", file=file)


def cmd_check(cfg, a):
    ok = True

    def report(name, good, detail):
        nonlocal ok
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} {name}: {detail}")

    report("config", True, cfg["_path"])
    report("categories", True, f"{cfg['categories_file']}: {', '.join(cfg['categories'])} "
                               f"(fallback {cfg['fallback']})")
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
        report("mail", True, "skipped (--test)")
    else:
        async def probe():
            async with Mailbox(cfg, verbose=a.verbose) as box:
                return await box.search(cfg["mail"]["inbox"], since=f"{datetime.now():%Y-%m-%d}", limit=1)
        try:
            asyncio.run(probe())
            report("mail", True, f"account {cfg['mail']['account'] or '(default)'}, folder {cfg['mail']['inbox']}")
        except Exception as e:
            report("mail", False, f"{type(e).__name__}: {str(e)[:200]}")
    return 0 if ok else 1


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
                      require_file=a.command not in ("config", "category") and not getattr(a, "test", False))
    if a.command == "config":
        print(json.dumps({k: v for k, v in cfg.items() if k != "categories_prompt"}, indent=2, ensure_ascii=False))
        return 0
    if a.command == "category":
        return cmd_category(cfg, a)
    try:
        window.parse_window(cfg["window"]["default"])
    except ValueError as e:
        raise SystemExit(f"amanuense: {e}") from None
    try:
        if a.command == "check":
            return cmd_check(cfg, a)
        {"digest": cmd_digest, "tag": cmd_tag}[a.command](cfg, a)
    except MailError as e:
        raise SystemExit(f"amanuense: {e}") from None
    return 0


if __name__ == "__main__":
    sys.exit(main())

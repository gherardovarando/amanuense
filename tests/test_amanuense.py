import asyncio
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from amanuense import categories, cli, config, model, render, samples, templates, triage, window
from amanuense.samples import FakeMailbox

@pytest.fixture(autouse=True)
def no_user_config(tmp_path, monkeypatch):
    """Never read the real ~/.config/amanuense during tests."""
    monkeypatch.setenv("AMANUENSE_CONFIG", str(tmp_path / "absent" / "config.toml"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


NOW = datetime(2026, 10, 8, 15, 30, tzinfo=timezone(timedelta(hours=2)))


def test_windows():
    assert window.parse_window("24h", NOW) == (NOW - timedelta(hours=24), NOW)
    assert window.parse_window("3d", NOW)[0] == NOW - timedelta(days=3)
    start, end = window.parse_window("yesterday", NOW)
    assert (start.day, start.hour, end.day, end.hour) == (7, 0, 8, 0)
    assert window.parse_window("today", NOW)[0].hour == 0
    assert window.parse_window("week", NOW)[0].weekday() == 0
    start, end = window.parse_window("2026-10-01..2026-10-05", NOW)
    assert (start.day, end.day) == (1, 6)
    assert window.parse_window("2026-10-01", NOW) == (datetime(2026, 10, 1, tzinfo=NOW.tzinfo), NOW)
    last = NOW - timedelta(hours=5)
    assert window.parse_window("last", NOW, last_run=last) == (last, NOW)
    assert window.parse_window("last", NOW)[0] == NOW - timedelta(hours=24)
    for bad in ("soon", "2026-13-01", "2026-10-05..2026-10-01"):
        with pytest.raises(ValueError):
            window.parse_window(bad, NOW)


CATS = """# Categories

<!-- notes for people -->

## research

Papers.

Examples:
- A coauthor sends a draft → research.

## teaching
keyword: $label2

Courses.

## info
keyword: none
fallback: yes

Broadcasts.

## Edge cases

- Lists are info.
"""


def test_config_precedence(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[window]\ndefault = "3d"\n[model]\nname = "a"\n')
    (tmp_path / "categories.md").write_text(CATS)
    cfg = config.load(f, {"model": {"name": "b"}})
    assert cfg["window"]["default"] == "3d"      # file beats default
    assert cfg["model"]["name"] == "b"           # command line beats file
    assert cfg["window"]["unread"] is True       # default kept
    assert cfg["categories_file"] == str(tmp_path / "categories.md")


def test_cli_overrides():
    a = cli.build_parser().parse_args(
        ["digest", "-w", "yesterday", "--no-unread", "--mcp-command", "node ~/x/index.js", "--no-skip", "--tag"])
    o = cli.overrides(a)
    assert o["window"] == {"default": "yesterday", "unread": False}
    assert o["mcp"] == {"command": "node", "args": ["~/x/index.js"]}
    assert o["skip"] == {"enabled": False}
    assert o["tag"] == {"enabled": True}


def test_categories_file():
    c = categories.parse(CATS)
    assert c.names == ["research", "teaching", "info"]           # "Edge cases" is a note
    assert (c.keyword("research"), c.keyword("teaching"), c.keyword("info")) == ("research", "$label2", None)
    assert c.fallback == "info"
    assert c.items[0].examples == 1
    assert "keyword:" not in c.prompt and "notes for people" not in c.prompt and "Lists are info" in c.prompt
    assert categories.parse("## a\n## b\n").fallback == "b"     # default fallback: the last one
    for bad in ("no sections", "## a\n## a\n", "## a\nfallback: yes\n## b\nfallback: yes\n",
                "## a\nkeyword: \\Seen\n"):
        with pytest.raises(categories.CategoryError):
            categories.parse(bad)


def test_category_edit(tmp_path):
    f = tmp_path / "categories.md"
    f.write_text(CATS)
    categories.add(f, "phd", "Doctoral matters.", ["A student asks about enrolment → phd, needs action."], keyword="$label7")
    c = categories.load(f)
    assert c.names == ["research", "teaching", "info", "phd"] and c.keyword("phd") == "$label7"
    assert c.fallback == "info"                                  # explicit fallback unchanged
    assert f.read_text().index("## phd") < f.read_text().index("## Edge cases")
    categories.add_example(f, "teaching", "A student asks about an exam → teaching, needs action.")
    categories.add_example(f, "research", "A preprint from a coauthor → research.")
    c = categories.load(f)
    assert [x.examples for x in c.items] == [2, 1, 0, 1]
    with pytest.raises(categories.CategoryError):
        categories.add(f, "phd")
    with pytest.raises(categories.CategoryError):
        categories.add_example(f, "nope", "x")
    with pytest.raises(categories.CategoryError):
        categories.add(f, "two words")


def test_tag_keywords(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("")
    (tmp_path / "categories.md").write_text(CATS)
    cfg = config.load(f)
    assert config.tag_keyword(cfg, "teaching") == "$label2"
    assert config.tag_keyword(cfg, "info") is None
    assert config.keyword_categories(cfg) == {"research": "research", "$label2": "teaching"}


def test_obsolete_settings_warn(tmp_path, capsys):
    f = tmp_path / "config.toml"
    f.write_text('categories = ["a"]\n[tag]\nexclude = ["info"]\n')
    (tmp_path / "categories.md").write_text(CATS)
    cfg = config.load(f)
    assert cfg["categories"] == ["research", "teaching", "info"]  # the file wins
    err = capsys.readouterr().err
    assert "`categories`" in err and "`tag.exclude`" in err


def test_clean():
    r = model.clean({"category": "zzz", "needs_action": False, "todo": "x", "deadline": "soon"}, ["a", "info"], "info")
    assert (r["category"], r["todo"], r["deadline"]) == ("info", None, None)


def fake_classify(cfg, system, msg):
    cat = "admin" if "facturas" in msg["body"] else "research"
    return model.clean({"category": cat, "needs_action": True, "todo": "do it", "deadline": None, "summary": "s",
                        "sensitive": "sick leave" in msg["body"]},
                       cfg["categories"], cfg["fallback"])


async def entered(cfg, box, opts):
    async with box as b:
        return await triage.run(cfg, b, opts)


def run_fake(monkeypatch, cfg, box, **kw):
    monkeypatch.setattr(model, "classify", fake_classify)
    start, end = window.parse_window("24h")
    return asyncio.run(entered(cfg, box, triage.Options(start=start, end=end, **kw)))


def test_digest_selection_and_handled(monkeypatch):
    cfg = config.load(None, require_file=False)
    msgs = run_fake(monkeypatch, cfg, FakeMailbox(cfg))
    by_uid = {m["uid"]: m for m in msgs}
    assert len(msgs) == 6
    assert by_uid[6]["outside_window"]           # old unread mail included
    assert by_uid[5]["skip"]                     # noreply sender
    assert by_uid[2]["handled"]                  # replied to in Sent
    md = templates.render(cfg, "long", msgs, triage.Options(*window.parse_window("24h")), 1)
    assert "## Proposed to-dos" in md and "unread, older" in md

    msgs = run_fake(monkeypatch, cfg, FakeMailbox(cfg), unread=False)
    assert 6 not in {m["uid"] for m in msgs}


def test_tagging(monkeypatch):
    cfg = config.load(None, {"tag": {"todo_keyword": "$label4"}}, require_file=False)  # example categories
    box = FakeMailbox(cfg)
    box.inbox[2]["customKeywords"] = ["reviewing"]  # tagged by hand earlier
    msgs = run_fake(monkeypatch, cfg, box, tag=True, triage_tagged=False)
    assert ("add", 1, "admin") in box.written
    assert ("add", 1, "$label4") in box.written
    assert not any(uid == 3 for _, uid, _ in box.written)   # manual tag left alone
    assert not any(uid == 5 for _, uid, _ in box.written)   # skipped mail untouched
    assert not any(kw == "$label4" and uid == 2 for _, uid, kw in box.written)  # handled: no to-do

    box = FakeMailbox(cfg)
    box.inbox[2]["customKeywords"] = ["reviewing"]
    run_fake(monkeypatch, cfg, box, tag=True, retag=True)
    assert ("remove", 3, "reviewing") in box.written and ("add", 3, "research") in box.written

    box = FakeMailbox(cfg)
    run_fake(monkeypatch, cfg, box, tag=True, dry_run=True)
    assert box.written == []


@pytest.fixture
def taskdb(tmp_path, monkeypatch):
    """A throwaway Taskwarrior database; the real ~/.task is never touched."""
    from amanuense import tasks
    import shutil
    if not shutil.which("task"):
        pytest.skip("taskwarrior not installed")
    (tmp_path / "taskdata").mkdir()
    (tmp_path / "taskrc").write_text("")
    monkeypatch.setenv("TASKRC", str(tmp_path / "taskrc"))
    monkeypatch.setenv("TASKDATA", str(tmp_path / "taskdata"))
    return tasks


def test_tasks(monkeypatch, taskdb):
    cfg = config.load(None, {"mail": {"account": "work"}}, require_file=False)
    assert taskdb.enabled(cfg)                    # "auto" and task is installed
    box = FakeMailbox(cfg)
    msgs = run_fake(monkeypatch, cfg, box)
    open_todos = [m for m in msgs if "ai" in m and not m["handled"]]

    assert taskdb.add_tasks(cfg, msgs, dry_run=True) == len(open_todos)
    assert taskdb.existing_refs(cfg) == (set(), set())        # dry run wrote nothing

    assert taskdb.add_tasks(cfg, msgs) == len(open_todos)
    exported = __import__("json").loads(taskdb._task(cfg, "export", read_only=True))
    t = next(t for t in exported if "uid 1," in t["annotations"][0]["description"])
    assert t["tags"] == ["admin", "email"] and t["description"] == "do it"
    assert "message-id <1@test>" in t["annotations"][0]["description"]
    assert not any("uid 2," in t["annotations"][0]["description"] for t in exported)  # handled: no task

    assert taskdb.add_tasks(cfg, msgs) == 0                   # second run: nothing new
    assert all(m["task"] == "exists" for m in open_todos)


def test_tasks_respect_manual_annotation(monkeypatch, taskdb):
    cfg = config.load(None, {"mail": {"account": "work"}}, require_file=False)
    taskdb._task(cfg, "add", "Answer the student", "+teaching")
    taskdb._task(cfg, "1", "annotate", "2026-10-01 IMAP uid 4, account work, subject: Duda examen")
    taskdb._task(cfg, "add", "Accounting", "+admin")
    taskdb._task(cfg, "2", "annotate", "IMAP uid 999, account other, subject: x, msgid:<1@test>. PDFs in a folder")
    msgs = run_fake(monkeypatch, cfg, FakeMailbox(cfg))
    taskdb.add_tasks(cfg, msgs)
    assert next(m for m in msgs if m["uid"] == 4)["task"] == "exists"   # by account + uid
    assert next(m for m in msgs if m["uid"] == 1)["task"] == "exists"   # by msgid


def test_tasks_disabled(monkeypatch):
    from amanuense import tasks
    cfg = config.load(None, {"taskwarrior": {"enabled": "auto", "command": "no-such-task-binary"}}, require_file=False)
    assert not tasks.enabled(cfg)
    cfg = config.load(None, {"taskwarrior": {"enabled": False}}, require_file=False)
    assert not tasks.enabled(cfg)


FOLDERS = {"folders": [{"name": "Quarantine", "label": "held mail", "expires_days": 14}]}


def test_extra_folders(monkeypatch):
    cfg = config.load(None, FOLDERS, require_file=False)
    msgs = run_fake(monkeypatch, cfg, FakeMailbox(cfg))
    held = next(m for m in msgs if m["uid"] == 7)
    assert held["folder"] == "Quarantine" and "ai" in held      # read and triaged, though 4 days old
    days = templates.item(held, cfg)["days_left"]
    assert 8 <= days <= 10
    opts = triage.Options(*window.parse_window("24h"))
    md = templates.render(cfg, "long", msgs, opts, 1)
    assert "## Deleted soon" in md and "in held mail" in md

    msgs = run_fake(monkeypatch, cfg, FakeMailbox(cfg), folders=False)
    assert 7 not in {m["uid"] for m in msgs}


def test_missing_folder_is_reported(monkeypatch):
    cfg = config.load(None, {"folders": [{"name": "Nope"}]}, require_file=False)
    monkeypatch.setattr(model, "classify", fake_classify)
    opts = triage.Options(*window.parse_window("24h"))
    box = FakeMailbox(cfg)
    box.extra = {}  # the folder doesn't exist on the server
    msgs = asyncio.run(entered(cfg, box, opts))
    assert len(msgs) == 6 and "could not read folder Nope" in opts.problems[0]  # the run went on
    assert "**Problem:** could not read folder Nope" in templates.render(cfg, "brief", msgs, opts, 1)


def fresh_box(cfg):
    """FakeMailbox whose held message arrived in the window, so it can get a task."""
    box = FakeMailbox(cfg)
    box.extra[cfg["folders"][0]["name"]][0]["date"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    return box


def test_tasks_for_expiring_mail(monkeypatch, taskdb):
    cfg = config.load(None, {**FOLDERS, "mail": {"account": "work"}}, require_file=False)
    msgs = run_fake(monkeypatch, cfg, FakeMailbox(cfg))
    taskdb.add_tasks(cfg, msgs)
    held = next(m for m in msgs if m["uid"] == 7)
    assert held["before_window"] and held.get("task") is None      # 4 days old: earlier runs saw it, no task
    assert taskdb.add_tasks(cfg, [held]) == 0
    msgs = run_fake(monkeypatch, cfg, fresh_box(cfg))
    taskdb.add_tasks(cfg, msgs)
    t = next(t for t in __import__("json").loads(taskdb._task(cfg, "export", read_only=True))
             if "folder Quarantine" in t["annotations"][0]["description"])
    held = next(m for m in msgs if m["uid"] == 7)
    assert t["due"] == taskdb._utc(datetime.fromisoformat(held["expires"]))      # due when it gets deleted
    assert "IMAP uid 7, account work, folder Quarantine," in t["annotations"][0]["description"]
    ids, uids = taskdb.existing_refs(cfg)
    assert ("work", "Quarantine", 7) in uids and ("work", "INBOX", 1) in uids
    assert taskdb.add_tasks(cfg, run_fake(monkeypatch, cfg, fresh_box(cfg))) == 0


def fake_ask(cfg, s):
    asks = "could you" in s["body"].lower() or "can we" in s["body"].lower()
    return {"expects_reply": asks, "waiting_for": "an answer" if asks else None, "category": "research"}


def test_waiting_candidates():
    from amanuense import waiting
    cfg = config.load(None, require_file=False)
    box = FakeMailbox(cfg)
    now = datetime.now().astimezone()
    received = [{**m, "folder": "INBOX"} for m in box.inbox] + [{**m, "folder": "Archive"} for m in box.archive]
    found = {s["uid"] for s in waiting.candidates(cfg, box.sent, received, now)}
    assert found == {102, 103}   # Lee and Bob; Carla replied (archived), Dan's thread was followed up 2 days ago
    assert waiting.conversation({"subject": "RE: Fwd: Referee report", "to": ["Dan <dan@example.org>"]}) == \
        waiting.conversation({"subject": "Referee report", "to": ["dan@example.org"]})


def test_waiting_find(monkeypatch):
    from amanuense import waiting
    monkeypatch.setattr(waiting, "ask", fake_ask)
    cfg = config.load(None, require_file=False)

    async def go():
        async with FakeMailbox(cfg) as box:
            return await waiting.find(cfg, box)
    waits = asyncio.run(go())
    assert [s["uid"] for s in waits] == [102]                       # Bob's thank-you is not waiting
    assert waits[0]["days"] == 10 and "body" not in waits[0]


def test_followup_tasks(monkeypatch, taskdb):
    from amanuense import waiting
    monkeypatch.setattr(waiting, "ask", fake_ask)
    cfg = config.load(None, {"mail": {"account": "work"}}, require_file=False)

    async def go():
        async with FakeMailbox(cfg) as box:
            return await waiting.find(cfg, box)
    waits = asyncio.run(go())
    assert taskdb.add_followups(cfg, waits) == 1
    t = __import__("json").loads(taskdb._task(cfg, "export", read_only=True))[0]
    assert set(t["tags"]) == {"research", "email", "followup"} and t["description"] == "Follow up: an answer"
    assert "folder Sent" in t["annotations"][0]["description"]
    assert taskdb.add_followups(cfg, asyncio.run(go())) == 0          # not twice


def test_cli_digest_end_to_end(monkeypatch, tmp_path, capsys):
    """The real command path (mailbox session, waiting, render), with stub model calls."""
    from amanuense import waiting
    monkeypatch.setattr(model, "classify", fake_classify)
    monkeypatch.setattr(waiting, "ask", fake_ask)
    out = tmp_path / "digest.md"
    assert cli.main(["digest", "--test", "--no-tasks", "-o", str(out)]) == 0
    md = out.read_text()
    assert "## To do" in md and "## Waiting for replies" in md and "Figure 2" in md
    assert "1 waiting for replies" in capsys.readouterr().out
    assert cli.main(["digest", "--test", "--no-tasks", "-t", "long", "-o", str(out)]) == 0
    assert "## Proposed to-dos" in out.read_text()
    assert cli.main(["tag", "--test", "--dry-run"]) == 0


def test_durations():
    assert window.parse_duration(7) == timedelta(days=7)
    assert window.parse_duration("36h") == timedelta(hours=36)
    assert window.parse_duration("2w") == timedelta(weeks=2)
    assert window.describe_duration("2w") == "2 weeks" and window.describe_duration("1d") == "1 day"
    with pytest.raises(ValueError):
        window.parse_duration("soon")
    a = cli.build_parser().parse_args(["digest", "--waiting-after", "3d", "--waiting-lookback", "2w"])
    assert cli.overrides(a)["waiting"] == {"after": "3d", "lookback": "2w"}
    with pytest.raises(SystemExit):
        config.load(None, {"waiting": {"after": "a while"}}, require_file=False)


def test_waiting_after_option():
    from amanuense import waiting
    cfg = config.load(None, {"waiting": {"after": "11d"}}, require_file=False)
    box = FakeMailbox(cfg)
    now = datetime.now().astimezone()
    received = [{**m, "folder": "INBOX"} for m in box.inbox] + [{**m, "folder": "Archive"} for m in box.archive]
    assert {s["uid"] for s in waiting.candidates(cfg, box.sent, received, now)} == set()  # Lee: 10 days < 11


def fake_priorities(cfg, system, msg):
    """uid 1: urgent to-do, uid 3: normal to-do, uid 4: worth knowing, others: low-priority FYI."""
    need, prio = {1: (True, "high"), 3: (True, "normal"), 4: (False, "normal")}.get(int(msg["uid"]), (False, "low"))
    return model.clean({"category": "research", "needs_action": need, "todo": f"todo {msg['uid']}" if need else None,
                        "deadline": None, "priority": prio, "summary": f"summary {msg['uid']}"},
                       cfg["categories"], cfg["fallback"])


def test_far_deadline_is_not_urgent():
    cfg = config.load(None, {}, require_file=False)
    soon, later = (f"{datetime.now() + timedelta(days=d):%Y-%m-%d}" for d in (1, 12))
    m = lambda dl: {"ai": {"needs_action": True, "priority": "high", "deadline": dl}}
    assert [templates.priority(m(d), cfg) for d in (soon, later, None)] == ["high", "normal", "high"]
    cfg["output"]["urgent_within"] = "2w"
    assert templates.priority(m(later), cfg) == "high"


def test_clean_priority():
    r = model.clean({"category": "info", "needs_action": True, "todo": "x", "priority": "urgent!"}, ["info"], "info")
    assert r["priority"] == "normal"
    r = model.clean({"category": "info", "needs_action": False, "priority": None}, ["info"], "info")
    assert r["priority"] == "low"


def test_brief_digest(monkeypatch):
    cfg = config.load(None, {}, require_file=False)
    monkeypatch.setattr(model, "classify", fake_priorities)
    start, end = window.parse_window("24h")
    opts = triage.Options(start=start, end=end, folders=False)
    msgs = asyncio.run(entered(cfg, FakeMailbox(cfg), opts))
    md = templates.render(cfg, "brief", msgs, opts, 60)
    assert md.index("todo 1") < md.index("todo 3") and "**Urgent**" in md
    assert "## Worth knowing" in md and "summary 4" in md
    assert "summary 6" not in md and "low-priority (research" in md
    assert "already answered" in md and "skipped by the skip rules" in md
    full = templates.render(cfg, "long", msgs, opts, 60)
    assert "summary 6" in full and "fyi, low" in full


def test_render_saved_results(monkeypatch, tmp_path):
    cfg = config.load(None, {}, require_file=False)
    monkeypatch.setattr(model, "classify", fake_priorities)
    start, end = window.parse_window("24h")
    opts = triage.Options(start=start, end=end, folders=False)
    msgs = asyncio.run(entered(cfg, FakeMailbox(cfg), opts))
    cli.save_results(opts, 60, None, msgs, [])
    assert oct(cli.results_file().stat().st_mode & 0o777) == "0o600"
    out = tmp_path / "again.md"
    assert cli.main(["render", "--test", "-t", "long", "-o", str(out)]) == 0
    assert "summary 6" in out.read_text()
    assert cli.main(["render", "--test", "-o", str(out)]) == 0
    assert "summary 6" not in out.read_text()


def test_shipped_templates_render():
    cfg = config.load(None, {}, require_file=False)
    msgs, waits = samples.results(cfg)
    opts = triage.Options(*window.parse_window("24h"))
    names = set(templates.available())
    assert {"brief", "long", "tiny", "todo", "counts", "oneline", "page"} <= names and "_macros" not in names
    for name in names:
        assert templates.render(cfg, name, msgs, opts, 60, None, waits).strip(), name
    one = templates.render(cfg, "oneline", msgs, opts, 60, None, waits)
    assert one.count("\n") == 1 and "2 urgent" in one


def test_counts_and_oneline_have_no_mail_text():
    cfg = config.load(None, {}, require_file=False)
    msgs, waits = samples.results(cfg)
    opts = triage.Options(*window.parse_window("24h"))
    text = "".join(templates.render(cfg, n, msgs, opts, 60, None, waits) for n in ("counts", "oneline"))
    for m in msgs:
        for value in (m.get("subject"), m.get("from"), (m.get("ai") or {}).get("summary"), (m.get("ai") or {}).get("todo")):
            if value and len(value) > 3:
                assert value not in text
    assert "Prof Lee" not in text and "Figure 2" not in text


def test_mail_text_is_inert_in_templates():
    cfg = config.load(None, {}, require_file=False)
    msgs, waits = samples.results(cfg)
    msgs[0]["ai"]["todo"] = "Pay <b>now</b> {{ out }} {% if 1 %}x{% endif %}"
    opts = triage.Options(*window.parse_window("24h"))
    assert "Pay &lt;b&gt;now&lt;/b&gt; {{ out }} {% if 1 %}x{% endif %}" in templates.render(cfg, "page", msgs, opts, 60, None, waits)
    assert "Pay <b>now</b> {{ out }} {% if 1 %}x{% endif %}" in templates.render(cfg, "brief", msgs, opts, 60, None, waits)


def test_user_template_and_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = config.load(None, {}, require_file=False)
    assert cli.main(["template", "copy", "tiny", "mine"]) == 0
    mine = templates.user_dir() / "mine.md.j2"
    assert mine.exists() and (templates.user_dir() / "_macros.j2").exists()
    mine.write_text("{% import '_macros.j2' as x %}{{ todos | length }} things; {{ out.greeting }}\n")
    cfg["output"]["greeting"] = "hello"
    msgs, waits = samples.results(cfg)
    opts = triage.Options(*window.parse_window("24h"))
    assert templates.render(cfg, "mine", msgs, opts, 60) == "5 things; hello\n"
    mine.write_text("{{ todos | length }} {{ nonsense }}\n")
    with pytest.raises(templates.TemplateError, match="nonsense"):
        templates.render(cfg, "mine", msgs, opts, 60)
    mine.write_text("{% for %}\n")
    with pytest.raises(templates.TemplateError, match="line 1"):
        templates.render(cfg, "mine", msgs, opts, 60)
    with pytest.raises(templates.TemplateError, match="available"):
        templates.find("nope")
    cfg["output"]["template"] = "mine"
    with pytest.raises(SystemExit, match="template"):  # a digest stops before reading any mail
        cli.check_templates(cfg, cli.build_parser().parse_args(["digest"]))


def test_several_outputs(monkeypatch, tmp_path):
    from amanuense import waiting
    monkeypatch.setattr(model, "classify", fake_classify)
    monkeypatch.setattr(waiting, "ask", fake_ask)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f"""
categories_file = "{config.resources.files('amanuense') / 'data' / 'categories.example.md'}"
[output]
template = "brief"
path = "{tmp_path}/{{date}}/mail.md"
[[outputs]]
template = "oneline"
path = "{tmp_path}/{{date}}/mail.txt"
[[outputs]]
template = "page"
path = "{tmp_path}/{{date}}/mail.html"
""")
    a = cli.build_parser().parse_args(["-c", str(cfg_file), "digest"])
    cfg = config.load(cfg_file, {}, require_file=True)
    paths = [p for _, p in cli.outputs(cfg, a)]
    assert [p.rsplit(".", 1)[1] for p in paths] == ["md", "txt", "html"]
    for t, ext in (("long", "md"), ("page", "html"), ("json", "json")):
        a = cli.build_parser().parse_args(["-c", str(cfg_file), "render", "-t", t])
        cfg = config.load(cfg_file, cli.overrides(a))  # -t only changes the template, the file goes next to the usual one
        assert cli.outputs(cfg, a) == [(t, f"{tmp_path}/{{date}}/mail-{{template}}.{ext}")]


@pytest.fixture
def fake_caldav(tmp_path):
    """A tiny CalDAV server: discovery (principal -> home -> calendars) and PUT."""
    import http.server
    import threading
    store = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _auth(self):
            import base64
            ok = self.headers.get("Authorization") == "Basic " + base64.b64encode(b"me:secret").decode()
            if not ok:
                self.send_response(401)
                self.end_headers()
            return ok

        def _xml(self, body):
            data = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                    + body + "</d:multistatus>").encode()
            self.send_response(207)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_PROPFIND(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if not self._auth():
                return
            if self.path == "/.well-known/caldav":
                self.send_response(301)
                self.send_header("Location", "/dav/")
                self.end_headers()
            elif self.path == "/dav/":
                self._xml("<d:response><d:href>/dav/</d:href><d:propstat><d:prop><d:current-user-principal>"
                          "<d:href>/dav/principals/me/</d:href></d:current-user-principal></d:prop></d:propstat></d:response>")
            elif self.path == "/dav/principals/me/":
                self._xml("<d:response><d:href>/dav/principals/me/</d:href><d:propstat><d:prop><c:calendar-home-set>"
                          "<d:href>/dav/calendars/me/</d:href></c:calendar-home-set></d:prop></d:propstat></d:response>")
            else:
                cal = lambda href, name, comp: (
                    f"<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:displayname>{name}</d:displayname>"
                    f"<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><c:supported-calendar-component-set>"
                    f'<c:comp name="{comp}"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response>')
                self._xml("<d:response><d:href>/dav/calendars/me/</d:href><d:propstat><d:prop><d:resourcetype>"
                          "<d:collection/></d:resourcetype></d:prop></d:propstat></d:response>"
                          + cal("/dav/calendars/me/personal/", "Personal", "VEVENT")
                          + cal("/dav/calendars/me/tasks/", "Tasks", "VTODO"))

        def do_PUT(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            if not self._auth():
                return
            created = self.path not in store
            store[self.path] = body
            self.send_response(201 if created else 204)
            self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    pw = tmp_path / "pw"
    pw.write_text("secret\n")
    pw.chmod(0o600)
    yield {"server": f"http://127.0.0.1:{srv.server_port}/", "store": store, "password_file": str(pw)}
    srv.shutdown()


def test_ics_escaping_and_folding():
    from amanuense import caldav
    start = datetime(2026, 10, 9, 7, 30).astimezone()
    text = caldav.ics("amanuense-2026-10-09", start, 15, "**5 to-dos**, 2 urgent; ok",
                      "- ❗ " + "é" * 60 + "\n- second", alarm=True)
    lines = text.split("\r\n")
    assert all(len(l.encode()) <= 75 for l in lines)
    unfolded = text.replace("\r\n ", "")
    assert r"SUMMARY:5 to-dos\, 2 urgent\; ok" in unfolded and "\\n- second" in unfolded
    assert "BEGIN:VALARM" in text and "UID:amanuense-2026-10-09" in text
    assert "BEGIN:VTODO" in text and "DUE:" in text and "STATUS:NEEDS-ACTION" in text
    event = caldav.ics("x", start, 15, "t", "n", alarm=False, kind="event")
    assert "BEGIN:VEVENT" in event and "DTEND:" in event and "VALARM" not in event


def test_calendar_discover_and_put(fake_caldav, tmp_path):
    from amanuense import caldav
    c = {**config.DEFAULTS["calendar"], "enabled": True, "server": fake_caldav["server"], "username": "me",
         "password_file": fake_caldav["password_file"]}
    found = caldav.discover(c)
    assert [n for n, _ in found] == ["Personal", "Tasks"]   # calendars for events and for tasks
    c["url"] = found[0][1]
    day = (datetime.now() + timedelta(days=1)).replace(hour=5, minute=31).astimezone()
    start = caldav.put(c, day, "5 to-dos", "notes")
    assert f"{start:%H:%M}" == "07:30"
    assert next(iter(fake_caldav["store"])).endswith(f"/dav/calendars/me/personal/amanuense-{day:%Y-%m-%d}.ics")
    caldav.put(c, day, "6 to-dos", "notes")                  # a rerun replaces the day's event
    (path, body), = fake_caldav["store"].items()
    assert "SUMMARY:6 to-dos" in body and "DTSTART:" in body
    c["password_file"] = str(tmp_path / "loose")
    (tmp_path / "loose").write_text("x")
    (tmp_path / "loose").chmod(0o644)
    with pytest.raises(caldav.CalendarError, match="chmod 600"):
        caldav.put(c, day, "t", "n")
    c["password_file"] = fake_caldav["password_file"]
    c["username"] = "someone"
    with pytest.raises(caldav.CalendarError, match="refused"):
        caldav.put(c, day, "t", "n")


def test_event_time_never_in_the_past():
    from amanuense import caldav
    c = {"time": "07:30"}
    now = datetime(2026, 10, 9, 9, 0).astimezone()
    assert caldav.event_start(c, now.replace(hour=5), now) == now + timedelta(minutes=2)
    assert caldav.event_start(c, now.replace(hour=5), now.replace(hour=6)).hour == 7


def test_digest_delivers_to_calendar(fake_caldav, tmp_path, monkeypatch):
    cfg = config.load(None, {"calendar": {"enabled": True, "server": fake_caldav["server"], "username": "me",
                                          "password_file": fake_caldav["password_file"],
                                          "url": fake_caldav["server"] + "dav/calendars/me/personal/"}},
                      require_file=False)
    msgs, waits = samples.results(cfg)
    opts = triage.Options(*window.parse_window("24h"))
    assert cli.deliver_calendar(cfg, opts, 60, None, msgs, waits).startswith("calendar task")
    body = next(iter(fake_caldav["store"].values())).replace("\r\n ", "")
    assert "SUMMARY:5 to-dos (2 urgent)" in body and "Check Section 4" in body


def test_same_email_in_two_folders(monkeypatch, taskdb):
    folders = [{"name": "Quarantine", "expires_days": 14}, {"name": "Conf", "expires_days": 14, "project": "conf"}]
    cfg = config.load(None, {"folders": folders, "mail": {"account": "work"}}, require_file=False)
    box = fresh_box(cfg)
    box.extra["Conf"] = [{**box.extra["Quarantine"][0], "uid": 9}]   # same Message-ID, other folder and uid
    msgs = run_fake(monkeypatch, cfg, box)
    copies = [m for m in msgs if m["messageId"] == "<7@test>"]
    assert len(copies) == 1 and copies[0]["folder"] == "Quarantine" and copies[0]["project"] == "conf"
    msgs.append({**copies[0], "uid": 9, "folder": "Conf"})   # had it been triaged twice anyway
    taskdb.add_tasks(cfg, msgs)
    exported = __import__("json").loads(taskdb._task(cfg, "export", read_only=True))
    held = [t for t in exported if "<7@test>" in t["annotations"][0]["description"]]
    assert len(held) == 1 and held[0]["project"] == "conf"
    assert "deletes the message" in held[0]["annotations"][1]["description"]


def test_tasks_free_text_folder_note(monkeypatch, taskdb):
    cfg = config.load(None, {**FOLDERS, "mail": {"account": "work"}}, require_file=False)
    taskdb._task(cfg, "add", "Camera-ready papers", "+events")
    taskdb._task(cfg, "1", "annotate", "filed: Quarantine uid 3 + corrected uid 7")
    msgs = run_fake(monkeypatch, cfg, fresh_box(cfg))
    taskdb.add_tasks(cfg, msgs)
    assert next(m for m in msgs if m["uid"] == 7)["task"] == "exists"
    assert next(m for m in msgs if m["uid"] == 3)["task"] == "added"     # uid 3 of the inbox is another email


def test_tasks_reply_in_thread_is_noted(monkeypatch, taskdb):
    cfg = config.load(None, {"mail": {"account": "work"}}, require_file=False)
    taskdb._task(cfg, "add", "Review the manuscript", "+reviewing")
    taskdb._task(cfg, "1", "annotate", "msgid:<earlier@test>")
    box = FakeMailbox(cfg)
    box.inbox[2]["inReplyTo"] = "<earlier@test>"
    msgs = run_fake(monkeypatch, cfg, box)
    n = taskdb.add_tasks(cfg, msgs)
    assert next(m for m in msgs if m["uid"] == 3)["task"] == "noted"
    exported = __import__("json").loads(taskdb._task(cfg, "export", read_only=True))
    review = next(t for t in exported if t["description"] == "Review the manuscript")
    assert "same thread: " in review["annotations"][1]["description"]
    assert "<3@test>" in review["annotations"][1]["description"]
    assert len(exported) == n + 1
    assert taskdb.add_tasks(cfg, msgs) == 0 and next(m for m in msgs if m["uid"] == 3)["task"] == "exists"


def test_task_past_deadline():
    from amanuense import tasks
    cfg = config.load(None, require_file=False)
    now = datetime.now().astimezone()
    m = {"uid": 1, "subject": "s", "messageId": "<1@test>",
         "ai": {"todo": "t", "summary": "", "category": "admin", "deadline": f"{now - timedelta(days=3):%Y-%m-%d}"}}
    t = tasks.build(cfg, m, now=now)
    assert "due" not in t and "has passed" in t["annotations"][1]["description"]
    m["ai"]["deadline"] = f"{now + timedelta(days=20):%Y-%m-%d}"
    m["expires"] = (now + timedelta(days=5)).isoformat()
    t = tasks.build(cfg, m, now=now)
    assert t["due"] == tasks._utc(now + timedelta(days=5))
    assert "the email's own deadline is" in t["annotations"][1]["description"]


def test_tasks_same_topic(monkeypatch, taskdb):
    cfg = config.load(None, {"mail": {"account": "work"}, "taskwarrior": {"projects": {"grantx": r"joint grant"}}},
                      require_file=False)
    taskdb._task(cfg, "add", "Old letter", "+research")
    taskdb._task(cfg, "1", "annotate", "IMAP uid 900, account work, subject: Letter of support, msgid:<old@test>")
    taskdb._task(cfg, "1", "done")
    box = FakeMailbox(cfg)
    box.inbox[0]["subject"] = "Re: Justificación gastos proyecto"
    box.inbox[2]["subject"] = "RE: [list] Justificación  gastos proyecto 😀"   # same topic, another email
    box.inbox[5]["body"] += " (joint grant)"
    box.inbox[5]["subject"] = "Re: Letter of support"
    msgs = run_fake(monkeypatch, cfg, box)
    taskdb.add_tasks(cfg, msgs)
    by_uid = {m["uid"]: m for m in msgs}
    assert by_uid[1]["task"] == "added" and by_uid[3]["task"] == "noted"
    exported = __import__("json").loads(taskdb._task(cfg, "export", read_only=True))
    first = next(t for t in exported if "uid 1," in t["annotations"][0]["description"])
    assert "same thread: " in first["annotations"][1]["description"] and "😀" in first["annotations"][1]["description"]
    letter = next(t for t in exported if "uid 6," in t["annotations"][0]["description"])
    assert letter["status"] == "pending" and "earlier completed task: Old letter" in letter["annotations"][-1]["description"]
    data = (Path(os.environ["TASKDATA"]) / "pending.data").read_bytes()
    data.decode("utf-8")                                   # no CESU-8 surrogates from the emoji


def test_task_project_rules_and_length():
    from amanuense import tasks
    cfg = config.load(None, {"taskwarrior": {"projects": {"conf": r"conf2026@", "course": r"DUDA ENTREGA"}}},
                      require_file=False)
    m = {"uid": 1, "subject": "Duda entrega", "from": "a@x", "to": ["conf2026@example.org"]}
    assert tasks.project(cfg, m) == "conf"                     # first matching rule
    assert tasks.project(cfg, {**m, "to": []}) == "course"
    assert tasks.project(cfg, {**m, "project": "folder"}) == "folder"
    assert tasks.project(cfg, {"uid": 2, "subject": "x"}) == ""
    assert tasks.shorten("word " * 40).endswith("word…") and len(tasks.shorten("word " * 40)) <= 121
    with pytest.raises(SystemExit):
        config.load(None, {"taskwarrior": {"projects": {"bad": "("}}}, require_file=False)


def test_tasks_reply_noted(monkeypatch, taskdb):
    cfg = config.load(None, {"mail": {"account": "work"}}, require_file=False)
    msgs = run_fake(monkeypatch, cfg, FakeMailbox(cfg))
    taskdb.add_tasks(cfg, msgs)
    taskdb._task(cfg, "add", "Follow up: figure", "+followup")
    taskdb._task(cfg, "+followup", "annotate", "msgid:<s2@test>")
    sent = [{"uid": 110, "date": "2026-10-09T10:00:00+02:00", "messageId": "<r1@test>", "inReplyTo": "<1@test>"},
            {"uid": 111, "date": "2026-10-09T11:00:00+02:00", "messageId": "<r2@test>", "inReplyTo": "<s2@test>"},
            {"uid": 112, "date": "2026-10-09T12:00:00+02:00", "messageId": "<r3@test>"}]
    assert taskdb.note_replies(cfg, sent, dry_run=True) == 1
    assert taskdb.note_replies(cfg, sent) == 1                 # the follow-up task is left alone
    assert taskdb.note_replies(cfg, sent) == 0                 # each reply noted once
    exported = __import__("json").loads(taskdb._task(cfg, "export", read_only=True))
    t = next(t for t in exported if "uid 1," in t["annotations"][0]["description"])
    assert t["status"] == "pending"                            # noted, never closed
    assert t["annotations"][-1]["description"].startswith("You replied on 2026-10-09 ")
    assert "<r1@test>" in t["annotations"][-1]["description"]
    assert "<r1@test>" not in taskdb.existing_refs(cfg)[0]     # the reply is not an email with a task
    taskdb._task(cfg, t["uuid"], "done")
    sent[0]["messageId"] = "<r4@test>"
    assert taskdb.note_replies(cfg, sent) == 0                 # only pending tasks


def test_sensitive(monkeypatch, taskdb):
    cfg = config.load(None, {"mail": {"account": "work"}, "sensitive": {"subject": "letter of support"}},
                      require_file=False)
    assert model.clean({}, ["info"], "info")["sensitive"]                    # no answer: sensitive
    assert "sick leave" in model.system_prompt(cfg) and "sensitive" in model.schema(["info"])["required"]
    box = FakeMailbox(cfg)
    box.inbox[3]["body"] = "Hola profe, I am on sick leave this week, can I take the exam later?"
    box.unread = set()                          # uid 6 only through the window below
    monkeypatch.setattr(model, "classify", fake_classify)
    opts = triage.Options(*window.parse_window("2w"), tag=True)
    msgs = asyncio.run(entered(cfg, box, opts))
    by_uid = {m["uid"]: m for m in msgs}
    assert by_uid[4]["sensitive"] and by_uid[6]["sensitive"] and not by_uid[1]["sensitive"]  # model, rule, neither
    assert ("add", 4, "sensitive") in box.written and ("add", 6, "sensitive") in box.written
    assert ("add", 1, "sensitive") not in box.written
    assert {uid for a, uid, kw in box.written if kw == "screened"} == {1, 2, 3, 4, 6}   # every triaged one
    assert not any(uid == 5 for _, uid, _ in box.written)                                # skipped: not checked

    box = FakeMailbox(cfg)                      # already tagged: nothing added twice, nothing removed
    box.inbox[3]["body"] = "sick leave"
    box.inbox[3]["customKeywords"] = ["sensitive", "screened"]
    asyncio.run(entered(cfg, box, triage.Options(*window.parse_window("2w"), tag=True, retag=True)))
    assert not any(uid == 4 and kw in ("sensitive", "screened") for _, uid, kw in box.written)

    taskdb.add_tasks(cfg, msgs)
    exported = __import__("json").loads(taskdb._task(cfg, "export", read_only=True))
    t = next(t for t in exported if "uid 4," in t["annotations"][0]["description"])
    assert t["description"] == "Sensitive email from Estudiante: needs action" and "sensitive" in t["tags"]
    assert "subject: (sensitive)" in t["annotations"][0]["description"]
    assert "Duda" not in str(t)                                            # no subject anywhere
    other = next(t for t in exported if "uid 1," in t["annotations"][0]["description"])
    assert "sensitive" not in other["tags"] and other["description"] == "do it"

    md = templates.render(cfg, "brief", msgs, opts, 1)
    assert "🔒" in md
    assert __import__("json").loads(render.to_json(cfg, msgs, opts, 1))["messages"][0]["sensitive"] is not None
    with pytest.raises(SystemExit):
        config.load(None, {"sensitive": {"from": "("}}, require_file=False)

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from amanuense import categories, cli, config, model, render, triage, window
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
    return model.clean({"category": cat, "needs_action": True, "todo": "do it", "deadline": None, "summary": "s"},
                       cfg["categories"], cfg["fallback"])


def run_fake(monkeypatch, cfg, box, **kw):
    monkeypatch.setattr(model, "classify", fake_classify)
    start, end = window.parse_window("24h")
    return asyncio.run(triage.run(cfg, box, triage.Options(start=start, end=end, **kw)))


def test_digest_selection_and_handled(monkeypatch):
    cfg = config.load(None, require_file=False)
    msgs = run_fake(monkeypatch, cfg, FakeMailbox(cfg))
    by_uid = {m["uid"]: m for m in msgs}
    assert len(msgs) == 6
    assert by_uid[6]["outside_window"]           # old unread mail included
    assert by_uid[5]["skip"]                     # noreply sender
    assert by_uid[2]["handled"]                  # replied to in Sent
    md = render.to_markdown(cfg, msgs, triage.Options(*window.parse_window("24h")), 1)
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
    days = render.days_left(held)
    assert 8 <= days <= 10
    opts = triage.Options(*window.parse_window("24h"))
    md = render.to_markdown(cfg, msgs, opts, 1)
    assert "## Deleted soon" in md and "in held mail" in md

    msgs = run_fake(monkeypatch, cfg, FakeMailbox(cfg), folders=False)
    assert 7 not in {m["uid"] for m in msgs}


def test_missing_folder_is_reported(monkeypatch):
    cfg = config.load(None, {"folders": [{"name": "Nope"}]}, require_file=False)
    monkeypatch.setattr(model, "classify", fake_classify)
    opts = triage.Options(*window.parse_window("24h"))
    box = FakeMailbox(cfg)
    box.extra = {}  # the folder doesn't exist on the server
    msgs = asyncio.run(triage.run(cfg, box, opts))
    assert len(msgs) == 6 and "could not read folder Nope" in opts.problems[0]  # the run went on
    assert "**Problem:** could not read folder Nope" in render.to_markdown(cfg, msgs, opts, 1)


def test_tasks_for_expiring_mail(monkeypatch, taskdb):
    cfg = config.load(None, {**FOLDERS, "mail": {"account": "work"}}, require_file=False)
    msgs = run_fake(monkeypatch, cfg, FakeMailbox(cfg))
    taskdb.add_tasks(cfg, msgs)
    t = next(t for t in __import__("json").loads(taskdb._task(cfg, "export", read_only=True))
             if "folder Quarantine" in t["annotations"][0]["description"])
    held = next(m for m in msgs if m["uid"] == 7)
    assert t["due"] == taskdb._utc(datetime.fromisoformat(held["expires"]))      # due when it gets deleted
    assert "IMAP uid 7, account work, folder Quarantine," in t["annotations"][0]["description"]
    ids, uids = taskdb.existing_refs(cfg)
    assert ("work", "Quarantine", 7) in uids and ("work", "INBOX", 1) in uids
    assert taskdb.add_tasks(cfg, run_fake(monkeypatch, cfg, FakeMailbox(cfg))) == 0

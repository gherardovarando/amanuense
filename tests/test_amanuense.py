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


def test_prompt_file(tmp_path, capsys):
    """The shipped example is the built-in prompt; a prompt.md next to the config replaces it."""
    from importlib import resources
    cfg = config.load(None, {}, require_file=False)
    assert cfg["model"]["prompt_file"] == "" and model.prompt_template(cfg) == model.PROMPT
    example = resources.files("amanuense") / "data" / "prompt.example.md"
    assert model.prompt_template({"model": {"prompt_file": str(example)}}) == model.PROMPT   # keep them the same
    filled = model.system_prompt(cfg)
    assert "{" not in filled.split("Reply ONLY")[0] and filled.endswith(cfg["categories_prompt"])
    (tmp_path / "config.toml").write_text("")
    (tmp_path / "categories.md").write_text((resources.files("amanuense") / "data" / "categories.example.md").read_text())
    (tmp_path / "prompt.md").write_text("<!-- a note -->\nSort mail for {owner}; keep {braces}. Categories: {categories}.\n")
    cfg = config.load(tmp_path / "config.toml")
    assert cfg["model"]["prompt_file"] == str(tmp_path / "prompt.md")
    filled = model.system_prompt(cfg)
    assert filled.startswith("Sort mail for a university professor; keep {braces}. Categories: research,")
    assert "a note" not in filled
    with pytest.raises(SystemExit, match="exists"):                                      # never overwrites yours
        cli.main(["-c", str(tmp_path / "config.toml"), "prompt", "copy"])
    (tmp_path / "config.toml").write_text('[model]\nprompt_file = "mine.md"\n')
    with pytest.raises(SystemExit, match="No prompt file"):
        config.load(tmp_path / "config.toml")


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
    assert [kw for _, uid, kw in box.written if uid == 3] == ["screened"]   # manual tag kept, only checked
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


def test_render_json_round_trip(monkeypatch, tmp_path, capsys):
    """render -t json, filtered (as with jq), read back with `render FILE` or `render -`."""
    import io, json
    cfg = config.load(None, {}, require_file=False)
    msgs, waits = samples.results(cfg)
    opts = triage.Options(*window.parse_window("24h"))
    cli.save_results(opts, 60, None, msgs, waits)
    for name in ("long", "page", "todo"):
        assert cli.main(["render", "--test", "-t", name]) == 0
        direct = capsys.readouterr().out
        assert cli.main(["render", "--test", "-t", "json"]) == 0
        f = tmp_path / "run.json"
        f.write_text(capsys.readouterr().out)
        assert cli.main(["render", str(f), "-t", name]) == 0     # a FILE prints, never overwrites the digest
        assert capsys.readouterr().out == direct, name
    data = json.loads(f.read_text())
    gone = data["messages"][0]["subject"]
    data["messages"] = data["messages"][1:]                     # what a jq filter does
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
    assert cli.main(["render", "-", "-t", "long"]) == 0
    out = capsys.readouterr().out
    assert gone not in out and data["messages"][0]["subject"] in out
    data["messages"][0]["category"] = "some other category"   # from another config: kept, not lost
    f.write_text(json.dumps(data))
    assert cli.main(["render", str(f), "-t", "long"]) == 0
    assert "some other category" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    with pytest.raises(SystemExit, match="not the JSON output"):
        cli.main(["render", "-"])


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
    etag = lambda body: f'"{hash(body)}"'

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

        def do_GET(self):
            if not self._auth():
                return
            data = store.get(self.path, "").encode()
            self.send_response(200 if self.path in store else 404)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_REPORT(self):                            # every item of the calendar; the client filters by time
            from xml.sax.saxutils import escape
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if not self._auth():
                return
            self._xml("".join(f"<d:response><d:href>{k}</d:href><d:propstat><d:prop><d:getetag>{etag(v)}</d:getetag>"
                              f"<c:calendar-data>{escape(v)}</c:calendar-data></d:prop></d:propstat></d:response>"
                              for k, v in store.items() if k.startswith(self.path)))

        def do_PUT(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            if not self._auth():
                return
            if ((self.headers.get("If-None-Match") == "*" and self.path in store)     # create-only: never replace
                    or (self.headers.get("If-Match") and self.headers["If-Match"] != etag(store.get(self.path)))):
                self.send_response(412)                 # changed since it was read
                self.end_headers()
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


def test_tasks_about_a_closed_task(monkeypatch, taskdb):
    """A reminder about a task marked done (or deleted) by mistake gets a new task that says so."""
    import json
    cfg = config.load(None, {"mail": {"account": "work"}}, require_file=False)
    taskdb._task(cfg, "add", "Submit the review of ABC-D-26-01234")
    taskdb._task(cfg, "/Submit/", "annotate", "msgid:<review@test>")
    taskdb._task(cfg, "/Submit/", "done")
    taskdb._task(cfg, "add", "Fill in the form for 1234567")
    taskdb._task(cfg, "/Fill/", "delete")
    taskdb._task(cfg, "add", "Accept or decline 7654321 now")
    taskdb._task(cfg, "add", "Accept or decline 7654321 earlier")
    taskdb._task(cfg, "/earlier/", "done")
    box = FakeMailbox(cfg)
    box.inbox[0]["inReplyTo"] = "<review@test>"                                  # thread of a done task
    box.inbox[2]["subject"] = "Reminder: form for 1234567"                        # item of a deleted task
    box.inbox[3]["subject"] = "Reminder: 7654321"                                 # a pending and a done task
    msgs = run_fake(monkeypatch, cfg, box)
    taskdb.add_tasks(cfg, msgs, dry_run=True)
    by_uid = {m["uid"]: m for m in msgs}
    assert by_uid[1]["task"] == "would add" and by_uid[1]["earlier"] == "completed"
    taskdb.add_tasks(cfg, msgs)
    assert [by_uid[u]["task"] for u in (1, 3, 4)] == ["added", "added", "noted"]
    assert by_uid[3]["earlier"] == "deleted" and "earlier" not in by_uid[4]
    exported = json.loads(taskdb._task(cfg, "export", read_only=True))
    old = next(t for t in exported if t["description"].startswith("Submit the review"))
    assert old["status"] == "completed" and len(old["annotations"]) == 1           # left alone
    note = lambda t: (t.get("annotations") or [{"description": ""}])[0]["description"]
    new = next(t for t in exported if "uid 1," in note(t))
    assert new["status"] == "pending" and "again" in new["tags"]
    assert "marked done on 20" in new["annotations"][-1]["description"]
    form = next(t for t in exported if "uid 3," in note(t))
    assert "marked deleted" in form["annotations"][-1]["description"] and "needed after all" in form["annotations"][-1]["description"]
    pending = next(t for t in exported if t["description"].endswith(" now"))
    assert note(pending).startswith("same item: ")
    opts = triage.Options(*window.parse_window("24h"))
    for name in ("long", "page"):
        assert "task added (an earlier task was done)" in templates.render(cfg, name, msgs, opts, 1)


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
    assert letter["status"] == "pending" and "again" in letter["tags"] and by_uid[6]["earlier"] == "completed"
    assert "An earlier task on this was marked done on " in letter["annotations"][-1]["description"]
    assert "'Old letter'. Check it was really done." in letter["annotations"][-1]["description"]
    data = Path(os.environ["TASKDATA"]) / "pending.data"  # Taskwarrior 2 only; 3 keeps an SQLite file
    if data.exists():
        data.read_bytes().decode("utf-8")                  # no CESU-8 surrogates from the emoji


def test_tasks_same_reference(monkeypatch, taskdb):
    """A reminder in a new thread that names the manuscript of a pending task is noted there."""
    import json
    cfg = config.load(None, {"mail": {"account": "work"}}, require_file=False)
    taskdb._task(cfg, "add", "Review ABC-D-26-01234")                             # made by hand
    taskdb._task(cfg, "add", "Accept or decline")
    taskdb._task(cfg, "2", "annotate", "IMAP uid 900, account work, subject: Invitation to review manuscript 1234567, msgid:<inv@test>")
    taskdb._task(cfg, "add", "Old review of 7777777")
    taskdb._task(cfg, "3", "done")                                               # done: a new task is fine
    box = FakeMailbox(cfg)
    box.inbox[0]["subject"] = "Late review: ABC-D-26-01234"
    box.inbox[2]["subject"] = "Reminder: manuscript 1234567 awaits your answer"
    box.inbox[3]["subject"] = "New submission XYZ-D-26-0001, as 7777777"         # 7777777's task is done
    msgs = run_fake(monkeypatch, cfg, box)
    for m in msgs:
        if m["uid"] == 6 and "ai" in m:
            m["ai"]["todo"] = "Submit the review of XYZ-D-26-0001"                # same item, named in the to-do
    taskdb.add_tasks(cfg, msgs)
    by_uid = {m["uid"]: m for m in msgs}
    assert by_uid[1]["task"] == "noted" and by_uid[3]["task"] == "noted"
    assert by_uid[4]["task"] == "added" and by_uid[6]["task"] == "noted"          # the earlier email in this run
    assert by_uid[4]["earlier"] == "completed" and "earlier" not in by_uid[1]
    tasks = json.loads(taskdb._task(cfg, "status:pending", "export", read_only=True))
    hand = next(t for t in tasks if t["description"] == "Review ABC-D-26-01234")
    assert hand["annotations"][0]["description"].startswith("same item: ") and "uid 1," in hand["annotations"][0]["description"]
    assert taskdb.references(cfg, "Invoice 2026-10-27, 12:30, 1.234.567 EUR") == set()
    assert taskdb.references(cfg, "Ref. ABC-2026-0123.R1 and 1234567") == {"ABC-2026-0123.R1", "1234567"}


def test_task_project_rules_and_length():
    from amanuense import tasks
    cfg = config.load(None, {"taskwarrior": {"projects": {"conf": r"conf2026@", "course": r"HOMEWORK QUESTION"}}},
                      require_file=False)
    m = {"uid": 1, "subject": "Homework question", "from": "a@x", "to": ["conf2026@example.org"]}
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

    box = FakeMailbox(cfg)                      # tag: tagged by hand but not checked yet
    box.inbox[3]["body"] = "sick leave"
    box.inbox[3]["customKeywords"] = ["research"]
    box.inbox[2]["customKeywords"] = ["research", "screened"]
    box.inbox[0]["customKeywords"] = ["screened"]                         # e.g. info mail: no category keyword
    again = asyncio.run(entered(cfg, box, triage.Options(*window.parse_window("2w"), tag=True, triage_tagged=False)))
    four = next(m for m in again if m["uid"] == 4)
    assert ("add", 4, "sensitive") in box.written and ("add", 4, "screened") in box.written
    assert four["ai"]["category"] == "research"                            # your category is kept
    assert not any(uid == 4 and a == "remove" for a, uid, _ in box.written)
    assert not any(uid == 3 for _, uid, _ in box.written)                  # tagged and checked: left alone
    assert not any(uid == 1 for _, uid, _ in box.written)                  # screened, no category: left alone
    assert "ai" not in next(m for m in again if m["uid"] == 1)             # and not sent to the model again
    assert render.stats(again)["sensitive"] == 2                           # uid 4 (model) and 6 (rule)

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


def test_held_mail_read_once(monkeypatch):
    cfg = config.load(None, FOLDERS, require_file=False)
    calls = []
    monkeypatch.setattr(model, "classify", lambda c, s, m: calls.append(m["uid"]) or fake_classify(c, s, m))
    start, end = window.parse_window("24h")

    def go(box, **kw):
        opts = triage.Options(start=start, end=end, **kw)
        return opts, {m["uid"]: m for m in asyncio.run(entered(cfg, box, opts))}

    opts, first = go(FakeMailbox(cfg))                          # held mail (4 days old), never read: model
    assert 7 in calls and first[7]["before_window"] and not first[7].get("ai_reused")
    cli.save_results(opts, 60, None, list(first.values()), [])  # as the digest does
    previous = cli.previous_verdicts()
    assert previous[("Quarantine", 7, "<7@test>")]["todo"] == "do it"

    calls.clear()
    box = FakeMailbox(cfg)
    opts, second = go(box, previous=previous, tag=True)         # next day: the earlier verdict, no model call
    assert 7 not in calls and 1 in calls
    assert second[7]["ai_reused"] and second[7]["ai"]["todo"] == "do it"
    assert ("add", 7, "screened") in box.written                # still tagged if it wasn't yet
    ctx = templates.context(cfg, list(second.values()), opts, 1)
    assert any(i["uid"] == 7 for i in ctx["todos"]) and ctx["expiring"][0]["uid"] == 7
    assert render.stats(list(second.values()))["reused"] == 1

    calls.clear()
    box = FakeMailbox(cfg)
    box.extra["Quarantine"][0]["customKeywords"] = ["screened"]
    _, third = go(box)                                          # screened, no verdict kept: listed, not read
    assert 7 not in calls and "ai" not in third[7] and third[7]["expires"]

    calls.clear()
    box = fresh_box(cfg)
    box.extra["Quarantine"][0]["customKeywords"] = ["screened"]
    go(box, previous=previous)                                  # arrived in the window: always read
    assert 7 in calls


def test_events_dry_run():
    cfg = config.load(None, require_file=False)
    assert "event_class" in model.system_prompt(cfg) and "event_title" in model.schema(["info"])["required"]
    assert list(model.schema(["info"])["properties"]).index("event_time") < list(model.schema(["info"])["properties"]).index("event_until")
    e = lambda **kw: model.clean({"category": "info", "event_class": "confirmed", **kw}, ["info"], "info")["event"]
    for kind in ("proposed", "announced", "none", None):         # only a confirmed event counts
        assert model.clean({"category": "info", "event_class": kind, "event_title": "x", "event_date": "2026-10-30"},
                           ["info"], "info")["event"] is None
    assert model.clock("3:30 PM") == "15:30" and model.clock("15h30") == "15:30" and model.clock("soon") is None
    assert e(event_title="x", event_date="2026-10-30", event_until="15:30")["start"] == "15:30"   # a lone end time
    assert e(event_title="Call with Smith", event_date="2026-10-30", event_time="9:30", event_until="10:00",
             event_place=" Room  2 ") == {"title": "Call with Smith", "date": "2026-10-30", "start": "09:30",
                                          "end": "10:00", "place": "Room 2", "why": None}
    assert e(event_title="x", event_date="2026-10-30", event_why=" Plan  the course. ")["why"] == "Plan the course."
    assert model.clean({"category": "info", "event_class": "none", "event_why": "x"}, ["info"], "info")["event"] is None
    assert e(event_title="x", event_date="2026-10-30", event_time="15:00", event_until="14:00")["end"] is None
    assert e(event_title="x", event_date="2026-10-30", event_time="soon")["start"] is None
    assert e(event_title="x", event_date="2026-02-30") is None and e(event_title="", event_date="2026-10-30") is None
    assert e() is None and "event_title" not in model.clean({"event_title": "x"}, ["info"], "info")

    msgs, waits = samples.results(cfg)
    today = datetime.now().astimezone()
    day = lambda d: f"{today + timedelta(days=d):%Y-%m-%d}"
    by_uid = {m["uid"]: m for m in msgs}
    by_uid[1]["ai"]["event"] = {"title": "Old meeting", "date": day(-1), "start": None, "end": None, "place": None}
    by_uid[3]["ai"]["event"] = {"title": "Defence", "date": day(1), "start": "11:00", "end": None, "place": None}
    by_uid[4]["ai"]["event"] = dict(by_uid[3]["ai"]["event"])     # the same event in another email
    opts = triage.Options(*window.parse_window("24h"))
    ctx = templates.context(cfg, msgs, opts, 1)
    assert [i["event"]["title"] for i in ctx["events"]] == ["Defence", "Call with Anna about Section 4"]  # no past, once
    for name in ("brief", "long", "page", "counts"):
        out = templates.render(cfg, name, msgs, opts, 1, None, waits)
        assert ("Events" in out) and ("Old meeting" not in out)
    assert "11:00: Defence" in templates.render(cfg, "brief", msgs, opts, 1, None, waits)
    assert render.stats(msgs)["events"] == 3                   # upcoming ones


def test_events_added_to_calendar(fake_caldav):
    from amanuense import events
    cfg = config.load(None, {"calendar": {"server": fake_caldav["server"], "username": "me",
                                          "password_file": fake_caldav["password_file"]},
                             "events": {"url": fake_caldav["server"] + "dav/calendars/me/amanuense/"}},
                      require_file=False)
    msgs, _ = samples.results(cfg)
    by_uid = {m["uid"]: m for m in msgs}
    day = lambda d: f"{datetime.now() + timedelta(days=d):%Y-%m-%d}"
    by_uid[4]["ai"]["event"] = {"title": "Exam review", "date": day(2), "start": None, "end": None, "place": "Room 1"}
    by_uid[4]["sensitive"] = True
    by_uid[3]["ai"]["event"] = {"title": "Old", "date": day(-2), "start": "10:00", "end": None, "place": None}

    assert events.add(cfg, msgs, dry_run=True) == (0, None) and fake_caldav["store"] == {}
    assert by_uid[2]["event_status"] == "would add"
    assert events.add(cfg, msgs) == (2, None)                          # uid 2 (sample) and 4; not the past one
    assert {by_uid[2]["event_status"], by_uid[4]["event_status"]} == {"added"} and "event_status" not in by_uid[3]
    bodies = {k: v.replace("\r\n ", "") for k, v in fake_caldav["store"].items()}
    call = next(b for b in bodies.values() if "Anna" in b)
    assert "STATUS:TENTATIVE" in call and "CLASS:PRIVATE" in call and "DTEND:" in call
    assert "SUMMARY:Call with Anna about Section 4" in call and "message-id <2@test>" in call
    assert "DESCRIPTION:Agree on the changes to Section 4.\\n\\nProposed by amanuense" in call   # the reason first
    exam = next(b for b in bodies.values() if "VALUE=DATE" in b)          # all day
    assert "SUMMARY:Event (sensitive email from Estudiante)" in exam
    assert "Exam review" not in exam and "Duda" not in exam and "LOCATION" not in exam

    fake_caldav["store"][next(iter(fake_caldav["store"]))] = "edited by me"
    assert events.add(cfg, msgs) == (0, None)                          # rerun: nothing replaced
    assert by_uid[2]["event_status"] == "exists" and "edited by me" in fake_caldav["store"].values()

    cfg["calendar"]["username"] = "someone"
    added, problem = events.add(cfg, msgs)
    assert added == 0 and "refused" in problem and by_uid[2]["event_status"] == "failed"

    opts = triage.Options(*window.parse_window("24h"))
    by_uid[2]["event_status"] = "added"
    brief = templates.render(cfg, "brief", msgs, opts, 1)
    assert "## Events" in brief and "added (tentative)" in brief and "I would add" not in brief


def test_events_same_meeting(fake_caldav):
    """A later email about the same meeting, from someone else, adds its link to the tentative event."""
    from amanuense import events
    cfg = config.load(None, {"calendar": {"server": fake_caldav["server"], "username": "me",
                                          "password_file": fake_caldav["password_file"]},
                             "events": {"url": fake_caldav["server"] + "dav/calendars/me/amanuense/"},
                             "mail": {"addresses": ["Me@Example.org"]}}, require_file=False)
    day = f"{datetime.now() + timedelta(days=3):%Y-%m-%d}"
    def mail(uid, frm, subject, title, place=None, to=("me@example.org",), cc=(), start="09:30"):
        return {"uid": uid, "from": frm, "to": list(to), "cc": list(cc), "subject": subject, "messageId": f"<{uid}@test>",
                "date": f"{datetime.now().astimezone():%Y-%m-%dT%H:%M:%S%z}", "handled": False,
                "ai": {"category": "info", "needs_action": False, "todo": None, "summary": "s", "deadline": None, "event": {"title": title, "date": day, "start": start, "end": "10:30", "place": place, "why": None}}}
    a = mail(1, "Ana <ana@uni.example>", "Workshop planning", "Workshop planning meeting", "Room 3",
             to=("me@example.org", "bob@uni.example"))
    b = mail(2, "Carla <carla@uni.example>", "Meeting link", "Meeting with Ana and Bob", "https://zoom.example/j/1",
             cc=("Ana <ANA@uni.example>",))                                   # Ana sent the first one
    c = mail(3, "Dan <dan@other.example>", "Thesis defence", "Thesis defence",  # only the time and Bob, in Cc
             cc=("bob@uni.example",))
    d = mail(4, "Eve <eve@other.example>", "Your exam", "Exam", start="11:00")
    assert events.add(cfg, [a]) == (1, None) and a["event_status"] == "added"
    assert events.add(cfg, [a, b, c, d]) == (2, None)
    assert [m["event_status"] for m in (a, b, c, d)] == ["exists", "merged", "added", "added"]
    assert b["event_other"] == "Workshop planning meeting" and c["event_other"] == "Workshop planning meeting"
    assert "event_other" not in d
    store = {k: v.replace("\r\n ", "") for k, v in fake_caldav["store"].items()}
    first = next(v for v in store.values() if "Workshop planning meeting" in v)
    assert "LOCATION:Room 3 · https://zoom.example/j/1" in first and "STATUS:TENTATIVE" in first
    assert "Also in an email from Carla\\, subject: Meeting link (message-id <2@test>): https://zoom.example/j/1." in first
    assert "X-AMANUENSE-PEOPLE:ana@uni.example,bob@uni.example,carla@uni.example,me@example.org" in first
    assert "X-AMANUENSE-FROM:ana@uni.example,carla@uni.example" in first
    defence = next(v for v in store.values() if "SUMMARY:Thesis defence" in v)
    assert "Another event starts at the same time: 'Workshop planning meeting'. If it is the same meeting\\, delete one." in defence
    before = dict(fake_caldav["store"])
    assert events.add(cfg, [a, b, c, d]) == (0, None)                  # rerun: nothing changes
    assert [m["event_status"] for m in (a, b, c, d)] == ["exists"] * 4 and fake_caldav["store"] == before

    key = next(k for k, v in fake_caldav["store"].items() if "Workshop planning meeting" in v)
    fake_caldav["store"][key] = fake_caldav["store"][key].replace("STATUS:TENTATIVE", "STATUS:CONFIRMED")
    e = mail(5, "Ana <ana@uni.example>", "Workshop planning: agenda", "Workshop planning", "Room 4")
    before = dict(fake_caldav["store"])
    assert events.add(cfg, [e]) == (0, None) and e["event_status"] == "exists"      # confirmed: left alone
    assert fake_caldav["store"] == before

    cal = {"title": "Planning", "notes": "", "senders": {"ana@uni.example"},
           "people": {"ana@uni.example", "me@example.org", "list@uni.example"}}
    same = lambda m: events.same_meeting(cfg, m, cal)
    assert same(mail(6, "Zoe <zoe@x.example>", "Link", "Other", to=("me@example.org", "ana@uni.example"))) == ["shared people"]
    assert same(mail(7, "Zoe <zoe@x.example>", "Link", "Other", to=("list@uni.example",))) == []   # only a list
    assert same(mail(8, "list@uni.example", "x", "Other")) == ["shared people"]                    # the list sent one
    assert same(mail(9, "Ana <ana@uni.example>", "x", "Other", to=())) == ["shared people"]
    mine = {"title": "Planning", "notes": "", "senders": {"me@example.org"}, "people": {"me@example.org"}}
    assert events.same_meeting(cfg, mail(10, "Zoe <zoe@x.example>", "x", "Other"), mine) == []      # you don't count
    assert events.same_meeting(cfg, mail(11, "z@x", "Re: [list] Summer school planning", "Summer school: link"),
                               {"title": "Meeting for summer school planning", "people": set(),
                                "notes": "Proposed by amanuense from an email from Z, subject: Summer school planning "
                                         "(message-id <x>)."}) == ["similar title", "same subject"]
    assert events.same_meeting(cfg, mail(12, "z@x", "Defence of 1234567", "Defence"),
                               {"title": "Viva 1234567", "people": set(), "notes": ""}) == ["same reference"]
    assert events.same_meeting(cfg, mail(13, "z@x", "Meeting", "Meeting with Ana"),
                               {"title": "Meeting with Bob", "people": set(), "notes": ""}) == []

    opts = triage.Options(*window.parse_window("24h"))
    b["event_status"], c["event_status"] = "merged", "added"            # as after the first run
    for name in ("brief", "page"):
        out = templates.render(cfg, name, [a, b, c, d], opts, 1)
        assert "added to 'Workshop planning meeting' at the same time" in out
        assert "another event starts at the same time: 'Workshop planning meeting'" in out
    assert render.stats([a, b, c, d])["events_merged"] == 1
    with pytest.raises(SystemExit, match="mail.addresses"):
        config.load(None, {"mail": {"addresses": "me@example.org"}}, require_file=False)


def test_tasks_same_meeting(fake_caldav, taskdb):
    """Two emails about one meeting (found through the calendar) give one task, in either order."""
    import json
    from amanuense import events
    cfg = config.load(None, {"calendar": {"server": fake_caldav["server"], "username": "me",
                                          "password_file": fake_caldav["password_file"]},
                             "events": {"url": fake_caldav["server"] + "dav/calendars/me/amanuense/"},
                             "mail": {"account": "work", "addresses": ["me@example.org"]}}, require_file=False)
    day = f"{datetime.now() + timedelta(days=3):%Y-%m-%d}"
    def mail(uid, frm, subject, title, todo, place=None, to=("me@example.org",)):
        return {"uid": uid, "from": frm, "to": list(to), "cc": [], "subject": subject, "messageId": f"<{uid}@test>",
                "handled": False, "ai": {"category": "info", "needs_action": True, "todo": todo, "summary": "",
                                          "deadline": None, "event": {"title": title, "date": day, "start": "09:30",
                                                                      "end": None, "place": place, "why": None}}}
    a = mail(1, "Ana <ana@uni.example>", "Workshop planning", "Workshop planning meeting", "Prepare the budget for the planning meeting",
             to=("me@example.org", "bob@uni.example"))
    b = mail(2, "Bob <bob@uni.example>", "Link for tomorrow", "Call with Ana", "Send the agenda before the call", "https://zoom.example/j/1")
    msgs = [b, a]                                         # the link email first: tasks still end up as one
    events.add(cfg, msgs)
    assert {a["event_status"], b["event_status"]} == {"added", "merged"}
    assert a["same_meeting"] == ["<2@test>"] and b["same_meeting"] == ["<1@test>"]
    assert taskdb.add_tasks(cfg, msgs) == 1
    assert b["task"] == "added" and a["task"] == "noted"
    t = json.loads(taskdb._task(cfg, "export", read_only=True))
    same = [n["description"] for n in t[0]["annotations"] if n["description"].startswith("same meeting: ")]
    assert len(t) == 1 and len(same) == 1 and "uid 1," in same[0]

    c = mail(3, "Carla <carla@uni.example>", "Room change", "Workshop planning", "Print the handouts", "Room 5",
             to=("me@example.org", "ana@uni.example"))
    events.add(cfg, [c])                                  # a later run: the task is already there
    assert c["event_status"] == "merged" and sorted(c["same_meeting"]) == ["<1@test>", "<2@test>"]
    assert taskdb.add_tasks(cfg, [c]) == 0 and c["task"] == "noted"
    assert t[0]["due"] == taskdb._utc(datetime.strptime(f"{day} 09:30", "%Y-%m-%d %H:%M").astimezone())
    assert "Due at the start of the event 'Call with Ana', " in t[0]["annotations"][1]["description"]

    # only "attend": the calendar is the reminder; without the event in a calendar the task stays
    d = mail(4, "Eve <eve@uni.example>", "Seminar", "Seminar by Eve", "Attend Eve's seminar")
    d["ai"]["event"]["start"] = "15:00"
    f = mail(5, "Fay <fay@uni.example>", "Talk", "Talk by Fay", "Join Fay's talk")
    f["ai"]["event"]["start"] = "17:00"
    events.add(cfg, [d])
    f["event_status"] = "would add"                       # e.g. no events.url
    assert taskdb.add_tasks(cfg, [d, f]) == 1 and d["task"] == "in calendar" and f["task"] == "added"
    opts = triage.Options(*window.parse_window("24h"))
    assert "in the calendar, no task" in templates.render(cfg, "long", [d, f], opts, 1)
    for body, kept in (("We meet on Tuesday at 10:00. The meeting is confirmed.", False),
                       ("La reunión está confirmada para el martes.", False),
                       ("We meet on Tuesday. Please confirm your attendance.", True),
                       ("Nos vemos el martes. Por favor, confirmad asistencia.", True),
                       ("We meet on Tuesday. RSVP by Friday.", True), ("", True)):  # no text: keep it
        g = {**d, "body": body, "ai": {**d["ai"], "todo": "Confirm attendance at Eve's seminar"}, "event_status": "exists"}
        assert taskdb.in_calendar(g) is not kept, body


def test_events_your_own_calendar(fake_caldav, taskdb):
    """An event you made yourself, at the same time, in a calendar in events.check: nothing is added."""
    from amanuense import caldav, events
    srv = fake_caldav["server"]
    cfg = config.load(None, {"calendar": {"server": srv, "username": "me", "password_file": fake_caldav["password_file"]},
                             "events": {"url": srv + "dav/calendars/me/amanuense/", "check": ["personal"]},
                             "mail": {"account": "work"}}, require_file=False)
    day = datetime.now() + timedelta(days=4)
    start = day.replace(hour=12, minute=30, second=0, microsecond=0).astimezone()
    fake_caldav["store"]["/dav/calendars/me/personal/mine.ics"] = caldav.ics(
        "mine", start, 30, "Thesis meeting", "made by hand", alarm=False, kind="event")
    def mail(uid, start, todo="Attend the thesis meeting"):
        return {"uid": uid, "from": "Stu <stu@uni.example>", "to": ["me@example.org"], "subject": "Re: thesis",
                "messageId": f"<{uid}@test>", "handled": False,
                "ai": {"category": "teaching", "needs_action": True, "todo": todo, "summary": "", "deadline": None,
                       "event": {"title": "Meeting with Stu", "date": f"{day:%Y-%m-%d}", "start": start,
                                 "end": None, "place": None, "why": None}}}
    a, b = mail(1, "12:30"), mail(2, "16:00")
    assert events.add(cfg, [a, b]) == (1, None)
    assert a["event_status"] == "yours" and a["event_other"] == "Thesis meeting" and b["event_status"] == "added"
    assert len([k for k in fake_caldav["store"] if "/amanuense/" in k]) == 1             # only b's
    assert taskdb.add_tasks(cfg, [a]) == 0 and a["task"] == "in calendar"      # only "attend": no task either
    opts = triage.Options(*window.parse_window("24h"))
    a["date"], a["ai"]["summary"] = f"{datetime.now().astimezone():%Y-%m-%dT%H:%M:%S%z}", "s"
    assert "you already have 'Thesis meeting' at that time" in templates.render(cfg, "long", [a], opts, 1)

    cfg["events"]["check"] = ["Nope"]
    c = mail(3, "09:00")
    added, problem = events.add(cfg, [c])
    assert added == 1 and "no calendar named 'Nope'" in problem       # a wrong name: a warning, events still added
    assert ("events.check Nope", False) == cli.calendar_checks(cfg)[-1][:2]
    with pytest.raises(SystemExit, match="events.check"):
        config.load(None, {"events": {"check": "Personal"}}, require_file=False)


def test_calendar_checks(fake_caldav):
    base = {"server": fake_caldav["server"], "username": "me", "password_file": fake_caldav["password_file"]}
    cfg = config.load(None, {"calendar": {**base, "enabled": True, "url": fake_caldav["server"] + "dav/calendars/me/tasks"},
                             "events": {"url": fake_caldav["server"] + "dav/calendars/me/nope/"}}, require_file=False)
    (n1, ok1, d1), (n2, ok2, d2) = cli.calendar_checks(cfg)
    assert (n1, ok1, d1) == ("calendar", True, '"Tasks"') and n2 == "events calendar" and not ok2 and "not one of" in d2
    cfg["calendar"]["username"] = "x"
    assert all(not ok and "refused" in d for _, ok, d in cli.calendar_checks(cfg))
    assert cli.calendar_checks(config.load(None, require_file=False)) == []


def test_calendar_events(fake_caldav, capsys):
    """`calendar events` lists the coming events of every calendar, read-only."""
    from amanuense import caldav
    c = {"server": fake_caldav["server"], "username": "me", "password_file": fake_caldav["password_file"],
         "url": fake_caldav["server"] + "dav/calendars/me/tasks/"}
    now = datetime.now().astimezone()
    d = lambda days, h=10: (now + timedelta(days=days)).replace(hour=h, minute=0, second=0, microsecond=0)
    utc = lambda t: t.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cal = lambda *lines: "\r\n".join(["BEGIN:VCALENDAR", "BEGIN:VEVENT", *lines, "END:VEVENT", "END:VCALENDAR"])
    store = fake_caldav["store"]
    store["/dav/calendars/me/personal/a.ics"] = caldav.ics("a", d(1, 15), 30, "Call with Smith, paper", "Plan the revision.\nmore", False, "event")
    store["/dav/calendars/me/personal/b.ics"] = cal(f"DTSTART:{utc(d(-3))}", f"DTEND:{utc(d(-3, 11))}", "SUMMARY:Old")
    store["/dav/calendars/me/personal/c.ics"] = cal(f"DTSTART;VALUE=DATE:{d(2):%Y%m%d}", "SUMMARY:Open day", "STATUS:TENTATIVE")
    store["/dav/calendars/me/personal/r.ics"] = cal(f"DTSTART;TZID=Europe/Madrid:{d(-14, 9):%Y%m%dT%H%M%S}",
                                                     "RRULE:FREQ=WEEKLY", "SUMMARY:Class", "LOCATION:Room 1")
    store["/dav/calendars/me/personal/x.ics"] = cal(f"DTSTART:{utc(d(30))}", "SUMMARY:Far away")
    store["/dav/calendars/me/tasks/t.ics"] = cal(f"DTSTART:{utc(d(1))}", "SUMMARY:Digest item")
    before = dict(store)
    cli.print_events(c, 7)
    out = capsys.readouterr().out
    assert out.startswith("3 events") and "calendars: Personal" in out and "Tasks" not in out.splitlines()[0]
    assert "Old" not in out and "Far away" not in out and "Digest item" not in out   # past, too far, digest calendar
    assert "15:00–15:30  Call with Smith, paper  [Personal]" in out and "    Plan the revision." in out and "more" not in out
    assert "all day  Open day  [Personal, tentative]" in out
    assert "- repeating, from " in out and "Class @ Room 1  [Personal]" in out               # not expanded by this server: marked
    assert out.index("Class") < out.index("Call with Smith")         # soonest first
    assert store == before                                             # read-only
    cli.print_events(c, 7, ["Tasks"])
    assert "Digest item" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.print_events(c, 7, ["Nope"])


def test_run_log(fake_caldav, monkeypatch, tmp_path, capsys):
    """The run log holds counts and ids, never text from mail; `log --events` follows the events added."""
    from amanuense import events, runlog
    import json as _json
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = config.load(None, {"calendar": {"server": fake_caldav["server"], "username": "me",
                                          "password_file": fake_caldav["password_file"]},
                             "events": {"url": fake_caldav["server"] + "dav/calendars/me/amanuense/"}},
                      require_file=False)
    msgs, _ = samples.results(cfg)
    by_uid = {m["uid"]: m for m in msgs}
    day = lambda d: f"{datetime.now() + timedelta(days=d):%Y-%m-%d}"
    by_uid[4]["ai"]["event"] = {"title": "Exam review", "date": day(2), "start": None, "end": None, "place": "Room 1",
                                "why": None}
    events.add(cfg, msgs)
    opts = triage.Options(*window.parse_window("24h"))
    cli.log_run("digest", cfg, opts, msgs, 120.0, None, 1)
    text = runlog.path().read_text()
    assert oct(runlog.path().stat().st_mode & 0o777) == "0o600"
    for m in msgs:                                   # nothing from the mail itself
        for k in ("subject", "from"):
            if m.get(k):
                assert m[k] not in text
        assert not (m.get("ai") or {}).get("summary") or m["ai"]["summary"] not in text
    assert "Anna" not in text and "Exam review" not in text and "Room 1" not in text
    e = _json.loads(text)
    assert e["command"] == "digest" and e["replies_noted"] == 1 and len(e["calendar_events"]) == 2
    assert {x["status"] for x in e["calendar_events"]} == {"added"}

    a = cli.build_parser().parse_args(["log"])
    cli.cmd_log(cfg, a)
    assert "2 events (2 added)" in capsys.readouterr().out
    store = fake_caldav["store"]
    gone = next(k for k, v in store.items() if "Anna" in v)
    del store[gone]                                  # he deleted one
    k = next(iter(store))
    store[k] = store[k].replace("STATUS:TENTATIVE", "STATUS:CONFIRMED")   # and confirmed the other
    cli.cmd_log(cfg, cli.build_parser().parse_args(["log", "--events"]))
    out = capsys.readouterr().out
    assert "2 events added: 1 confirmed, 1 deleted" in out and "Exam review" not in out
    cli.cmd_log(cfg, cli.build_parser().parse_args(["log", "--events", "--details"]))
    assert "Event (sensitive email from" in capsys.readouterr().out   # uid 4 is sensitive: the calendar title says so

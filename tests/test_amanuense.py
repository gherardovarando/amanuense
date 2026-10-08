import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from amanuense import cli, config, model, render, triage, window
from amanuense.samples import FakeMailbox

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


def test_config_precedence(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[window]\ndefault = "3d"\n[model]\nname = "a"\n')
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


def test_tag_keywords():
    cfg = config.load(None, {"tag": {"keywords": {"research": "$label1"}}}, require_file=False)
    assert config.tag_keyword(cfg, "research") == "$label1"
    assert config.tag_keyword(cfg, "teaching") == "teaching"
    assert config.tag_keyword(cfg, "info") is None
    assert config.keyword_categories(cfg)["$label1"] == "research"


def test_clean():
    cats = ["a", "b", "info"]
    r = model.clean({"category": "zzz", "needs_action": False, "todo": "x", "deadline": "soon"}, cats)
    assert (r["category"], r["todo"], r["deadline"]) == ("info", None, None)


def fake_classify(cfg, system, msg):
    cat = "admin" if "facturas" in msg["body"] else "research"
    return model.clean({"category": cat, "needs_action": True, "todo": "do it", "deadline": None, "summary": "s"},
                       cfg["categories"])


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
    cfg = config.load(None, {"tag": {"todo_keyword": "$label4"}}, require_file=False)
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

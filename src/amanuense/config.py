"""Configuration: built-in defaults < config file < command-line options."""
import copy
import os
import re
import shutil
import sys
import tomllib
from importlib import resources
from pathlib import Path

from . import categories, window

DEFAULTS = {
    "categories_file": "categories.md",
    "owner": {"description": "a university professor", "languages": "English"},
    "window": {"default": "24h", "unread": True, "max_unread": 100, "max_messages": 300},
    "mail": {"account": "", "inbox": "INBOX", "sent": "Sent"},
    "mcp": {"command": "imap-mcp-server", "args": [], "env": {}},
    "model": {
        "api": "ollama",  # "ollama" or "openai" (any OpenAI-compatible server)
        "url": "http://localhost:11434",
        "name": "qwen3:4b-instruct",
        "api_key_env": "",
        "body_chars": 3000,
        "num_ctx": 4096,
        "temperature": 0.0,
        "timeout": 600,
        "think": None,
        "prompt_file": "prompt.md",  # your own instructions for the model, if this file exists; else the built-in ones
    },
    "output": {"template": "brief", "path": "~/amanuense/{date}.md", "max_fyi": 10, "urgent_within": "3d"},
    "outputs": [],  # [[outputs]] template, path: more files from the same run
    "skip": {"enabled": True, "subject": r"\[(BULK|SPAM\?)\]|newsletter",
             "from": r"no-?reply|newsletter|mailer-daemon|notifications?@"},
    "tag": {"enabled": False, "todo_keyword": ""},
    "sensitive": {
        "description": ("health, disability, sick leave or reasons for an absence; personal or family matters; "
                        "HR, salaries, contracts, evaluations of people, disciplinary or legal matters; "
                        "students' personal circumstances, or grades tied to names; confidential peer review "
                        "and committee deliberations; ID numbers, bank details, passwords and other credentials"),
        "from": "", "subject": "",   # regular expressions: matching mail is sensitive whatever the model says
        "keyword": "sensitive",      # IMAP keyword set when tagging; "" = none
        "screened": "screened",      # IMAP keyword on every message the model checked, when tagging; "" = none
        "task_tag": "sensitive",     # Taskwarrior tag on their tasks
    },
    "waiting": {"enabled": True, "after": "7d", "lookback": "30d", "max_messages": 50,
                "tasks": False, "task_tag": "followup"},
    "folders": [],  # [[folders]] name, label, expires_days, all, max_messages
    "calendar": {"enabled": False, "server": "", "url": "", "username": "", "password_file": "",
                 "password_command": "", "kind": "task", "time": "07:30", "minutes": 15, "alarm": True,
                 "title_template": "oneline", "template": "tiny"},
    "events": {"url": "", "minutes": 60},  # CalDAV calendar for events from mail ("" = only listed in the digest)
    "taskwarrior": {"enabled": "auto", "command": "task", "tags": ["email"], "project": "", "projects": {}, "rc": {},
                    # references that name one item (a manuscript, a case): mail that names the same one as a
                    # pending task, in any thread, is noted on that task instead of making a new one
                    "references": [r"\b[A-Z]{2,8}-D-\d{2}-\d{3,}\b",          # Editorial Manager: ABC-D-26-01234
                                   r"\b[A-Z]{2,8}-\d{4}-\d{3,}(?:\.R\d+)?\b",  # ScholarOne: ABC-2026-0123.R1
                                   r"(?<![\d.,:/-])\d{6,9}(?![\d.,:/-])"]},       # a long number: 1234567
}

# Settings that moved to the categories file.
OBSOLETE = {("categories",): "categories are the `## name` sections of the categories file",
            ("tag", "keywords"): "set `keyword:` in the category's section of the categories file",
            ("tag", "exclude"): "set `keyword: none` in the category's section of the categories file",
            ("output", "detail"): "use output.template (brief, long, tiny, ...; `amanuense template list`)",
            ("output", "format"): "use output.template; template = \"json\" gives JSON"}


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "amanuense"


def default_config_path():
    return Path(os.environ.get("AMANUENSE_CONFIG") or config_dir() / "config.toml")


def merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def expand(p):
    return Path(os.path.expanduser(str(p)))


def load(path=None, overrides=None, require_file=True):
    """Return the effective config. `overrides` is a nested dict from the command line."""
    path = Path(path) if path else default_config_path()
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        cfg = merge(cfg, tomllib.loads(path.read_text()))
    elif require_file:
        raise SystemExit(f"No config at {path}. Run `amanuense init` to create one, or pass --config.")
    cfg = merge(cfg, overrides or {})
    cfg["_path"] = str(path) if path.exists() else None
    for keys, hint in OBSOLETE.items():
        d = cfg
        for k in keys:
            d = d.get(k) if isinstance(d, dict) else None
        if d is not None:
            print(f"amanuense: warning: `{'.'.join(keys)}` in {path} is ignored; {hint}", file=sys.stderr)

    cats = expand(cfg["categories_file"])
    if not cats.is_absolute():
        cats = path.parent / cats
    if not cats.exists() and not cfg["_path"]:  # no config file at all (e.g. --test): use the example
        cats = resources.files("amanuense") / "data" / "categories.example.md"
    if not cats.exists():
        raise SystemExit(f"No categories file at {cats}. Run `amanuense init` or set categories_file.")
    cfg["categories_file"] = str(cats)
    try:
        c = categories.load(cats)
    except categories.CategoryError as e:
        raise SystemExit(f"amanuense: {e}") from None
    cfg["categories"] = c.names
    cfg["fallback"] = c.fallback
    cfg["keywords"] = {x.name: x.keyword for x in c.items}
    cfg["categories_prompt"] = c.prompt
    prompt = expand(cfg["model"]["prompt_file"] or DEFAULTS["model"]["prompt_file"])
    if not prompt.is_absolute():
        prompt = path.parent / prompt
    if prompt.exists():
        cfg["model"]["prompt_file"] = str(prompt)
    elif cfg["model"]["prompt_file"] != DEFAULTS["model"]["prompt_file"]:  # named on purpose but missing
        raise SystemExit(f"No prompt file at {prompt}. Fix model.prompt_file, or remove it for the built-in prompt.")
    else:
        cfg["model"]["prompt_file"] = ""  # the built-in prompt
    try:
        window.parse_duration(cfg["output"]["urgent_within"])
    except ValueError as e:
        raise SystemExit(f"config: output.urgent_within: {e}") from None
    for key in ("after", "lookback"):
        try:
            window.parse_duration(cfg["waiting"][key])
        except ValueError as e:
            raise SystemExit(f"config: waiting.{key}: {e}") from None
    for pattern in cfg["taskwarrior"]["references"]:
        try:
            re.compile(pattern)
        except (re.error, TypeError) as e:
            raise SystemExit(f"config: taskwarrior.references: bad pattern {pattern!r}: {e}")
    for name, pattern in cfg["taskwarrior"]["projects"].items():
        try:
            re.compile(pattern)
        except (re.error, TypeError) as e:
            raise SystemExit(f"config: taskwarrior.projects.{name}: {e}") from None
    for f in cfg["folders"]:
        if not isinstance(f, dict) or not f.get("name"):
            raise SystemExit("config: every [[folders]] entry needs a name")
    if cfg["output"].get("format") == "json":  # older configs
        cfg["output"]["template"] = "json"
    if cfg["calendar"]["kind"] not in ("task", "event"):
        raise SystemExit(f"config: calendar.kind must be \"task\" or \"event\", not {cfg['calendar']['kind']!r}")
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", str(cfg["calendar"]["time"])):
        raise SystemExit(f"config: calendar.time must be HH:MM, not {cfg['calendar']['time']!r}")
    for o in cfg["outputs"]:
        if not isinstance(o, dict) or not o.get("template") or not o.get("path"):
            raise SystemExit("config: every [[outputs]] entry needs a template and a path")
    for section, key in (("skip", "subject"), ("skip", "from"), ("sensitive", "subject"), ("sensitive", "from")):
        try:
            re.compile(cfg[section][key])
        except re.error as e:
            raise SystemExit(f"config: bad regular expression in {section}.{key}: {e}") from None
    return cfg


def tag_keyword(cfg, category):
    """IMAP keyword for a category, or None if that category is not tagged."""
    return cfg["keywords"].get(category)


def keyword_categories(cfg):
    """Map from IMAP keyword back to category, for messages tagged earlier (also by hand)."""
    return {tag_keyword(cfg, c): c for c in cfg["categories"] if tag_keyword(cfg, c)}


def init(force=False):
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for src, dst in (("config.example.toml", "config.toml"), ("categories.example.md", "categories.md")):
        target = d / dst
        if target.exists() and not force:
            continue
        with resources.as_file(resources.files("amanuense") / "data" / src) as f:
            shutil.copyfile(f, target)
        written.append(target)
    return d, written

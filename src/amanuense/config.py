"""Configuration: built-in defaults < config file < command-line options."""
import copy
import os
import re
import shutil
import tomllib
from importlib import resources
from pathlib import Path

DEFAULTS = {
    "categories": ["research", "teaching", "admin", "events", "reviewing", "info"],
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
    },
    "output": {"path": "~/amanuense/{date}.md", "format": "markdown"},
    "skip": {"enabled": True, "subject": r"\[(BULK|SPAM\?)\]|newsletter",
             "from": r"no-?reply|newsletter|mailer-daemon|notifications?@"},
    "tag": {"enabled": False, "keywords": {}, "exclude": ["info"], "todo_keyword": ""},
}


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

    cats = expand(cfg["categories_file"])
    if not cats.is_absolute():
        cats = path.parent / cats
    cfg["categories_file"] = str(cats)
    if not cfg["categories"]:
        raise SystemExit("config: `categories` is empty")
    for key in ("subject", "from"):
        try:
            re.compile(cfg["skip"][key])
        except re.error as e:
            raise SystemExit(f"config: bad regular expression in skip.{key}: {e}") from None
    return cfg


def categories_text(cfg):
    p = Path(cfg["categories_file"])
    if not p.exists() and not cfg.get("_path"):  # no config file at all (e.g. --test): use the example
        return (resources.files("amanuense") / "data" / "categories.example.md").read_text()
    if not p.exists():
        raise SystemExit(f"No categories file at {p}. Run `amanuense init` or set categories_file.")
    return p.read_text()


def tag_keyword(cfg, category):
    """IMAP keyword for a category, or None if that category is not tagged."""
    if category in cfg["tag"]["exclude"]:
        return None
    return cfg["tag"]["keywords"].get(category, category)


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

"""The categories file: the only place where categories are defined.

Format (Markdown):

    ## research                 <- a one-word heading starts a category
    keyword: $label1            <- optional: IMAP keyword when tagging (default: the name; "none" = never tagged)
    fallback: yes               <- optional: used when the model's answer is not a known category

    Description, then examples:
    - An email like X → research, needs action.

    ## Edge cases               <- a heading with spaces is a general note, not a category

Everything except the keyword/fallback lines and <!-- comments --> goes into the model's instructions.
If no category is marked as fallback, the last one is.
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

HEADING = re.compile(r"^##\s+(.+?)\s*$")
NAME = re.compile(r"^[A-Za-z0-9_$.-]+$")
SETTING = re.compile(r"^(keyword|fallback)\s*:\s*(.*?)\s*$", re.I)


class CategoryError(ValueError):
    pass


@dataclass
class Category:
    name: str
    keyword: str | None
    fallback: bool = False
    examples: int = 0
    lines: list = field(default_factory=list, repr=False)


@dataclass
class Categories:
    items: list
    prompt: str
    fallback: str

    @property
    def names(self):
        return [c.name for c in self.items]

    def keyword(self, name):
        return next(c.keyword for c in self.items if c.name == name)


def parse(text):
    items, prompt, current = [], [], None
    text = re.sub(r"<!--.*?-->\n?", "", text, flags=re.S)  # comments are for people, not the model
    for line in text.splitlines():
        if m := HEADING.match(line):
            title = m.group(1)
            current = None
            if NAME.match(title):
                if any(c.name == title for c in items):
                    raise CategoryError(f"category {title!r} is defined twice")
                current = Category(name=title, keyword=title)
                items.append(current)
        elif current and (s := SETTING.match(line)):
            key, value = s.group(1).lower(), s.group(2)
            if key == "keyword":
                current.keyword = None if value.lower() in ("", "none", "no") else value
                if current.keyword and current.keyword.startswith("\\"):
                    raise CategoryError(f"{current.name}: keyword {value!r} is an IMAP system flag")
            else:
                current.fallback = value.lower() in ("yes", "true", "1")
            continue  # settings are not shown to the model
        elif current and line.lstrip().startswith("- "):
            current.examples += 1
        prompt.append(line)
    if not items:
        raise CategoryError("no categories: add sections like `## research`")
    marked = [c.name for c in items if c.fallback]
    if len(marked) > 1:
        raise CategoryError(f"more than one fallback category: {', '.join(marked)}")
    return Categories(items=items, prompt="\n".join(prompt).strip() + "\n",
                      fallback=marked[0] if marked else items[-1].name)


def load(path):
    try:
        return parse(Path(path).read_text())
    except CategoryError as e:
        raise CategoryError(f"{path}: {e}") from None


def add(path, name, description="", examples=(), keyword=None, fallback=False):
    """Insert a new category section after the last existing one (before the general notes)."""
    if not NAME.match(name):
        raise CategoryError(f"bad category name {name!r}: one word, letters, digits, _ - . $")
    p = Path(path)
    text = p.read_text()
    cats = parse(text)
    if name in cats.names:
        raise CategoryError(f"category {name!r} already exists")
    if fallback and any(c.fallback for c in cats.items):
        raise CategoryError("there is already a fallback category")
    section = [f"## {name}"]
    if keyword is not None:
        section.append(f"keyword: {keyword or 'none'}")
    if fallback:
        section.append("fallback: yes")
    section.append("")
    if description:
        section += [description, ""]
    if examples:
        section += ["Examples:"] + [f"- {e}" for e in examples] + [""]
    lines = text.splitlines()
    insert_at = _section_end(lines, cats.names[-1])
    if insert_at > 0 and lines[insert_at - 1].strip():
        section.insert(0, "")
    lines[insert_at:insert_at] = section
    _write(p, lines)


def add_example(path, name, example):
    """Append an example line at the end of a category's section."""
    p = Path(path)
    lines = p.read_text().splitlines()
    if name not in parse("\n".join(lines)).names:
        raise CategoryError(f"no category {name!r}")
    end = _section_end(lines, name)
    while end > 0 and not lines[end - 1].strip():  # after the last non-blank line
        end -= 1
    start = _section_start(lines, name)
    if not any(l.strip().lower() == "examples:" for l in lines[start:end]):
        lines[end:end] = ["", "Examples:"]
        end += 2
    lines.insert(end, f"- {example}")
    _write(p, lines)


def _section_start(lines, name):
    return next(i for i, l in enumerate(lines) if (m := HEADING.match(l)) and m.group(1) == name)


def _section_end(lines, name):
    start = _section_start(lines, name)
    for i in range(start + 1, len(lines)):
        if HEADING.match(lines[i]):
            return i
    return len(lines)


def _write(p, lines):
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n")
    os.chmod(tmp, p.stat().st_mode)
    tmp.replace(p)

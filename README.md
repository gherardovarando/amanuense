# amanuense

A private secretary for your inbox. A small language model running on your own machine reads your recent mail, writes a daily digest with proposed to-dos, and can tag each message with its category in the mailbox. Your mail never leaves the machine.

## What it does

- **`amanuense digest`** reads the mail in a time window (the last 24 hours by default) plus any unread mail older than that. It skips broadcasts and notifications by fixed rules, and asks the local model to give each remaining email:
  - a category
  - whether it needs action
  - a one-line to-do
  - a deadline
  - a summary

  The output is a Markdown or JSON file. It starts with the proposed to-dos sorted by deadline, then lists the emails grouped by category. Emails you already answered are marked as handled.
- **`amanuense tag`** sets an IMAP keyword with the category on each message. Thunderbird shows these keywords as tags, and other mail clients that support keywords do too. Messages that already carry a category keyword, for example one you set by hand, are left alone. `digest --tag` does both in one run.
- **`amanuense check`** tests the configuration, the model server and the mail server.
- **`amanuense init`** creates a config file and a categories file for you to edit.
- **`amanuense config`** prints the effective configuration.

## How it stays private and safe

- The model runs locally, with [Ollama](https://ollama.com) or any OpenAI-compatible server such as llama.cpp, vLLM or LM Studio. No mail content goes to an external service unless you point `model.url` at one.
- Mail is read through [imap-mcp-server](https://github.com/nikolausm/imap-mcp-server). amanuense starts the server with only the tools a run needs:
  - search and read one email, always
  - add and remove a keyword, only when tagging

  Sending, forwarding, moving, flagging and deleting are never enabled, so the program can't do them even by mistake. Reading doesn't mark messages as read.
- The model never chooses which tools to call. The program makes fixed calls, passes only the email text to the model, and gets JSON back.
- The terminal output is counts only, so no mail content ends up in logs. The digest file is created readable only by you.
- Your configuration and category examples live in `~/.config/amanuense/`, outside the repository.

## Requirements

- Python 3.11 or later
- A model server. With Ollama, `qwen3:4b-instruct` (about 2.5 GB) works on a 4-core CPU without a GPU, at roughly 30 seconds per email.
- [imap-mcp-server](https://github.com/nikolausm/imap-mcp-server) with your IMAP account configured in it

## Install

```sh
git clone https://github.com/gherardovarando/amanuense
cd amanuense
python3 -m venv .venv
.venv/bin/pip install -e .               # installs the `amanuense` command into .venv/bin
ln -s "$PWD/.venv/bin/amanuense" ~/.local/bin/amanuense   # optional: put it on your PATH
ollama pull qwen3:4b-instruct

amanuense init      # writes ~/.config/amanuense/config.toml and categories.md
amanuense check
```

`pipx install .` also works and gives you the command on your PATH directly.

In `~/.config/amanuense/config.toml`, set at least:
- `mail.account`: the account name in imap-mcp-server
- `mail.sent`: your Sent folder, used to spot replies
- `mcp.command` and `mcp.args`: how to start imap-mcp-server
- `owner.description`: a few words about whose mail it is

## Use

```sh
amanuense digest --test                  # made-up emails, no mailbox access; prints the digest
amanuense digest                         # last 24 h + older unread mail -> output.path
amanuense digest -w yesterday            # yesterday, midnight to midnight
amanuense digest -w 3d --no-unread       # last 3 days only
amanuense digest -w last                 # since the previous successful digest
amanuense digest -w 2026-10-01..2026-10-05 --format json -o week.json
amanuense digest --tag                   # digest, and tag the messages in the mailbox
amanuense tag --dry-run -v               # decide tags without writing them
amanuense tag --todo-keyword '$label4'   # tag, and mark to-dos with Thunderbird's "To Do" tag
amanuense tag -w 2w --retag              # re-triage two weeks and replace category tags
```

Window specifications for `-w`/`--window`:

| Spec | Meaning |
|---|---|
| `24h`, `3d`, `2w` | The last N hours, days or weeks |
| `today`, `yesterday` | Calendar days |
| `week` | Since Monday |
| `last` | Since the previous successful run of the same command (24 h the first time) |
| `2026-10-01` | Since that day |
| `2026-10-01..2026-10-05` | Those days, inclusive |

Other options common to `digest` and `tag`:
- `--unread` / `--no-unread`, `--max-unread N`, `--max-messages N`
- `--account`, `--folder`, `--sent-folder`, `--no-skip`
- `--model`, `--model-url`, `--model-api {ollama,openai}`
- `--mcp-command "node /path/to/index.js"`, `--categories FILE`
- `--test`, `--dry-run`, `-v`

Run `amanuense COMMAND --help` for details. Command-line options override the config file, which overrides the built-in defaults.

### Every morning

A cron job:

```
0 6 * * * ~/.local/bin/amanuense digest -w last
```

Or a systemd user timer that runs the same command.

## Categories

The category names are in `config.toml` (`categories`); the model must pick one of them. Their descriptions and examples are in `categories.md`, which becomes part of the model's instructions. Small models learn much more from examples than from descriptions. When a category is often wrong, add a line in the form "an email like X → category, needs action or not". Keep the `## ` headings the same as the names in `categories`.

When tagging, each category becomes an IMAP keyword with the same name, unless `[tag.keywords]` maps it to another keyword. Categories in `tag.exclude` (by default `info`) get no keyword.

## Development

```sh
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

The tests use made-up messages and a stub model. They need neither a mail server nor a model server.

## Limitations

- Categories and to-dos are a small model's guesses. Treat the to-dos as proposals.
- Replies are detected by `In-Reply-To`, by the `\Answered` flag, or by a message in the Sent folder to the sender after the email's date.
- Only the email body is read, not attachments. Bodies are cut to `model.body_chars` characters.
- Unread mail outside the window is capped at `window.max_unread` messages, newest first, so a large backlog can't make a run take hours.

## How this was made

This tool was written by an AI coding agent: **Claude Opus 5.5** (model ID `claude-opus-5-5`, 1M-token context) by Anthropic, running in **Claude Code**, in October 2026. The design, features and constraints came from the repository owner's requests during the session, and they reviewed the results along the way:
- a fully local model
- no mail content visible to the agent or to any external service
- read-only access to the mailbox, with keyword tagging as the only change allowed
- a configurable command-line tool

The agent wrote the code and tests and tested them only against made-up emails. Runs on real mail were checked by the owner, since the agent never sees their content.

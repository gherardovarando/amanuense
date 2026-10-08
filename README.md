# amanuense

A private daily digest of your inbox, written by a small language model that runs on your own machine. Your mail never leaves it.

Every run reads the last 24 hours of the inbox plus any older unread mail, skips broadcasts and notifications, and asks the local model to give each remaining email a category, decide whether it needs action, and write a one-line to-do and summary. The result is a Markdown file with the proposed to-dos sorted by deadline, then the emails grouped by category. Emails you have already replied to are marked as handled.

## How it stays private and safe

- The model runs locally with [Ollama](https://ollama.com). No mail content goes to any external service.
- Mail is read through [imap-mcp-server](https://github.com/nikolausm/imap-mcp-server), started with only two tools enabled: search and read one email (`IMAP_MCP_ENABLED_TOOLS`). The script can't send, move, flag or delete mail, and reading doesn't mark messages as read.
- The model never chooses which tools to call. The script makes fixed calls and only passes the email text to the model, which returns JSON.
- Only counts are printed to the terminal, so no mail content ends up in logs.
- Your personal settings (`config.toml`) and category examples (`categories.md`) are ignored by git.

## Requirements

- Python 3.11 or later
- [Ollama](https://ollama.com) with a model that follows instructions well. `qwen3:4b-instruct` (about 2.5 GB) works on a 4-core CPU without a GPU, at roughly 30 seconds per email.
- [imap-mcp-server](https://github.com/nikolausm/imap-mcp-server) with your IMAP account configured in it

## Setup

```sh
git clone https://github.com/gherardovarando/amanuense
cd amanuense
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
ollama pull qwen3:4b-instruct

cp config.example.toml config.toml        # account, folders, output folder, skip rules
cp categories.example.md categories.md    # category descriptions and examples
```

Edit `config.toml`. In particular, set:
- `mail.account` to the account name configured in imap-mcp-server
- `mail.sent` to your Sent folder, used to detect replies
- `owner.description`, a few words about whose mail it is

## Use

```sh
.venv/bin/python digest.py --test        # six made-up emails, no IMAP access
.venv/bin/python digest.py               # last 24 hours + older unread mail
.venv/bin/python digest.py --hours 72    # longer window
```

The digest is written to `<output.dir>/<YYYY-MM-DD>/<output.filename>`.

To run it every morning, use a cron job or a systemd user timer, for example:

```
0 6 * * * cd /path/to/amanuense && .venv/bin/python digest.py >> digest.log 2>&1
```

## Improving the categories

The model sees `categories.md` as part of its instructions. Small models learn much more from examples than from descriptions. When a category is often wrong, add an example line in the form "an email like X → category, needs action or not". Keep the `## ` headings the same as the `categories` list in `config.toml`.

## Limitations

- Categories and to-dos are a small model's guesses. Treat the to-dos as proposals.
- Replies are detected by `In-Reply-To`, by the `\Answered` flag, or by a message in the Sent folder to the sender after the email's date.
- Older unread mail is capped at `mail.max_unread` messages, newest first, so a large backlog can't make a run take hours (about 30 seconds per email).
- Only the email body is read, not attachments. Bodies are cut to `model.body_chars` characters.

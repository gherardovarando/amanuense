# amanuense

![amanuense logo: a scribe, shaped like a keyhole, writing in a closed room](docs/logo/icon-128.png)

A private secretary for your inbox. A small language model running on your own machine reads your recent mail, writes a daily digest with proposed to-dos, and can tag each message with its category in the mailbox. Your mail never leaves the machine.

## What it does

- **`amanuense digest`** reads the mail in a time window (the last 24 hours by default) plus any unread mail older than that. It skips broadcasts and notifications by fixed rules, and asks the local model to give each remaining email:
  - a category
  - whether it needs action
  - a one-line to-do
  - a deadline
  - a priority (high, normal or low)
  - a summary
  - whether it is sensitive (see [Sensitive mail](#sensitive-mail))
  - an event it fixes, if any (see [Events from mail](#events-from-mail))

  The layout of the output is a template you choose (see [Templates](#templates)). The default, `brief`, has the to-dos, urgent ones first, then by deadline; the mail you're waiting on; and up to ten things worth knowing. Low-priority mail, mail you already answered and skipped mail are only counted. `long` lists every triaged email grouped by category, and there are shorter and safer-to-share layouts too. One run can write several files, each with its own template.

  Small models call too much urgent, so a to-do whose deadline is more than three days away (`output.urgent_within`) is never shown as urgent.

  Taskwarrior is optional. If [Taskwarrior](https://taskwarrior.org) is installed, each new to-do also becomes a task (see [Taskwarrior](#taskwarrior)). Without it you get the same digest, with the same to-dos.
- **Waiting for replies.** It also checks the mail you sent. Anything that asked a question or a favour and got no answer after a week is listed with what you're waiting for, optionally as a follow-up task.
- **Extra folders.** Besides the inbox, it can read other folders, such as a quarantine or an oversized-mail folder that the server empties after some days. Their messages are triaged like the rest. A "Deleted soon" section at the top of the digest says how many days each one has left, and their tasks are due before the deletion date.
- **Sensitive mail.** Mail about health, personal matters, HR, students' circumstances, confidential reviews or credentials is marked sensitive. Its task names only the sender, and tagging sets a `sensitive` keyword in the mailbox, so other tools and agents can leave it closed.
- **`amanuense tag`** sets an IMAP keyword with the category on each message. Thunderbird shows these keywords as tags, and other mail clients that support keywords do too. Messages that already carry a category keyword, for example one you set by hand, keep it. If they have no `screened` keyword yet, the model still checks them for [sensitive material](#sensitive-mail). Mail that already has `screened` was checked by an earlier run and is skipped, unless `--retag`. `digest --tag` does both in one run.
- **`amanuense render`** lays out the last digest again without reading mail or calling the model, e.g. `amanuense render -t long`. The results of the last run are kept for this in `~/.local/state/amanuense/last-digest.json`, mode 600.
- **`amanuense log`** shows the past runs from a log of counts and ids, or what became of the events it added (see [Run log](#run-log)).
- **`amanuense template`** lists, shows, copies and previews the digest templates.
- **`amanuense calendar`** lists your CalDAV calendars, lists the events of the coming days (`calendar events`), or puts the last digest in one (see [Calendar](#calendar)).
- **`amanuense check`** tests the configuration, the model server and the mail server.
- **`amanuense category`** lists the categories, adds one, or adds an example to one.
- **`amanuense init`** creates a config file and a categories file for you to edit.
- **`amanuense config`** prints the effective configuration.

## How it stays private and safe

- The model runs locally, with [Ollama](https://ollama.com) or any OpenAI-compatible server such as llama.cpp, vLLM or LM Studio. No mail content goes to an external service unless you point `model.url` at one.
- Mail is read through [imap-mcp-server](https://github.com/nikolausm/imap-mcp-server). amanuense starts the server with only the tools a run needs:
  - search and read one email, always
  - add and remove a keyword, only when tagging

  Sending, forwarding, moving, flagging and deleting are never enabled, so the program can't do them even by mistake. Reading doesn't mark messages as read.
- The model never chooses which tools to call. The program makes fixed calls, passes only the email text to the model, and gets JSON back.
- The terminal output is counts only, so no mail content ends up in logs. The digest file, and the copy of the last results kept for `render`, are created readable only by you.
- Your configuration and category examples live in `~/.config/amanuense/`, outside the repository.

## Requirements

- Python 3.11 or later (installs `mcp` and `jinja2`)
- A model server. With Ollama, `qwen3:4b-instruct` (about 2.5 GB) works on a 4-core CPU without a GPU, at roughly 30 seconds per email.
- [imap-mcp-server](https://github.com/nikolausm/imap-mcp-server) with your IMAP account configured in it
- Optional: [Taskwarrior](https://taskwarrior.org) 2.6 or 3, for tasks; a CalDAV server, for events and the phone reminder. Without them you still get the digest.

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
amanuense digest -w 2026-10-01..2026-10-05 -t json -o week.json
amanuense digest --tag                   # digest, and tag the messages in the mailbox
amanuense digest --no-tasks              # don't add Taskwarrior tasks this time
amanuense digest --no-extra-folders      # inbox only
amanuense digest --waiting-after 3d      # flag sent mail with no reply after 3 days
amanuense digest --waiting-lookback 2w   # only look at what you sent in the last two weeks
amanuense digest --waiting-tasks         # and add a follow-up task for each
amanuense digest --dry-run               # show "task (dry run)" instead of adding tasks or tags
amanuense digest -t long                 # every message, grouped by category
amanuense digest -t tiny -o -            # a glance, printed
amanuense digest --urgent-within 1d      # only same/next-day deadlines count as urgent
amanuense render -t long                 # the last run again, in full, next to the usual file
amanuense template list                  # the layouts you can use
amanuense template preview counts        # try a layout on made-up results
amanuense tag --dry-run -v               # decide tags without writing them
amanuense tag --todo-keyword '$label4'   # tag, and mark to-dos with Thunderbird's "To Do" tag
amanuense tag -w 2w --retag              # re-triage two weeks and replace category tags

amanuense category list
amanuense category add phd -d "Doctoral matters: enrolment, committees, thesis deposit." \
    -e "The doctoral school asks to sign the annual evaluation → phd, needs action." -k '$label7'
amanuense category example info "A call for papers sent to a mailing list → info, no action."
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

## Templates

The digest's layout is a [Jinja](https://jinja.palletsprojects.com/en/stable/templates/) template. The package does the work (collecting, triaging, sorting, counting) and the template only decides what to show and how.

| Template | What it shows |
|---|---|
| `brief` (default) | To-dos with urgent ones first, waiting for replies, up to `max_fyi` things worth knowing; the rest counted |
| `long` | Everything: to-dos, waiting for replies, every triaged email by category with its summary, skipped mail |
| `tiny` | A glance for the phone: one line of counts, the next `tiny_items` (3) to-dos with urgent ones marked ❗, and how many more |
| `todo` | Only the to-do checklist, grouped by category |
| `counts` | Numbers, categories and deadline dates only: no sender, subject or summary, so no mail text |
| `oneline` | One line for a notification or status bar: counts and the next deadline, no mail text |
| `page` | The brief digest as a self-contained HTML page for the phone, with light and dark mode |
| `json` | Every field of every message, for other programs (built in, not a template) |

Choose one with `output.template` or `-t`, or write several files per run with `[[outputs]]` in the config. To change one, copy it and edit the copy:

```sh
amanuense template copy brief mine        # -> ~/.config/amanuense/templates/mine.md.j2
amanuense template preview mine           # made-up results: no mail, no model, instant
amanuense render -t mine                  # the last real run, in your layout
```

A template in `~/.config/amanuense/templates/` overrides a shipped one with the same name. The file extension before `.j2` (`.md`, `.html`, `.txt`) is the extension of the output file. `_macros.j2` has the shared pieces (a to-do line, a waiting line); `{% import "_macros.j2" as x %}` and `{{ x.todo(m) }}`.

**What a template sees**

| Variable | Contents |
|---|---|
| `todos` | Open to-dos, urgent first, then by deadline and category |
| `urgent` | The urgent ones among them |
| `fyi` | Open emails without an action but worth knowing (priority normal or high) |
| `low` | Open low-priority emails without an action |
| `handled` | Emails you already answered |
| `skipped`, `errors` | Emails skipped by the rules, and emails the model failed on |
| `expiring` | Emails in folders the server empties soon, soonest first |
| `older_unread` | Unread emails from before the window |
| `events` | Emails that fix an upcoming event, soonest first, each event once |
| `messages`, `triaged` | Every email read, and those the model triaged |
| `by_category` | A list of `{name, items}`, one per category that has emails |
| `waits` | Sent mail waiting for a reply: `subject`, `to`, `to_list`, `date`, `days`, `waiting_for`, `category`, `task` |
| `count(list)` | `{category: number}` for a list of emails |
| `window` | `start`, `end`, `text` |
| `now`, `model`, `minutes`, `categories`, `problems`, `task_error`, `tagging`, `dry_run`, `unread_included` | The run |
| `waiting_after`, `waiting_lookback` | The waiting settings, in words |
| `out` | The `[output]` section of the config: any key you add there is `out.<key>` |

Each email has `uid`, `folder`, `folder_label`, `sender`, `from`, `subject`, `date`, `attachments`, `category`, `needs_action`, `todo`, `deadline`, `priority` (`high`, `normal`, `low`), `summary`, `sensitive`, `event`, `handled`, `older_unread`, `skipped`, `error`, `task` (`added`, `would add`, `exists` or none), `expires` and `days_left`.

Filters: `date("%a %d %b")` for dates, `plural("message")` for counts (`1 message`, `3 messages`), and Jinja's own (`length`, `join`, `selectattr`, `sort`, …).

Templates run sandboxed, and unknown variables are an error, not silently empty: `amanuense check` and the start of every digest try each configured template on made-up results first, so a typo never costs a run. Mail text is only ever inserted as data, never run as template code. HTML templates (`*.html.j2`) escape it.

## Calendar

To get the digest on your phone without a new app or service, amanuense can put it in a CalDAV calendar that the phone already syncs (Nextcloud, ownCloud, SOGo, Baïkal, iCloud, Fastmail, a university's DAV server…). Each run creates or replaces one task due that morning (or an event, with `kind = "event"`):

- title: the `oneline` template, counts only (`5 to-dos (2 urgent) · next deadline Sat 10 Oct (research)`), which is what a lock screen shows
- notes: the `tiny` template
- a reminder at that time, marked private; tick the task off once you've read it

```toml
[calendar]
enabled = true
server = "https://dav.example.org/"
username = "me"
password_file = "~/.config/amanuense/caldav-password"   # chmod 600
```

If the server's web address leads to a login page, give its DAV address instead (for SOGo, `https://host/SOGo/dav/`). Then `amanuense calendar list` shows the calendars; copy one's `url` into the section. `amanuense calendar put` sends the last digest right away, to try it. The password is read from `password_file` (refused if others can read it), from `password_command` (e.g. `pass show caldav`), or from `$AMANUENSE_CALDAV_PASSWORD`. It is never stored elsewhere or printed.

The calendar server sees the event text, so use a server you'd trust with the mail itself, or set `template = "counts"` to keep mail text out of it.

## Categories

The categories file (`~/.config/amanuense/categories.md`) is the only place where categories are defined. Each section with a one-word heading is a category:

```markdown
## teaching
keyword: $label2

Courses, students asking about a course, exams, grades.

Examples:
- A student asks whether topic 5 is in Tuesday's exam → teaching, needs action.

## info
keyword: none
fallback: yes

Newsletters, broadcasts, mailing lists, notifications.

## Edge cases

- Anything sent to a list with no personal request is info.
```

- **`keyword:`** is the IMAP keyword set when tagging. It defaults to the category name; `none` means messages in this category are never tagged.
- **`fallback: yes`** marks the category used when the model's answer isn't a known category. Without it, the last category is the fallback.
- **Headings with spaces**, like "Edge cases", are general notes.
- **Everything else** goes into the model's instructions: descriptions, examples and notes, minus the `keyword:`/`fallback:` lines and `<!-- comments -->`.

Small models learn much more from examples than from descriptions. When a category is often wrong, add an example in the form "an email like X → category, needs action or not", by hand or with `amanuense category example`. `amanuense category add` inserts a new section after the last category. To rename or remove a category, edit the file.

### Refining the categories every day

A small local model gets better mostly through its categories file, so it pays to review its work daily, especially in the first weeks:

1. After the morning run, go through the tasks it made: delete the duplicates, complete what was already handled, fix wrong categories, projects, due dates and vague descriptions. A word on why ("dup", "already done", "not my action") helps the next step.
2. Once a day, ask a more capable agent or model to compare what amanuense produced with what you kept and changed, and to turn the recurring mistakes into fixes. Most fixes are a new example or rule in `categories.md`, for instance "a student's absence note → teaching, needs action". Some are a `[taskwarrior.projects]` rule or a skip rule in the config.
3. Before the next run, check the changes with `amanuense check`. You can also try a rule on a made-up email of the same kind.

The review can be based only on the tasks, the digest and your corrections, never on the mail itself, so the mail stays on your machine. Show a cloud agent only what you would be happy to share. Rules that apply to just one person or project (who handles what, which folder means which project) belong in your own config and categories file, not in the shipped examples.

## Events from mail

The model also says whether an email fixes an event you are expected to attend: a meeting, call, talk, class or defence on a stated date. It gives a title, the date, start and end times and a place when the email states them, and one sentence on what the event is for and your role in it.

- **Not events:** a proposal still to be agreed ("can we meet on Tuesday?"), a deadline, or an event you are only told about.
- **In the digest:** `brief`, `long` and `page` list the upcoming events, soonest first, each once. `counts` has the number, and the summary line says how many were found and added.
- **In a calendar:** with `events.url` set to a CalDAV calendar, each upcoming event is added there:
  ```toml
  [events]
  url = "https://dav.example.org/calendars/me/amanuense/"   # `amanuense calendar list` shows yours
  minutes = 60      # length of an event whose end time the email doesn't say
  ```
  It uses the server, user and password of the `[calendar]` section. A separate calendar is best: it shows next to yours, and you can hide or empty it.
- **How events look:** tentative and private. The description starts with what the event is for, then names the sender, the subject and the Message-ID of the email. Mail marked [sensitive](#sensitive-mail) gives only "Event (sensitive email from X)", with no reason, subject or place. An email that gives a date but no time makes an all-day event.
- **Never overwritten:** each event is created once. Its id comes from the date, the start time and the sender, so the same meeting in a later email from the same person isn't added twice. amanuense never replaces, edits or deletes an event: confirm it, move it or delete it yourself, and it stays that way. A meeting moved to another time in a later email gets a new event, and the old one stays until you delete it.
- **Reading them back:** `amanuense calendar events` lists the events of the next 7 days (`--days N`) in all your calendars except the digest's own, or in one (`--calendar NAME`): day, time, title, place, calendar and status, and under each the first line of its notes. It only reads. An assistant that may not read your mail can use it to see your agenda, including the events amanuense proposed.
- **Trial runs:** `--test` and `--dry-run` write nothing. Without `events.url`, the digest lists "Events I would add".

Templates see `events` (the emails with an upcoming event) and `m.event` on each email (`title`, `date`, `start`, `end`, `place`, `why`, or none), plus `m.event_status` (`added`, `exists`, `would add`, `failed`).

## Run log

Each `digest` and `tag` run adds one line to `~/.local/state/amanuense/runs.jsonl` (mode 600). It holds counts and ids only, never text from mail: no subjects, senders, titles or summaries. So you can show it to someone, or to an assistant, that must not see your mail.

- **Per run:** time, command, window, model, minutes and seconds per message; messages, triaged, reused, skipped, errors, sensitive, to-dos per category, tasks added and noted, replies noted, tagged; problems (error messages).
- **Per event put in the calendar:** its calendar id, date, start time, the IMAP uid of its email, and added / exists / failed.

```
amanuense log                    # the last 14 runs, one line each (-n 30 for more)
amanuense log --events           # each event added, and whether it is now tentative, confirmed, moved or deleted
amanuense log --events --details # the same with each title, read from the calendar
```

`log --events` only reads the calendar. Events you deleted are a simple measure of the model's mistakes. The mail uid leads back to the email in your mail client.

## Sensitive mail

Some mail should be read only by you: health, sick leave, personal or family matters, HR and salaries, evaluations of people, students' personal circumstances, confidential reviews, ID numbers, bank details and passwords. amanuense marks such mail, so that an assistant with access to your mailbox or task list (for example an AI agent) knows not to open it without your say-so.

- **How it decides:** the model answers one more question per email, "sensitive: yes or no", and is told to say yes when unsure. Mail matching your own rules is sensitive whatever the model says:
  ```toml
  [sensitive]
  from = 'occupational-health@|hr@'
  subject = 'confidential|sick leave'
  # description = "..."   # what counts as sensitive, in the model's instructions
  ```
- **In the mailbox** (when tagging, `--tag` or `tag.enabled`):
  - `sensitive` on sensitive mail (`sensitive.keyword`);
  - `screened` on every email the model checked (`sensitive.screened`), sensitive or not. Mail without it hasn't been checked yet, for example mail that arrived after the run.

  Both keywords are only ever added, never removed, even with `--retag`. Set either to `""` to turn it off.
- **Tasks:** the description is only "Sensitive email from <sender>: needs action". The task gets the `sensitive` tag (`sensitive.task_tag`), and its annotation has `subject: (sensitive)` instead of the subject. A task list shown to someone else, or read by an agent, says nothing about the content.
- **Digest:** your digest still shows the full to-do and summary, marked 🔒.

A small model will miss some sensitive mail, so a reader should still check the sender and subject before opening anything. Mail skipped by the skip rules, and mail the model failed on, gets neither keyword.

## Extra folders

```toml
[[folders]]
name = "Quarantine"
label = "held mail"     # shown in the digest
expires_days = 14       # the server deletes mail here after 14 days
all = true              # read the whole folder whatever the window (default)
max_messages = 100
project = "conf2026"    # optional: Taskwarrior project for tasks from this folder
```

- **Which messages:** a folder with `all = true` is read in full on every run, because a holding area is small and everything in it matters until it's gone. With `all = false`, only its messages in the time window are read.
- **Read by the model once:** a message dated before the window was read on an earlier day. The digest keeps the model's answer from the last run, so the message keeps its to-do without another model call. If there is no earlier answer but the message has the `screened` keyword, it is listed without being read again. Only a message never read before goes to the model. This saves about a minute per held message per run.
- **Deletion date:** the message's date plus `expires_days`. It's approximate, because servers count from arrival.
- **In the digest:** expiring messages are listed under "Deleted soon", soonest first.
- **Tasks:** only messages dated inside the window get tasks. Older ones were there on earlier runs, or before you started using amanuense, so they're listed in the digest but get no task: a backlog doesn't turn into stale tasks. The due date is the earlier of the email's deadline and the deletion date; when it's the deletion date, a note on the task says so. With `project`, their tasks get that project.
- **Same email in two folders** (sent to two of your addresses): it's triaged once, from the inbox or the first folder listed.
- **Errors:** if a folder can't be read, for example because it doesn't exist, the run goes on and the digest says so. `amanuense check` lists each folder with its message count.

## Waiting for replies

`digest` also lists mail you sent that is still waiting for an answer:

1. It reads your Sent folder for the last `waiting.lookback` (default `30d`). Messages are grouped into conversations by subject, without Re:/Fwd:, and recipients. Only the latest message of each conversation counts, so a reminder you sent restarts the clock.
2. A conversation is waiting when that latest message is older than `waiting.after` (default `7d`), and nobody it went to has written to you since. Replies are looked for in every folder except Trash, Spam and Drafts, so filed replies count. A message that replies to yours (In-Reply-To) counts too.
3. The local model reads each of those messages and decides whether it asked for something, and what you're waiting for. Thank-you notes and FYIs drop out.

With `waiting.tasks = true` (or `--waiting-tasks`), each one also becomes a Taskwarrior task: "Follow up: …", with the category, `email` and `followup` tags. The no-duplicates rule applies as for the other tasks.

Both durations take `36h`, `7d`, `2w` or a plain number of days, in the config or with `--waiting-after` and `--waiting-lookback`. These messages are checked on every run while they are within the lookback, so each costs a model call per run; `waiting.max_messages` caps that.

## Taskwarrior

When `taskwarrior.enabled` is `"auto"` (the default) and the `task` command is installed, `digest` adds one task for each open to-do: emails that need action and that you haven't answered. It works with Taskwarrior 2.6 and 3 (tested with 3.5). Without the `task` command, or with `enabled = false`, this step is skipped and the rest of the run is the same.

- **Tags:** the category, plus `taskwarrior.tags` (default `email`). Categories with `keyword: none` get no category tag.
- **Description:** the to-do. **Due date:** the deadline, if the email has one. A deadline already past sets no due date; a note on the task says it has passed instead.
- **Project:** the folder's `project` (see [Extra folders](#extra-folders)), else the first `[taskwarrior.projects]` rule that matches, else `taskwarrior.project`, else none. A rule is a name and a regular expression, matched case-insensitively against the subject, sender, recipients and folder:
  ```toml
  [taskwarrior.projects]
  conf2026 = 'CONF ?2026|conf2026@'
  ```
- **One task per topic:** an email whose subject (without Re:/Fwd:/[list] prefixes and punctuation) is the same as a pending task's, or as an earlier email's in the same run, becomes a note ("same thread: …") on that task. If the matching task is completed or deleted, a new task is made and a note mentions the earlier one. Subjects shorter than 12 characters are too generic and never group.
- **Annotation:** points back to the email, for example `2026-10-08 IMAP uid 123, account work, subject: Review invitation (amanuense, message-id <abc@x>)`.
- **No duplicates:** before adding, amanuense reads all your tasks, in any status. An email whose Message-ID or account and UID already appears in an annotation gets no new task, even if you completed or deleted the earlier one. Tasks you made by hand count if their annotation has `IMAP uid N, account A` or `msgid:<...>`, or names one of the extra folders together with `uid N` (for example "/pkg uid 131 + corrected uid 132"). The same email twice in one run gets one task.
- **Replies in a task's thread:** an email that replies (In-Reply-To) to an email that already has a task gets no new task: a note pointing to it is added to that task instead.
- **Your replies:** when you reply to the email of a pending task, the next digest adds a note to the task: `You replied on 2026-10-09 10:00 (amanuense, reply id <…>)`. The reply is found in the Sent folder by its In-Reply-To. amanuense never closes the task: you decide, or let another tool or agent close tasks from these notes. Follow-up tasks get no such note. Each reply is noted once.
- **Changes to existing tasks:** only these notes. amanuense never completes, deletes or edits a task.
- **Your data stays as it is:** that lookup runs with garbage collection and hooks off, so it never changes your task data. New tasks are added with one `task import`, so your hooks (for example a git sync) run once, plus one `task annotate` per reply note.
- **Test and dry runs:** `--test` and `--dry-run` never add tasks; the digest shows "task (dry run)" instead. `amanuense check` reports whether Taskwarrior is on and how many tasks already point to emails.
- **`tag` command:** `tag` never adds tasks.

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

The logo was drawn by the same agent, by hand in SVG: a scribe shaped like a keyhole, writing in a closed room. The name is set in Fira Mono Medium (SIL Open Font License), turned into outlines so it shows the same everywhere. The files are in `docs/logo/`.

## License

BSD 3-Clause, see [LICENSE](LICENSE). The logo in `docs/logo/` is under the same licence; the font of its name, Fira Mono, is under the SIL Open Font License.

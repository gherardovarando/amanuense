"""Made-up emails for --test runs: no IMAP access, but the model is called."""
from datetime import datetime, timedelta, timezone


def _msg(uid, frm, subject, body, hours_ago, now, **extra):
    return {"uid": uid, "from": frm, "to": ["me@example.org"], "subject": subject, "body": body,
            "date": (now - timedelta(hours=hours_ago)).isoformat(), "messageId": f"<{uid}@test>",
            "flags": [], "customKeywords": [], "attachments": [], **extra}


class FakeMailbox:
    """Stands in for Mailbox: same methods, made-up messages, keyword changes kept in memory."""

    def __init__(self, cfg, write=False):
        now = datetime.now(timezone.utc)
        self.cfg = cfg
        self.written = []
        self.inbox = [
            _msg(1, "Servicio de Contabilidad <conta@example.org>", "Justificación gastos proyecto",
                 "Buenos días, necesitamos las facturas firmadas del viaje antes del 15 de octubre. Un saludo, María",
                 3, now),
            _msg(2, "Anna Rossi <anna@example.org>", "Re: draft of the paper",
                 "Hi, I pushed the new simulations. Could you check Section 4 before Friday? Anna", 5, now),
            _msg(3, "Journal Editorial Office <editor@example.org>", "Review invitation",
                 "Dear Dr Smith, would you be willing to review the manuscript 'X'? Please respond within 7 days.",
                 8, now),
            _msg(4, "Estudiante <est@example.org>", "Duda examen",
                 "Hola profe, ¿entra el tema 5 en el examen del martes?", 10, now),
            _msg(5, "University News <noreply@example.org>", "Open day next week", "...", 12, now),
            _msg(6, "Colleague <col@example.org>", "Letter of support",
                 "Hi, could you send me a short letter of support for our joint grant by 20 October? Thanks",
                 24 * 9, now),
        ]
        self.unread = {6}
        # The extra folders of the config exist and are empty, except the first one, which has a message
        # (e.g. a quarantine or oversized-mail folder).
        self.extra = {f["name"]: [] for f in cfg.get("folders") or []}
        if cfg.get("folders"):
            self.extra[cfg["folders"][0]["name"]] = [
                _msg(7, "Data Office <data@example.org>", "Dataset for the project (large attachment)",
                     "Hi, here is the full dataset we discussed, please download it and confirm it opens. Thanks",
                     24 * 4, now, attachments=["dataset.zip"])]
        me = "Me <me@example.org>"
        self.sent = [
            {"uid": 101, "from": me, "to": ["Anna Rossi <anna@example.org>"], "subject": "Re: draft of the paper",
             "date": now.isoformat(), "messageId": "<s1@test>", "body": "Thanks Anna, I will check Section 4 tomorrow."},
            {"uid": 102, "from": me, "to": ["Prof Lee <lee@example.org>"], "subject": "Figure 2 of the joint paper",
             "date": (now - timedelta(days=10)).isoformat(), "messageId": "<s2@test>",
             "body": "Dear Prof Lee, could you send me the final version of Figure 2? I need it to submit. Best"},
            {"uid": 103, "from": me, "to": ["Bob <bob@example.org>"], "subject": "Re: slides",
             "date": (now - timedelta(days=9)).isoformat(), "messageId": "<s3@test>",
             "body": "Thanks a lot Bob, all clear now. Have a nice weekend."},
            {"uid": 104, "from": me, "to": ["Carla <carla@example.org>"], "subject": "Meeting next week",
             "date": (now - timedelta(days=12)).isoformat(), "messageId": "<s4@test>",
             "body": "Hi Carla, can we meet on Tuesday at 10? Let me know."},
            {"uid": 105, "from": me, "to": ["Dan <dan@example.org>"], "subject": "Referee report",
             "date": (now - timedelta(days=20)).isoformat(), "messageId": "<s5@test>",
             "body": "Dear Dan, could you confirm you received my report?"},
            {"uid": 106, "from": me, "to": ["Dan <dan@example.org>"], "subject": "Re: Referee report",
             "date": (now - timedelta(days=2)).isoformat(), "messageId": "<s6@test>",
             "body": "Dear Dan, just checking whether my report arrived?"},
        ]
        # Carla answered; her reply was filed in an archive folder.
        self.archive = [_msg(201, "Carla <carla@example.org>", "Re: Meeting next week", "Tuesday is fine.",
                             24 * 11, now, inReplyTo="<s4@test>")]

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        pass

    def _check(self):
        if not getattr(self, "entered", False):  # the real Mailbox has no session outside `async with`
            raise RuntimeError("mailbox used outside `async with`")

    async def search(self, folder, since=None, before=None, seen=None, limit=50, **_):
        self._check()
        if folder in self.extra:
            msgs = self.extra[folder]
        elif folder == self.cfg["mail"]["sent"]:
            msgs = self.sent
        elif folder == self.cfg["mail"]["inbox"]:
            msgs = self.inbox
        else:
            raise RuntimeError(f"Mailbox doesn't exist: {folder}")
        if seen is False:
            msgs = [m for m in msgs if m.get("uid") in self.unread]
        return [{k: v for k, v in m.items() if k not in ("body", "attachments")} for m in msgs][:limit]

    async def search_all(self, since=None, limit=50, **_):
        self._check()
        folders = {self.cfg["mail"]["inbox"]: self.inbox, self.cfg["mail"]["sent"]: self.sent,
                   "Archive": self.archive, **self.extra}
        return [{**{k: v for k, v in m.items() if k not in ("body", "attachments")}, "folder": f}
                for f, ms in folders.items() for m in ms][:limit]

    async def get(self, folder, uid, body_chars):
        self._check()
        pool = self.sent if folder == self.cfg["mail"]["sent"] else self.extra.get(folder, self.inbox)
        m = next(m for m in pool if m["uid"] == uid)
        return {"body": m["body"][:body_chars], "attachments": m.get("attachments", [])}

    async def add_keyword(self, folder, uid, keyword):
        self.written.append(("add", uid, keyword))

    async def remove_keyword(self, folder, uid, keyword):
        self.written.append(("remove", uid, keyword))


def results(cfg):
    """Made-up triage results (no mailbox, no model), for previewing and checking templates."""
    now = datetime.now().astimezone()
    cats = cfg["categories"]
    pick = lambda *names: next((c for c in names if c in cats), cfg["fallback"])
    day = lambda d: f"{now + timedelta(days=d):%Y-%m-%d}"
    box = FakeMailbox({**cfg, "folders": cfg.get("folders") or [{"name": "Held"}]})
    held = next(iter(box.extra.values()))[0]
    verdicts = {  # uid: (category, needs_action, todo, deadline, priority, summary)
        1: (pick("admin"), True, "Send the signed trip invoices to accounting.", day(6), "normal",
            "Accounting needs the signed invoices of the trip."),
        2: (pick("research"), True, "Check Section 4 of the draft.", day(2), "high",
            "Anna pushed new simulations and asks to check Section 4."),
        3: (pick("reviewing"), True, "Accept or decline the review invitation.", day(7), "normal",
            "A journal invites you to review a manuscript."),
        4: (pick("teaching"), True, "Tell the student whether topic 5 is in the exam.", None, "high",
            "A student asks whether topic 5 is in Tuesday's exam."),
        6: (pick("research"), True, "Send a letter of support for the joint grant.", day(12), "normal",
            "A colleague asks for a letter of support."),
        7: (pick("research"), False, None, None, "normal", "The full project dataset, sent as a large attachment."),
        8: (pick("info"), False, None, None, "low", "The department newsletter."),
    }
    msgs = [dict(m) for m in box.inbox] + [{**held, "folder": next(iter(box.extra)), "folder_label": "held mail",
                                            "expires": (now + timedelta(days=4)).isoformat()}]
    msgs.append(_msg(8, "Department <dept@example.org>", "Newsletter", "...", 20, now))
    for m in msgs:
        m.pop("body", None)
        m["handled"] = False
        m["sensitive"] = m["uid"] == 4
        if m["uid"] == 5:
            m["skip"] = True
        elif m["uid"] in verdicts:
            c, need, todo, dl, prio, summary = verdicts[m["uid"]]
            m["ai"] = {"category": c, "needs_action": need, "todo": todo, "deadline": dl, "priority": prio,
                       "summary": summary, "event": None}
    next(m for m in msgs if m["uid"] == 2)["ai"]["event"] = {"title": "Call with Anna about Section 4", "date": day(3), "start": "10:00",
                              "end": "10:30", "place": "video call"}
    msgs.append({**_msg(9, "Bob <bob@example.org>", "Re: slides", "", 30, now), "handled": True,
                 "ai": {"category": pick("teaching"), "needs_action": True, "todo": "Answer Bob.", "deadline": None,
                        "priority": "normal", "summary": "Bob asks about the slides; you answered."}})
    sent = next(s for s in box.sent if s["uid"] == 102)
    waits = [{**{k: v for k, v in sent.items() if k != "body"}, "days": 10,
              "waiting": {"expects_reply": True, "waiting_for": "the final Figure 2 from Prof Lee",
                          "category": pick("research")}}]
    return msgs, waits

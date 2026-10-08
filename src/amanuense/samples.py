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
        self.sent = [{"to": ["Anna Rossi <anna@example.org>"], "date": now.isoformat(), "messageId": "<s1@test>"}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def search(self, folder, since=None, before=None, seen=None, limit=50, **_):
        msgs = self.sent if folder == self.cfg["mail"]["sent"] else self.inbox
        if seen is False:
            msgs = [m for m in msgs if m.get("uid") in self.unread]
        return [{k: v for k, v in m.items() if k not in ("body", "attachments")} for m in msgs][:limit]

    async def get(self, folder, uid, body_chars):
        m = next(m for m in self.inbox if m["uid"] == uid)
        return {"body": m["body"][:body_chars], "attachments": m["attachments"]}

    async def add_keyword(self, folder, uid, keyword):
        self.written.append(("add", uid, keyword))

    async def remove_keyword(self, folder, uid, keyword):
        self.written.append(("remove", uid, keyword))

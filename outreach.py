#!/usr/bin/env python3
"""
YC founder outreach - find, queue, send, and track emails from the command line.
State lives in Postgres (DATABASE_URL, default: local `outreach` db).

  find     --batch "Summer 2025"|2025 [--hiring-only]           find founders + emails into the db
  import   founders.csv                                         load a CSV from find_emails.py
  queue    [--batch B] [--confidence high,medium] [--company C] mark founders ready to send
  unqueue  [--batch B] | skip EMAIL...                          take founders out of the queue
  status                                                        totals: queued, sent today, left, replied...
  list     [--status S] [--batch B]                             show founders
  preview  [--n 3]                                              render the template for the next queued founders
  test                                                          send the next queued email to yourself
  send     [--limit N] [--dry-run]                              send queued emails, throttled, up to the daily limit
  replies                                                       check Gmail for replies and bounces
  ab                                                            reply rate per subject line

Template: edit template.txt ("Subject: ..." line(s), blank line, then the body).
A/B test: put 2-3 "Subject:" lines at the top - sends are split evenly and tracked per subject.
Placeholders: {first_name} {founder} {company} {one_liner} {batch} {title} {website}
"""
import argparse
import csv
import html
import imaplib
import os
import random
import re
import smtplib
import sys
import time
from datetime import datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

try:
    import psycopg
except ImportError:
    # re-run with the project's virtualenv so `python3 outreach.py` works without activating it
    _venv = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv")
    _venv_py = os.path.join(_venv, "bin", "python")
    if os.path.exists(_venv_py) and os.path.realpath(sys.prefix) != os.path.realpath(_venv):
        os.execv(_venv_py, [_venv_py, *sys.argv])
    sys.exit("psycopg not installed: python3 -m venv .venv && .venv/bin/pip install 'psycopg[binary]'")
from psycopg.rows import dict_row

import find_emails as fe

ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(ROOT, "template.txt")
PLACEHOLDERS = ["first_name", "founder", "company", "one_liner", "batch", "title", "website"]
CONTACT_COLS = ["batch", "company", "one_liner", "website", "domain", "founder", "first_name",
                "title", "linkedin", "twitter", "email", "confidence", "source"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  id          SERIAL PRIMARY KEY,
  batch       TEXT, company TEXT NOT NULL, one_liner TEXT, website TEXT, domain TEXT,
  founder     TEXT NOT NULL, first_name TEXT, title TEXT, linkedin TEXT, twitter TEXT,
  email       TEXT, confidence TEXT, source TEXT,
  status      TEXT NOT NULL DEFAULT 'new'
              CHECK (status IN ('new','queued','sent','replied','bounced','failed','skipped')),
  message_id  TEXT, error TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  queued_at   TIMESTAMPTZ, sent_at TIMESTAMPTZ, replied_at TIMESTAMPTZ,
  UNIQUE (company, founder)
);
CREATE INDEX IF NOT EXISTS contacts_status_idx ON contacts (status);
CREATE INDEX IF NOT EXISTS contacts_sent_at_idx ON contacts (sent_at);
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS subject_tpl TEXT;  -- which subject variant was sent
"""


# ---------------------------------------------------------------- config

def load_env():
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
CFG = {
    "database_url": os.environ.get("DATABASE_URL", "postgresql://postgres@/outreach"),
    "address": os.environ.get("GMAIL_ADDRESS", ""),
    "password": os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", ""),
    "from_name": os.environ.get("FROM_NAME", ""),
    "daily_limit": int(os.environ.get("DAILY_LIMIT", 40)),
    "min_delay": float(os.environ.get("MIN_DELAY", 60)),
    "max_delay": float(os.environ.get("MAX_DELAY", 180)),
}


def require_gmail():
    if not (CFG["address"] and CFG["password"]):
        sys.exit("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env (see .env.example)")


def db():
    con = psycopg.connect(CFG["database_url"], row_factory=dict_row, autocommit=True)
    con.execute(SCHEMA)
    return con


# ---------------------------------------------------------------- template

def load_template():
    """Returns ([subject, ...], body). Several 'Subject:' lines = A/B/C test variants."""
    if not os.path.exists(TEMPLATE_PATH):
        sys.exit("Missing template.txt - create it with: cp template.example.txt template.txt")
    lines = open(TEMPLATE_PATH).read().strip().split("\n")
    subjects = []
    while lines and re.match(r"\s*Subject:", lines[0], re.I):
        subjects.append(lines.pop(0).split(":", 1)[1].strip())
    if not subjects or not lines or lines[0].strip():
        sys.exit("template.txt must start with one or more 'Subject: ...' lines, then a blank line, then the body")
    return subjects, "\n".join(lines).strip() + "\n"


def variant_label(subjects, subject_tpl):
    return chr(ord("A") + subjects.index(subject_tpl)) if subject_tpl in subjects else "-"


def pick_subject(con, subjects):
    """Balance variants: choose the subject sent least so far (random among ties)."""
    counts = {r["subject_tpl"]: r["n"] for r in con.execute(
        "SELECT subject_tpl, count(*) n FROM contacts WHERE subject_tpl = ANY(%s) GROUP BY 1", (subjects,))}
    fewest = min(counts.get(s, 0) for s in subjects)
    return random.choice([s for s in subjects if counts.get(s, 0) == fewest])


def render(tpl, c):
    c = {**c, "one_liner": (c.get("one_liner") or "").strip().rstrip(".")}

    def sub(m):
        key = m.group(1)
        return str(c.get(key) or "") if key in PLACEHOLDERS else m.group(0)
    return re.sub(r"\{(\w+)\}", sub, tpl)


LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def body_plain(text):
    """[text](url) -> text (url)"""
    return LINK_RE.sub(r"\1 (\2)", text)


def body_html(text):
    """[text](url) -> <a>, paragraphs/line breaks kept; no styling so it looks hand-written."""
    esc = html.escape(text, quote=True)
    esc = LINK_RE.sub(r'<a href="\2">\1</a>', esc)
    paras = [p.replace("\n", "<br>") for p in esc.strip().split("\n\n")]
    return '<div dir="ltr">' + "".join(f"<div>{p}</div><div><br></div>" for p in paras[:-1]) + \
           f"<div>{paras[-1]}</div></div>"


def build_message(c, subject, body, to=None, subject_prefix=""):
    msg = EmailMessage()
    msg["From"] = f'{CFG["from_name"]} <{CFG["address"]}>' if CFG["from_name"] else CFG["address"]
    msg["To"] = to or c["email"]
    msg["Subject"] = subject_prefix + render(subject, c)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=CFG["address"].split("@")[-1] or "gmail.com")
    text = render(body, c)
    msg.set_content(body_plain(text))
    if LINK_RE.search(text):
        msg.add_alternative(body_html(text), subtype="html")
    return msg


def smtp_connect():
    s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30, context=fe._SSL)
    s.login(CFG["address"], CFG["password"])
    return s


# ---------------------------------------------------------------- helpers

CONF_RANK = "array_position(ARRAY['none','low','medium','high'], coalesce(NULLIF({}, ''), 'none'))"


def upsert(con, rows):
    """Insert new founders. Existing ones that haven't been queued/sent get their email
    refreshed when the new result is at least as confident. Returns (added, updated)."""
    added = updated = 0
    cols = ",".join(CONTACT_COLS)
    marks = ",".join(["%s"] * len(CONTACT_COLS))
    sql = f"""
        INSERT INTO contacts ({cols}) VALUES ({marks})
        ON CONFLICT (company, founder) DO UPDATE SET
            email = EXCLUDED.email, confidence = EXCLUDED.confidence, source = EXCLUDED.source,
            domain = EXCLUDED.domain, one_liner = EXCLUDED.one_liner
        WHERE contacts.status = 'new'
          AND {CONF_RANK.format('EXCLUDED.confidence')} >= {CONF_RANK.format('contacts.confidence')}
          AND (contacts.email IS DISTINCT FROM EXCLUDED.email
               OR contacts.confidence IS DISTINCT FROM EXCLUDED.confidence)
        RETURNING (xmax = 0) AS inserted"""
    for r in rows:
        vals = {c: str(r.get(c) or "").strip() for c in CONTACT_COLS}
        if not vals["founder"] or not vals["company"]:
            continue
        vals["email"] = vals["email"].lower()
        res = con.execute(sql, [vals[c] for c in CONTACT_COLS]).fetchone()
        if res:
            added += res["inserted"]
            updated += not res["inserted"]
    return added, updated


def sent_today(con):
    return con.execute("SELECT count(*) n FROM contacts WHERE sent_at >= date_trunc('day', now())").fetchone()["n"]


def filters(args, base):
    where, params = [base], []
    if getattr(args, "batch", None):
        where.append("batch = %s")
        params.append(args.batch)
    if getattr(args, "company", None):
        where.append("company ILIKE %s")
        params.append(args.company)
    if getattr(args, "confidence", None):
        where.append("confidence = ANY(%s)")
        params.append(args.confidence.split(","))
    return " AND ".join(where), params


# ---------------------------------------------------------------- commands

def cmd_find(args):
    con = db()
    # a bare year expands to that year's batches (missing seasons just return nothing)
    batches = [f"{s} {args.batch}" for s in ("Winter", "Spring", "Summer", "Fall")] \
        if args.batch.isdigit() else [args.batch]
    companies = fe.fetch_companies(batches, args.hiring_only)
    if args.limit:
        companies = companies[:args.limit]
    print(f"{len(companies)} companies in {args.batch}{' (hiring)' if args.hiring_only else ''}")
    added = updated = 0
    for i, c in enumerate(companies, 1):
        try:
            rows = fe.process_company(c, args.github, not args.no_smtp)
            a, u = upsert(con, rows)
            added, updated = added + a, updated + u
            summary = ", ".join(f"{r['founder']}={r['confidence']}" for r in rows)
            print(f"[{i}/{len(companies)}] {c['name']}: {summary}")
        except Exception as e:
            print(f"[{i}/{len(companies)}] {c['name']}: ERROR {e}")
    next_cmd = "queue" if len(batches) > 1 else f'queue --batch "{args.batch}"'
    print(f"\nAdded {added} new founders, updated {updated} emails. Next: python3 outreach.py {next_cmd}")


def cmd_import(args):
    con = db()
    with open(args.csv) as f:
        rows = list(csv.DictReader(f))
    added, updated = upsert(con, rows)
    print(f"Added {added} new founders, updated {updated} emails ({len(rows)} rows in {args.csv})")


def cmd_queue(args):
    con = db()
    where, params = filters(args, "status = 'new' AND email <> ''")
    n = con.execute(f"UPDATE contacts SET status='queued', queued_at=now() WHERE {where}", params).rowcount
    print(f"Queued {n} founders")


def cmd_unqueue(args):
    con = db()
    where, params = filters(args, "status = 'queued'")
    n = con.execute(f"UPDATE contacts SET status='new', queued_at=NULL WHERE {where}", params).rowcount
    print(f"Unqueued {n} founders")


def cmd_skip(args):
    con = db()
    emails = [e.lower() for e in args.emails]
    n = con.execute("UPDATE contacts SET status='skipped' WHERE email = ANY(%s) AND status IN ('new','queued','failed')",
                    (emails,)).rowcount
    print(f"Skipped {n}")


def cmd_status(args):
    con = db()
    counts = {r["status"]: r["n"] for r in con.execute("SELECT status, count(*) n FROM contacts GROUP BY status")}
    total = sum(counts.values())
    today = sent_today(con)
    contacted = sum(counts.get(k, 0) for k in ("sent", "replied", "bounced"))
    replied = counts.get("replied", 0)
    no_email = con.execute("SELECT count(*) n FROM contacts WHERE coalesce(email,'') = ''").fetchone()["n"]

    print(f"Total founders      {total:5d}   ({no_email} without email)")
    print(f"New (not queued)    {counts.get('new', 0):5d}")
    print(f"Queued              {counts.get('queued', 0):5d}")
    print(f"Sent today          {today:5d} / {CFG['daily_limit']}   ({max(0, CFG['daily_limit'] - today)} left today)")
    print(f"Contacted total     {contacted:5d}")
    print(f"Replied             {replied:5d}   ({100 * replied / contacted:.0f}% reply rate)" if contacted
          else f"Replied             {replied:5d}")
    print(f"Bounced             {counts.get('bounced', 0):5d}")
    print(f"Failed              {counts.get('failed', 0):5d}")
    print(f"Skipped             {counts.get('skipped', 0):5d}")

    rows = con.execute("""
        SELECT batch, count(*) total,
               count(*) FILTER (WHERE status='queued') queued,
               count(*) FILTER (WHERE status IN ('sent','replied','bounced')) contacted,
               count(*) FILTER (WHERE status='replied') replied
        FROM contacts GROUP BY batch ORDER BY batch""").fetchall()
    if len(rows) > 1:
        print(f"\n{'batch':16s} {'total':>6s} {'queued':>7s} {'sent':>6s} {'replied':>8s}")
        for r in rows:
            print(f"{r['batch'] or '-':16s} {r['total']:6d} {r['queued']:7d} {r['contacted']:6d} {r['replied']:8d}")

    cmd_ab(args, con)


def cmd_ab(args, con=None):
    con = con or db()
    rows = con.execute("""
        SELECT subject_tpl,
               count(*) FILTER (WHERE status IN ('sent','replied')) delivered,
               count(*) FILTER (WHERE status='replied') replied,
               count(*) FILTER (WHERE status='bounced') bounced
        FROM contacts WHERE subject_tpl IS NOT NULL GROUP BY subject_tpl
        ORDER BY 2 DESC""").fetchall()
    if not rows:
        return
    subjects, _ = load_template()
    print(f"\nSubject A/B test {'':40s} {'sent':>5s} {'replied':>8s} {'rate':>6s}")
    for r in rows:
        label = variant_label(subjects, r["subject_tpl"])
        rate = f"{100 * r['replied'] / r['delivered']:.1f}%" if r["delivered"] else "-"
        print(f"  {label if label != '-' else 'old'}  {r['subject_tpl'][:52]:52s} {r['delivered']:5d} {r['replied']:8d} {rate:>6s}")
    if min(r["delivered"] for r in rows) < 50:
        print("  (under ~50 sends per subject the difference is mostly noise - keep going before picking a winner)")


def cmd_list(args):
    con = db()
    where, params = filters(args, "TRUE")
    if args.status:
        where += " AND status = %s"
        params.append(args.status)
    rows = con.execute(f"""SELECT status, confidence, company, founder, email, sent_at FROM contacts
                           WHERE {where} ORDER BY company, founder""", params).fetchall()
    for r in rows:
        sent = r["sent_at"].strftime("%m-%d %H:%M") if r["sent_at"] else ""
        print(f"{r['status']:8s} {r['confidence'] or '':6s} {r['company'][:22]:22s} {r['founder'][:22]:22s} "
              f"{r['email'] or '-':34s} {sent}")
    print(f"\n{len(rows)} rows")


def cmd_preview(args):
    con = db()
    rows = con.execute("SELECT * FROM contacts WHERE status='queued' ORDER BY queued_at, id LIMIT %s", (args.n,)).fetchall()
    if not rows:
        rows = con.execute("SELECT * FROM contacts WHERE email <> '' ORDER BY id LIMIT %s", (args.n,)).fetchall()
        if rows:
            print("(queue is empty - previewing with other founders)\n")
    subjects, body = load_template()
    for c in rows:
        print("=" * 70)
        print(f"To:      {c['founder']} <{c['email']}>   [{c['confidence']}]")
        for s in subjects:
            tag = f"Subject {variant_label(subjects, s)}" if len(subjects) > 1 else "Subject"
            print(f"{tag + ':':10s} {render(s, c)}")
        print()
        print(body_plain(render(body, c)))
    unknown = set(re.findall(r"\{(\w+)\}", "".join(subjects) + body)) - set(PLACEHOLDERS)
    if unknown:
        print(f"WARNING: unknown placeholders in template.txt: {', '.join(sorted(unknown))}")


def cmd_test(args):
    require_gmail()
    con = db()
    c = con.execute("SELECT * FROM contacts WHERE status='queued' ORDER BY queued_at, id LIMIT 1").fetchone() \
        or con.execute("SELECT * FROM contacts WHERE email <> '' ORDER BY id LIMIT 1").fetchone()
    if not c:
        sys.exit("No founders in the db yet")
    subjects, body = load_template()
    with smtp_connect() as s:
        for subj in subjects:
            label = f"{variant_label(subjects, subj)} " if len(subjects) > 1 else ""
            s.send_message(build_message(c, subj, body, to=CFG["address"],
                                         subject_prefix=f"[TEST {label}-> {c['email']}] "))
    print(f"Sent {len(subjects)} test email(s) to {CFG['address']} (rendered for {c['founder']}, {c['company']})")


def cmd_send(args):
    con = db()
    limit = CFG["daily_limit"] - sent_today(con)
    if args.limit:
        limit = min(limit, args.limit)
    if limit <= 0:
        sys.exit(f"Daily limit reached ({CFG['daily_limit']}). Run again tomorrow or raise DAILY_LIMIT in .env")
    queue = con.execute("SELECT * FROM contacts WHERE status='queued' ORDER BY queued_at, id LIMIT %s", (limit,)).fetchall()
    if not queue:
        sys.exit("Queue is empty. Run: python3 outreach.py queue")

    subjects, body = load_template()
    if args.dry_run:
        print(f"DRY RUN - would send {len(queue)} emails:")
        for c in queue:
            print(f"  {c['founder']:24s} {c['email']:34s} {c['company']}")
        return

    require_gmail()
    ab = f", A/B testing {len(subjects)} subjects" if len(subjects) > 1 else ""
    print(f"Sending {len(queue)} emails, {CFG['min_delay']:.0f}-{CFG['max_delay']:.0f}s apart{ab}. Ctrl+C to stop safely.\n")
    smtp = smtp_connect()
    sent = 0
    try:
        for i, c in enumerate(queue, 1):
            if i > 1:
                wait = random.uniform(CFG["min_delay"], CFG["max_delay"])
                print(f"   waiting {wait:.0f}s...", end="\r", flush=True)
                time.sleep(wait)
            subj = pick_subject(con, subjects)
            msg = build_message(c, subj, body)
            try:
                try:
                    smtp.send_message(msg)
                except smtplib.SMTPServerDisconnected:
                    smtp = smtp_connect()
                    smtp.send_message(msg)
            except smtplib.SMTPRecipientsRefused as e:
                con.execute("UPDATE contacts SET status='failed', error=%s WHERE id=%s", (str(e)[:300], c["id"]))
                print(f"[{i}/{len(queue)}] FAILED {c['email']}: {e}")
                continue
            except (smtplib.SMTPDataError, smtplib.SMTPSenderRefused) as e:
                print(f"\nGmail refused the message, stopping: {e}")
                break
            con.execute("UPDATE contacts SET status='sent', sent_at=now(), message_id=%s, subject_tpl=%s, error=NULL "
                        "WHERE id=%s", (msg["Message-ID"], subj, c["id"]))
            sent += 1
            label = f" [{variant_label(subjects, subj)}]" if len(subjects) > 1 else ""
            print(f"[{i}/{len(queue)}] sent{label} -> {c['founder']} <{c['email']}> ({c['company']})")
    except KeyboardInterrupt:
        print("\nStopped. Everything sent so far is recorded.")
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    print(f"\nSent {sent}. Sent today: {sent_today(con)}/{CFG['daily_limit']}")


def cmd_replies(args):
    require_gmail()
    con = db()
    sent = con.execute("SELECT id, email, message_id, sent_at FROM contacts WHERE status='sent'").fetchall()
    if not sent:
        print("No sent emails awaiting a reply")
        return
    by_email = {r["email"].lower(): r["id"] for r in sent}
    by_mid = {r["message_id"]: r["id"] for r in sent if r["message_id"]}
    since = min(r["sent_at"] for r in sent).strftime("%d-%b-%Y")
    replied, bounced = set(), set()

    M = imaplib.IMAP4_SSL("imap.gmail.com", ssl_context=fe._SSL)
    try:
        M.login(CFG["address"], CFG["password"])
        M.select("INBOX", readonly=True)
        _, data = M.search(None, "SINCE", since)
        ids = data[0].split()
        for i in range(0, len(ids), 200):
            chunk = b",".join(ids[i:i + 200]).decode()
            _, parts = M.fetch(chunk, "(BODY.PEEK[HEADER.FIELDS (FROM IN-REPLY-TO REFERENCES X-FAILED-RECIPIENTS "
                                      "AUTO-SUBMITTED X-AUTOREPLY X-AUTORESPOND)])")
            for p in parts:
                if not isinstance(p, tuple):
                    continue
                h = message_from_bytes(p[1])
                failed = h.get("X-Failed-Recipients")
                if failed:
                    for a in failed.split(","):
                        if a.strip().lower() in by_email:
                            bounced.add(by_email[a.strip().lower()])
                    continue
                frm = parseaddr(h.get("From", ""))[1].lower()
                if frm == CFG["address"].lower():
                    continue
                auto = (h.get("Auto-Submitted") or "no").strip().lower() != "no"
                if auto or h.get("X-Autoreply") or h.get("X-Autorespond"):
                    continue  # out-of-office etc. - not a real reply
                if frm in by_email:
                    replied.add(by_email[frm])
                    continue
                refs = f'{h.get("In-Reply-To", "")} {h.get("References", "")}'
                for mid, cid in by_mid.items():
                    if mid in refs:
                        replied.add(cid)
    finally:
        try:
            M.logout()
        except Exception:
            pass

    replied -= bounced
    if replied:
        con.execute("UPDATE contacts SET status='replied', replied_at=now() WHERE id = ANY(%s)", (list(replied),))
    if bounced:
        con.execute("UPDATE contacts SET status='bounced' WHERE id = ANY(%s)", (list(bounced),))
    print(f"{len(replied)} new replies, {len(bounced)} bounces")
    for r in con.execute("SELECT founder, company, email FROM contacts WHERE id = ANY(%s)", (list(replied | bounced),)):
        tag = "bounced" if r["email"] in {e for e, i in by_email.items() if i in bounced} else "replied"
        print(f"  {tag:8s} {r['founder']} ({r['company']}) <{r['email']}>")


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("find", help="find founders + emails for a YC batch")
    p.add_argument("--batch", required=True, help='"Summer 2025", or a year like 2025 for all its batches')
    p.add_argument("--hiring-only", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--github", action="store_true")
    p.add_argument("--no-smtp", action="store_true")
    p.set_defaults(fn=cmd_find)

    p = sub.add_parser("import", help="import a CSV from find_emails.py")
    p.add_argument("csv")
    p.set_defaults(fn=cmd_import)

    for name, fn, hlp in (("queue", cmd_queue, "queue founders for sending"),
                          ("unqueue", cmd_unqueue, "remove founders from the queue")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--batch")
        p.add_argument("--company")
        p.add_argument("--confidence", default="high,medium" if name == "queue" else None)
        p.set_defaults(fn=fn)

    p = sub.add_parser("skip", help="never email these addresses")
    p.add_argument("emails", nargs="+")
    p.set_defaults(fn=cmd_skip)

    sub.add_parser("status", help="show totals").set_defaults(fn=cmd_status)
    sub.add_parser("ab", help="reply rate per subject line").set_defaults(fn=cmd_ab)

    p = sub.add_parser("list", help="list founders")
    p.add_argument("--status")
    p.add_argument("--batch")
    p.add_argument("--confidence")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("preview", help="render the template for queued founders")
    p.add_argument("--n", type=int, default=3)
    p.set_defaults(fn=cmd_preview)

    sub.add_parser("test", help="send one rendered email to yourself").set_defaults(fn=cmd_test)

    p = sub.add_parser("send", help="send queued emails")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_send)

    sub.add_parser("replies", help="check Gmail for replies and bounces").set_defaults(fn=cmd_replies)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

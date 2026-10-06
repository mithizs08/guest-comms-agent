"""Read unread guest emails from Gmail, run them through the agent, and save
each reply as a Gmail draft for a human to review.

Nothing is ever sent: this module only uses IMAP (reading, labelling and
saving drafts) and contains no SMTP or send code at all.

Usage:
    python src/gmail_intake.py             # process unread emails
    python src/gmail_intake.py --dry-run   # process and print, change nothing in Gmail
    python src/gmail_intake.py --local     # use properties.json instead of HubSpot

Needs GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env. Optional
ALLOWED_SENDERS (comma-separated addresses) limits which senders are
processed; if it's empty, every unread email is.
"""

import email
import email.policy
import imaplib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr

import anthropic
from dotenv import load_dotenv

from agent import ROOT, get_property_source, process_email
from hubspot_client import PropertySourceError

IMAP_HOST = "imap.gmail.com"
LOG_PATH = ROOT / "output" / "intake_log.jsonl"
PROCESSED_LABEL = "agent-processed"
CATEGORY_LABELS = {
    c: f"triage/{c}" for c in ["routine", "human_review", "urgent", "needs_owner_clarification"]
}
# A cap per run, so an unexpected flood of emails can't run up API costs.
MAX_PER_RUN = 20

# Gmail search, not IMAP flags: a processed email stays unread so it still
# stands out to the human reviewing drafts, so "unread" alone can't tell us
# what's been handled. The label can.
UNPROCESSED_QUERY = f"in:inbox is:unread -label:{PROCESSED_LABEL}"


def search_query(allowed_senders: str) -> str:
    # The inbox address is guessable, so anyone could email it and spend
    # Claude credit. Filtering in the Gmail search means other senders' emails
    # are never fetched, so they can't use up the per-run cap either.
    senders = [s.strip() for s in allowed_senders.split(",") if s.strip()]
    if not senders:
        return UNPROCESSED_QUERY
    if any(not re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", s) for s in senders):
        raise ValueError("ALLOWED_SENDERS must be a comma-separated list of email addresses")
    return f"{UNPROCESSED_QUERY} {{{' '.join(f'from:{s}' for s in senders)}}}"


class StaticPropertySource:
    """Properties fetched once per run. One HubSpot outage then stops the run
    cleanly up front, instead of failing on every email."""

    def __init__(self, properties: list[dict]):
        self._properties = properties

    def all_properties(self) -> list[dict]:
        return self._properties


def log(entry: dict) -> None:
    # Only metadata is logged, not the guest's address or message, so the log
    # isn't a second copy of guest personal data.
    LOG_PATH.parent.mkdir(exist_ok=True)
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def body_text(msg: email.message.Message) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    return text.strip()


def to_agent_format(msg: email.message.Message) -> str:
    # The agent expects the same simple From/Subject/body layout as the
    # sample emails, so real emails go through exactly the code we evaluated.
    return f"From: {msg['From']}\nSubject: {msg['Subject']}\n\n{body_text(msg)}"


def drafts_folder(imap: imaplib.IMAP4_SSL) -> str:
    # Gmail's folder names change with the account language ("[Gmail]/Drafts",
    # "[Gmail]/Brouillons"...), but the \Drafts flag doesn't.
    _, folders = imap.list()
    for line in folders:
        line = line.decode()
        if "\\Drafts" in line:
            return line.rsplit(' "/" ', 1)[1].strip()
    raise RuntimeError("Couldn't find the Gmail Drafts folder")


def save_draft_reply(imap, folder: str, original: email.message.Message, reply_text: str, account: str) -> None:
    subject = original["Subject"] or ""
    message_id = original["Message-ID"] or ""
    draft = EmailMessage()
    draft["From"] = account
    draft["To"] = original["Reply-To"] or original["From"]
    draft["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    # These headers (plus the matching subject) are what make Gmail put the
    # draft in the guest's thread rather than a new conversation.
    if message_id:
        draft["In-Reply-To"] = message_id
        draft["References"] = f"{original['References'] or ''} {message_id}".strip()
    draft.set_content(reply_text)
    status, _ = imap.append(folder, "(\\Draft)", imaplib.Time2Internaldate(time.time()), draft.as_bytes())
    if status != "OK":
        raise RuntimeError(f"Saving draft failed: {status}")


def add_labels(imap, uid: bytes, labels: list[str]) -> None:
    quoted = " ".join(f'"{label}"' for label in labels)
    status, _ = imap.uid("STORE", uid, "+X-GM-LABELS", f"({quoted})")
    if status != "OK":
        raise RuntimeError(f"Labelling failed: {status}")


def ensure_labels(imap) -> None:
    for label in [PROCESSED_LABEL, *CATEGORY_LABELS.values()]:
        imap.create(f'"{label}"')  # returns NO if it already exists, which is fine


def run(dry_run: bool, local: bool) -> None:
    load_dotenv(ROOT / ".env")
    account = os.environ.get("GMAIL_ADDRESS", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    mode = "dry_run" if dry_run else "live"

    try:
        properties = get_property_source(local).all_properties()
    except PropertySourceError as e:
        # Unread emails stay unprocessed, so the next run picks them up.
        log({"status": "run_skipped", "mode": mode, "error": f"Property data unavailable: {e}"})
        sys.exit(f"Property data unavailable, nothing processed: {e}")
    source = StaticPropertySource(properties)
    client = anthropic.Anthropic()

    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST)
        imap.login(account, password)
    except (imaplib.IMAP4.error, OSError) as e:
        log({"status": "run_skipped", "mode": mode, "error": f"Gmail login/connection failed: {e}"})
        sys.exit(f"Gmail login/connection failed: {e}")

    try:
        imap.select("INBOX")
        if not dry_run:
            ensure_labels(imap)
            folder = drafts_folder(imap)
        query = search_query(os.environ.get("ALLOWED_SENDERS", ""))
        _, data = imap.uid("SEARCH", "X-GM-RAW", f'"{query}"')
        uids = data[0].split()[:MAX_PER_RUN]
        print(f"{len(uids)} unprocessed email(s){' (dry run)' if dry_run else ''}")

        for uid in uids:
            message_id = None
            try:
                # BODY.PEEK reads without marking the email as read.
                _, msg_data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                msg = email.message_from_bytes(msg_data[0][1], policy=email.policy.default)
                message_id = msg["Message-ID"]

                # Never reply to ourselves, or one run's drafts could feed the next.
                if parseaddr(msg["From"])[1].lower() == account.lower():
                    continue

                result = process_email(client, to_agent_format(msg), source)
                draft_created = False
                if not dry_run:
                    # Draft first, labels last: if drafting fails the email
                    # isn't marked processed, so it's retried next run.
                    if result["draft_reply"]:
                        save_draft_reply(imap, folder, msg, result["draft_reply"], account)
                        draft_created = True
                    add_labels(imap, uid, [CATEGORY_LABELS[result["category"]], PROCESSED_LABEL])

                log({
                    "status": "processed", "mode": mode, "message_id": message_id,
                    "platform": result["platform"], "property_code": result["property_code"],
                    "category": result["category"], "draft_created": draft_created,
                })
                if dry_run:
                    print(f"\n--- {msg['Subject']}")
                    print(json.dumps(result, indent=2, ensure_ascii=False))
                else:
                    # Live runs happen in GitHub Actions, whose logs are public
                    # on a public repo, so no guest content is printed.
                    print(f"Processed: {result['category']} / {result['property_code']} / draft={draft_created}")
            except Exception as e:
                # One bad email (or a Claude/network hiccup) shouldn't stop the
                # rest. It isn't labelled, so the next run tries it again.
                log({"status": "error", "mode": mode, "message_id": message_id, "error": f"{type(e).__name__}: {e}"})
                print(f"\n--- Skipped an email: {type(e).__name__}: {e}")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv[1:], local="--local" in sys.argv[1:])

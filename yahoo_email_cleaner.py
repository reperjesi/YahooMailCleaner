#!/usr/bin/env python3
"""
Yahoo Mail Bulk Email Cleaner
==============================
Deletes emails matching filter rules or from high-volume senders.

SETUP:
  1. Enable "Allow apps that use less secure sign in" in Yahoo Account Security,
     OR generate an App Password at https://login.yahoo.com/account/security
     (recommended if you have 2FA enabled).
  2. Install dependencies:  pip install imapclient
  3. Set your Yahoo email via the YAHOO_EMAIL environment variable if desired,
     otherwise enter it when prompted.

USAGE:
  python yahoo_email_cleaner.py [OPTIONS]

  --rules "from:*realtor* AND subject:*realty*"
      Delete emails matching this rule string. Supports:
        from:*pattern*       sender contains pattern
        subject:*pattern*    subject contains pattern
        to:*pattern*         recipient contains pattern
      Combine with AND / OR (uppercase). Use * as wildcard.

  --high-volume 300
      Delete ALL emails from any sender who has sent you >= N emails.
      Scans your entire inbox first, then deletes by sender.

  --mailbox INBOX
      Mailbox/folder to operate on (default: INBOX).
      Use "ALL Mail" for Yahoo's all-mail folder.

  --dry-run
      Print what would be deleted without actually deleting anything.

  --batch-size 100
      Number of emails to delete per IMAP command (default: 100).

EXAMPLES:
  # Dry-run: preview matching emails
  python yahoo_email_cleaner.py --rules "from:*realtor* AND subject:*realty*" --dry-run

  # Delete emails matching rule
  python yahoo_email_cleaner.py --rules "from:*realtor* OR from:*zillow* OR from:*trulia*"

  # Delete from senders with 300+ emails
  python yahoo_email_cleaner.py --high-volume 300

  # Combine both modes
  python yahoo_email_cleaner.py --rules "from:*newsletter*" --high-volume 300

  # Target a specific folder
  python yahoo_email_cleaner.py --high-volume 300 --mailbox "Bulk Mail"
"""

import argparse
import os
import re
import sys
from collections import Counter
from getpass import getpass

try:
    from imapclient import IMAPClient
except ImportError:
    print("ERROR: imapclient not installed. Run:  pip install imapclient")
    sys.exit(1)

# ── Credentials ──────────────────────────────────────────────────────────────
# The password is prompted at runtime so it is never stored in the script.
YAHOO_EMAIL = "reperjesi@yahoo.com"  # os.environ.get("YAHOO_EMAIL", "")
YAHOO_PASSWORD = "xrzywmaeeauacszj"
IMAP_HOST   = "imap.mail.yahoo.com"
IMAP_PORT   = 993
# ─────────────────────────────────────────────────────────────────────────────


# ── Rule parser ───────────────────────────────────────────────────────────────

FIELD_MAP = {
    "from":    "FROM",
    "subject": "SUBJECT",
    "to":      "TO",
    "cc":      "CC",
    "body":    "BODY",
}

def _wildcard_to_value(pattern: str) -> str:
    """Strip leading/trailing * wildcards — IMAP SEARCH does substring match."""
    return pattern.strip("*").strip()

def _parse_term(term: str) -> list:
    """
    Parse a single term like 'from:*realtor*' into an IMAP search criterion list.
    Returns e.g. ['FROM', 'realtor']
    """
    term = term.strip()
    m = re.match(r'^(\w+):(.+)$', term, re.IGNORECASE)
    if not m:
        raise ValueError(f"Unrecognised filter term: {term!r}  "
                         f"(expected field:value, e.g. from:*realtor*)")
    field, value = m.group(1).lower(), _wildcard_to_value(m.group(2))
    if field not in FIELD_MAP:
        raise ValueError(f"Unknown field {field!r}. Supported: {list(FIELD_MAP)}")
    return [FIELD_MAP[field], value]

def parse_rules(rule_string: str) -> list:
    """
    Parse a rule string into an IMAP search criteria list.

    Supports AND / OR (case-sensitive keywords) and multiple terms.
    Examples:
      "from:*realtor*"                        → ['FROM', 'realtor']
      "from:*realtor* AND subject:*realty*"   → ['FROM','realtor','SUBJECT','realty']
      "from:*zillow* OR from:*trulia*"        → ['OR','FROM','zillow','FROM','trulia']
    """
    # Split on AND first, then handle OR within each chunk
    rule_string = rule_string.strip()

    # Tokenise: split by AND / OR boundaries keeping the operator
    tokens = re.split(r'\s+(AND|OR)\s+', rule_string)
    # tokens alternates: term, operator, term, operator, term ...

    terms   = [tokens[i] for i in range(0, len(tokens), 2)]
    ops     = [tokens[i] for i in range(1, len(tokens), 2)]

    parsed_terms = [_parse_term(t) for t in terms]

    if not ops:
        # Single term
        return parsed_terms[0]

    # Fold operators left-to-right
    criteria = parsed_terms[0]
    for op, next_term in zip(ops, parsed_terms[1:]):
        if op == "AND":
            # IMAP implicit AND: just concatenate
            criteria = criteria + next_term
        elif op == "OR":
            # IMAP OR takes exactly two operands: OR crit1a crit1b crit2a crit2b
            criteria = ["OR"] + criteria + next_term
        else:
            raise ValueError(f"Unknown operator: {op!r}")

    return criteria


# ── Core logic ────────────────────────────────────────────────────────────────

def chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def delete_messages(client, uids: list, dry_run: bool, batch_size: int):
    """Flag messages as deleted and expunge."""
    if not uids:
        return 0
    if dry_run:
        print(f"  [DRY RUN] Would delete {len(uids)} message(s).")
        return len(uids)

    deleted = 0
    for batch in chunk(uids, batch_size):
        client.add_flags(batch, [b'\\Deleted'])
        deleted += len(batch)
        print(f"  Marked {deleted}/{len(uids)} messages for deletion...", end="\r")

    client.expunge()
    print(f"  ✓ Deleted {deleted} message(s).           ")
    return deleted


def run_rule_filter(client, rules: str, dry_run: bool, batch_size: int):
    """Delete emails matching the parsed rule string."""
    print(f"\n── Rule filter: {rules!r} ──")
    try:
        criteria = parse_rules(rules)
    except ValueError as e:
        print(f"ERROR parsing rules: {e}")
        return

    print(f"  IMAP criteria: {criteria}")
    uids = client.search(criteria)
    print(f"  Found {len(uids)} matching message(s).")

    if uids:
        # Preview a few senders
        if len(uids) <= 5 or dry_run:
            sample = client.fetch(uids[:10], ['ENVELOPE'])
            for uid, data in sample.items():
                env = data.get(b'ENVELOPE')
                if env:
                    sender = (env.from_[0].mailbox or b'?').decode(errors='replace') + \
                             '@' + (env.from_[0].host or b'?').decode(errors='replace')
                    subj   = (env.subject or b'(no subject)').decode(errors='replace')
                    print(f"    [{uid}] {sender}  |  {subj[:60]}")

        delete_messages(client, list(uids), dry_run, batch_size)


def run_high_volume(client, threshold: int, dry_run: bool, batch_size: int):
    """Delete all emails from senders with >= threshold messages."""
    print(f"\n── High-volume sender cleanup (threshold: {threshold}) ──")
    print("  Fetching all message envelopes (this may take a while)…")

    all_uids = client.search(["ALL"])
    print(f"  Total messages in mailbox: {len(all_uids)}")

    if not all_uids:
        print("  Mailbox is empty.")
        return

    # Fetch envelopes in batches to avoid timeouts
    sender_map: dict[str, list] = {}   # sender_address → [uid, ...]
    fetched = 0
    for batch in chunk(all_uids, 500):
        data = client.fetch(batch, ['ENVELOPE'])
        for uid, msg_data in data.items():
            env = msg_data.get(b'ENVELOPE')
            if env and env.from_:
                mailbox = (env.from_[0].mailbox or b'').decode(errors='replace').lower()
                host    = (env.from_[0].host    or b'').decode(errors='replace').lower()
                sender  = f"{mailbox}@{host}"
                sender_map.setdefault(sender, []).append(uid)
        fetched += len(batch)
        print(f"  Scanned {fetched}/{len(all_uids)} messages…", end="\r")

    print()

    # Find senders over threshold
    heavy_senders = {s: uids for s, uids in sender_map.items()
                     if len(uids) >= threshold}

    if not heavy_senders:
        print(f"  No senders found with {threshold}+ emails.")
        return

    print(f"\n  Found {len(heavy_senders)} sender(s) with {threshold}+ emails:\n")
    total_uids = []
    for sender, uids in sorted(heavy_senders.items(), key=lambda x: -len(x[1])):
        print(f"    {len(uids):>6}  {sender}")
        total_uids.extend(uids)

    print(f"\n  Total to delete: {len(total_uids)} message(s)")

    if not dry_run:
        confirm = input("\n  Proceed with deletion? [y/N] ").strip().lower()
        if confirm != 'y':
            print("  Aborted.")
            return

    delete_messages(client, total_uids, dry_run, batch_size)


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Yahoo Mail bulk email cleaner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--rules",       type=str,  default="",
                        help='Filter rule, e.g. "from:*realtor* AND subject:*realty*"')
    parser.add_argument("--high-volume", type=int,  default=0,  metavar="N",
                        help="Delete all mail from senders with >= N emails")
    parser.add_argument("--mailbox",     type=str,  default="INBOX",
                        help='Mailbox to operate on (default: INBOX)')
    parser.add_argument("--dry-run",     action="store_true",
                        help="Preview only — do not delete anything")
    parser.add_argument("--batch-size",  type=int,  default=100, metavar="N",
                        help="IMAP batch size for deletions (default: 100)")
    args = parser.parse_args()

    if not args.rules and not args.high_volume:
        parser.error("Specify at least one of --rules or --high-volume.")

    # ── Credentials ──
    email = YAHOO_EMAIL or input("Yahoo email: ").strip()
    #password = getpass("App password (from Yahoo Account Security): ")
    password = YAHOO_PASSWORD or input("Yahoo app password (not user password!): ").strip()

    if args.dry_run:
        print("\n*** DRY RUN MODE — nothing will be deleted ***\n")

    print(f"\nConnecting to {IMAP_HOST}…")
    try:
        with IMAPClient(IMAP_HOST, port=IMAP_PORT, ssl=True) as client:
            client.login(email, password)
            print(f"✓ Logged in as {email}")

            client.select_folder(args.mailbox, readonly=args.dry_run)
            print(f"✓ Opened mailbox: {args.mailbox}")

            if args.rules:
                run_rule_filter(client, args.rules, args.dry_run, args.batch_size)

            if args.high_volume:
                run_high_volume(client, args.high_volume, args.dry_run, args.batch_size)
    except Exception as exc:
        print(f"ERROR: Unable to connect to Yahoo Mail or open mailbox: {exc}")
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()

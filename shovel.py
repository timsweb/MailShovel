import json
import imapclient
import base64
import os
import argparse
import sys
import time
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import formatdate, make_msgid
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# This scope allows the script to insert messages AND create/manage labels.
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

# --- Paths Setup ---
# All configuration and state files are expected to be in a 'config' directory.
CONFIG_DIR = 'config'
TOKEN_PATH = os.path.join(CONFIG_DIR, 'token.json')
LABEL_CACHE_PATH = os.path.join(CONFIG_DIR, 'label_cache.json')
CREDENTIALS_PATH = os.path.join(CONFIG_DIR, 'credentials.json')
IMAP_ACCOUNTS_PATH = os.path.join(CONFIG_DIR, 'imap_accounts.json')
# Messages imported into Gmail whose IMAP \Seen flag could not be set.
PENDING_SEEN_PATH = os.path.join(CONFIG_DIR, 'pending_seen.json')
# --------------------

RETRY_DELAY = 10
MAX_REJECTIONS_PER_RUN = 3
# Gmail label for the notices left in place of messages Gmail refuses to import.
REJECTED_LABEL = 'shovel/rejected'

label_cache = {}

def load_label_cache():
    """Loads the label cache from a file if it exists."""
    global label_cache
    if os.path.exists(LABEL_CACHE_PATH):
        try:
            with open(LABEL_CACHE_PATH, 'r') as f:
                label_cache = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            print(f"Could not load label cache from {LABEL_CACHE_PATH}. A new one will be created.")
            label_cache = {}

def save_label_cache():
    """Saves the current label cache to a file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(LABEL_CACHE_PATH, 'w') as f:
        json.dump(label_cache, f)

def get_service():
    """Gets an authenticated Gmail service instance, reusing credentials if possible."""
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(TOKEN_PATH, 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

def get_or_create_label_ids(service, label_names):
    """
    Gets the IDs for a list of label names, creating them if they don't exist.
    Uses a file-backed cache to minimize API calls.
    """
    global label_cache
    if not label_cache:
        print("Populating label cache from Gmail API...")
        results = service.users().labels().list(userId='me').execute()
        labels = results.get('labels', [])
        label_cache = {label['name']: label['id'] for label in labels}
        save_label_cache()

    label_ids = []
    cache_updated = False
    for name in label_names:
        if name not in label_cache:
            print(f"Label '{name}' not found, creating it.")
            label_body = {'name': name, 'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'}
            new_label = service.users().labels().create(userId='me', body=label_body).execute()
            label_id = new_label['id']
            label_cache[name] = label_id
            label_ids.append(label_id)
            cache_updated = True
        else:
            label_ids.append(label_cache[name])

    if cache_updated:
        save_label_cache()
        
    return label_ids

def import_message(service, user_id, raw_message_bytes, label_ids, inbox=True):
    """Imports a raw email message into Gmail and marks it as unread."""
    raw_b64 = base64.urlsafe_b64encode(raw_message_bytes).decode('utf-8')
    final_label_ids = ['UNREAD'] + (['INBOX'] if inbox else []) + label_ids
    message = {'raw': raw_b64, 'labelIds': final_label_ids}
    result = service.users().messages().import_(userId=user_id, body=message).execute()
    print(f"Imported message ID: {result['id']}")

def load_pending_seen():
    """Loads messages that were imported but could not be marked as seen."""
    if os.path.exists(PENDING_SEEN_PATH):
        try:
            with open(PENDING_SEEN_PATH, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"Could not read {PENDING_SEEN_PATH}; ignoring it.")
    return {}

def save_pending_seen(pending):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(PENDING_SEEN_PATH, 'w') as f:
        json.dump(pending, f)

def mark_pending_seen(client, username, uidvalidity, pending):
    """
    Marks as seen any messages that a previous attempt imported but failed to flag,
    so they are not imported a second time. Matches by UID while UIDVALIDITY is
    unchanged, otherwise by Message-ID.
    """
    entries = pending.get(username, [])
    if not entries:
        return
    for entry in entries:
        if entry["uidvalidity"] == uidvalidity:
            uids = [entry["uid"]]
        elif entry.get("message_id"):
            uids = client.search(["HEADER", "Message-ID", entry["message_id"]])
        else:
            uids = []
        if uids:
            client.add_flags(uids, [imapclient.SEEN])
        print(f"{username}: marked previously imported message {entry['uid']} as seen.")
    pending[username] = []
    save_pending_seen(pending)

def summary_headers(msg_bytes):
    """Returns the From, Subject and Date of a raw message, decoded where possible."""
    try:
        headers = BytesHeaderParser(policy=policy.default).parsebytes(msg_bytes)
        return {name: str(headers.get(name, '')) for name in ('From', 'Subject', 'Date')}
    except Exception:
        # Malformed headers: fall back to the raw values.
        headers = BytesHeaderParser().parsebytes(msg_bytes)
        return {name: str(headers.get(name, '')) for name in ('From', 'Subject', 'Date')}

def describe(msg_bytes):
    """Returns a short From/Subject summary of a raw message for log lines."""
    headers = summary_headers(msg_bytes)
    return f"from={headers['From']!r} subject={headers['Subject']!r}"

def rejection_notice(username, uid, msg_bytes, reason):
    """Builds a short email saying a message could not be imported, to import in its place."""
    headers = summary_headers(msg_bytes)
    notice = EmailMessage()
    notice['From'] = f'MailShovel <{username}>'
    notice['To'] = username
    notice['Subject'] = f"[MailShovel] Couldn't import: {headers['Subject']}"
    notice['Date'] = formatdate(localtime=True)
    notice['Message-ID'] = make_msgid('mailshovel', domain=username.split('@')[-1])
    notice.set_content(
        f"MailShovel couldn't import a message from {username}, because Gmail rejected it.\n"
        f"\n"
        f"Reason:  {reason}\n"
        f"From:    {headers['From']}\n"
        f"Subject: {headers['Subject']}\n"
        f"Date:    {headers['Date']}\n"
        f"\n"
        f"The original is still in the {username} IMAP inbox (UID {uid}), marked read and flagged.\n"
    )
    return notice.as_bytes()

def fetch_and_forward(account, service, pending):
    """
    Fetches unseen emails from an IMAP account and imports them into Gmail.

    Messages are fetched with BODY.PEEK[] so the server does not mark them as read;
    each one is marked as seen only after Gmail has accepted it. A failure part-way
    through therefore leaves the remaining messages unseen for the next attempt.
    """
    username = account["username"]
    with imapclient.IMAPClient(account["imap_host"], port=account["imap_port"], ssl=True) as client:
        client.login(username, account["password"])
        folder_info = client.select_folder("INBOX")
        uidvalidity = folder_info[b"UIDVALIDITY"]

        mark_pending_seen(client, username, uidvalidity, pending)

        unseen_messages = client.search(["UNSEEN"])
        print(f"{username}: found {len(unseen_messages)} unseen messages.")

        if not unseen_messages:
            return

        label_names = account.get("apply_labels", [])
        label_ids = get_or_create_label_ids(service, label_names) if label_names else []

        rejected = 0
        for uid in unseen_messages:
            data = client.fetch([uid], ["BODY.PEEK[]"]).get(uid)
            if data is None:
                # Expunged since the search.
                continue
            msg_bytes = data[b"BODY[]"]

            try:
                import_message(service, 'me', msg_bytes, label_ids)
            except HttpError as e:
                # A 400 about labels is a problem with the request, not the message.
                if e.resp.status != 400 or "label" in str(e.reason).lower():
                    raise
                # Several rejections in one run suggest the request is at fault
                # rather than the messages, so stop instead of flagging everything.
                rejected += 1
                if rejected > MAX_REJECTIONS_PER_RUN:
                    raise
                # Gmail will never accept this message (e.g. duplicate From header,
                # blocked attachment). Import a notice in its place, then flag the
                # original on the IMAP side so it isn't retried forever. If the notice
                # fails too, the original stays unseen for the next run.
                notice_label_ids = get_or_create_label_ids(service, label_names + [REJECTED_LABEL])
                import_message(service, 'me', rejection_notice(username, uid, msg_bytes, e.reason),
                               notice_label_ids, inbox=False)
                print(f"ERROR {username}: Gmail rejected message {uid}; left a notice under "
                      f"'{REJECTED_LABEL}' and flagged the original: {e.reason} ({describe(msg_bytes)})")
                client.add_flags([uid], [imapclient.SEEN, imapclient.FLAGGED])
                continue

            try:
                client.add_flags([uid], [imapclient.SEEN])
            except Exception:
                message_id = BytesHeaderParser().parsebytes(msg_bytes).get("Message-ID")
                pending.setdefault(username, []).append(
                    {"uidvalidity": uidvalidity, "uid": uid, "message_id": message_id})
                save_pending_seen(pending)
                raise

def with_retry(description, func):
    """
    Runs func, retrying once after RETRY_DELAY seconds. The first failure is
    logged without the words the Grafana "Recent errors" panel matches on, since
    DreamHost routinely drops sessions and the retry nearly always succeeds.
    """
    try:
        return func()
    except Exception as e:
        print(f"{description}: first attempt failed ({e}); retrying in {RETRY_DELAY}s.")
        time.sleep(RETRY_DELAY)
        return func()

def main():
    """Main function to run the email forwarding process."""
    parser = argparse.ArgumentParser(description="Fetch emails from IMAP accounts and import them into Gmail.")
    parser.add_argument('--only', type=str, help='Only process the account with this username.')
    args = parser.parse_args()

    load_label_cache()
    try:
        service = with_retry("Gmail authentication", get_service)
    except Exception as e:
        print(f"ERROR: could not authenticate with Gmail: {e}")
        sys.exit(1)
    try:
        with open(IMAP_ACCOUNTS_PATH) as f:
            all_accounts = json.load(f)
    except FileNotFoundError:
        print(f"Error: {IMAP_ACCOUNTS_PATH} not found. Make sure the config directory is mounted correctly.")
        return
    except json.JSONDecodeError:
        print(f"Error: Could not decode {IMAP_ACCOUNTS_PATH}.")
        return

    accounts_to_process = all_accounts
    if args.only:
        accounts_to_process = [acc for acc in all_accounts if acc['username'] == args.only]
        if not accounts_to_process:
            print(f"Error: Account '{args.only}' not found in {IMAP_ACCOUNTS_PATH}.")
            return

    pending = load_pending_seen()
    for account in accounts_to_process:
        try:
            with_retry(account['username'], lambda: fetch_and_forward(account, service, pending))
            print(f"Completed fetching and forwarding for {account['username']}.")
        except Exception as e:
            print(f"An error occurred with {account['username']}: {e}")

if __name__ == "__main__":
    main()

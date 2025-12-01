import json
import imapclient
import base64
import os
import argparse
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
# --------------------

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

def import_message(service, user_id, raw_message_bytes, label_ids):
    """Imports a raw email message into Gmail and marks it as unread."""
    raw_b64 = base64.urlsafe_b64encode(raw_message_bytes).decode('utf-8')
    final_label_ids = ['UNREAD', 'INBOX'] + label_ids
    message = {'raw': raw_b64, 'labelIds': final_label_ids}
    result = service.users().messages().import_(userId=user_id, body=message).execute()
    print(f"Imported message ID: {result['id']}")

def fetch_and_forward(account, service):
    """Fetches unseen emails from an IMAP account and forwards them to Gmail."""
    with imapclient.IMAPClient(account["imap_host"], port=account["imap_port"], ssl=True) as client:
        client.login(account["username"], account["password"])
        client.select_folder("INBOX")
        
        unseen_messages = client.search(["UNSEEN"])
        print(f"{account['username']}: found {len(unseen_messages)} unseen messages.")

        if not unseen_messages:
            return

        label_names = account.get("apply_labels", [])
        label_ids = get_or_create_label_ids(service, label_names) if label_names else []

        for msgid, data in client.fetch(unseen_messages, ["RFC822"]).items():
            msg_bytes = data[b"RFC822"]
            import_message(service, 'me', msg_bytes, label_ids)
            client.set_flags(msgid, [imapclient.SEEN])

def main():
    """Main function to run the email forwarding process."""
    parser = argparse.ArgumentParser(description="Fetch emails from IMAP accounts and import them into Gmail.")
    parser.add_argument('--only', type=str, help='Only process the account with this username.')
    args = parser.parse_args()

    load_label_cache()
    service = get_service()
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

    for account in accounts_to_process:
        try:
            fetch_and_forward(account, service)
            print(f"Completed fetching and forwarding for {account['username']}.")
        except Exception as e:
            print(f"An error occurred with {account['username']}: {e}")

if __name__ == "__main__":
    main()

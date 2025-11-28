# MailShovel

MailShovel is a Python script designed to fetch unseen emails from multiple IMAP accounts and import them into a single Gmail account. It's ideal for consolidating emails from various sources into Gmail, allowing you to leverage Gmail's powerful search and organization features.

## Features

*   Fetches only unseen emails from configured IMAP accounts.
*   Imports emails into Gmail, marking them as unread and in the Inbox by default.
*   Supports applying custom Gmail labels to imported emails based on the source IMAP account.
*   Caches Gmail label IDs (`label_cache.json`) to minimize API calls and improve efficiency.
*   Command-line argument (`--only`) to process specific IMAP accounts, enabling flexible scheduling with cron jobs.

## Prerequisites

Before you begin, ensure you have the following:

1.  **Python 3.x:** Installed on your system.
2.  **Google Cloud Project:**
    *   A Google Cloud Project with the **Gmail API** enabled.
    *   OAuth 2.0 Client ID credentials (type: Desktop app) downloaded as `credentials.json`.
3.  **IMAP Accounts:** Access details (host, port, username, password) for the IMAP accounts you wish to consolidate.

## Installation

1.  **Clone the repository (or download the files):**
    ```bash
    git clone <repository_url>
    cd MailShovel
    ```
    (Replace `<repository_url>` with the actual URL if this is in a Git repository.)

2.  **Install Python dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

## Configuration

### 1. Google API Credentials (`credentials.json`)

1.  Go to the [Google Cloud Console](https://console.cloud.google.com/).
2.  Select your project (or create a new one).
3.  Navigate to **APIs & Services > Library**.
4.  Search for "Gmail API" and ensure it is **Enabled**.
5.  Navigate to **APIs & Services > Credentials**.
6.  Click **+ CREATE CREDENTIALS** and choose **OAuth client ID**.
7.  For "Application type", select **Desktop app**.
8.  Give it a name (e.g., "MailShovel Desktop Client") and click **Create**.
9.  A dialog will appear with your client ID and client secret. Click **DOWNLOAD JSON**.
10. Rename the downloaded file to `credentials.json` and place it in the same directory as `shovel.py`.

### 2. IMAP Accounts (`imap_accounts.json`)

Create a file named `imap_accounts.json` in the same directory as `shovel.py`. This file should be a JSON array of objects, where each object represents an IMAP account.

**Example `imap_accounts.json`:**

```json
[
  {
    "imap_host": "imap.example.com",
    "imap_port": 993,
    "username": "your_imap_user@example.com",
    "password": "your_imap_password",
    "apply_labels": ["MyIMAPAccount", "Important"]
  },
  {
    "imap_host": "imap.another.org",
    "imap_port": 993,
    "username": "another_user@another.org",
    "password": "another_password",
    "apply_labels": ["AnotherAccount"]
  }
]
```

*   `imap_host`: The IMAP server hostname (e.g., `imap.dreamhost.com`).
*   `imap_port`: The IMAP server port (usually `993` for SSL).
*   `username`: Your full IMAP username.
*   `password`: Your IMAP account password.
*   `apply_labels` (optional): A list of strings. These will be used as Gmail label names. If a label does not exist in your Gmail account, the script will create it automatically.

## Usage

### First Run / Authentication

The first time you run the script, it will open a browser window for you to authenticate with your Google account. This is a one-time process. After successful authentication, a `token.json` file will be created to store your credentials for future runs.

### Running the Script

*   **To process all configured IMAP accounts:**
    ```bash
    python shovel.py
    ```

*   **To process a specific IMAP account (using the username from `imap_accounts.json`):**
    ```bash
    python shovel.py --only your_imap_user@example.com
    ```
    This is particularly useful for setting up different polling schedules via cron jobs.

### Generated Files

*   `token.json`: Stores your Google API authentication credentials after the first successful authentication. **Keep this file secure.**
*   `label_cache.json`: Caches Gmail label names and their corresponding IDs to reduce API calls. This file is automatically managed by the script.

## Scheduling with Cron Jobs

You can use the `--only` argument to set up cron jobs that check different accounts at different intervals.

**Example `crontab` entries:**

```cron
# Check 'frequent@example.com' every 5 minutes
*/5 * * * * /usr/bin/python3 /path/to/MailShovel/shovel.py --only frequent@example.com >> /var/log/mailshovel_frequent.log 2>&1

# Check 'less-frequent@example.com' every hour
0 * * * * /usr/bin/python3 /path/to/MailShovel/shovel.py --only less-frequent@example.com >> /var/log/mailshovel_less_frequent.log 2>&1
```

Remember to replace `/usr/bin/python3` with the actual path to your Python 3 executable and `/path/to/MailShovel/shovel.py` with the absolute path to your script.

## Troubleshooting

*   **`HttpError 400 when requesting ... "Invalid label: <label_name>"`**:
    This usually means the Gmail API is not enabled for your Google Cloud Project, or the scope is incorrect. Ensure the Gmail API is enabled in the Google Cloud Console and that the `gmail.modify` scope is used. The script automatically handles label creation, so this error should be rare if the API is enabled.

*   **`Some requested scopes were invalid. {invalid=[https://www.googleapis.com/auth/gmail.import]}`**:
    This error indicates that the Gmail API is likely not enabled in your Google Cloud Project. Follow the steps in the "Google API Credentials" section to enable it.

*   **`imap_accounts.json not found` or `Could not decode imap_accounts.json`**:
    Ensure `imap_accounts.json` exists in the same directory as `shovel.py` and is correctly formatted JSON.

*   **Re-authentication on every run**:
    Check if `token.json` is being created and saved correctly. Ensure the script has write permissions in its directory. If `credentials.json` is missing or invalid, it will also force re-authentication.

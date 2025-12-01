# MailShovel

MailShovel is a Python script designed to fetch unseen emails from multiple IMAP accounts and import them into a single 
Gmail account. It's ideal for consolidating emails from various sources into Gmail, allowing you to leverage Gmail's 
powerful search and organization features. I run this in docker, but you don't have to. 

## Features

*   **Fetches Unseen Emails:** Connects to IMAP accounts and processes only new messages.
*   **Imports to Gmail:** Imports emails into your Gmail account, marking them as `UNREAD` and placing them in the `INBOX`.
*   **Automatic Labeling:** Applies custom Gmail labels based on the source IMAP account. If a label doesn't exist, it's created automatically.
*   **Flexible Scheduling:** Supports a command-line argument (`--only`) to process specific accounts, perfect for cron jobs.

## Prerequisites

1.  **Docker:** Installed on your system (e.g., Docker Desktop for Mac/Windows, or Docker Engine for Linux/Raspberry Pi), or if not Python. 
2.  **Google Cloud Project:** A project with the **Gmail API** enabled.

> ⚠️ **The IMAP lib doesn't seem to like Python 3.12.** 3.11 seems to be the sweet spot. 

## Setup & Configuration

### Step 1: Add Your Configuration Files

1.  **Google API Credentials (`credentials.json`)**
    *   Follow the [official Google instructions](https://developers.google.com/workspace/guides/create-credentials) to create OAuth 2.0 client credentials for a **Desktop app**.
    *   Download the JSON file and save it as `credentials.json` inside the `config/` directory.

2.  **IMAP Accounts (`imap_accounts.json`)**
    *   Create a file named `imap_accounts.json` inside the `config/` directory.
    *   Add a JSON array of your IMAP account details.

    **Example `config/imap_accounts.json`:**
    ```json
    [
      {
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "username": "your_imap_user@example.com",
        "password": "your_imap_password",
        "apply_labels": ["MyIMAPAccount", "Important"]
      }
    ]
    ```

Your project directory should now look like this:
```
.
├── config/
│   ├── credentials.json
│   └── imap_accounts.json
├── Dockerfile
├── shovel.py
└── requirements.txt
```

## Run with Docker

### Step 1: Build the Docker Image

Build the image from the `Dockerfile`. This command creates a local image named `mailshovel`.
```bash
docker build -t mailshovel .
```

### Step 2: Run the Script

Run the container using the command below. This single command handles everything:
*   **On the first run,** it will be interactive for the one-time Google authentication.
*   **On all subsequent runs,** it will run non-interactively.

```bash
docker run -it --rm \
  -v ./config:/app/config \
  --name mailshovel \
  mailshovel
```

After the first run, your `config` directory will contain the new `token.json` and `label_cache.json` files.

### Scheduling with Cron Jobs

To run the script on a schedule, use the same `docker run` command in your crontab.

*   **To process a single, specific account every 5 minutes:**
    ```cron
    */5 * * * * docker run --rm -v /path/to/project/config:/app/config mailshovel python shovel.py --only user@example.com >> /var/log/mailshovel.log 2>&1
    ```

*   **To process all accounts every hour:**
    ```cron
    0 * * * * docker run --rm -v /path/to/project/config:/app/config mailshovel >> /var/log/mailshovel.log 2>&1
    ```
**Important:** In cron jobs, always use the absolute path to your `config` directory (e.g., `/home/username/MailShovel/config`).

## Troubleshooting

*   **`FileNotFoundError: [Errno 2] No such file or directory: 'config/credentials.json'`**: This means the `config` directory was not correctly mounted. Double-check your `docker run` command's `-v` volume path. Ensure you are using an absolute path for cron jobs.
*   **Authentication Issues:** If you need to re-authenticate, simply delete the `token.json` file from your local `config/` directory and run the script again.
*   **Re-authentication required every week** Disable test most of the app in the Consent Screen section of the Google Cloud console. 

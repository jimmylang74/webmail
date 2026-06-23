# Email Client

A web-based email client running on Ubuntu with Python Flask, supporting multiple email accounts, automatic classification, sender grouping, and auto-forwarding.

## Features

- **Multi-account support**: Configure multiple POP3/IMAP email servers per user
- **Smart classification**: Automatic Ad/Normal/Important categorization based on content analysis
- **Hierarchical inbox**: Emails grouped by sender within importance categories
- **Server badges**: Each email shows which server it was received from
- **Auto-forwarding**: Forward emails from specific groups to other addresses
- **User management**: Admin can create/delete regular users
- **SQLite storage**: Zero-configuration database

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python app.py
```

Then open http://localhost:5566 in your browser.

**Default admin credentials**: admin / 1234

## Directory Structure

```
├── app.py                  # Main Flask application
├── config.py               # Configuration
├── modules/                # Python backend modules
│   ├── db.py              # Database interface
│   ├── auth.py            # Authentication
│   ├── email_fetch.py     # POP3/IMAP fetch
│   ├── email_send.py      # SMTP send
│   ├── email_classify.py  # Ad detection
│   └── forward.py         # Auto-forwarding
├── web/                    # Frontend
│   ├── templates/         # HTML templates
│   └── static/            # CSS and JavaScript
├── db/                    # SQLite data
└── doc/                   # Documentation
```

## Usage

### First Login
1. Open http://localhost:5566
2. Login with admin / 1234
3. Click "Admin" to create additional users

### Add Email Server
1. Click "Config" in the toolbar
2. Click "+ Add Server"
3. Enter your email server details (POP3/IMAP)
4. For Gmail: enable "App Passwords" in Google Account settings

### Fetch Emails
1. Click "Fetch" to download from all configured servers
2. Emails appear in Inbox, auto-classified into Ad/Normal/Important groups
3. Each email shows a badge identifying its source server

### Auto-Forwarding
1. Go to Config page
2. Under "Auto-Forward Rules", click "+ Add Rule"
3. Select a group and enter the forwarding address
4. Enable/disable rules as needed

## Protocol Support

| Protocol | Port | Encryption |
|----------|------|------------|
| POP3 | 110 | STARTTLS |
| POP3S | 995 | SSL/TLS |
| IMAP | 143 | STARTTLS |
| IMAPS | 993 | SSL/TLS |
| SMTP | 25/587 | STARTTLS |
| SMTPS | 465 | SSL/TLS |

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| LOGIN_PORT | 5566 | Web server port |
| SECRET_KEY | (built-in) | Flask session secret |

## Sender Classification

The classifier uses keyword matching to categorize emails:

- **Ad**: Promotional keywords, marketing domains, unsubscribe links
- **Important**: Urgency keywords, meeting/invoice/payment terms, direct correspondence
- **Normal**: Everything else

Users can override automatic classification by reassigning sender groups in the Config page.

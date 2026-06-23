# Database Schema

## Entity Relationship Diagram

```mermaid
erDiagram
    users ||--o{ email_servers : "has"
    users ||--o{ importance_groups : "has"
    users ||--o{ sender_groups : "has"
    users ||--o{ emails : "has"
    users ||--o{ forward_rules : "has"
    importance_groups ||--o{ sender_groups : "categorizes"
    importance_groups ||--o{ emails : "classifies"
    importance_groups ||--o{ forward_rules : "triggers"
    sender_groups ||--o{ emails : "groups"
    sender_groups ||--o{ forward_rules : "triggers"
    email_servers ||--o{ emails : "receives from"

    users {
        int id PK
        string username UK
        string password
        string role "admin | user"
        datetime created_at
    }

    email_servers {
        int id PK
        int user_id FK
        string server_name "Display name"
        string incoming_server "Hostname"
        int incoming_port "Port number"
        string incoming_protocol "POP3 | IMAP"
        string outgoing_server "SMTP hostname"
        int outgoing_port "SMTP port"
        string username "Email account"
        string password "App password"
        bool delete_after_download
        bool use_ssl
        datetime last_fetch_at
        datetime created_at
    }

    importance_groups {
        int id PK
        int user_id FK
        string name "Ad | Normal | Important"
        int sort_order
        datetime created_at
    }

    sender_groups {
        int id PK
        int user_id FK
        string sender_email
        string sender_name
        string group_name "Display name"
        int importance_group_id FK
        bool is_auto_classified
        datetime created_at
    }

    emails {
        int id PK
        int user_id FK
        int server_id FK
        int sender_group_id FK
        int importance_group_id FK
        string message_id "Dedup key"
        string sender
        string sender_name
        string recipients
        string subject
        text body_text
        text body_html
        datetime received_date
        bool is_read
        string folder "inbox | outbox | drafts | deleted"
        string server_badge "Badge text"
        datetime created_at
    }

    forward_rules {
        int id PK
        int user_id FK
        int importance_group_id FK
        int sender_group_id FK
        string forward_to
        bool enabled
        datetime created_at
    }
```

## Table Descriptions

### users
Stores user accounts. The first user created is `admin` with role `admin`. Admin can create/delete regular users (role `user`).

### email_servers
Each user can configure multiple email servers. Supports both POP3 and IMAP for incoming, SMTP for outgoing (optional). Passwords are stored as plaintext for SMTP/POP3 authentication (standard practice for local email clients).

### importance_groups
Three default groups created for each new user: `Ad` (sort_order=0), `Normal` (sort_order=1), `Important` (sort_order=2). Users cannot delete these.

### sender_groups
Auto-created when a new sender is encountered during email fetch. Each sender group belongs to an importance group. Users can manually reassign importance.

### emails
The core table storing all email messages across all folders. The `message_id` field is used for deduplication when fetching from the same server multiple times. The `folder` field determines which mailbox view the email appears in.

### forward_rules
Rules for auto-forwarding. A rule can target either an importance group (all emails in that category) or a specific sender group. Multiple rules can coexist.

## Indexes

- `idx_emails_user_folder` - Fast lookup of emails by user and folder
- `idx_emails_user_sender` - Fast grouping by sender
- `idx_emails_message_id` - Deduplication check
- `idx_sender_groups_user` - Per-user sender group lookup
- `idx_forward_rules_user` - Per-user forward rule lookup

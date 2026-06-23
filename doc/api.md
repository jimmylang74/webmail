# API Documentation

All API endpoints return JSON. Authentication is session-based.

## Authentication

### POST /api/login
Login with username and password.
```json
{"username": "admin", "password": "1234"}
// Response: {"success": true, "role": "admin"}
```

### POST /api/logout
Clear session.

### GET /api/session
Get current session info. Requires login.
```json
{"user_id": 1, "username": "admin", "role": "admin"}
```

## Admin API

### GET /api/admin/users
List all users. Admin only.
```json
{"users": [{"id": 1, "username": "admin", "role": "admin", "created_at": "..."}]}
```

### POST /api/admin/users
Create a new user. Admin only.
```json
{"username": "newuser", "password": "pass123"}
```

### POST /api/admin/users/:id/delete
Delete a user. Cannot delete admin.

### POST /api/admin/change-password
Change own password.
```json
{"new_password": "newpass123"}
```

## Email Servers

### GET /api/servers
Get all servers for current user.

### POST /api/servers
Add a new email server configuration.
```json
{
  "server_name": "Gmail",
  "incoming_server": "pop.gmail.com",
  "incoming_port": 995,
  "incoming_protocol": "POP3",
  "outgoing_server": "smtp.gmail.com",
  "outgoing_port": 587,
  "username": "user@gmail.com",
  "password": "app-password",
  "delete_after_download": false,
  "use_ssl": true
}
```

### PUT /api/servers/:id
Update server configuration.

### DELETE /api/servers/:id
Delete server configuration.

### POST /api/servers/:id/fetch
Trigger fetch for a specific server (background thread).

### POST /api/fetch-all
Trigger fetch for all user's servers (background thread).

## Mailbox

### GET /api/mailbox/tree
Get the folder tree structure with counts.
```json
{
  "folders": [
    {
      "id": "inbox", "name": "Inbox", "count": 42, "unread": 5,
      "children": [
        {
          "id": "imp_1", "name": "Important", "imp_group_id": 1, "count": 10,
          "children": [
            {"id": "sender_5", "name": "boss", "sender_group_id": 5, "count": 3}
          ]
        }
      ]
    }
  ]
}
```

### GET /api/emails
Get email list with filters.
- Query params: `folder`, `imp_group_id`, `sender_group_id`, `page`, `per_page`, `search`
```json
{"emails": [...], "total": 100, "page": 1, "per_page": 50}
```

### GET /api/emails/:id
Get full email content. Marks as read.

### POST /api/emails/:id/move
Move email to a folder.
```json
{"folder": "deleted"}
```

### DELETE /api/emails/:id
Permanently delete an email.

### DELETE /api/emails/group/:sender_group_id
Delete all emails in a sender group.

### DELETE /api/emails/group/importance/:imp_group_id
Delete all inbox emails in an importance group.

## Sender Groups

### GET /api/sender-groups
List all sender groups with importance assignments.

### PUT /api/sender-groups/:id
Update sender group importance assignment.
```json
{"importance_group_id": 1}
```

### POST /api/sender-groups/auto-classify
Run auto-classification on all unassigned sender groups.

## Compose

### POST /api/compose
Send an email via SMTP.
```json
{
  "server_id": 1,
  "to": "recipient@example.com",
  "subject": "Hello",
  "body_text": "Message body"
}
```

### POST /api/drafts
Save a draft.
```json
{
  "server_id": 1,
  "to": "recipient@example.com",
  "subject": "Draft",
  "body_text": "Unfinished message"
}
```

### GET /api/drafts
List all drafts.

## Forward Rules

### GET /api/forward-rules
List all forward rules.

### POST /api/forward-rules
Create a forward rule.
```json
{
  "forward_to": "other@example.com",
  "importance_group_id": 1,
  "sender_group_id": null
}
```

### PUT /api/forward-rules/:id
Update a forward rule.

### DELETE /api/forward-rules/:id
Delete a forward rule.

## Stats

### GET /api/stats
Get folder counts.
```json
{"inbox": 42, "outbox": 5, "drafts": 2, "deleted": 8, "unread": 3}
```

### GET /api/groups/importance
Get importance groups.
```json
{"groups": [{"id": 1, "name": "Ad", "sort_order": 0}, ...]}
```

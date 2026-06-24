# Agent Instructions

## Git Commit Convention

Commit messages should be written in **English** by default.

Every commit message must include:

1. **List of added and modified files**
2. **A brief description for each added/modified file**
3. **An overall brief description of the changes**

### Format

```
[<type>] <short summary>

<brief overall description of what changed and why>

- Added:
  - <new file>: <what it does>
- Modified:
  - <changed file>: <what changed in this file>
```

### Example

```
[fix] Resolve inbox group count mismatch

Fix the inbox tree so the badge count on each importance group matches the emails shown when expanded.

- Added:
  - AGENTS.md: document git commit convention
- Modified:
  - app.py: group sender groups and emails by email.importance_group_id in _build_inbox_children
  - web/static/js/mail.js: sync tree counts with expanded email lists
```

### Types

- `[feat]` — new feature
- `[fix]` — bug fix
- `[refactor]` — code change that neither fixes a bug nor adds a feature
- `[docs]` — documentation only changes
- `[style]` — formatting, missing semi colons, etc.
- `[test]` — adding or correcting tests
- `[chore]` — maintenance tasks

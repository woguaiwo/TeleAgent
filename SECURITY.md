# Security Policy

TeleAgent sits between a user, Telegram, and an interactive terminal program.
Treat it as automation for a local shell session.

## Reporting Vulnerabilities

Please report security issues privately through GitHub Security Advisories when
available. If the repository has no advisory channel yet, open a minimal issue
asking for a private contact path without including exploit details.

## Sensitive Data

Do not commit:

- Telegram bot tokens
- Telegram chat IDs from real users
- `.teleagent/` directories
- `teleagent-history.log` or `teleagent-raw.log`
- debug inbox/outbox files

Raw terminal history can contain secrets, file paths, prompts, model outputs,
environment details, and command transcripts.

## Operational Notes

- Keep `allowed_chat_ids` restricted to trusted chat IDs.
- Use `/ta rawhistory` only for debugging.
- Review regex auto-reply rules carefully; broad rules can approve or answer the
  wrong terminal prompt.
- Prefer `--dry-run` before using new auto-reply rules with destructive tools.

# Contributing

Thanks for considering a contribution to TeleAgent.

## Development Setup

```bash
python -m pip install -e .
python -m unittest discover -s tests
```

TeleAgent intentionally has no runtime dependencies outside the Python standard
library. Keep new dependencies out of the runtime path unless there is a strong
reason.

## Pull Requests

- Keep changes focused and include tests for behavior changes.
- Do not commit local logs, Telegram tokens, chat IDs, or raw terminal captures.
- Prefer deterministic tests that use debug mode instead of real Telegram.
- Update `README.md` when changing user-facing commands or configuration.

## Local Debugging

Use Telegram debug mode when reproducing bridge behavior:

```toml
[telegram]
enabled = true
debug_mode = true
debug_inbox_path = "teleagent-debug-inbox.txt"
debug_outbox_path = "teleagent-debug-outbox.jsonl"
```

Append simulated user messages to the inbox file and inspect the JSONL outbox.

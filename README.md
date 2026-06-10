# TeleAgent

TeleAgent is a small terminal wrapper for interactive CLI tools such as `codex`
or `kimi`. It launches the real command inside a pseudo-terminal, watches the
terminal output, and can bridge model replies and user input through Telegram.

TeleAgent is useful when you want to keep a CLI coding agent running on a
server, but answer prompts, approve commands, or read final output from a phone.

Highlights:

- wraps terminal CLIs without requiring their internal APIs
- forwards Telegram messages to the wrapped CLI
- mirrors terminal menus and approval prompts as numbered Telegram choices
- supports cleaned full-output mode, summary mode, and history export
- optionally reads Codex and Kimi structured session logs
- has no runtime dependencies outside the Python standard library

## Quick start

Install from a local checkout and create your global default config:

```bash
git clone https://github.com/woguaiwo/TeleAgent.git
cd TeleAgent
./install.sh
```

The installer runs `pip install -e .`, then creates:

- `~/.config/teleagent/teleagent.toml`
- `~/.config/teleagent/telegram-token`

It asks whether to configure Telegram. If you skip Telegram setup, TeleAgent is
still installed and can run locally, but phone bridging stays disabled until you
edit the config.

Manual install:

```bash
python -m pip install -e .
teleagent --init-global
```

Run an agent from the project directory you want it to work in:

```bash
cd /path/to/your/project
teleagent -- codex
```

For Kimi:

```bash
teleagent -- kimi
```

TeleAgent runs the wrapped command from the directory where you invoke it. If
you run `teleagent` inside `/data/my-project`, the default `codex` process sees
`/data/my-project` as its working directory.

When you run `teleagent` without `-c/--config`, it first checks for
`./teleagent.toml`. If the current directory does not have one, TeleAgent
initializes a project-local config:

- if `~/.config/teleagent/teleagent.toml` exists, it copies that file
- otherwise, it writes the built-in default template

This lets each project start from your defaults while still allowing later
per-project edits.

`pip install` itself does not write to your home directory. Use `./install.sh`
or `teleagent --init-global` when you want TeleAgent to create the global
defaults explicitly.

Set `settings.default_command` in `teleagent.toml` to run `teleagent` directly:

```toml
[settings]
default_command = ["codex"]
```

Then:

```bash
teleagent
```

For Claude:

```bash
python -m teleagent -c examples/teleagent.toml -- claude
```

Use `--dry-run` to see matches without sending replies:

```bash
python -m teleagent -c examples/teleagent.toml --dry-run -- codex
```

## Telegram bridge

Telegram is disabled in the public example config. To enable it, create a
Telegram bot with BotFather, initialize your global config, write its token to
the token file, and add your chat id:

```bash
teleagent --init-global --enable-telegram --telegram-chat-id 123456789
```

Then paste your bot token into:

```bash
~/.config/teleagent/telegram-token
```

Equivalent manual setup:

```bash
mkdir -p ~/.config/teleagent
cp examples/teleagent.toml ~/.config/teleagent/teleagent.toml
touch ~/.config/teleagent/telegram-token
chmod 700 ~/.config/teleagent
chmod 600 ~/.config/teleagent/telegram-token
```

For project-local configs, TeleAgent creates the configured `token_file`
automatically as an empty `0600` file. Open that file and paste your bot token
into it. The default project-local token path is `.teleagent/telegram-token`.

```toml
[settings]
default_command = ["codex"]

[telegram]
enabled = true
token_env = "TELEGRAM_BOT_TOKEN"
token_file = ".teleagent/telegram-token"
allowed_chat_ids = [123456789]
debug_mode = false
debug_inbox_path = "teleagent-debug-inbox.txt"
debug_outbox_path = "teleagent-debug-outbox.jsonl"
debug_chat_id = 0
forward_patterns = [
  "Final answer:[ \\t]*([^\\r\\n]*)",
  "Conclusion:[ \\t]*([^\\r\\n]*)",
]
poll_timeout = 20
max_message_chars = 3500
history_path = "teleagent-history.log"
raw_history_path = "teleagent-raw.log"
output_mode = "summary"
output_sources = ["terminal"]
codex_state_root = "~/.codex"
kimi_state_root = "~/.kimi"
idle_forward_seconds = 3.0
all_chunk_chars = 2500
summary_threshold_chars = 1200
summary_max_chars = 800
auto_summary = true
summary_timeout_seconds = 30.0
summary_fallback_chars = 3500
input_submit_delay_seconds = 0.05
input_submit_keys = ["enter", "linefeed"]
summary_submit_delay_seconds = 0.2
summary_submit_keys = ["enter", "linefeed"]
summary_prompt_template = "请把你刚才过长的回复总结成 {max_chars} 字以内。面向手机聊天阅读：先给一句话结论，再用短条目列关键点；不要复述完整原文。"
```

Token resolution order is `token`, then `token_env`, then `token_file`. Keeping
the token in `token_file` avoids exporting it in every shell.

When wrapped CLI output matches a `forward_patterns` regex, TeleAgent sends the
matched text to every allowed chat. If the regex has a capture group, group 1 is
sent; otherwise the whole match is sent.

Any text message from an allowed chat is forwarded to the wrapped CLI as one
input line and submitted with Enter. This is intended for answering model
prompts, approvals, or follow-up questions from your phone.

Telegram input controls:

- `hello`: type `hello` and press Enter
- `/start`: ignored by TeleAgent
- `/ta all`: switch Telegram to full-output mode
- `/ta summary`: switch Telegram back to summary mode
- `/ta history`: send the full local output history file to Telegram
- `/ta rawhistory`: send the raw PTY output file for debugging terminal rendering
- `/ta help`: show TeleAgent control commands
- `/send hello`: type `hello` and press Enter
- `/type hello`: type `hello` without pressing Enter
- `/enter` or `/submit`: submit the current input using `input_submit_keys`
- `/key esc`: press Escape
- `/key tab`: press Tab
- `/key backspace`: press Backspace
- `/key enter`: press raw Enter only
- `/key up`, `/key down`, `/key left`, `/key right`: press arrow keys
- `/key ctrl-c`, `/key ctrl-d`: send common control keys

Unknown slash commands are forwarded to the wrapped CLI. This is important for
Codex and Claude commands such as `/model` or `/resume`. The older direct
TeleAgent aliases `/all`, `/summary`, `/history`, and `/rawhistory` still work,
but `/ta ...` is preferred to avoid collisions with CLI-native commands.

When a forwarded slash command opens a terminal selection menu, TeleAgent tries
to mirror that menu to Telegram as numbered choices. Reply with `1`, `2`, `3`,
and so on to select an item; TeleAgent sends the needed arrow keys and Enter to
the wrapped CLI. If menu detection misses a case, use `/key up`, `/key down`,
and `/key enter` for manual control.

Keep `allowed_chat_ids` strict. Telegram messages from all other chats are
ignored.

For receiving agent output, avoid forwarding every terminal update. Agent CLIs
usually stream many intermediate lines, redraw terminal UI, and print tool logs.
Prefer one of these patterns:

- Configure the agent prompt to end important responses with a stable marker,
  such as `Final answer: ...`, and forward only that marker.
- Add forwarding rules for approval prompts and questions that require you to
  reply from Telegram.
- For full remote monitoring, use a batching mode that sends recent output only
  after the terminal has been quiet for a few seconds. This keeps Telegram
  readable and avoids hitting message limits.

TeleAgent now supports both full output and summary output:

- All model output is appended to `history_path`.
- Raw terminal output is appended to `raw_history_path`; this may contain ANSI
  escape sequences and TUI redraw data.
- `output_sources = ["terminal"]` keeps the existing UI/terminal reader as the
  default path. This is also the default UI mode for `codex`, `claude`, and
  `kimi`.
- Optional structured sources can be enabled explicitly, for example
  `["terminal", "codex_rollout"]` or `["terminal", "kimi_wire", "kimi_context"]`.
  When enabled after `terminal`, they act as fallbacks for CLIs whose UI output
  is missing or unusable.
- For Kimi, `kimi_wire` reads live `wire.jsonl` turn events and is the preferred
  session-log fallback. `kimi_context` reads `context.jsonl` assistant text and
  remains available as an extra compatibility source.
- When the wrapped command is `kimi` and `output_sources` is still the default
  `["terminal"]`, TeleAgent automatically uses `["kimi_wire", "terminal"]` for
  that run. This keeps Kimi replies structured while preserving the UI reader as
  a fallback.
- `codex_state_root` and `kimi_state_root` point to the Codex and Kimi state
  directories. The defaults are `~/.codex` and `~/.kimi`.
- `/history` sends the cleaned text history. Use `/rawhistory` only for
  debugging.
- In `output_mode = "summary"`, short idle chunks are sent directly. If a chunk
  is longer than `summary_threshold_chars`, TeleAgent sends a notice and injects
  `summary_prompt_template` into the wrapped CLI so the model can produce a
  compact summary.
- `summary_submit_keys` controls how the injected summary prompt is submitted.
  The default sends Enter and then linefeed because some terminal UIs accept one
  but not the other when input is injected programmatically.
- `input_submit_keys` controls how ordinary Telegram messages and `/enter` are
  submitted. The default also sends Enter and then linefeed; if your CLI submits
  twice, set it to `["enter"]`, and if it only inserts a newline, try
  `["linefeed"]` or `["ctrl-j"]`.
- In `output_mode = "all"`, idle chunks are split into Telegram-sized messages.
- Send `/ta history` in Telegram to receive the full history file.
- Send `/ta all` or `/ta summary` to switch modes during a running session.

### Debug interception mode

Set `debug_mode = true` to test the Telegram bridge without connecting to
Telegram. In this mode `token` and `TELEGRAM_BOT_TOKEN` are not required.

```toml
[telegram]
enabled = true
debug_mode = true
debug_inbox_path = "teleagent-debug-inbox.txt"
debug_outbox_path = "teleagent-debug-outbox.jsonl"
```

Run the wrapper normally:

```bash
python -m teleagent -c examples/teleagent.toml -- codex
```

Append one line to `debug_inbox_path` to simulate one Telegram message:

```bash
printf '你好\n' >> teleagent-debug-inbox.txt
printf '/enter\n' >> teleagent-debug-inbox.txt
```

Every message that would have been sent to Telegram is appended to
`debug_outbox_path` as JSONL. The `text` field is the exact final mobile message
after truncation, terminal-noise cleanup, and mobile formatting.

## Configuration

Create a TOML file with `[[rules]]` entries:

```toml
[[rules]]
name = "continue"
pattern = "continue\\? \\[y/N\\]"
reply = "y"
once = false
```

Rule fields:

- `name`: label used in logs
- `pattern`: Python regular expression matched against recent terminal output
- `reply`: text to send, with a newline added automatically if missing
- `once`: when true, the rule only fires once per wrapped process
- `delay_seconds`: optional delay before replying

Settings:

```toml
[settings]
buffer_size = 8192
log_matches = true
default_command = ["codex"]
```

Telegram is configured under `[telegram]` and is disabled by default in the
public template.
If no `-c/--config` is provided, TeleAgent looks for `./teleagent.toml`. When
that file is missing, it initializes one in the current directory: first from
`~/.config/teleagent/teleagent.toml` if available, otherwise from the built-in
default template. `TELEAGENT_CONFIG` can override this and disables the
automatic project initialization.
The config file location does not change the wrapped command's working
directory; the wrapped command inherits the shell directory where `teleagent`
was started.

## Design notes

This wrapper does not try to understand Codex or Claude internally. It treats
them as terminal programs and reacts only to visible prompts. That makes it
portable, but it also means your rules should be specific. Broad patterns such
as `".*"` or `"\\?"` are risky because they may answer the wrong question.

Good rules match the exact wording of a confirmation prompt, a menu option, or
an approval request. For sensitive operations, prefer `--dry-run` first.

## Security

Never commit Telegram bot tokens, real chat IDs, `.teleagent/`,
`teleagent-history.log`, `teleagent-raw.log`, or debug inbox/outbox files. Raw
terminal captures can contain private prompts, command transcripts, paths, and
secrets. See `SECURITY.md` for details.

## License

TeleAgent is released under the MIT License. See `LICENSE`.

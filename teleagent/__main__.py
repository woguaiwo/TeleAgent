from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import textwrap
from pathlib import Path

from .wrapper import WrapperConfig, _config_for_command, run_wrapped


LOCAL_CONFIG_PATH = Path("teleagent.toml")
PROJECT_TOKEN_FILE = ".teleagent/telegram-token"
TELEAGENT_HOME_ENV = "TELEAGENT_HOME"
TELEAGENT_CONFIG_DIR_ENV = "TELEAGENT_CONFIG_DIR"
DEFAULT_PROJECT_CONFIG = """
[settings]
buffer_size = 8192
log_matches = true
event_log_path = "teleagent-events.log"
local_cursor_mode = "passthrough"
local_cursor_idle_seconds = 0.75
default_command = ["codex"]

[telegram]
enabled = false
token_env = "TELEGRAM_BOT_TOKEN"
token_file = ".teleagent/telegram-token"
allowed_chat_ids = []
debug_mode = false
debug_inbox_path = "teleagent-debug-inbox.txt"
debug_outbox_path = "teleagent-debug-outbox.jsonl"
debug_chat_id = 0
forward_patterns = [
  "Final answer:[ \\\\t]*([^\\\\r\\\\n]*)",
  "Conclusion:[ \\\\t]*([^\\\\r\\\\n]*)",
]
poll_timeout = 20
max_message_chars = 3500
history_path = "teleagent-history.log"
raw_history_path = "teleagent-raw.log"
diagnostic_log_path = "teleagent-diagnostics.jsonl"
diagnostic_retention_days = 7.0
diagnostic_snippet_chars = 240
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
summary_background_wait_seconds = 45.0
summary_background_ready_stable_seconds = 15.0
background_terminal_timeout_seconds = 600.0
input_submit_delay_seconds = 0.05
input_submit_keys = ["enter", "linefeed"]
slash_submit_delay_seconds = 0.2
slash_submit_keys = ["enter"]
summary_submit_delay_seconds = 0.2
summary_submit_keys = ["enter", "linefeed"]
summary_prompt_template = "请把你刚才过长的回复总结成 {max_chars} 字以内。面向手机聊天阅读：先给一句话结论，再用短条目列关键点；不要复述完整原文。"

[[rules]]
name = "generic-yes-no-continue"
pattern = "(continue|proceed)\\\\?\\\\s*\\\\[y/N\\\\]"
reply = "y"

[[rules]]
name = "generic-numbered-choice-first"
pattern = "enter choice\\\\s*:"
reply = "1"

[[rules]]
name = "once-confirm-start"
pattern = "start new session\\\\?\\\\s*\\\\[y/N\\\\]"
reply = "y"
once = true
"""


def _teleagent_home() -> Path:
    configured = os.environ.get(TELEAGENT_HOME_ENV, "")
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return Path.home()


def _global_config_dir() -> Path:
    configured = os.environ.get(TELEAGENT_CONFIG_DIR_ENV, "")
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return _teleagent_home() / ".config" / "teleagent"


def _global_config_path() -> Path:
    return _global_config_dir() / "teleagent.toml"


def _global_token_path() -> Path:
    return _global_config_dir() / "telegram-token"


def _expand_config_path(path: str) -> Path:
    return Path(os.path.expandvars(path)).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="teleagent",
        description="Wrap an interactive CLI and auto-reply to configured prompts.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help=(
            "Path to a TOML config file. Defaults to ./teleagent.toml, "
            "initializing it from global defaults or the built-in template if missing."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log matched replies without sending them to the wrapped command.",
    )
    parser.add_argument(
        "--print-config-path",
        action="store_true",
        help="Print the config path TeleAgent would use and exit.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Print configuration diagnostics and exit without starting the wrapped command.",
    )
    parser.add_argument(
        "--init-global",
        action="store_true",
        help=(
            "Create the global config and token file, then exit. "
            "Use TELEAGENT_HOME or TELEAGENT_CONFIG_DIR to avoid a quota-limited HOME."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --init-global, overwrite an existing global config.",
    )
    parser.add_argument(
        "--global-default-command",
        default=None,
        help='With --init-global, set settings.default_command, for example "codex" or "kimi".',
    )
    parser.add_argument(
        "--enable-telegram",
        action="store_true",
        help="With --init-global, set telegram.enabled = true.",
    )
    parser.add_argument(
        "--telegram-chat-id",
        action="append",
        default=[],
        help="With --init-global, add an allowed Telegram chat id. May be repeated.",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run after --, for example: -- codex",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.init_global:
        return _init_global_config(
            force=args.force,
            default_command=args.global_default_command,
            enable_telegram=args.enable_telegram,
            chat_ids=args.telegram_chat_id,
        )

    config_path, initialized_from, copy_error = _resolve_config_path_with_status(
        args.config,
        create_local=not args.print_config_path,
    )
    if args.print_config_path:
        print(config_path)
        return 0

    if initialized_from:
        print(
            f"[teleagent] initialized config from {initialized_from} to {config_path}",
            file=sys.stderr,
        )
    elif copy_error:
        print(f"[teleagent] could not copy project config: {copy_error}", file=sys.stderr)

    config = WrapperConfig.load(config_path)
    for warning in _prepare_runtime_files(config):
        print(f"[teleagent] {warning}", file=sys.stderr)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]

    if not command:
        command = list(config.default_command)
    config = _config_for_command(config, command)

    if args.doctor:
        _print_doctor(config_path, config, command)
        return 0

    if not command:
        print(
            "teleagent: missing command. Example: teleagent -- codex\n"
            "Set settings.default_command = [\"codex\"] to run `teleagent` directly.",
            file=sys.stderr,
        )
        return 2
    validation_error = _validate_runtime_config(config)
    if validation_error:
        print(validation_error, file=sys.stderr)
        return 2
    return run_wrapped(command, config=config, dry_run=args.dry_run)


def _resolve_config_path(explicit_path: str | None) -> Path:
    path, _, _ = _resolve_config_path_with_status(explicit_path)
    return path


def _resolve_config_path_with_status(
    explicit_path: str | None,
    *,
    local_path: Path | None = None,
    global_path: Path | None = None,
    create_local: bool = True,
) -> tuple[Path, str, str]:
    local_config = local_path or LOCAL_CONFIG_PATH
    global_config = global_path or _global_config_path()

    if explicit_path:
        return _expand_config_path(explicit_path), "", ""
    env_path = os.environ.get("TELEAGENT_CONFIG", "")
    if env_path:
        return _expand_config_path(env_path), "", ""

    if local_config.exists():
        return local_config, "", ""

    if not create_local:
        return local_config, "", ""

    expanded_global = _expand_config_path(str(global_config))
    if expanded_global.exists():
        try:
            _write_project_config_from_global(expanded_global, local_config)
        except OSError as exc:
            return expanded_global, "", str(exc)
        return local_config, str(expanded_global), ""

    try:
        _write_default_project_config(local_config)
    except OSError as exc:
        return local_config, "", str(exc)
    return local_config, "built-in default template", ""


def _write_default_project_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_private_file(path.parent / PROJECT_TOKEN_FILE)
    path.write_text(textwrap.dedent(DEFAULT_PROJECT_CONFIG).lstrip(), encoding="utf-8")


def _write_project_config_from_global(global_path: Path, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    text = global_path.read_text(encoding="utf-8")
    local_text, uses_project_token = _localize_project_token_file(text)
    if uses_project_token:
        _ensure_private_file(local_path.parent / PROJECT_TOKEN_FILE)
    local_path.write_text(local_text, encoding="utf-8")


def _localize_project_token_file(text: str) -> tuple[str, bool]:
    token_line = re.compile(r"(?m)^(\s*token_file\s*=\s*).*$")
    if not token_line.search(text):
        return text, False

    project_token = json.dumps(PROJECT_TOKEN_FILE)

    def replace_token(match: re.Match[str]) -> str:
        return f"{match.group(1)}{project_token}"

    return token_line.sub(replace_token, text, count=1), True


def _init_global_config(
    *,
    force: bool,
    default_command: str | None,
    enable_telegram: bool,
    chat_ids: list[str],
) -> int:
    config_path = _global_config_path()
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.parent.chmod(0o700)
    except OSError as exc:
        print(f"teleagent: could not create global config directory: {config_path.parent}", file=sys.stderr)
        print(f"teleagent: {exc}", file=sys.stderr)
        print(
            "Set TELEAGENT_HOME=/path/to/writable/dir or "
            "TELEAGENT_CONFIG_DIR=/path/to/writable/config-dir and rerun.",
            file=sys.stderr,
        )
        return 2

    parsed_chat_ids, chat_id_error = _parse_chat_ids(chat_ids)
    if chat_id_error:
        print(chat_id_error, file=sys.stderr)
        return 2

    parsed_default_command: list[str] | None = None
    if default_command:
        try:
            parsed_default_command = shlex.split(default_command)
        except ValueError as exc:
            print(f"teleagent: invalid --global-default-command: {exc}", file=sys.stderr)
            return 2
        if not parsed_default_command:
            print("teleagent: --global-default-command cannot be empty", file=sys.stderr)
            return 2

    created_config = False
    if config_path.exists() and not force:
        print(f"global config already exists: {config_path}")
        print("use --force with --init-global to overwrite it")
    else:
        try:
            config_text = _render_global_config(
                default_command=parsed_default_command,
                enable_telegram=enable_telegram,
                chat_ids=parsed_chat_ids,
            )
            config_path.write_text(config_text, encoding="utf-8")
            config_path.chmod(0o600)
        except OSError as exc:
            print(f"teleagent: could not write global config: {config_path}", file=sys.stderr)
            print(f"teleagent: {exc}", file=sys.stderr)
            return 2
        created_config = True
        action = "overwrote" if force else "created"
        print(f"{action} global config: {config_path}")

    try:
        config = WrapperConfig.load(config_path)
        token_file = config.telegram.token_file or str(_global_token_path())
    except (OSError, ValueError):
        token_file = str(_global_token_path())
    token_path = _expand_config_path(token_file)
    try:
        _ensure_private_file(token_path)
    except OSError as exc:
        print(f"teleagent: could not create token file: {token_path}", file=sys.stderr)
        print(f"teleagent: {exc}", file=sys.stderr)
        return 2
    print(f"created token file if missing: {token_path}")

    if created_config and not enable_telegram:
        print("telegram is disabled by default; edit the config or rerun with --enable-telegram")
    elif enable_telegram and not parsed_chat_ids:
        print("telegram is enabled, but allowed_chat_ids is empty; add your chat id before running")
    return 0


def _render_global_config(
    *,
    default_command: list[str] | None,
    enable_telegram: bool,
    chat_ids: list[int],
) -> str:
    text = textwrap.dedent(DEFAULT_PROJECT_CONFIG).lstrip()
    text = text.replace(
        'token_file = ".teleagent/telegram-token"',
        f"token_file = {json.dumps(str(_global_token_path()))}",
        1,
    )
    if default_command is not None:
        text = text.replace(
            'default_command = ["codex"]',
            f"default_command = {json.dumps(default_command)}",
            1,
        )
    if enable_telegram:
        text = text.replace("enabled = false", "enabled = true", 1)
    if chat_ids:
        text = text.replace("allowed_chat_ids = []", f"allowed_chat_ids = {chat_ids}", 1)
    return text


def _parse_chat_ids(raw_chat_ids: list[str]) -> tuple[list[int], str]:
    parsed: list[int] = []
    for raw in raw_chat_ids:
        pieces = [piece.strip() for piece in raw.split(",")]
        for piece in pieces:
            if not piece:
                continue
            try:
                parsed.append(int(piece))
            except ValueError:
                return [], f"teleagent: invalid --telegram-chat-id value: {piece!r}"
    return parsed, ""


def _ensure_private_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent != Path("."):
        path.parent.chmod(0o700)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)


def _prepare_runtime_files(config: WrapperConfig) -> list[str]:
    telegram = config.telegram
    if not telegram.enabled or telegram.debug_mode or not telegram.token_file:
        return []

    token_path = _expand_config_path(telegram.token_file)
    token_parent = token_path.parent

    try:
        if token_parent != Path("."):
            token_parent.mkdir(parents=True, exist_ok=True)
        token_path.touch(mode=0o600, exist_ok=True)
        token_path.chmod(0o600)
    except OSError as exc:
        return [f"could not create token file {token_path}: {exc}"]
    return []


def _validate_runtime_config(config: WrapperConfig) -> str:
    telegram = config.telegram
    if not telegram.enabled:
        return ""
    if not telegram.allowed_chat_ids:
        return (
            "teleagent: telegram.enabled is true, but telegram.allowed_chat_ids is empty.\n"
            "Add your Telegram chat id to allowed_chat_ids, or set telegram.enabled = false."
        )
    if telegram.debug_mode or telegram.resolved_token():
        return ""

    lines = [
        "teleagent: telegram.enabled is true, but no Telegram bot token was found.",
        f"Token lookup order: telegram.token, environment variable {telegram.token_env}, telegram.token_file.",
    ]
    if telegram.token_file:
        token_path = _expand_config_path(telegram.token_file)
        lines.extend(
            [
                f"Configured token file: {token_path}",
                "Paste your bot token into that file, then rerun teleagent.",
            ]
        )
        lines.append(f"Token file permission is managed automatically: {shlex.quote(str(token_path))}")
    else:
        lines.append("Set telegram.token_file, telegram.token, or the token environment variable.")
    lines.append("For local-only use, set telegram.enabled = false in teleagent.toml.")
    return "\n".join(lines)



def _print_doctor(config_path: Path, config: WrapperConfig, command: list[str]) -> None:
    telegram = config.telegram
    config_exists = config_path.exists()
    token_source = "missing"
    token_file_exists = False
    token_file_has_value = False
    if telegram.debug_mode:
        token_source = "debug_mode"
    elif telegram.token:
        token_source = "telegram.token"
    elif os.environ.get(telegram.token_env, ""):
        token_source = telegram.token_env
    elif telegram.token_file:
        token_path = _expand_config_path(telegram.token_file)
        token_file_exists = token_path.exists()
        if token_file_exists:
            try:
                token_file_has_value = bool(token_path.read_text(encoding="utf-8").strip())
            except OSError:
                token_file_has_value = False
        token_state = "ready" if token_file_has_value else "empty"
        if not token_file_exists:
            token_state = "missing"
        token_source = f"telegram.token_file ({token_state})"

    print(f"cwd: {Path.cwd()}")
    print(f"teleagent_home: {_teleagent_home()}")
    print(f"teleagent_config_dir: {_global_config_dir()}")
    print(f"teleagent_home_env: {os.environ.get(TELEAGENT_HOME_ENV, '')}")
    print(f"teleagent_config_dir_env: {os.environ.get(TELEAGENT_CONFIG_DIR_ENV, '')}")
    print(f"config_path: {config_path}")
    print(f"config_exists: {config_exists}")
    print(f"default_command: {list(config.default_command)!r}")
    print(f"effective_command: {command!r}")
    print(f"local_cursor_mode: {config.local_cursor_mode}")
    print(f"local_cursor_idle_seconds: {config.local_cursor_idle_seconds}")
    print(f"telegram.enabled: {telegram.enabled}")
    print(f"telegram.debug_mode: {telegram.debug_mode}")
    print(f"telegram.allowed_chat_ids: {list(telegram.allowed_chat_ids)!r}")
    print(f"telegram.token_source: {token_source}")
    if telegram.token_file:
        print(f"telegram.token_file: {_expand_config_path(telegram.token_file)}")
        print(f"telegram.token_file_exists: {token_file_exists}")
        print(f"telegram.token_file_has_value: {token_file_has_value}")
    print(f"telegram.history_path: {telegram.history_path}")
    print(f"telegram.raw_history_path: {telegram.raw_history_path}")
    print(f"telegram.diagnostic_log_path: {telegram.diagnostic_log_path}")
    print(f"telegram.diagnostic_retention_days: {telegram.diagnostic_retention_days}")
    print(f"telegram.diagnostic_snippet_chars: {telegram.diagnostic_snippet_chars}")
    print(f"telegram.output_mode: {telegram.output_mode}")
    print(f"telegram.output_sources: {list(telegram.output_sources)!r}")
    print(f"telegram.codex_state_root: {telegram.codex_state_root}")
    print(f"telegram.kimi_state_root: {telegram.kimi_state_root}")
    print(f"telegram.auto_summary: {telegram.auto_summary}")
    print(f"telegram.summary_threshold_chars: {telegram.summary_threshold_chars}")
    print(f"telegram.summary_max_chars: {telegram.summary_max_chars}")
    print(f"telegram.summary_timeout_seconds: {telegram.summary_timeout_seconds}")
    print(f"telegram.summary_fallback_chars: {telegram.summary_fallback_chars}")
    print(f"telegram.summary_background_wait_seconds: {telegram.summary_background_wait_seconds}")
    print(
        "telegram.summary_background_ready_stable_seconds: "
        f"{telegram.summary_background_ready_stable_seconds}"
    )
    print(
        "telegram.background_terminal_timeout_seconds: "
        f"{telegram.background_terminal_timeout_seconds}"
    )
    print(f"telegram.input_submit_delay_seconds: {telegram.input_submit_delay_seconds}")
    print(f"telegram.input_submit_keys: {list(telegram.input_submit_keys)!r}")
    print(f"telegram.slash_submit_delay_seconds: {telegram.slash_submit_delay_seconds}")
    print(f"telegram.slash_submit_keys: {list(telegram.slash_submit_keys)!r}")


if __name__ == "__main__":
    raise SystemExit(main())

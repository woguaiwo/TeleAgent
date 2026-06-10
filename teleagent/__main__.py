from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
import textwrap
from pathlib import Path

from .wrapper import WrapperConfig, _config_for_command, run_wrapped


LOCAL_CONFIG_PATH = Path("teleagent.toml")
DEFAULT_PROJECT_CONFIG = """
[settings]
buffer_size = 8192
log_matches = true
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


def _global_config_path() -> Path:
    return Path.home() / ".config" / "teleagent" / "teleagent.toml"


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
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run after --, for example: -- codex",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        return Path(explicit_path).expanduser(), "", ""
    env_path = os.environ.get("TELEAGENT_CONFIG", "")
    if env_path:
        return Path(env_path).expanduser(), "", ""

    if local_config.exists():
        return local_config, "", ""

    if not create_local:
        return local_config, "", ""

    expanded_global = global_config.expanduser()
    if expanded_global.exists():
        try:
            local_config.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(expanded_global, local_config)
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
    (path.parent / ".teleagent").mkdir(exist_ok=True)
    path.write_text(textwrap.dedent(DEFAULT_PROJECT_CONFIG).lstrip(), encoding="utf-8")


def _prepare_runtime_files(config: WrapperConfig) -> list[str]:
    telegram = config.telegram
    if not telegram.enabled or telegram.debug_mode or not telegram.token_file:
        return []

    token_path = Path(telegram.token_file).expanduser()
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
        token_path = Path(telegram.token_file).expanduser()
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
        token_path = Path(telegram.token_file).expanduser()
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
    print(f"config_path: {config_path}")
    print(f"config_exists: {config_exists}")
    print(f"default_command: {list(config.default_command)!r}")
    print(f"effective_command: {command!r}")
    print(f"telegram.enabled: {telegram.enabled}")
    print(f"telegram.debug_mode: {telegram.debug_mode}")
    print(f"telegram.allowed_chat_ids: {list(telegram.allowed_chat_ids)!r}")
    print(f"telegram.token_source: {token_source}")
    if telegram.token_file:
        print(f"telegram.token_file: {Path(telegram.token_file).expanduser()}")
        print(f"telegram.token_file_exists: {token_file_exists}")
        print(f"telegram.token_file_has_value: {token_file_has_value}")
    print(f"telegram.history_path: {telegram.history_path}")
    print(f"telegram.raw_history_path: {telegram.raw_history_path}")
    print(f"telegram.output_mode: {telegram.output_mode}")
    print(f"telegram.output_sources: {list(telegram.output_sources)!r}")
    print(f"telegram.codex_state_root: {telegram.codex_state_root}")
    print(f"telegram.kimi_state_root: {telegram.kimi_state_root}")
    print(f"telegram.auto_summary: {telegram.auto_summary}")
    print(f"telegram.summary_threshold_chars: {telegram.summary_threshold_chars}")
    print(f"telegram.summary_max_chars: {telegram.summary_max_chars}")
    print(f"telegram.summary_timeout_seconds: {telegram.summary_timeout_seconds}")
    print(f"telegram.summary_fallback_chars: {telegram.summary_fallback_chars}")


if __name__ == "__main__":
    raise SystemExit(main())

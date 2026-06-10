from __future__ import annotations

import codecs
import os
import pty
import re
import select
import signal
import sys
import termios
import time
import tomllib
import tty
from dataclasses import dataclass, replace
from pathlib import Path

from .telegram import TelegramBridge, TelegramConfig, TelegramInput, TelegramInputKind


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    reply: str
    once: bool = False
    delay_seconds: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, object], index: int) -> "Rule":
        pattern = data.get("pattern")
        reply = data.get("reply")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"rules[{index}].pattern must be a non-empty string")
        if not isinstance(reply, str):
            raise ValueError(f"rules[{index}].reply must be a string")

        name = data.get("name")
        once = data.get("once", False)
        delay_seconds = data.get("delay_seconds", 0.0)
        if name is not None and not isinstance(name, str):
            raise ValueError(f"rules[{index}].name must be a string")
        if not isinstance(once, bool):
            raise ValueError(f"rules[{index}].once must be a boolean")
        if not isinstance(delay_seconds, int | float):
            raise ValueError(f"rules[{index}].delay_seconds must be a number")

        return cls(
            name=name or f"rule-{index + 1}",
            pattern=re.compile(pattern, re.IGNORECASE | re.MULTILINE),
            reply=reply,
            once=once,
            delay_seconds=float(delay_seconds),
        )


@dataclass(frozen=True)
class WrapperConfig:
    rules: tuple[Rule, ...]
    buffer_size: int = 8192
    log_matches: bool = True
    default_command: tuple[str, ...] = ()
    telegram: TelegramConfig = TelegramConfig()

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "WrapperConfig":
        config_path = Path(path)
        if not config_path.exists():
            return cls(rules=())

        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)

        rules_raw = raw.get("rules", [])
        if not isinstance(rules_raw, list):
            raise ValueError("rules must be a list of tables")

        rules: list[Rule] = []
        for index, item in enumerate(rules_raw):
            if not isinstance(item, dict):
                raise ValueError(f"rules[{index}] must be a table")
            rules.append(Rule.from_dict(item, index))

        settings = raw.get("settings", {})
        if not isinstance(settings, dict):
            raise ValueError("settings must be a table")

        buffer_size = settings.get("buffer_size", 8192)
        log_matches = settings.get("log_matches", True)
        default_command_raw = settings.get("default_command", [])
        if not isinstance(buffer_size, int) or buffer_size < 256:
            raise ValueError("settings.buffer_size must be an integer >= 256")
        if not isinstance(log_matches, bool):
            raise ValueError("settings.log_matches must be a boolean")
        if not isinstance(default_command_raw, list) or not all(
            isinstance(item, str) and item for item in default_command_raw
        ):
            raise ValueError("settings.default_command must be a list of non-empty strings")

        telegram_raw = raw.get("telegram", {})
        if not isinstance(telegram_raw, dict):
            raise ValueError("telegram must be a table")

        return cls(
            rules=tuple(rules),
            buffer_size=buffer_size,
            log_matches=log_matches,
            default_command=tuple(default_command_raw),
            telegram=TelegramConfig.from_dict(telegram_raw),
        )


def run_wrapped(
    command: list[str],
    *,
    config: WrapperConfig,
    dry_run: bool = False,
) -> int:
    master_fd, slave_fd = pty.openpty()
    child_pid = os.fork()
    if child_pid == 0:
        _run_child(command, slave_fd)

    os.close(slave_fd)
    old_stdin_attrs: list[object] | None = None
    read_stdin = sys.stdin.isatty()
    if read_stdin:
        old_stdin_attrs = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())

    old_winch = signal.getsignal(signal.SIGWINCH)
    signal.signal(signal.SIGWINCH, lambda signum, frame: _resize_pty(master_fd))
    _resize_pty(master_fd)

    used_once: set[str] = set()
    output_buffer = ""
    config = _config_for_command(config, command)
    telegram = TelegramBridge(config.telegram)
    local_input_tracker = _LocalInputTracker()
    telegram.start()
    if telegram.enabled:
        mode = "debug" if config.telegram.debug_mode else "telegram"
        print(
            f"\r\n[teleagent] {mode} bridge enabled; "
            f"allowed_chat_ids={list(config.telegram.allowed_chat_ids)!r}\r\n",
            file=sys.stderr,
            end="",
            flush=True,
        )

    try:
        while True:
            read_fds = [master_fd]
            if read_stdin:
                read_fds.append(sys.stdin.fileno())
            if telegram.enabled:
                read_fds.append(telegram.read_fd)
            readable, _, _ = select.select(read_fds, [], [], 0.5)
            for error in telegram.pop_errors():
                print(f"\r\n[teleagent] telegram error: {error}\r\n", file=sys.stderr)
            summary_prompt = telegram.flush_idle_output()
            if summary_prompt:
                telegram.mark_injected_prompt(summary_prompt)
                _submit_summary_prompt(master_fd, summary_prompt, config.telegram)
            if master_fd in readable:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break

                os.write(sys.stdout.fileno(), chunk)
                decoded_chunk = chunk.decode(errors="ignore")
                telegram.record_output(decoded_chunk)
                output_buffer = (output_buffer + decoded_chunk)[
                    -config.buffer_size :
                ]
                matched = _maybe_reply(
                    master_fd,
                    output_buffer,
                    config=config,
                    used_once=used_once,
                    dry_run=dry_run,
                )
                if matched:
                    output_buffer = ""
                elif telegram.maybe_forward_output(output_buffer):
                    output_buffer = ""
            if read_stdin and sys.stdin.fileno() in readable:
                user_input = os.read(sys.stdin.fileno(), 4096)
                if not user_input:
                    break
                for completed_input in local_input_tracker.feed(user_input):
                    telegram.mark_user_input(completed_input)
                os.write(master_fd, user_input)

            if telegram.enabled and telegram.read_fd in readable:
                for action in telegram.drain_replies():
                    if action.kind == TelegramInputKind.SEND:
                        menu_action = telegram.consume_menu_choice(action.text)
                        if menu_action is not None:
                            action = menu_action
                    print(
                        f"\r\n[teleagent] telegram -> cli: {_describe_telegram_input(action)}\r\n",
                        file=sys.stderr,
                        end="",
                        flush=True,
                    )
                    if action.kind == TelegramInputKind.COMMAND:
                        telegram.handle_command(action.text)
                        continue
                    if action.kind in (TelegramInputKind.SEND, TelegramInputKind.TYPE):
                        telegram.mark_user_input(action.text)
                    _write_telegram_input(master_fd, action, config.telegram)
    finally:
        telegram.close()
        if old_stdin_attrs is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_stdin_attrs)
        signal.signal(signal.SIGWINCH, old_winch)
        try:
            os.close(master_fd)
        except OSError:
            pass

    _, status = os.waitpid(child_pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def _config_for_command(config: WrapperConfig, command: list[str]) -> WrapperConfig:
    if not command or not _is_kimi_command(command[0]):
        return config
    telegram = config.telegram
    if not telegram.enabled:
        return config
    if "kimi_wire" in telegram.output_sources or "kimi_context" in telegram.output_sources:
        return config
    if telegram.output_sources != ("terminal",):
        return config
    return replace(
        config,
        telegram=replace(telegram, output_sources=("kimi_wire", "terminal")),
    )


def _is_kimi_command(command: str) -> bool:
    name = Path(command).name.lower()
    return name in {"kimi", "kimi-code", "kimi_cli", "kimi-cli"}


def _run_child(command: list[str], slave_fd: int) -> None:
    os.setsid()
    os.dup2(slave_fd, 0)
    os.dup2(slave_fd, 1)
    os.dup2(slave_fd, 2)
    if slave_fd > 2:
        os.close(slave_fd)

    try:
        os.execvp(command[0], command)
    except FileNotFoundError:
        print(f"teleagent: command not found: {command[0]}", file=sys.stderr)
        raise SystemExit(127)


class _LocalInputTracker:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._escape_sequence = ""

    def feed(self, data: bytes) -> list[str]:
        completed: list[str] = []
        for char in self._decoder.decode(data):
            if self._consume_escape_char(char):
                continue
            if char == "\x1b":
                self._escape_sequence = char
                continue
            if char in ("\r", "\n"):
                stripped = self._buffer.strip()
                if stripped:
                    completed.append(stripped)
                self._buffer = ""
                continue
            if char in ("\b", "\x7f"):
                self._buffer = self._buffer[:-1]
                continue
            if char in ("\x03", "\x04"):
                self._buffer = ""
                continue
            if char == "\t" or ord(char) >= 32:
                self._buffer += char
        return completed

    def _consume_escape_char(self, char: str) -> bool:
        if not self._escape_sequence:
            return False
        self._escape_sequence += char
        if len(self._escape_sequence) == 2:
            if char not in "[O":
                self._escape_sequence = ""
            return True
        if self._escape_sequence.startswith("\x1b[") and len(self._escape_sequence) >= 3 and "@" <= char <= "~":
            self._escape_sequence = ""
        elif self._escape_sequence.startswith("\x1bO") and len(self._escape_sequence) >= 3:
            self._escape_sequence = ""
        elif len(self._escape_sequence) > 16:
            self._escape_sequence = ""
        return True


def _maybe_reply(
    master_fd: int,
    output_buffer: str,
    *,
    config: WrapperConfig,
    used_once: set[str],
    dry_run: bool,
) -> bool:
    for rule in config.rules:
        if rule.once and rule.name in used_once:
            continue
        if not rule.pattern.search(output_buffer):
            continue

        if rule.delay_seconds:
            time.sleep(rule.delay_seconds)

        reply = rule.reply
        if not reply.endswith("\n"):
            reply += "\n"

        if config.log_matches:
            status = "dry-run matched" if dry_run else "matched"
            print(
                f"\r\n[teleagent] {status} {rule.name!r}; reply={rule.reply!r}\r\n",
                file=sys.stderr,
                end="",
                flush=True,
            )

        if not dry_run:
            os.write(master_fd, reply.encode())
        if rule.once:
            used_once.add(rule.name)
        return True
    return False


def _resize_pty(master_fd: int) -> None:
    if not sys.stdin.isatty():
        return
    try:
        size = os.get_terminal_size(sys.stdin.fileno())
    except OSError:
        return

    import fcntl
    import struct

    packed = struct.pack("HHHH", size.lines, size.columns, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, packed)


def _write_telegram_input(
    master_fd: int,
    action: TelegramInput,
    telegram_config: TelegramConfig,
) -> None:
    if action.kind == TelegramInputKind.COMMAND:
        return
    if action.kind == TelegramInputKind.IGNORE:
        return
    if action.kind == TelegramInputKind.SEND:
        os.write(master_fd, action.text.encode())
        submit_keys = _submit_keys_for_send(action.text, telegram_config)
        submit_delay = _submit_delay_for_send(action.text, telegram_config)
        _submit_telegram_input(
            master_fd,
            telegram_config,
            submit_keys=submit_keys,
            delay_seconds=submit_delay,
            after_text=True,
        )
        return
    if action.kind == TelegramInputKind.TYPE:
        os.write(master_fd, action.text.encode())
        return
    if action.kind == TelegramInputKind.ENTER:
        _submit_telegram_input(
            master_fd,
            telegram_config,
            submit_keys=telegram_config.input_submit_keys,
            delay_seconds=telegram_config.input_submit_delay_seconds,
            after_text=False,
        )
        return
    if action.kind == TelegramInputKind.KEY:
        _write_key_sequence_list(master_fd, _split_key_names(action.text))


def _submit_summary_prompt(
    master_fd: int,
    prompt: str,
    telegram_config: TelegramConfig,
) -> None:
    os.write(master_fd, prompt.encode())
    if telegram_config.summary_submit_delay_seconds:
        time.sleep(telegram_config.summary_submit_delay_seconds)
    _write_key_sequence_list(master_fd, telegram_config.summary_submit_keys)


def _submit_telegram_input(
    master_fd: int,
    telegram_config: TelegramConfig,
    *,
    submit_keys: tuple[str, ...],
    delay_seconds: float,
    after_text: bool,
) -> None:
    if after_text and delay_seconds:
        time.sleep(delay_seconds)
    _write_key_sequence_list(master_fd, submit_keys)


def _submit_keys_for_send(text: str, telegram_config: TelegramConfig) -> tuple[str, ...]:
    stripped = text.strip()
    if _is_interactive_slash_command(stripped):
        return telegram_config.slash_submit_keys
    return telegram_config.input_submit_keys


def _submit_delay_for_send(text: str, telegram_config: TelegramConfig) -> float:
    stripped = text.strip()
    if _is_interactive_slash_command(stripped):
        return telegram_config.slash_submit_delay_seconds
    return telegram_config.input_submit_delay_seconds


def _is_interactive_slash_command(text: str) -> bool:
    return bool(re.fullmatch(r"/(?:model|resume|sessions)(?:\s+.*)?", text))


def _write_key_sequence_list(master_fd: int, keys: tuple[str, ...]) -> None:
    for key in keys:
        sequence = _key_sequence(key)
        if sequence is not None:
            os.write(master_fd, sequence)
            time.sleep(0.05)


def _split_key_names(text: str) -> tuple[str, ...]:
    return tuple(part for part in re.split(r"[\s,]+", text.strip()) if part)


def _key_sequence(name: str) -> bytes | None:
    keys = {
        "enter": b"\r",
        "return": b"\r",
        "ctrl-m": b"\r",
        "linefeed": b"\n",
        "lf": b"\n",
        "ctrl-j": b"\n",
        "esc": b"\x1b",
        "escape": b"\x1b",
        "tab": b"\t",
        "backspace": b"\x7f",
        "ctrl-c": b"\x03",
        "ctrl-d": b"\x04",
        "up": b"\x1b[A",
        "down": b"\x1b[B",
        "right": b"\x1b[C",
        "left": b"\x1b[D",
    }
    return keys.get(name.lower())


def _describe_telegram_input(action: TelegramInput) -> str:
    if action.kind == TelegramInputKind.COMMAND:
        return f"command {action.text!r}"
    if action.kind == TelegramInputKind.IGNORE:
        return f"ignore {action.text!r}"
    if action.kind == TelegramInputKind.SEND:
        return f"send {action.text!r} + Enter"
    if action.kind == TelegramInputKind.TYPE:
        return f"type {action.text!r}"
    if action.kind == TelegramInputKind.ENTER:
        return "Enter"
    return f"key {action.text!r}"

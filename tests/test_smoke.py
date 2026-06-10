from __future__ import annotations

import json
import hashlib
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest
import os
from datetime import datetime, timezone
from pathlib import Path

from teleagent.telegram import (
    SelectionMenu,
    TelegramBridge,
    TelegramConfig,
    TelegramInput,
    TelegramInputKind,
    _CodexRolloutOutputSource,
    _KimiContextOutputSource,
    _KimiWireOutputSource,
    _clean_terminal_text,
    _extract_selection_menu,
    _format_for_mobile,
    _remove_recent_user_echoes,
    _terminal_chunk_has_pending_signal,
    _terminal_raw_has_active_status,
    _terminal_snapshot_text,
    parse_telegram_input,
)
from teleagent.__main__ import _resolve_config_path, _resolve_config_path_with_status
from teleagent.wrapper import (
    WrapperConfig,
    _LocalInputTracker,
    _config_for_command,
    _write_telegram_input,
)


ROOT = Path(__file__).resolve().parents[1]


class SmokeTest(unittest.TestCase):
    def test_fake_cli_auto_reply(self) -> None:
        env = os.environ.copy()
        env["TELEGRAM_BOT_TOKEN"] = "fake-token"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "teleagent",
                "-c",
                str(ROOT / "examples" / "teleagent.toml"),
                "--",
                sys.executable,
                str(ROOT / "examples" / "fake_cli.py"),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            env=env,
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("first=y", result.stdout)
        self.assertIn("second=1", result.stdout)
        self.assertIn("matched 'generic-yes-no-continue'", result.stdout)

    def test_default_command_runs_when_no_command_is_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "teleagent.toml"
            config_path.write_text(
                f"""
[settings]
default_command = ["{sys.executable}", "{ROOT / "examples" / "fake_cli.py"}"]

[telegram]
enabled = false

[[rules]]
name = "yes"
pattern = "continue\\\\?\\\\s*\\\\[y/N\\\\]"
reply = "y"

[[rules]]
name = "choice"
pattern = "enter choice\\\\s*:"
reply = "1"
""",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "teleagent",
                    "-c",
                    str(config_path),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("first=y", result.stdout)
        self.assertIn("second=1", result.stdout)

    def test_default_command_inherits_invocation_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "project"
            config_path = tmp_path / "config" / "teleagent.toml"
            workdir.mkdir()
            config_path.parent.mkdir()
            config_path.write_text(
                f"""
[settings]
default_command = ["{sys.executable}", "-c", "import os; print(os.getcwd())"]

[telegram]
enabled = false
""",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["TELEAGENT_CONFIG"] = str(config_path)
            env["PYTHONPATH"] = str(ROOT)
            result = subprocess.run(
                [sys.executable, "-m", "teleagent"],
                cwd=workdir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                env=env,
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(str(workdir), result.stdout)

    def test_doctor_prints_effective_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "teleagent.toml"
            config_path.write_text(
                f"""
[settings]
default_command = ["{sys.executable}", "-c", "print('ok')"]

[telegram]
enabled = true
debug_mode = true
""",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "teleagent",
                    "-c",
                    str(config_path),
                    "--doctor",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("config_exists: True", result.stdout)
        self.assertIn("telegram.enabled: True", result.stdout)
        self.assertIn("telegram.debug_mode: True", result.stdout)
        self.assertIn("telegram.output_sources: ['terminal']", result.stdout)
        self.assertIn("telegram.codex_state_root: ~/.codex", result.stdout)
        self.assertIn("telegram.kimi_state_root: ~/.kimi", result.stdout)
        self.assertIn("telegram.summary_timeout_seconds: 30.0", result.stdout)
        self.assertIn("telegram.summary_fallback_chars: 3500", result.stdout)
        self.assertIn("telegram.slash_submit_delay_seconds: 0.2", result.stdout)
        self.assertIn("telegram.slash_submit_keys: ['enter']", result.stdout)
        self.assertIn("effective_command:", result.stdout)

    def test_doctor_reports_empty_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            token_path = workdir / ".teleagent" / "telegram-token"
            config_path = workdir / "teleagent.toml"
            config_path.write_text(
                """
[settings]
default_command = ["codex"]

[telegram]
enabled = true
token_file = ".teleagent/telegram-token"
allowed_chat_ids = [123]
""",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.pop("TELEGRAM_BOT_TOKEN", None)
            env["PYTHONPATH"] = str(ROOT)

            result = subprocess.run(
                [sys.executable, "-m", "teleagent", "--doctor"],
                cwd=workdir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                env=env,
            )
            token_file_exists = token_path.exists()
            token_file_mode = token_path.stat().st_mode & 0o777

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("telegram.token_source: telegram.token_file (empty)", result.stdout)
        self.assertIn("telegram.token_file_exists: True", result.stdout)
        self.assertIn("telegram.token_file_has_value: False", result.stdout)
        self.assertTrue(token_file_exists)
        self.assertEqual(token_file_mode, 0o600)

    def test_missing_project_token_exits_with_clear_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            config_path = workdir / "teleagent.toml"
            config_path.write_text(
                f"""
[settings]
default_command = ["{sys.executable}", "-c", "print('should not start')"]

[telegram]
enabled = true
token_file = ".teleagent/telegram-token"
allowed_chat_ids = [123]
""",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.pop("TELEGRAM_BOT_TOKEN", None)
            env["PYTHONPATH"] = str(ROOT)

            result = subprocess.run(
                [sys.executable, "-m", "teleagent"],
                cwd=workdir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                env=env,
            )
            token_dir_exists = (workdir / ".teleagent").is_dir()
            token_file_exists = (workdir / ".teleagent" / "telegram-token").exists()

        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("no Telegram bot token was found", result.stdout)
        self.assertIn(".teleagent/telegram-token", result.stdout)
        self.assertIn("Paste your bot token into that file", result.stdout)
        self.assertNotIn("Traceback", result.stdout)
        self.assertNotIn("should not start", result.stdout)
        self.assertTrue(token_dir_exists)
        self.assertTrue(token_file_exists)

    def test_debug_telegram_forwards_cli_slash_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            inbox = workdir / "inbox.txt"
            outbox = workdir / "outbox.jsonl"
            child = workdir / "child.py"
            child.write_text(
                """
import sys

print("ready", flush=True)
for line in sys.stdin:
    line = line.rstrip("\\r\\n")
    print(f"child received: {line}", flush=True)
    if line == "/model gpt-5":
        break
""",
                encoding="utf-8",
            )
            config_path = workdir / "teleagent.toml"
            config_path.write_text(
                f"""
[settings]
default_command = ["{sys.executable}", "{child}"]

[telegram]
enabled = true
debug_mode = true
debug_inbox_path = "{inbox}"
debug_outbox_path = "{outbox}"
idle_forward_seconds = 0.0
summary_threshold_chars = 1000
input_submit_delay_seconds = 0.0
input_submit_keys = ["linefeed"]
""",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            process = subprocess.Popen(
                [sys.executable, "-m", "teleagent", "-c", str(config_path)],
                cwd=workdir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if outbox.exists() and "ready" in outbox.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.05)

                with inbox.open("a", encoding="utf-8") as handle:
                    handle.write("/resume\n")
                    handle.write("/model gpt-5\n")

                stdout, _ = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

            records = _read_debug_records(outbox)

        self.assertEqual(process.returncode, 0, stdout)
        outbox_text = "\n".join(str(record.get("text", "")) for record in records)
        self.assertIn("child received: /resume", outbox_text)
        self.assertIn("child received: /model gpt-5", outbox_text)
        self.assertNotIn("TeleAgent 控制命令", outbox_text)

    def test_debug_telegram_menu_choice_sends_arrow_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            inbox = workdir / "inbox.txt"
            outbox = workdir / "outbox.jsonl"
            child = workdir / "menu_child.py"
            child.write_text(
                """
import os
import sys
import termios
import tty

print("Select model", flush=True)
print("❯ gpt-5.5 xhigh", flush=True)
print("  gpt-5.5 medium", flush=True)
print("  gpt-5.4-mini", flush=True)

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
try:
    tty.setraw(fd)
    chunks = []
    while True:
        chunk = os.read(fd, 1)
        chunks.append(chunk)
        if chunk in (b"\\r", b"\\n"):
            break
    data = b"".join(chunks)
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)

print("keys=" + data.hex(), flush=True)
""",
                encoding="utf-8",
            )
            config_path = workdir / "teleagent.toml"
            config_path.write_text(
                f"""
[settings]
default_command = ["{sys.executable}", "{child}"]

[telegram]
enabled = true
debug_mode = true
debug_inbox_path = "{inbox}"
debug_outbox_path = "{outbox}"
idle_forward_seconds = 0.0
summary_threshold_chars = 1000
input_submit_delay_seconds = 0.0
input_submit_keys = ["linefeed"]
""",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            process = subprocess.Popen(
                [sys.executable, "-m", "teleagent", "-c", str(config_path)],
                cwd=workdir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
            )
            try:
                deadline = time.monotonic() + 5
                saw_menu = False
                while time.monotonic() < deadline:
                    records = _read_debug_records(outbox)
                    if any("请选择模型" in str(record.get("text", "")) for record in records):
                        saw_menu = True
                        break
                    time.sleep(0.05)
                self.assertTrue(saw_menu, _read_debug_records(outbox))

                with inbox.open("a", encoding="utf-8") as handle:
                    handle.write("3\n")

                stdout, _ = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

            records = _read_debug_records(outbox)

        self.assertEqual(process.returncode, 0, stdout)
        outbox_text = "\n".join(str(record.get("text", "")) for record in records)
        self.assertIn("请选择模型", outbox_text)
        self.assertIn("3. gpt-5.4-mini", outbox_text)
        self.assertIn("keys=1b5b421b5b420d", outbox_text)


class ConfigTest(unittest.TestCase):
    def test_telegram_config_parses(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as handle:
            handle.write(
                """
[settings]
default_command = ["codex"]

[telegram]
enabled = true
token = "fake-token"
allowed_chat_ids = [123]
forward_patterns = ["Final answer:\\\\s*(.*)"]
history_path = "tmp-history.log"
raw_history_path = "tmp-raw.log"
output_mode = "all"
input_submit_delay_seconds = 0.0
input_submit_keys = ["enter", "linefeed"]
slash_submit_delay_seconds = 0.15
slash_submit_keys = ["enter"]
summary_submit_delay_seconds = 0.1
summary_submit_keys = ["linefeed"]
"""
            )
            handle.flush()

            config = WrapperConfig.load(handle.name)

        self.assertTrue(config.telegram.enabled)
        self.assertEqual(config.default_command, ("codex",))
        self.assertEqual(config.telegram.resolved_token(), "fake-token")
        self.assertEqual(config.telegram.allowed_chat_ids, (123,))
        self.assertEqual(len(config.telegram.forward_patterns), 1)
        self.assertEqual(config.telegram.history_path, "tmp-history.log")
        self.assertEqual(config.telegram.raw_history_path, "tmp-raw.log")
        self.assertEqual(config.telegram.output_mode, "all")
        self.assertEqual(config.telegram.output_sources, ("terminal",))
        self.assertEqual(config.telegram.codex_state_root, "~/.codex")
        self.assertEqual(config.telegram.kimi_state_root, "~/.kimi")
        self.assertEqual(config.telegram.input_submit_delay_seconds, 0.0)
        self.assertEqual(config.telegram.input_submit_keys, ("enter", "linefeed"))
        self.assertEqual(config.telegram.slash_submit_delay_seconds, 0.15)
        self.assertEqual(config.telegram.slash_submit_keys, ("enter",))
        self.assertEqual(config.telegram.summary_submit_delay_seconds, 0.1)
        self.assertEqual(config.telegram.summary_submit_keys, ("linefeed",))
        self.assertEqual(config.telegram.summary_timeout_seconds, 30.0)
        self.assertEqual(config.telegram.summary_fallback_chars, 3500)

    def test_output_source_aliases_include_kimi_modes(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as handle:
            handle.write(
                """
[telegram]
enabled = true
debug_mode = true
allowed_chat_ids = [0]
output_sources = ["ui", "codex_rollout", "kimi", "kimi_wire"]
kimi_state_root = "~/.kimi"
"""
            )
            handle.flush()

            config = WrapperConfig.load(handle.name)

        self.assertEqual(
            config.telegram.output_sources,
            ("terminal", "codex_rollout", "kimi_wire"),
        )
        self.assertEqual(config.telegram.kimi_state_root, "~/.kimi")

    def test_kimi_command_uses_wire_before_terminal_by_default(self) -> None:
        config = WrapperConfig(
            rules=(),
            telegram=TelegramConfig(
                enabled=True,
                debug_mode=True,
                allowed_chat_ids=(0,),
                output_sources=("terminal",),
            ),
        )

        effective = _config_for_command(config, ["kimi"])

        self.assertEqual(effective.telegram.output_sources, ("kimi_wire", "terminal"))

    def test_non_kimi_command_keeps_terminal_default(self) -> None:
        config = WrapperConfig(
            rules=(),
            telegram=TelegramConfig(
                enabled=True,
                debug_mode=True,
                allowed_chat_ids=(0,),
                output_sources=("terminal",),
            ),
        )

        effective = _config_for_command(config, ["codex"])

        self.assertEqual(effective.telegram.output_sources, ("terminal",))

    def test_telegram_debug_config_does_not_require_token(self) -> None:
        previous_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        try:
            os.environ["TELEGRAM_BOT_TOKEN"] = "fake-env-token"
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "teleagent.toml"
                config_path.write_text(
                    f"""
[telegram]
enabled = true
debug_mode = true
debug_inbox_path = "{tmp}/inbox.txt"
debug_outbox_path = "{tmp}/outbox.jsonl"
""",
                    encoding="utf-8",
                )

                config = WrapperConfig.load(config_path)
        finally:
            if previous_token is None:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            else:
                os.environ["TELEGRAM_BOT_TOKEN"] = previous_token

        self.assertTrue(config.telegram.enabled)
        self.assertTrue(config.telegram.debug_mode)
        self.assertEqual(config.telegram.allowed_chat_ids, (0,))
        self.assertEqual(config.telegram.resolved_token(), "")

    def test_telegram_token_file_is_used_without_export(self) -> None:
        previous_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        try:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            with tempfile.TemporaryDirectory() as tmp:
                token_path = Path(tmp) / "telegram-token"
                token_path.write_text("file-token\n", encoding="utf-8")
                config_path = Path(tmp) / "teleagent.toml"
                config_path.write_text(
                    f"""
[telegram]
enabled = true
token_file = "{token_path}"
allowed_chat_ids = [123]
""",
                    encoding="utf-8",
                )

                config = WrapperConfig.load(config_path)
                resolved_token = config.telegram.resolved_token()
        finally:
            if previous_token is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = previous_token

        self.assertEqual(resolved_token, "file-token")

    def test_telegram_token_file_expands_environment_variables(self) -> None:
        previous_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        previous_home = os.environ.get("TELEAGENT_HOME")
        try:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            with tempfile.TemporaryDirectory() as tmp:
                teleagent_home = Path(tmp) / "teleagent-home"
                token_path = teleagent_home / ".config" / "teleagent" / "telegram-token"
                token_path.parent.mkdir(parents=True)
                token_path.write_text("env-file-token\n", encoding="utf-8")
                os.environ["TELEAGENT_HOME"] = str(teleagent_home)
                config_path = Path(tmp) / "teleagent.toml"
                config_path.write_text(
                    """
[telegram]
enabled = true
token_file = "$TELEAGENT_HOME/.config/teleagent/telegram-token"
allowed_chat_ids = [123]
""",
                    encoding="utf-8",
                )

                config = WrapperConfig.load(config_path)
                resolved_token = config.telegram.resolved_token()
        finally:
            if previous_token is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = previous_token
            if previous_home is None:
                os.environ.pop("TELEAGENT_HOME", None)
            else:
                os.environ["TELEAGENT_HOME"] = previous_home

        self.assertEqual(resolved_token, "env-file-token")

    def test_explicit_config_path_expands_user(self) -> None:
        self.assertEqual(_resolve_config_path("~/teleagent.toml"), Path.home() / "teleagent.toml")

    def test_missing_local_config_is_copied_from_global_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            local_path = tmp_path / "project" / "teleagent.toml"
            global_path = tmp_path / "global" / "teleagent.toml"
            local_path.parent.mkdir()
            global_path.parent.mkdir()
            global_path.write_text(
                '[settings]\ndefault_command = ["codex"]\n',
                encoding="utf-8",
            )

            resolved, initialized_from, copy_error = _resolve_config_path_with_status(
                None,
                local_path=local_path,
                global_path=global_path,
            )

            self.assertEqual(resolved, local_path)
            self.assertEqual(initialized_from, str(global_path))
            self.assertEqual(copy_error, "")
            self.assertEqual(local_path.read_text(encoding="utf-8"), global_path.read_text(encoding="utf-8"))

    def test_global_config_copy_uses_project_local_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            local_path = tmp_path / "project" / "teleagent.toml"
            global_path = tmp_path / "global" / "teleagent.toml"
            local_path.parent.mkdir()
            global_path.parent.mkdir()
            global_path.write_text(
                f"""
[settings]
default_command = ["codex"]

[telegram]
enabled = true
token_file = "{tmp_path}/global/telegram-token"
allowed_chat_ids = [123]
""",
                encoding="utf-8",
            )

            resolved, initialized_from, copy_error = _resolve_config_path_with_status(
                None,
                local_path=local_path,
                global_path=global_path,
            )

            self.assertEqual(resolved, local_path)
            self.assertEqual(initialized_from, str(global_path))
            self.assertEqual(copy_error, "")
            self.assertTrue((local_path.parent / ".teleagent" / "telegram-token").exists())
            config = WrapperConfig.load(local_path)
            self.assertTrue(config.telegram.enabled)
            self.assertEqual(config.telegram.allowed_chat_ids, (123,))
            self.assertEqual(config.telegram.token_file, ".teleagent/telegram-token")
            self.assertNotIn(str(tmp_path / "global" / "telegram-token"), local_path.read_text(encoding="utf-8"))

    def test_missing_local_and_global_config_uses_builtin_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            local_path = tmp_path / "project" / "teleagent.toml"
            global_path = tmp_path / "global" / "teleagent.toml"
            local_path.parent.mkdir()
            global_path.parent.mkdir()

            resolved, initialized_from, copy_error = _resolve_config_path_with_status(
                None,
                local_path=local_path,
                global_path=global_path,
            )

            self.assertEqual(resolved, local_path)
            self.assertEqual(initialized_from, "built-in default template")
            self.assertEqual(copy_error, "")
            self.assertTrue(local_path.exists())
            self.assertTrue((local_path.parent / ".teleagent").is_dir())
            self.assertTrue((local_path.parent / ".teleagent" / "telegram-token").exists())
            config = WrapperConfig.load(local_path)
            self.assertEqual(config.default_command, ("codex",))
            self.assertFalse(config.telegram.enabled)
            self.assertEqual(config.telegram.allowed_chat_ids, ())
            self.assertEqual(config.telegram.token_file, ".teleagent/telegram-token")

    def test_existing_local_config_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            local_path = tmp_path / "project" / "teleagent.toml"
            global_path = tmp_path / "global" / "teleagent.toml"
            local_path.parent.mkdir()
            global_path.parent.mkdir()
            local_path.write_text('[settings]\ndefault_command = ["claude"]\n', encoding="utf-8")
            global_path.write_text('[settings]\ndefault_command = ["codex"]\n', encoding="utf-8")

            resolved, initialized_from, copy_error = _resolve_config_path_with_status(
                None,
                local_path=local_path,
                global_path=global_path,
            )

            self.assertEqual(resolved, local_path)
            self.assertEqual(initialized_from, "")
            self.assertEqual(copy_error, "")
            self.assertIn("claude", local_path.read_text(encoding="utf-8"))

    def test_env_config_skips_project_auto_copy(self) -> None:
        previous = os.environ.get("TELEAGENT_CONFIG")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                local_path = tmp_path / "project" / "teleagent.toml"
                global_path = tmp_path / "global" / "teleagent.toml"
                env_path = tmp_path / "env" / "teleagent.toml"
                local_path.parent.mkdir()
                global_path.parent.mkdir()
                env_path.parent.mkdir()
                os.environ["TELEAGENT_CONFIG"] = str(env_path)
                global_path.write_text('[settings]\ndefault_command = ["codex"]\n', encoding="utf-8")

                resolved, initialized_from, copy_error = _resolve_config_path_with_status(
                    None,
                    local_path=local_path,
                    global_path=global_path,
                )
        finally:
            if previous is None:
                os.environ.pop("TELEAGENT_CONFIG", None)
            else:
                os.environ["TELEAGENT_CONFIG"] = previous

        self.assertEqual(resolved, env_path)
        self.assertEqual(initialized_from, "")
        self.assertEqual(copy_error, "")
        self.assertFalse(local_path.exists())

    def test_explicit_config_skips_project_auto_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            local_path = tmp_path / "project" / "teleagent.toml"
            global_path = tmp_path / "global" / "teleagent.toml"
            explicit_path = tmp_path / "custom.toml"
            local_path.parent.mkdir()
            global_path.parent.mkdir()
            global_path.write_text('[settings]\ndefault_command = ["codex"]\n', encoding="utf-8")

            resolved, initialized_from, copy_error = _resolve_config_path_with_status(
                str(explicit_path),
                local_path=local_path,
                global_path=global_path,
            )

            self.assertEqual(resolved, explicit_path)
            self.assertEqual(initialized_from, "")
            self.assertEqual(copy_error, "")
            self.assertFalse(local_path.exists())

    def test_init_global_creates_home_config_and_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["HOME"] = tmp
            env.pop("TELEAGENT_HOME", None)
            env.pop("TELEAGENT_CONFIG_DIR", None)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "teleagent",
                    "--init-global",
                    "--global-default-command",
                    "kimi",
                    "--enable-telegram",
                    "--telegram-chat-id",
                    "123",
                    "--telegram-chat-id",
                    "456",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config_path = Path(tmp) / ".config" / "teleagent" / "teleagent.toml"
            token_path = Path(tmp) / ".config" / "teleagent" / "telegram-token"
            self.assertTrue(config_path.exists())
            self.assertTrue(token_path.exists())
            config = WrapperConfig.load(config_path)
            self.assertEqual(config.default_command, ("kimi",))
            self.assertTrue(config.telegram.enabled)
            self.assertEqual(config.telegram.allowed_chat_ids, (123, 456))
            self.assertEqual(config.telegram.token_file, str(token_path))

    def test_init_global_can_use_teleagent_home_instead_of_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            real_home = tmp_path / "real-home"
            teleagent_home = tmp_path / "teleagent-home"
            env = os.environ.copy()
            env["HOME"] = str(real_home)
            env["TELEAGENT_HOME"] = str(teleagent_home)
            env.pop("TELEAGENT_CONFIG_DIR", None)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "teleagent",
                    "--init-global",
                    "--global-default-command",
                    "codex",
                    "--enable-telegram",
                    "--telegram-chat-id",
                    "123",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config_path = teleagent_home / ".config" / "teleagent" / "teleagent.toml"
            token_path = teleagent_home / ".config" / "teleagent" / "telegram-token"
            self.assertTrue(config_path.exists())
            self.assertTrue(token_path.exists())
            self.assertFalse((real_home / ".config" / "teleagent").exists())
            config = WrapperConfig.load(config_path)
            self.assertEqual(config.telegram.token_file, str(token_path))

    def test_init_global_can_use_explicit_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            real_home = tmp_path / "real-home"
            config_dir = tmp_path / "custom-config"
            env = os.environ.copy()
            env["HOME"] = str(real_home)
            env["TELEAGENT_CONFIG_DIR"] = str(config_dir)
            env.pop("TELEAGENT_HOME", None)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "teleagent",
                    "--init-global",
                    "--global-default-command",
                    "kimi",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config_path = config_dir / "teleagent.toml"
            token_path = config_dir / "telegram-token"
            self.assertTrue(config_path.exists())
            self.assertTrue(token_path.exists())
            self.assertFalse((real_home / ".config" / "teleagent").exists())
            config = WrapperConfig.load(config_path)
            self.assertEqual(config.default_command, ("kimi",))
            self.assertEqual(config.telegram.token_file, str(token_path))

    def test_init_global_rejects_invalid_chat_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["HOME"] = tmp
            env.pop("TELEAGENT_HOME", None)
            env.pop("TELEAGENT_CONFIG_DIR", None)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "teleagent",
                    "--init-global",
                    "--telegram-chat-id",
                    "not-a-number",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("invalid --telegram-chat-id", result.stderr)
            self.assertFalse((Path(tmp) / ".config" / "teleagent" / "teleagent.toml").exists())


class TelegramInputTest(unittest.TestCase):
    def test_default_message_sends_and_enters(self) -> None:
        action = parse_telegram_input("hello")

        self.assertEqual(action.kind, TelegramInputKind.SEND)
        self.assertEqual(action.text, "hello")

    def test_type_command_does_not_submit(self) -> None:
        action = parse_telegram_input("/type hello")

        self.assertEqual(action.kind, TelegramInputKind.TYPE)
        self.assertEqual(action.text, "hello")

    def test_enter_command(self) -> None:
        action = parse_telegram_input("/enter")

        self.assertEqual(action.kind, TelegramInputKind.ENTER)

    def test_submit_command_alias(self) -> None:
        action = parse_telegram_input("/submit")

        self.assertEqual(action.kind, TelegramInputKind.ENTER)

    def test_start_command_is_ignored(self) -> None:
        action = parse_telegram_input("/start")

        self.assertEqual(action.kind, TelegramInputKind.IGNORE)

    def test_history_command(self) -> None:
        action = parse_telegram_input("/history")

        self.assertEqual(action.kind, TelegramInputKind.COMMAND)
        self.assertEqual(action.text, "/history")

    def test_history_command_with_bot_suffix(self) -> None:
        action = parse_telegram_input("/history@TeleAgentBot")

        self.assertEqual(action.kind, TelegramInputKind.COMMAND)
        self.assertEqual(action.text, "/history")

    def test_raw_history_command(self) -> None:
        action = parse_telegram_input("/rawhistory")

        self.assertEqual(action.kind, TelegramInputKind.COMMAND)
        self.assertEqual(action.text, "/rawhistory")

    def test_ta_prefixed_history_command(self) -> None:
        action = parse_telegram_input("/ta history")

        self.assertEqual(action.kind, TelegramInputKind.COMMAND)
        self.assertEqual(action.text, "/history")

    def test_ta_auto_start_command(self) -> None:
        action = parse_telegram_input("/ta auto start")

        self.assertEqual(action.kind, TelegramInputKind.COMMAND)
        self.assertEqual(action.text, "/auto start")

    def test_ta_auto_hours_command(self) -> None:
        action = parse_telegram_input("/ta auto 7.5")

        self.assertEqual(action.kind, TelegramInputKind.COMMAND)
        self.assertEqual(action.text, "/auto 7.5")

    def test_ta_auto_end_command(self) -> None:
        action = parse_telegram_input("/ta auto end")

        self.assertEqual(action.kind, TelegramInputKind.COMMAND)
        self.assertEqual(action.text, "/auto end")

    def test_model_slash_command_is_forwarded_to_cli(self) -> None:
        action = parse_telegram_input("/model gpt-5")

        self.assertEqual(action.kind, TelegramInputKind.SEND)
        self.assertEqual(action.text, "/model gpt-5")

    def test_model_slash_command_with_bot_suffix_is_forwarded_cleanly(self) -> None:
        action = parse_telegram_input("/model@TeleAgentBot gpt-5")

        self.assertEqual(action.kind, TelegramInputKind.SEND)
        self.assertEqual(action.text, "/model gpt-5")

    def test_resume_slash_command_is_forwarded_to_cli(self) -> None:
        action = parse_telegram_input("/resume")

        self.assertEqual(action.kind, TelegramInputKind.SEND)
        self.assertEqual(action.text, "/resume")


class TerminalCleanTest(unittest.TestCase):
    def test_extract_selection_menu_from_tui_lines(self) -> None:
        raw = (
            "\x1b[2mSelect model\x1b[0m\n"
            "\x1b[1m❯ gpt-5.5 xhigh\x1b[0m\n"
            "  gpt-5.5 medium\n"
            "  gpt-5.4-mini\n"
        )

        menu = _extract_selection_menu(raw)

        self.assertIsNotNone(menu)
        assert menu is not None
        self.assertEqual(menu.title, "请选择模型：")
        self.assertEqual(menu.options, ("gpt-5.5 xhigh", "gpt-5.5 medium", "gpt-5.4-mini"))
        self.assertEqual(menu.selected_index, 0)

    def test_extract_selection_menu_requires_selected_marker(self) -> None:
        raw = "1. gpt-5\n2. gpt-4\n"

        self.assertIsNone(_extract_selection_menu(raw))

    def test_codex_input_prompt_is_not_selection_menu(self) -> None:
        raw = (
            "╭───────────────────────────────────────╮\n"
            "│ model:     gpt-5.5 xhigh   /model to change │\n"
            "│ directory: /data/lyxie/TeleAgent      │\n"
            "╰───────────────────────────────────────╯\n"
            "› 今天天气如何？\n"
            "gpt-5.5 xhigh · /data/lyxie/TeleAgent\n"
        )

        self.assertIsNone(_extract_selection_menu(raw))

    def test_normal_ai_reply_with_arrow_is_not_selection_menu(self) -> None:
        raw = (
            "一句话结论：可以这样处理。\n"
            "▶ 先确认配置文件是否存在。\n"
            "- 再检查 token 文件。\n"
            "- 最后重启 teleagent。\n"
        )

        self.assertIsNone(_extract_selection_menu(raw))

    def test_ai_model_comparison_is_not_selection_menu(self) -> None:
        raw = (
            "可以这样理解这些模型：\n"
            "▶ gpt-5 适合复杂推理和代码修改。\n"
            "- gpt-4 适合一般对话。\n"
            "- claude 适合长文本阅读。\n"
        )

        self.assertIsNone(_extract_selection_menu(raw))

    def test_strong_cursor_in_plain_ai_text_is_not_selection_menu(self) -> None:
        raw = (
            "模型对比：\n"
            "❯ gpt-5 适合复杂推理。\n"
            "  gpt-4 适合一般对话。\n"
            "  claude 适合长文本。\n"
        )

        self.assertIsNone(_extract_selection_menu(raw))

    def test_strong_cursor_with_terminal_controls_can_be_selection_menu(self) -> None:
        raw = (
            "\x1b[10;1H❯ gpt-5\n"
            "\x1b[11;1H  gpt-4\n"
            "\x1b[12;1H  claude\n"
        )

        menu = _extract_selection_menu(raw)

        self.assertIsNotNone(menu)
        assert menu is not None
        self.assertEqual(menu.options, ("gpt-5", "gpt-4", "claude"))

    def test_terminal_snapshot_renders_overwritten_screen(self) -> None:
        raw = (
            "\x1b[1;1Hold title\x1b[2;1Hold option"
            "\x1b[1;1HSelect Model and Effort\x1b[K"
            "\x1b[2;1H› 3. gpt-5.4-mini (current)  Small model\x1b[K"
        )

        snapshot = _terminal_snapshot_text(raw)

        self.assertIn("Select Model and Effort", snapshot)
        self.assertIn("› 3. gpt-5.4-mini", snapshot)
        self.assertNotIn("old title", snapshot)
        self.assertNotIn("old option", snapshot)

    def test_extract_model_menu_from_codex_numbered_tui_screen(self) -> None:
        raw = (
            "\x1b[1;1HSelect Model and Effort"
            "\x1b[2;1HAccess legacy models by running codex -m <model_name>"
            "\x1b[4;1H1.gpt-5.5(default)Frontier model for complex coding, research, and real-world work."
            "\x1b[5;1H2.gpt-5.4Strong model for everyday coding."
            "\x1b[6;1H› 3. gpt-5.4-mini (current)  Small, fast, and cost-efficient model."
            "\x1b[7;1H4.gpt-5.3-codexCoding-optimized model."
            "\x1b[9;1HPress enter to confirm or esc to go back"
        )

        menu = _extract_selection_menu(raw)

        self.assertIsNotNone(menu)
        assert menu is not None
        self.assertEqual(menu.title, "请选择模型：")
        self.assertEqual(
            menu.options,
            (
                "gpt-5.5",
                "gpt-5.4",
                "gpt-5.4-mini",
                "gpt-5.3-codex",
            ),
        )
        self.assertEqual(menu.selected_index, 2)

    def test_extract_reasoning_menu_from_codex_numbered_tui_screen(self) -> None:
        raw = (
            "\x1b[1;1HSelect Reasoning Level for gpt-5.5"
            "\x1b[3;1H1.LowFast responses with lighter reasoning"
            "\x1b[4;1H› 2. Medium (default)  Balances speed and reasoning depth"
            "\x1b[5;1H3.HighGreater reasoning depth for complex problems"
            "\x1b[6;1H4.ExtrahighExtra high reasoning depth"
        )

        menu = _extract_selection_menu(raw)

        self.assertIsNotNone(menu)
        assert menu is not None
        self.assertEqual(menu.title, "请选择推理强度：")
        self.assertEqual(menu.options, ("Low", "Medium", "High", "Extra high"))
        self.assertEqual(menu.selected_index, 1)

    def test_extract_resume_menu_from_codex_alt_screen(self) -> None:
        raw = (
            "\x1b[?1049h\x1b[1;1HResume a previous session"
            "\x1b[3;2HType to search"
            "\x1b[5;3H❯ 13s ago     IMU-Synthesis"
            "\x1b[6;5H8h ago      Hi你好"
            "\x1b[10;1Henter resume   esc exit   ↑/↓ browse"
        )

        menu = _extract_selection_menu(raw)

        self.assertIsNotNone(menu)
        assert menu is not None
        self.assertEqual(menu.title, "请选择会话：")
        self.assertEqual(menu.options, ("13s ago IMU-Synthesis", "8h ago Hi你好"))
        self.assertEqual(menu.selected_index, 0)

    def test_extract_resume_menu_after_alt_screen_restores_prompt(self) -> None:
        raw = (
            "\x1b[?1049h\x1b[1;1HResume a previous session"
            "\x1b[3;2HType to search"
            "\x1b[5;3H❯ 38m ago     Motion-X"
            "\x1b[6;5H10h ago     Hi你好"
            "\x1b[10;1Henter resume   esc exit   ↑/↓ browse"
            "\x1b[?1049l\x1b[4;1H› Implement {feature}"
            "\x1b[6;3Hgpt-5.5 xhigh · /data/lyxie/Motion-X"
        )

        menu = _extract_selection_menu(raw)

        self.assertIsNotNone(menu)
        assert menu is not None
        self.assertEqual(menu.title, "请选择会话：")
        self.assertEqual(menu.options, ("38m ago Motion-X", "10h ago Hi你好"))
        self.assertEqual(menu.selected_index, 0)

    def test_extract_kimi_sessions_menu_from_boxed_panel(self) -> None:
        raw = (
            "\x1b[?1049h"
            "\x1b[1;1H┌────────────────────| Sessions |────────────────────┐"
            "\x1b[3;1H│ ❯ WHAM                                            │"
            "\x1b[4;1H│   22m ago · c9caecf5                              │"
            "\x1b[5;1H│   hi                                              │"
            "\x1b[6;1H│   40m ago · 451c3264                              │"
            "\x1b[7;1H│   1                                               │"
            "\x1b[8;1H│   44m ago · 4a4914b9                              │"
            "\x1b[9;1H│   Hi                                              │"
            "\x1b[10;1H│   58m ago · f446825d                             │"
            "\x1b[12;1H└───────────────────────────────────────────────────┘"
            "\x1b[13;1H Ctrl+A to show all projects · Enter to select · Esc to cancel"
        )

        menu = _extract_selection_menu(raw)

        self.assertIsNotNone(menu)
        assert menu is not None
        self.assertEqual(menu.title, "请选择会话：")
        self.assertEqual(
            menu.options,
            (
                "WHAM (22m ago · c9caecf5)",
                "hi (40m ago · 451c3264)",
                "1 (44m ago · 4a4914b9)",
                "Hi (58m ago · f446825d)",
            ),
        )
        self.assertEqual(menu.selected_index, 0)

    def test_extract_kimi_sessions_menu_without_footer_controls(self) -> None:
        raw = (
            "SESSIONS (1 of 1)  [current directory]\n"
            "┌──────────────────────────────────|  Sessions  |───────────────────────────────────┐\n"
            "│                                                                                   │\n"
            "│ ❯ Novel Team                                                                      │\n"
            "│   31m ago · 073b7057                                                              │\n"
        )

        menu = _extract_selection_menu(raw)

        self.assertIsNotNone(menu)
        assert menu is not None
        self.assertEqual(menu.title, "请选择会话：")
        self.assertEqual(menu.options, ("Novel Team (31m ago · 073b7057)",))
        self.assertEqual(menu.selected_index, 0)

    def test_extract_kimi_sessions_waits_for_expected_count_without_footer(self) -> None:
        raw = (
            "SESSIONS (1 of 4)  [current directory]\n"
            "┌──────────────────────────────────|  Sessions  |───────────────────────────────────┐\n"
            "│ ❯ WHAM                                                                            │\n"
            "│   22m ago · c9caecf5                                                              │\n"
        )

        self.assertIsNone(_extract_selection_menu(raw))

    def test_extract_kimi_sessions_ignores_post_choice_redraw_noise(self) -> None:
        raw = (
            "┌────────────────────| Sessions |────────────────────┐\n"
            "│ ❯ WHAM                                            │\n"
            "│   22m ago · c9caecf5                              │\n"
            "│   hi                                              │\n"
            "│   40m ago · 451c3264                              │\n"
            "│   1                                               │\n"
            "│   44m ago · 4a4914b9                              │\n"
            "│   Hi                                              │\n"
            "│   58m ago · f446825d                              │\n"
            "└───────────────────────────────────────────────────┘\n"
            " Ctrl+A to show all projects · Enter to select · Esc to cancel\n"
            "2\n\n"
            " WHAM\n"
            " 22m ago · c9caecf5\n"
            "❯ hi\n"
            " 40m ago · 451c32643\n"
            " hi\n"
            " 40m ago · 451c3264\n"
            "❯ 1\n"
            " 44m ago · 4a4914b94\n"
        )

        menu = _extract_selection_menu(raw)

        self.assertIsNotNone(menu)
        assert menu is not None
        self.assertEqual(
            menu.options,
            (
                "WHAM (22m ago · c9caecf5)",
                "hi (40m ago · 451c3264)",
                "1 (44m ago · 4a4914b9)",
                "Hi (58m ago · f446825d)",
            ),
        )
        self.assertNotIn("WHAMago", "\n".join(menu.options))

    def test_generic_choose_list_is_not_terminal_menu(self) -> None:
        raw = (
            "请选择你想采用的方案：\n"
            "▶ 方案 A：先修 Telegram 输入。\n"
            "- 方案 B：先修输出过滤。\n"
            "- 方案 C：先补测试。\n"
        )

        self.assertIsNone(_extract_selection_menu(raw))

    def test_clean_terminal_text_removes_ansi_and_backspace(self) -> None:
        raw = "\x1b[31mHellx\b\x1b[0mo\r\n\x1b]0;title\x07World\x07"

        cleaned = _clean_terminal_text(raw)

        self.assertEqual(cleaned, "Hello\nWorld")

    def test_clean_terminal_text_removes_codex_status_noise(self) -> None:
        raw = (
            "M M M\n"
            "› y\n"
            "  能一句话告诉我吗？\n"
            "› Use /skills to list available skills gpt-5.4-mini medium · "
            "/data/lyxie/TeleAgent ]0;⠼ TeleAgent •Working(0s • esc to interrupt) "
            "]0;⠴ TeleAgent Worki•\n"
            "\n"
            "• 这是一个用 Python 写的终端自动化包装器。\n"
            "]0;TeleAgent  › Use /skills to list available skills\n"
        )

        cleaned = _clean_terminal_text(raw)

        self.assertIn("这是一个用 Python 写的终端自动化包装器。", cleaned)
        self.assertNotIn("Use /skills", cleaned)
        self.assertNotIn("TeleAgent", cleaned)
        self.assertNotIn("Working", cleaned)
        self.assertNotIn("能一句话告诉我吗", cleaned)

    def test_clean_terminal_text_removes_inline_working_noise(self) -> None:
        raw = (
            "对MMWorking(0s • ) › Summarize recent commits "
            "WoWorWorkWorkiWorkinWorkingWorkingorkingrkingkinging1ngg\n"
            "TeleAgent 是一个 Python 终端包装器，用来包住 codex、claude 等交互式 CLI。 "
            "› Summarize recent commits   核心功能：  - 监听终端输出。"
        )

        cleaned = _clean_terminal_text(raw)

        self.assertIn("TeleAgent 是一个 Python 终端包装器", cleaned)
        self.assertIn("核心功能", cleaned)
        self.assertNotIn("Working", cleaned)
        self.assertNotIn("Summarize recent commits", cleaned)
        self.assertNotIn("WoWorWork", cleaned)

    def test_clean_terminal_text_strips_short_edge_fragments(self) -> None:
        raw = "MM我很好，可以开始干活。你想让我看代码、跑实验，还是改某个具体问题？orking"

        cleaned = _clean_terminal_text(raw)

        self.assertEqual(
            cleaned,
            "我很好，可以开始干活。你想让我看代码、跑实验，还是改某个具体问题？",
        )

    def test_clean_terminal_text_strips_single_g_after_chinese(self) -> None:
        raw = "你好，我在。有什么需要我处理的？g"

        cleaned = _clean_terminal_text(raw)

        self.assertEqual(cleaned, "你好，我在。有什么需要我处理的？")

    def test_clean_terminal_text_preserves_normal_numbers(self) -> None:
        raw = "总字数控制在 800 字以内。今天是 2026 年。"

        cleaned = _clean_terminal_text(raw)

        self.assertEqual(cleaned, "总字数控制在 800 字以内。今天是 2026 年。")

    def test_mobile_formatting_unwraps_paragraphs_and_keeps_bullets(self) -> None:
        raw = "核心功能：\n  - 监听终端输出，按规则自动回复。\n  - 支持 Telegram。\n\n一句话：\n它是远程看护工具。"

        formatted = _format_for_mobile(raw)

        self.assertIn("核心功能：", formatted)
        self.assertIn("- 监听终端输出", formatted)
        self.assertIn("- 支持 Telegram", formatted)
        self.assertIn("\n\n", formatted)

    def test_mobile_formatting_removes_summary_prompt_echo(self) -> None:
        raw = (
            "请把你刚才过长的回复总结成字以内。面向手机聊天阅读："
            "先给一句话结论，再用短条目列关键点；不要复述完整原文。"
            "M一句话：TeleAgent 是一个 Python 终端包装器。"
            "关键点：- 它会启动真实命令。- 支持 Telegram。"
            "- 典型运行：python -m teleagent -c examples/teleagent.toml -- codex"
        )

        formatted = _format_for_mobile(raw)

        self.assertNotIn("请把你刚才", formatted)
        self.assertNotIn("不要复述完整原文", formatted)
        self.assertIn("一句话：", formatted)
        self.assertIn("- 它会启动真实命令。", formatted)
        self.assertIn("- 支持 Telegram。", formatted)

    def test_recent_user_echo_removal_handles_inline_command_noise(self) -> None:
        raw = "今天Write tests for @filenameM你想查哪个城市的天气？请给我城市名，比如“香港”或“北京”。"

        without_echo = _remove_recent_user_echoes(raw, ["今天天气如何？"])
        formatted = _format_for_mobile(without_echo)

        self.assertEqual(
            formatted,
            "你想查哪个城市的天气？请给我城市名，比如“香港”或“北京”。",
        )

    def test_recent_user_echo_removal_preserves_short_reply_prefix(self) -> None:
        without_echo = _remove_recent_user_echoes("你好，我在。", ["你好"])

        self.assertEqual(_format_for_mobile(without_echo), "你好，我在。")

    def test_recent_user_echo_removal_strips_mixed_prompt_prefix(self) -> None:
        raw = "hi 你好你好。需要我帮你看这个 Motion-X 仓库里的什么问题？"

        without_echo = _remove_recent_user_echoes(raw, ["hi 你好"])

        self.assertEqual(
            _format_for_mobile(without_echo),
            "你好。需要我帮你看这个 Motion-X 仓库里的什么问题？",
        )

    def test_recent_user_echo_removal_preserves_repeated_greeting_sentence(self) -> None:
        raw = "Hi 你好。要我帮你看代码、跑脚本，还是改仓库里的东西？"

        without_echo = _remove_recent_user_echoes(raw, ["Hi 你好"])

        self.assertEqual(
            _format_for_mobile(without_echo),
            "Hi 你好。要我帮你看代码、跑脚本，还是改仓库里的东西？",
        )

    def test_known_inline_noise_phrases_are_removed_anywhere(self) -> None:
        raw = "你好Write tests for @filenameM，我在。Summarize recent commits有什么需要我处理的？g"

        formatted = _format_for_mobile(raw)

        self.assertEqual(formatted, "你好，我在。有什么需要我处理的？")

    def test_mobile_formatting_extracts_bot_reply_from_chat_transcript(self) -> None:
        raw = (
            "Ritchie:\n"
            "今天天气如何？\n\n"
            "minsys-bot0:\n"
            "今天Write tests for @filenameM你想查哪个城市的天气？请给我城市名，比如“香港”或“北京”。"
        )

        formatted = _format_for_mobile(raw)

        self.assertEqual(
            formatted,
            "你想查哪个城市的天气？请给我城市名，比如“香港”或“北京”。",
        )

    def test_mobile_formatting_stress_with_common_tui_noise(self) -> None:
        expected = "你好，我在。有什么需要我处理的？"
        cases = [
            "MM" + expected + "orking",
            "Working(0s • ) › Summarize recent commits\n" + expected,
            "Write tests for @filenameM" + expected,
            "你好Write tests for @filenameM，我在。有什么需要我处理的？g",
            "⠼ TeleAgent •Working(0s • esc to interrupt)\n" + expected,
            "M M M\n" + expected + "\n]0;TeleAgent  › Use /skills to list available skills",
        ]

        for raw in cases:
            with self.subTest(raw=raw):
                formatted = _format_for_mobile(raw)
                self.assertEqual(formatted, expected)

    def test_recent_user_echo_stress_with_prefixes_and_noise(self) -> None:
        user_input = "今天天气如何？"
        expected = "你想查哪个城市的天气？请给我城市名，比如“香港”或“北京”。"
        cases = [
            "今天Write tests for @filenameM" + expected,
            "今天天Summarize recent commitsM" + expected,
            "今天天气Working" + expected,
            "› 今天天气如何？\n" + expected,
        ]

        for raw in cases:
            with self.subTest(raw=raw):
                without_echo = _remove_recent_user_echoes(raw, [user_input])
                self.assertEqual(_format_for_mobile(without_echo), expected)

    def test_noise_matrix_for_mobile_replies(self) -> None:
        messages = [
            "你好，我在。有什么需要我处理的？",
            "可以，请把具体文件名发给我。",
            "我需要城市名，才能查询天气。",
        ]
        prefixes = [
            "",
            "MM",
            "Write tests for @filenameM",
            "Working(0s • ) › Summarize recent commits\n",
            "⠼ TeleAgent •Working(0s • esc to interrupt)\n",
        ]
        infixes = [
            "",
            "Write tests for @filenameM",
            "Summarize recent commitsM",
            "Review code for bugsM",
            "Explain this codebase (0sM",
        ]
        suffixes = ["", "g", "M", "orking", "rking"]

        for message in messages:
            split_at = max(1, len(message) // 2)
            for prefix in prefixes:
                for infix in infixes:
                    for suffix in suffixes:
                        raw = prefix + message[:split_at] + infix + message[split_at:] + suffix
                        with self.subTest(message=message, prefix=prefix, infix=infix, suffix=suffix):
                            self.assertEqual(_format_for_mobile(raw), message)

    def test_lifestyle_conversation_noise_matrix(self) -> None:
        messages = [
            "我喜欢肖邦，也很欣赏德彪西和拉赫玛尼诺夫。",
            "晚饭可以吃清淡一点，比如番茄鸡蛋面。",
            "周末去公园散步会很舒服。",
            "这本书适合慢慢读，不用急。",
            "如果你想放松，可以听一点巴赫或爵士。",
            "我建议先喝点水，再休息十分钟。",
            "可以买一束小花放在桌上。",
            "这部电影适合晚上安静地看。",
            "跑步前最好先热身五分钟。",
            "咖啡少放糖会更清爽。",
        ]
        prefixes = [
            "",
            "MM",
            "Mimprove documentation in @filenames  esc to interupt)ngg",
            "⠼ TeleAgent •Working(0s • esc to interrupt)\n",
            "Explain this codebase (0sM",
        ]
        infixes = [
            "",
            "Mimprove documentation in @filenames  esc to interupt)ngg",
            "Improve Documentation In @Filename esc to interrupt)ng",
            "Write tests for @filenamesM",
            "Review code for bugsM",
            "Explain this codebase (0sM",
        ]
        suffixes = ["", "g", "ng", "ngg", "M", "orking"]

        for message in messages:
            split_at = max(1, len(message) // 2)
            for prefix in prefixes:
                for infix in infixes:
                    for suffix in suffixes:
                        raw = prefix + message[:split_at] + infix + message[split_at:] + suffix
                        with self.subTest(message=message, prefix=prefix, infix=infix, suffix=suffix):
                            self.assertEqual(_format_for_mobile(raw), message)

    def test_filter_audit_matrix_has_no_menu_false_positives_or_noise_leaks(self) -> None:
        messages = [
            "你好，我在。有什么需要我处理的？",
            "我建议先运行 teleagent --doctor。",
            "可以发送 /model 切换模型，也可以发送 /resume 恢复会话。",
            "gpt-5 适合复杂推理，claude 适合长文本阅读。",
            "请选择你想采用的方案，然后告诉我编号。",
            "我建议三步：1. 查配置；2. 查 token；3. 重启。",
            "▶ 这只是普通项目符号，不是终端菜单。",
            "❯ 这里是在解释符号含义，不是让你选择。",
        ]
        prefixes = [
            "",
            "MM",
            "Working(0s • ) › Summarize recent commits\n",
            "⠼ TeleAgent •Working(0s • esc to interrupt)\n",
            "│ model: gpt-5 /model to change │\n",
        ]
        infixes = [
            "",
            "Write tests for @filenameM",
            "Explain this codebase (0sM",
            "Review code for bugsM",
        ]
        suffixes = ["", "g", "orking", "\nToken usage: total=1 input=1 output=0"]
        leaked_tokens = [
            "Working",
            "Write tests for",
            "Explain this codebase",
            "Review code for bugs",
            "Token usage",
        ]

        for message in messages:
            split_at = max(1, len(message) // 2)
            for prefix in prefixes:
                for infix in infixes:
                    for suffix in suffixes:
                        raw = prefix + message[:split_at] + infix + message[split_at:] + suffix
                        with self.subTest(message=message, prefix=prefix, infix=infix, suffix=suffix):
                            self.assertIsNone(_extract_selection_menu(raw))
                            formatted = _format_for_mobile(raw)
                            for token in leaked_tokens:
                                self.assertNotIn(token, formatted)

    def test_trailing_full_noise_capsule_after_lifestyle_reply(self) -> None:
        raw = (
            "我喜欢肖邦，也很欣赏德彪西和拉赫玛尼诺夫。"
            "Mimprove documentation in @filenames  esc to interupt)ngg"
        )

        self.assertEqual(
            _format_for_mobile(raw),
            "我喜欢肖邦，也很欣赏德彪西和拉赫玛尼诺夫。",
        )

    def test_explain_codebase_status_noise_positions(self) -> None:
        expected = "我喜欢肖邦，也很欣赏德彪西和拉赫玛尼诺夫。"
        cases = [
            "Explain this codebase (0sM" + expected,
            "我喜欢肖邦，Explain this codebase (0sM也很欣赏德彪西和拉赫玛尼诺夫。",
            expected + "Explain this codebase (0sM",
        ]

        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_format_for_mobile(raw), expected)

    def test_mobile_formatting_removes_codex_tui_status_blocks(self) -> None:
        raw = (
            "Token usage: total=2, input=2, output=0\n"
            "To continue this session, run codex resume abc123"
            "╭────────────╮│ >_ OpenAI Codex (v0.137.0) │╰────────────╯MMMMMM\n"
            "  Tip: New Build faster with Codex."
            "你(0s1ingngg23ingngg45ingngg"
            "你好，我在。需要我看这个仓库、改代码、跑测试，还是先帮你分析问题？"
            "M(0singngg12ingngg34ingngg56ingngg7Work8ingngg9Work10singngg1Work2"
            "我先快速扫一下仓库结构、README 和关键入口文件。"
            "ingng · 2 background terminals running · /ps to view · /stop to close"
            "Exploring  └ List rg --files\n"
            "\n"
            "Explored\n"
            "  └ List rg --files\n"
            "  └ Search class TelegramBridge|def _format_for_mobile in telegram.py\n"
            "           _format_for_mobile in telegram.py\n"
            "\n"
            "Ran pwd && ls\n"
            "  └ /data/lyxie/TeleAgent\n"
            "    README.md\n"
            "    … +5 lines (ctrl + t to view transcript)\n"
            "\n"
            "────────────────────────•(17s8ingngg9Work20ingngg1Work2ingngg3Work4"
            "这个仓库很小，核心文件集中在 teleagent/。"
            "Exploring  └ Read __main__.py•(24s •  · 1 background terminal running · /ps to view · /stop to close"
        )

        formatted = _format_for_mobile(raw)

        self.assertIn("你好，我在。需要我看这个仓库、改代码、跑测试，还是先帮你分析问题？", formatted)
        self.assertIn("我先快速扫一下仓库结构、README 和关键入口文件。", formatted)
        self.assertIn("这个仓库很小，核心文件集中在 teleagent/。", formatted)
        for noise in (
            "Token usage",
            "OpenAI Codex",
            "Tip:",
            "Exploring",
            "Explored",
            "Ran pwd",
            "background terminal",
            "/ps to view",
            "ctrl + t",
            "Working",
            "ingngg",
            "────",
            "_format_for_mobile",
        ):
            self.assertNotIn(noise, formatted)

    def test_mobile_formatting_removes_summary_queue_chrome(self) -> None:
        raw = (
            "这不是玩具项目常见的“把 stdout 发过去”那么简单。"
            "请把你刚才过长的回复总结成"
            "tab to queue message                 94% context left"
            "800字以内。面向手机聊天阅读：先给一句话结论，再用短条目列关键点；不要复述完整原文。"
            "• Messagestobesubmittedafternexttoolcall (press enter and send immediately)  ↳"
            "一句话总结：这是一个非常务实的小工具。"
        )

        formatted = _format_for_mobile(raw)

        self.assertEqual(
            formatted,
            "这不是玩具项目常见的“把 stdout 发过去”那么简单。\n一句话总结：这是一个非常务实的小工具。",
        )

    def test_reid_history_status_noise_is_removed(self) -> None:
        raw = (
            "一句话结论：我们确认了“真值事件窗口”能救 acc_z，但 synthetic 侧简单触发还不够准，"
            "所以现在改成专门用 acc_z 触发来›tab to queue message33% context left验证。"
            "- oracle 事件窗口结果很好：- 右臂 acc_z 提到约 0.82"
            "- 低相关轴从 40 降到 9"
            "⚠ Heads up, you have less than 10% of your weekly limit left. Run /status for a breakdown."
            "继续吧›Run /review on my current changesgpt-5.5 xhigh · /data/lyxie/ReID_imu_generation\n"
            "›Run /review on my current changesgpt-5.5 xhigh · /data/lyxie/ReID_imu_generation"
            "•›tab to queue message33% context leftingngg1king2ingngg3king4ingngg5king6ingngg7"
            "我接着把刚生成的 acc_z 触发版评估完。重点看三件事。king8ingngg920ingngg1"
            "Updated Plan\n"
            "    □ Compare worst S2 right-arm acc_z cases against baseline\n"
            "Evaluating mocap_geom_forearm_upperarm_smooth4_best_subject_cluster_despiked_accz_triggered / S1_acting2 ...\n"
            "    +0.842  +0.862  +0.870\n"
            "Wrote outputs/sequence_calibrated_reports/low_correlation_axis_diagnostics_accz_triggered.csv\n"
            "Edited Exploration/progress_log.md (+10 -0)\n"
        )

        formatted = _format_for_mobile(raw)

        self.assertIn("一句话结论：", formatted)
        self.assertIn("我接着把刚生成的 acc_z 触发版评估完。重点看三件事。", formatted)
        for noise in (
            "tab to queue",
            "context left",
            "Run /review",
            "gpt-5.5",
            "ingngg",
            "king",
            "Updated Plan",
            "Evaluating",
            "Wrote outputs",
            "Edited Exploration",
            "Heads up",
            "/status",
        ):
            self.assertNotIn(noise, formatted)

    def test_reid_spinner_fragment_line_is_removed(self) -> None:
        raw = (
            ";138;49mg•••orking•rking•kinging•ngg•••••••orking•rking•kinging•ngg6M\n"
            "\n"
            "一句话结论：roll/local gravity 问题已基本打通。"
        )

        formatted = _format_for_mobile(raw)

        self.assertEqual(formatted, "一句话结论：roll/local gravity 问题已基本打通。")

    def test_reid_star_spinner_fragments_are_removed(self) -> None:
        cases = {
            "m•••••\n报告已写入 docs/。": "报告已写入 docs/。",
            "*****Work\n已写入中文报告：": "已写入中文报告：",
            "***99\n已写入中文报告：": "已写入中文报告：",
            "*****Work已写入中文报告：": "已写入中文报告：",
            "***99已写入中文报告：": "已写入中文报告：",
            "已写入中文报告：*****Work": "已写入中文报告：",
            "已写入中文报告：***99": "已写入中文报告：",
        }

        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_format_for_mobile(raw), expected)

    def test_reid_recent_status_bar_fragments_are_removed(self) -> None:
        cases = {
            "就能得到 IMU 自己的朝向。pt-5.5 xhigh · /data/lyxie/ReID_imu_generation": (
                "就能得到 IMU 自己的朝向。"
            ),
            "作为主线。pt-5.5 xhigh · /data/lyxie/ReID_imu_generation": "作为主线。",
            "官方给的这个calib_imugpt-5.5 xhigh · /data/lyxie/ReID_imu_generationbone": (
                "官方给的这个calib_imubone"
            ),
            "验证过的脚本语法正常；没有跑全量 unittest，因为这次主要是研究脚本和报告更新。6": (
                "验证过的脚本语法正常；没有跑全量 unittest，因为这次主要是研究脚本和报告更新。"
            ),
        }

        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_format_for_mobile(raw), expected)

    def test_reid_recent_short_terminal_fragments_are_removed(self) -> None:
        for raw in ("18;3H", "[18;2H", "4;35H1", "9mork", "ng"):
            with self.subTest(raw=raw):
                self.assertEqual(_format_for_mobile(raw), "")

    def test_reid_control_fragment_before_reply_text_is_removed(self) -> None:
        raw = ";2H\n\n- 说明官方 skeleton quaternion 和关节位置几乎完全自洽。"

        self.assertEqual(
            _format_for_mobile(raw),
            "- 说明官方 skeleton quaternion 和关节位置几乎完全自洽。",
        )

    def test_terminal_pending_signal_ignores_status_redraws(self) -> None:
        reply = (
            "\x1b[18;2H关键实验结果：\n\n"
            "- 几何自洽验证：\n"
            "  - 370/370 个骨段 case 最优都是 parent|local_to_world\n"
        )
        redraw_noise = (
            "\x1b]0;⠙ ReID_imu_generation\x07"
            "\x1b[15;2H\x1b[0m\x1b[49m\x1b[K"
            "\x1b[16;2H\x1b[0m\x1b[49m\x1b[K"
            "\x1b[18;3H\x1b[?2026l"
            "\x1b[20;97H3"
        )

        self.assertTrue(_terminal_chunk_has_pending_signal(reply))
        self.assertFalse(_terminal_chunk_has_pending_signal(redraw_noise))

    def test_reid_recent_filters_preserve_normal_numbers_and_text(self) -> None:
        cases = [
            "今天是 2026 年。",
            "误差仍然很大，约 56 deg。",
            "这是普通正文里的 pt-5.5 字样，不是状态栏。",
        ]

        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(_format_for_mobile(text), text)

    def test_reid_inline_composer_status_echo_is_removed(self) -> None:
        raw = (
            "报告约 505 行，放在 docs/ 下，未改其它文档。"
            "›各类pt-5.5 xhigh · /data/lyxie/ReID_imu_generation误差\n"
            "下一句模型回复。"
        )

        formatted = _format_for_mobile(raw)

        self.assertIn("报告约 505 行，放在 docs/ 下，未改其它文档。", formatted)
        self.assertIn("下一句模型回复。", formatted)
        self.assertNotIn("各类", formatted)
        self.assertNotIn("pt-5.5", formatted)
        self.assertNotIn("误差", formatted)

    def test_reid_inline_tab_status_is_removed_without_dropping_reply_tail(self) -> None:
        raw = (
            "继续推进完了，结论更硬了。"
            "›tab to queue message24% context left"
            "这轮做了几件事：- 评估了 acc_z 专门触发版"
        )

        formatted = _format_for_mobile(raw)

        self.assertIn("继续推进完了，结论更硬了。", formatted)
        self.assertIn("这轮做了几件事", formatted)
        self.assertIn("评估了 acc_z 专门触发版", formatted)
        self.assertNotIn("tab to queue", formatted)
        self.assertNotIn("context left", formatted)

    def test_reid_corrupted_background_status_is_removed(self) -> None:
        raw = (
            "一句话结论：坐标轴问题基本解决后，我们正在转向优化 acc 的物理生成。"
            "  1 backgroundterminal runng · /s toview · /stopo close"
        )

        formatted = _format_for_mobile(raw)

        self.assertEqual(
            formatted,
            "一句话结论：坐标轴问题基本解决后，我们正在转向优化 acc 的物理生成。",
        )

    def test_reid_inline_manual_input_echo_is_removed(self) -> None:
        raw = (
            "回答前半句。"
            "›我觉得现有算法是不是有问题?"
            "gpt-5.5 xhigh · /data/lyxie/ReID_imu_generation上的方向问题?\n"
            "回答后半句。"
        )

        formatted = _format_for_mobile(raw)

        self.assertIn("回答前半句。", formatted)
        self.assertIn("回答后半句。", formatted)
        self.assertNotIn("我觉得现有算法", formatted)
        self.assertNotIn("方向问题", formatted)
        self.assertNotIn("gpt-5.5", formatted)

    def test_reid_model_menu_chrome_is_not_sent_as_text(self) -> None:
        raw = (
            "一句话结论：我把事件触发器改成只看 acc_z。"
            "›//model         choose what model and reasoning effort to use"
            "/ideinclude current selection, open files, and other context from your IDE"
            "›/mo/model  choose what model and reasoning effort to usedel"
            "Select Model and Effort"
            "1.gpt-5.5(default)Frontier model for complex coding, research, and real-world work."
            "2.gpt-5.4Strong model for everyday coding."
            "› 3. gpt-5.4-mini (current)  Small, fast, and cost-efficient model for simpler coding tasks."
            "Press enter to confirm or esc to go back"
            "Model changed toxhigh · /data/lyxie/ReID_imu_generation"
        )

        formatted = _format_for_mobile(raw)

        self.assertEqual(formatted, "一句话结论：我把事件触发器改成只看 acc_z。")

    def test_motionx_slash_autocomplete_chrome_is_removed(self) -> None:
        raw = (
            "/           choose what model and reasoning effort to use"
            "/fast1.5x speed, increased usage"
            "/ideinclude current selection, open files, and other context from your IDE"
            "/permissionschoose what Codex is allowed to do"
            "/keymapremap TUI shortcuts"
            "/vimtoggle Vim mode for the composer"
            "/experimentaltoggle experimental features"
            "/approveapprove one retry of a recent auto-review denial"
        )

        formatted = _format_for_mobile(raw)

        self.assertEqual(formatted, "")

    def test_motionx_preserves_leading_m_words_in_replies(self) -> None:
        cases = [
            "Motion-X 数据本身不完整在当前仓库里。",
            "Motion-X 变成“全身 + 手 + 脸”的 expressive motion 数据。",
            "Model 输出里的正常正文应该保留。",
            "Mocap 数据可以转成 SMPL-X 322 维格式。",
        ]

        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_format_for_mobile(raw), raw)

    def test_motionx_drops_model_changed_status_line(self) -> None:
        cases = [
            "Model changed to gpt-5.5 xhigh",
            "odel changed to gpt-5.5 xhigh",
            "Model changed toxhigh · /data/lyxie/Motion-X",
        ]

        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_format_for_mobile(raw), "")


class TelegramWriteInputTest(unittest.TestCase):
    def test_local_input_tracker_collects_completed_manual_message(self) -> None:
        tracker = _LocalInputTracker()

        self.assertEqual(tracker.feed("hi ".encode()), [])
        self.assertEqual(tracker.feed("你好\r".encode()), ["hi 你好"])

    def test_local_input_tracker_handles_backspace_and_arrow_keys(self) -> None:
        tracker = _LocalInputTracker()

        completed = tracker.feed(b"abc\x7fd\x1b[A\r")

        self.assertEqual(completed, ["abd"])

    def test_send_uses_configured_submit_keys(self) -> None:
        payload = _capture_written_telegram_input(
            TelegramInput(TelegramInputKind.SEND, "你好"),
            TelegramConfig(
                input_submit_delay_seconds=0,
                input_submit_keys=("enter", "linefeed"),
            ),
        )

        self.assertEqual(payload, "你好".encode() + b"\r\n")

    def test_model_command_uses_single_enter_to_keep_menu_open(self) -> None:
        payload = _capture_written_telegram_input(
            TelegramInput(TelegramInputKind.SEND, "/model"),
            TelegramConfig(
                input_submit_delay_seconds=0,
                input_submit_keys=("enter", "linefeed"),
                slash_submit_delay_seconds=0,
            ),
        )

        self.assertEqual(payload, b"/model\r")

    def test_resume_command_uses_single_enter_to_keep_menu_open(self) -> None:
        payload = _capture_written_telegram_input(
            TelegramInput(TelegramInputKind.SEND, "/resume"),
            TelegramConfig(
                input_submit_delay_seconds=0,
                input_submit_keys=("enter", "linefeed"),
                slash_submit_delay_seconds=0,
            ),
        )

        self.assertEqual(payload, b"/resume\r")

    def test_sessions_command_uses_single_enter_to_keep_menu_open(self) -> None:
        payload = _capture_written_telegram_input(
            TelegramInput(TelegramInputKind.SEND, "/sessions"),
            TelegramConfig(
                input_submit_delay_seconds=0,
                input_submit_keys=("enter", "linefeed"),
                slash_submit_delay_seconds=0,
            ),
        )

        self.assertEqual(payload, b"/sessions\r")

    def test_slash_command_uses_configured_slash_submit_keys(self) -> None:
        payload = _capture_written_telegram_input(
            TelegramInput(TelegramInputKind.SEND, "/resume"),
            TelegramConfig(
                input_submit_delay_seconds=0,
                input_submit_keys=("enter", "linefeed"),
                slash_submit_delay_seconds=0,
                slash_submit_keys=("linefeed",),
            ),
        )

        self.assertEqual(payload, b"/resume\n")

    def test_enter_uses_configured_submit_keys(self) -> None:
        payload = _capture_written_telegram_input(
            TelegramInput(TelegramInputKind.ENTER),
            TelegramConfig(input_submit_keys=("ctrl-j",)),
        )

        self.assertEqual(payload, b"\n")

    def test_key_enter_stays_raw_single_key(self) -> None:
        payload = _capture_written_telegram_input(
            TelegramInput(TelegramInputKind.KEY, "enter"),
            TelegramConfig(input_submit_keys=("linefeed",)),
        )

        self.assertEqual(payload, b"\r")

    def test_key_sequence_writes_multiple_keys(self) -> None:
        payload = _capture_written_telegram_input(
            TelegramInput(TelegramInputKind.KEY, "down down enter"),
            TelegramConfig(),
        )

        self.assertEqual(payload, b"\x1b[B\x1b[B\r")


def _iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_rollout_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    _write_jsonl(path, records)


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def _append_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_kimi_metadata(state_root: Path, project_root: Path, session_id: str) -> Path:
    state_root.mkdir(parents=True, exist_ok=True)
    resolved_project = project_root.resolve()
    (state_root / "kimi.json").write_text(
        json.dumps(
            {
                "work_dirs": [
                    {
                        "path": str(resolved_project),
                        "kaos": "local",
                        "last_session_id": session_id,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    digest = hashlib.md5(str(resolved_project).encode("utf-8")).hexdigest()
    return state_root / "sessions" / digest / session_id


class CodexRolloutOutputSourceTest(unittest.TestCase):
    def test_reads_agent_message_and_dedupes_task_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_root = Path(tmp) / ".codex"
            project_root = Path(tmp) / "Motion-X"
            project_root.mkdir()
            rollout = codex_root / "sessions" / "2026" / "06" / "10" / "rollout-test.jsonl"
            timestamp = _iso_timestamp()
            _write_rollout_jsonl(
                rollout,
                [
                    {
                        "timestamp": timestamp,
                        "type": "session_meta",
                        "payload": {"cwd": str(project_root)},
                    },
                    {
                        "timestamp": timestamp,
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": "Motion-X 数据本身不完整。",
                        },
                    },
                    {
                        "timestamp": timestamp,
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "last_agent_message": "Motion-X 数据本身不完整。",
                        },
                    },
                ],
            )

            source = _CodexRolloutOutputSource(
                str(codex_root),
                cwd=project_root,
                launched_at=time.time() - 10,
            )
            messages = source.poll_messages()

        self.assertEqual(messages, ["Motion-X 数据本身不完整。"])

    def test_reads_response_item_assistant_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_root = Path(tmp) / ".codex"
            project_root = Path(tmp) / "Motion-X"
            project_root.mkdir()
            rollout = codex_root / "sessions" / "2026" / "06" / "10" / "rollout-test.jsonl"
            timestamp = _iso_timestamp()
            _write_rollout_jsonl(
                rollout,
                [
                    {
                        "timestamp": timestamp,
                        "type": "session_meta",
                        "payload": {"cwd": str(project_root)},
                    },
                    {
                        "timestamp": timestamp,
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "你好"}],
                        },
                    },
                    {
                        "timestamp": timestamp,
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "一句话结论：可以。"},
                                {"type": "output_text", "text": "- 关键点一。"},
                            ],
                        },
                    },
                ],
            )

            source = _CodexRolloutOutputSource(
                str(codex_root),
                cwd=project_root,
                launched_at=time.time() - 10,
            )
            messages = source.poll_messages()

        self.assertEqual(messages, ["一句话结论：可以。\n- 关键点一。"])


class KimiSessionOutputSourceTest(unittest.TestCase):
    def test_context_reads_new_assistant_text_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / ".kimi"
            project_root = Path(tmp) / "Motion-X"
            project_root.mkdir()
            session_dir = _write_kimi_metadata(state_root, project_root, "session-a")
            context = session_dir / "context.jsonl"
            _write_jsonl(
                context,
                [
                    {"role": "user", "content": "你好"},
                    {"role": "_usage", "token_count": 100},
                ],
            )
            source = _KimiContextOutputSource(
                str(state_root),
                cwd=project_root,
                launched_at=time.time() - 10,
            )

            self.assertEqual(source.poll_messages(), [])
            _append_jsonl(
                context,
                [
                    {"role": "user", "content": "继续"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "think", "think": "hidden reasoning"},
                            {"type": "text", "text": "一句话结论：Kimi 可以接入。"},
                            {"type": "text", "text": "\n- 默认仍走 UI 终端。"},
                        ],
                    },
                ],
            )
            messages = source.poll_messages()

        self.assertEqual(messages, ["一句话结论：Kimi 可以接入。\n- 默认仍走 UI 终端。"])

    def test_wire_reads_visible_text_on_turn_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / ".kimi"
            project_root = Path(tmp) / "Motion-X"
            project_root.mkdir()
            session_dir = _write_kimi_metadata(state_root, project_root, "session-b")
            wire = session_dir / "wire.jsonl"
            now = time.time()
            _write_jsonl(
                wire,
                [
                    {"type": "metadata", "protocol_version": "1.10"},
                    {
                        "timestamp": now - 60,
                        "message": {"type": "TurnBegin", "payload": {"user_input": "旧问题"}},
                    },
                    {
                        "timestamp": now - 60,
                        "message": {
                            "type": "ContentPart",
                            "payload": {"type": "text", "text": "旧回复不应发送。"},
                        },
                    },
                    {"timestamp": now - 60, "message": {"type": "TurnEnd", "payload": {}}},
                    {
                        "timestamp": now,
                        "message": {"type": "TurnBegin", "payload": {"user_input": "你好"}},
                    },
                    {
                        "timestamp": now,
                        "message": {
                            "type": "ContentPart",
                            "payload": {"type": "think", "think": "hidden reasoning"},
                        },
                    },
                    {
                        "timestamp": now,
                        "message": {
                            "type": "ContentPart",
                            "payload": {"type": "text", "text": "一句话结论：wire 可以用。"},
                        },
                    },
                    {
                        "timestamp": now,
                        "message": {
                            "type": "ContentPart",
                            "payload": {"type": "text", "text": "- 只发送可见文本。"},
                        },
                    },
                    {"timestamp": now, "message": {"type": "TurnEnd", "payload": {}}},
                ],
            )
            source = _KimiWireOutputSource(
                str(state_root),
                cwd=project_root,
                launched_at=now - 10,
            )
            messages = source.poll_messages()
            second_poll = source.poll_messages()

        self.assertEqual(messages, ["一句话结论：wire 可以用。\n\n- 只发送可见文本。"])
        self.assertEqual(second_poll, [])


class TelegramDebugBridgeTest(unittest.TestCase):
    def test_structured_output_source_prefers_rollout_over_terminal_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            codex_root = Path(tmp) / ".codex"
            project_root = Path.cwd()
            rollout = codex_root / "sessions" / "2026" / "06" / "10" / "rollout-test.jsonl"
            timestamp = _iso_timestamp()
            _write_rollout_jsonl(
                rollout,
                [
                    {
                        "timestamp": timestamp,
                        "type": "session_meta",
                        "payload": {"cwd": str(project_root)},
                    },
                    {
                        "timestamp": timestamp,
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": "Motion-X 数据本身不完整。",
                        },
                    },
                ],
            )
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                    codex_state_root=str(codex_root),
                    output_sources=("codex_rollout", "terminal"),
                )
            )
            try:
                bridge.record_output("MMotion-X 数据本身不完整。orking")
                bridge.flush_idle_output()
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(messages, ["Motion-X 数据本身不完整。"])

    def test_structured_output_source_falls_back_to_terminal_when_rollout_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                    codex_state_root=str(Path(tmp) / ".codex"),
                    output_sources=("codex_rollout", "terminal"),
                )
            )
            try:
                bridge.record_output("MM你好Write tests for @filenameM，我在。有什么需要我处理的？g")
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(messages, ["你好，我在。有什么需要我处理的？"])

    def test_terminal_source_can_fall_back_to_kimi_wire_when_ui_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            state_root = Path(tmp) / ".kimi"
            session_dir = _write_kimi_metadata(state_root, Path.cwd(), "session-c")
            now = time.time()
            _write_jsonl(
                session_dir / "wire.jsonl",
                [
                    {"type": "metadata", "protocol_version": "1.10"},
                    {
                        "timestamp": now,
                        "message": {"type": "TurnBegin", "payload": {"user_input": "你好"}},
                    },
                    {
                        "timestamp": now,
                        "message": {
                            "type": "ContentPart",
                            "payload": {
                                "type": "text",
                                "text": "一句话结论：Kimi 日志兜底成功。",
                            },
                        },
                    },
                    {"timestamp": now, "message": {"type": "TurnEnd", "payload": {}}},
                ],
            )
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                    kimi_state_root=str(state_root),
                    output_sources=("terminal", "kimi_wire"),
                )
            )
            try:
                bridge.record_output(
                    "•(53s •  · 3 background terminals running · /ps to view · /stop to close\n"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(messages, ["一句话结论：Kimi 日志兜底成功。"])

    def test_kimi_wire_source_preempts_noisy_terminal_ui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            state_root = Path(tmp) / ".kimi"
            session_dir = _write_kimi_metadata(state_root, Path.cwd(), "session-d")
            now = time.time()
            _write_jsonl(
                session_dir / "wire.jsonl",
                [
                    {"type": "metadata", "protocol_version": "1.10"},
                    {
                        "timestamp": now,
                        "message": {"type": "TurnBegin", "payload": {"user_input": "Hi"}},
                    },
                    {
                        "timestamp": now,
                        "message": {
                            "type": "ContentPart",
                            "payload": {
                                "type": "text",
                                "text": "Hello! How can I help you today?",
                            },
                        },
                    },
                    {"timestamp": now, "message": {"type": "TurnEnd", "payload": {}}},
                ],
            )
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                    kimi_state_root=str(state_root),
                    output_sources=("kimi_wire", "terminal"),
                )
            )
            try:
                bridge.record_output(
                    "The user has simply said \"Hi\". I should respond directly.\n"
                    "• Hello! How can I help you today? Feel free to ask me anything.\n"
                    "agent (Kimi-k2.6 ●)  /data/lyxie/WHAM\n"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(messages, ["Hello! How can I help you today?"])

    def test_history_command_sends_sanitized_clean_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            history = Path(tmp) / "teleagent-history.log"
            history.write_text(
                "一句话结论：保留这句。"
                "›//model         choose what model and reasoning effort to use"
                "Select Model and Effort"
                "1.gpt-5.5(default)Frontier model for complex coding."
                "› 2. gpt-5.4 (current) Strong model."
                "Press enter to confirm or esc to go back"
                "Model changed toxhigh · /data/lyxie/ReID_imu_generation\n",
                encoding="utf-8",
            )
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(history),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                )
            )
            try:
                bridge.handle_command("/history")
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

            messages = [record["text"] for record in records if record["type"] == "message"]
            self.assertEqual(len(messages), 1)
            self.assertIn("正在发送清理后历史文件", messages[0])
            documents = [record for record in records if record["type"] == "document"]
            self.assertEqual(len(documents), 1)
            clean_path = Path(str(documents[0]["path"]))
            self.assertEqual(clean_path.name, "teleagent-history.clean.log")
            clean_text = clean_path.read_text(encoding="utf-8")
            self.assertIn("一句话结论：保留这句。", clean_text)
            self.assertNotIn("Select Model", clean_text)
            self.assertNotIn("gpt-5.5", clean_text)

    def test_history_command_reports_document_send_failure(self) -> None:
        class FailingDocumentClient:
            def __init__(self) -> None:
                self.messages: list[str] = []

            def send_message(self, chat_id: int, text: str) -> None:
                del chat_id
                self.messages.append(text)

            def send_document(self, chat_id: int, path: Path, *, caption: str = "") -> None:
                del chat_id, path, caption
                raise OSError("send failed")

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / "teleagent-history.log"
            history.write_text("一句话结论：保留这句。", encoding="utf-8")
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(Path(tmp) / "outbox.jsonl"),
                    history_path=str(history),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                )
            )
            client = FailingDocumentClient()
            bridge._client = client
            try:
                bridge.handle_command("/history")
            finally:
                bridge.close()

        self.assertTrue(any("正在发送清理后历史文件" in message for message in client.messages))
        self.assertTrue(any("历史文件发送失败" in message for message in client.messages))

    def test_menu_choice_consumes_numeric_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(Path(tmp) / "outbox.jsonl"),
                )
            )
            bridge._pending_menu = SelectionMenu(
                "请选择模型：",
                ("gpt-5.5 xhigh", "gpt-5.5 medium", "gpt-5.4-mini"),
                0,
            )

            action = bridge.consume_menu_choice("3")

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.kind, TelegramInputKind.KEY)
        self.assertEqual(action.text, "down down enter")

    def test_invalid_menu_choice_is_not_forwarded_to_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                )
            )
            bridge._pending_menu = SelectionMenu("请选择模型：", ("a", "b"), 0)

            action = bridge.consume_menu_choice("9")

            records = _read_debug_records(outbox)

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.kind, TelegramInputKind.IGNORE)
        self.assertIn("选项编号无效", records[0]["text"])

    def test_plain_text_clears_pending_menu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(Path(tmp) / "outbox.jsonl"),
                )
            )
            bridge._pending_menu = SelectionMenu("请选择模型：", ("a", "b"), 0)

            first = bridge.consume_menu_choice("你好")
            second = bridge.consume_menu_choice("1")

        self.assertIsNone(first)
        self.assertIsNone(second)

    def test_prompt_echo_is_not_sent_as_menu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.mark_user_input("今天天气如何？")
                bridge.record_output(
                    "│ model:     gpt-5.5 xhigh   /model to change │\n"
                    "› 今天天气如何？\n"
                    "gpt-5.5 xhigh · /data/lyxie/TeleAgent\n"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [str(record.get("text", "")) for record in records if record["type"] == "message"]
        self.assertFalse(any("请选择" in message for message in messages))

    def test_debug_bridge_filters_marked_local_prompt_echo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.mark_user_input("hi 你好")
                bridge.record_output(
                    "\x1b[10;1H› hi 你好\n"
                    "\x1b[1;1Hhi 你好Use /skills to list available skills\n"
                    "• 你好。需要我帮你看这个 Motion-X 仓库里的什么问题？"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [str(record.get("text", "")) for record in records if record["type"] == "message"]
        self.assertEqual(messages, ["你好。需要我帮你看这个 Motion-X 仓库里的什么问题？"])

    def test_debug_bridge_menu_false_positive_matrix(self) -> None:
        cases = [
            (
                "prompt_echo",
                "│ model: gpt-5 /model to change │\n"
                "› 今天天气如何？\n"
                "gpt-5 · /data/project\n",
            ),
            (
                "ai_model_compare",
                "可以这样理解这些模型：\n"
                "▶ gpt-5 适合复杂推理。\n"
                "- gpt-4 适合一般对话。\n"
                "- claude 适合长文本。\n",
            ),
            (
                "plain_strong_cursor",
                "模型对比：\n"
                "❯ gpt-5 适合复杂推理。\n"
                "  gpt-4 适合一般对话。\n"
                "  claude 适合长文本。\n",
            ),
            (
                "generic_choose",
                "请选择你想采用的方案：\n"
                "▶ 方案 A：先修输入。\n"
                "- 方案 B：先修输出。\n",
            ),
            (
                "numbered_reply",
                "我建议三步：\n"
                "1. 先确认 token。\n"
                "2. 再运行 doctor。\n"
                "3. 最后启动。\n",
            ),
        ]

        for name, raw in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    outbox = Path(tmp) / "outbox.jsonl"
                    bridge = TelegramBridge(
                        TelegramConfig(
                            enabled=True,
                            debug_mode=True,
                            allowed_chat_ids=(0,),
                            debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                            debug_outbox_path=str(outbox),
                            history_path=str(Path(tmp) / "history.log"),
                            raw_history_path=str(Path(tmp) / "raw.log"),
                            idle_forward_seconds=0,
                            summary_threshold_chars=1000,
                        )
                    )
                    try:
                        bridge.record_output(raw)
                        bridge.flush_idle_output()
                        self.assertIsNone(bridge._pending_menu)
                    finally:
                        bridge.close()

                    records = _read_debug_records(outbox)

                messages = [
                    str(record.get("text", ""))
                    for record in records
                    if record["type"] == "message"
                ]
                self.assertFalse(any("请选择模型" in message for message in messages), messages)
                self.assertFalse(any("请选择终端菜单项" in message for message in messages), messages)

    def test_debug_bridge_receives_inbox_lines_as_telegram_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox.txt"
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(inbox),
                    debug_outbox_path=str(outbox),
                    poll_timeout=1,
                )
            )
            bridge.start()
            try:
                with inbox.open("a", encoding="utf-8") as handle:
                    handle.write("你好\n")
                    handle.write("/enter\n")

                actions = _drain_debug_actions(bridge, expected_count=2)
            finally:
                bridge.close()

        self.assertEqual([action.kind for action in actions], [TelegramInputKind.SEND, TelegramInputKind.ENTER])
        self.assertEqual(actions[0].text, "你好")

    def test_debug_bridge_captures_exact_final_outbound_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.record_output(
                    "MM你好Write tests for @filenameM，我在。有什么需要我处理的？g"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["type"], "message")
        self.assertEqual(records[0]["text"], "你好，我在。有什么需要我处理的？")

    def test_debug_bridge_sends_menu_as_numbered_choices_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.record_output("Select model\n❯ gpt-5\n  gpt-4\n  gpt-3\n")
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertIn("请选择模型", messages[0])
        self.assertIn("1. gpt-5", messages[0])
        self.assertIn("3. gpt-3", messages[0])
        self.assertNotEqual(messages[0], "Select model ❯ gpt-5 gpt-4 gpt-3")

    def test_debug_bridge_sends_codex_model_tui_as_numbered_choices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            history = Path(tmp) / "history.log"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(history),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.record_output(
                    "\x1b[1;1HSelect Model and Effort"
                    "\x1b[3;1H1.gpt-5.5(default)Frontier model for complex coding."
                    "\x1b[4;1H2.gpt-5.4Strong model for everyday coding."
                    "\x1b[5;1H› 3. gpt-5.4-mini (current)  Small, fast model."
                    "\x1b[6;1H4.gpt-5.3-codexCoding-optimized model."
                    "\x1b[8;1HPress enter to confirm or esc to go back"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)
            history_text = history.read_text(encoding="utf-8") if history.exists() else ""

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertIn("请选择模型", messages[0])
        self.assertIn("1. gpt-5.5", messages[0])
        self.assertIn("3. gpt-5.4-mini *", messages[0])
        self.assertNotIn("Frontier model", messages[0])
        self.assertEqual(history_text, "")

    def test_debug_bridge_sends_codex_resume_tui_as_numbered_choices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.record_output(
                    "\x1b[?1049h\x1b[1;1HResume a previous session"
                    "\x1b[3;2HType to search"
                    "\x1b[5;3H❯ 13s ago     IMU-Synthesis"
                    "\x1b[6;5H8h ago      Hi你好"
                    "\x1b[10;1Henter resume   esc exit   ↑/↓ browse"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertIn("请选择会话", messages[0])
        self.assertIn("1. 13s ago IMU-Synthesis *", messages[0])
        self.assertIn("2. 8h ago Hi你好", messages[0])
        self.assertNotIn("enter resume", messages[0])

    def test_debug_bridge_sends_codex_approval_prompt_without_waiting_for_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            history = Path(tmp) / "history.log"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(history),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=999,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.record_output(
                    "\x1b[9;3HWould you like to run the following command?"
                    "\x1b[11;3HReason: 是否允许我终止过慢诊断进程？"
                    "\x1b[14;3H$ pkill -f diagnose_xsens_to_mocap_bone_chain.py"
                    "\x1b[16;1H› 1. Yes, proceed (y)"
                    "\x1b[17;3H2. Yes, and don't ask again for commands that start with "
                    "`pkill -f diagnose_xsens_to_mocap_bone_chain.py` (p)"
                    "\x1b[18;3H3. No, and tell Codex what to do differently (esc)"
                    "\x1b[20;3HPress enter to confirm or esc to cancel"
                    "\x1b]0;[ ! ] Action Required | ReID_imu_generation\x07"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)
            history_text = history.read_text(encoding="utf-8") if history.exists() else ""

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertIn("请选择是否允许", messages[0])
        self.assertIn("1. Yes, proceed (y) *", messages[0])
        self.assertIn("2. Yes, and don't ask again", messages[0])
        self.assertIn("3. No, and tell Codex", messages[0])
        self.assertEqual(history_text, "")

    def test_extract_kimi_approval_menu_from_boxed_panel(self) -> None:
        raw = (
            "\x1b[0;33m╭─ approval ─────────────────────────╮\x1b[0m\n"
            "\x1b[0;33m│\x1b[0m  Shell is requesting approval to run command:  "
            "\x1b[0;33m│\x1b[0m\n"
            "\x1b[0;33m│\x1b[0m  find /data/lyxie/WHAM -maxdepth 3 -type f  "
            "\x1b[0;33m│\x1b[0m\n"
            "\x1b[0;33m│\x1b[0m \x1b[0;36m→ [1] Approve once\x1b[0m "
            "\x1b[0;33m│\x1b[0m\n"
            "\x1b[0;33m│\x1b[0m\n"
            "\x1b[0;33m│\x1b[0m \x1b[0;38;5;244m  [2] Approve for this session\x1b[0m "
            "\x1b[0;33m│\x1b[0m\n"
            "\x1b[0;33m│\x1b[0m\n"
            "\x1b[0;33m│\x1b[0m \x1b[0;38;5;244m  [3] Reject\x1b[0m "
            "\x1b[0;33m│\x1b[0m\n"
            "\x1b[0;33m│\x1b[0m\n"
            "\x1b[0;33m│\x1b[0m \x1b[0;38;5;244m  [4] Reject, tell the model what to do instead\x1b[0m "
            "\x1b[0;33m│\x1b[0m\n"
            "\x1b[0;33m╰────────────────────────────────────╯\x1b[0m"
        )

        menu = _extract_selection_menu(raw)

        self.assertIsNotNone(menu)
        assert menu is not None
        self.assertEqual(menu.title, "请选择是否允许：")
        self.assertEqual(
            menu.options,
            (
                "Approve once",
                "Approve for this session",
                "Reject",
                "Reject, tell the model what to do instead",
            ),
        )
        self.assertEqual(menu.selected_index, 0)

    def test_debug_bridge_sends_kimi_approval_prompt_without_waiting_for_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=999,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.record_output(
                    "\x1b[0;33m╭─ approval ─────────────────────────╮\x1b[0m\r\n"
                    "\x1b[0;33m│\x1b[0m  Shell is requesting approval to run command:  "
                    "\x1b[0;33m│\x1b[0m\r\n"
                    "\x1b[0;33m│\x1b[0m  find /data/lyxie/WHAM -maxdepth 3 -type f  "
                    "\x1b[0;33m│\x1b[0m\r\n"
                    "\x1b[0;33m│\x1b[0m \x1b[0;36m→ [1] Approve once\x1b[0m "
                    "\x1b[0;33m│\x1b[0m\r\n"
                    "\x1b[0;33m│\x1b[0m\r\n"
                    "\x1b[0;33m│\x1b[0m \x1b[0;38;5;244m  [2] Approve for this session\x1b[0m "
                    "\x1b[0;33m│\x1b[0m\r\n"
                    "\x1b[0;33m│\x1b[0m\r\n"
                    "\x1b[0;33m│\x1b[0m \x1b[0;38;5;244m  [3] Reject\x1b[0m "
                    "\x1b[0;33m│\x1b[0m\r\n"
                    "\x1b[0;33m│\x1b[0m\r\n"
                    "\x1b[0;33m│\x1b[0m \x1b[0;38;5;244m  [4] Reject, tell the model what to do instead\x1b[0m "
                    "\x1b[0;33m│\x1b[0m\r\n"
                    "\x1b[0;33m╰────────────────────────────────────╯\x1b[0m"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertIn("请选择是否允许", messages[0])
        self.assertIn("1. Approve once *", messages[0])
        self.assertIn("2. Approve for this session", messages[0])
        self.assertIn("3. Reject", messages[0])
        self.assertIn("4. Reject, tell the model", messages[0])

    def test_debug_bridge_sends_kimi_sessions_as_numbered_choices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=999,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.record_output(
                    "\x1b[?1049h"
                    "\x1b[1;1H┌────────────────────| Sessions |────────────────────┐"
                    "\x1b[3;1H│ ❯ WHAM                                            │"
                    "\x1b[4;1H│   22m ago · c9caecf5                              │"
                    "\x1b[5;1H│   hi                                              │"
                    "\x1b[6;1H│   40m ago · 451c3264                              │"
                    "\x1b[7;1H│   1                                               │"
                    "\x1b[8;1H│   44m ago · 4a4914b9                              │"
                    "\x1b[9;1H│   Hi                                              │"
                    "\x1b[10;1H│   58m ago · f446825d                             │"
                    "\x1b[12;1H└───────────────────────────────────────────────────┘"
                    "\x1b[13;1H Ctrl+A to show all projects · Enter to select · Esc to cancel"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertIn("请选择会话", messages[0])
        self.assertIn("1. WHAM (22m ago · c9caecf5) *", messages[0])
        self.assertIn("2. hi (40m ago · 451c3264)", messages[0])
        self.assertIn("3. 1 (44m ago · 4a4914b9)", messages[0])
        self.assertIn("4. Hi (58m ago · f446825d)", messages[0])

    def test_debug_bridge_waits_for_chunked_kimi_sessions_panel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=999,
                    summary_threshold_chars=1000,
                )
            )
            try:
                chunks = [
                    "\x1b[?1049h\x1b[H"
                    "\x1b[0;38;5;81;48;5;235;1m SESSIONS (1 of 4) \x1b[0m\r",
                    "\x1b[0;38;5;24m┌────────────────|"
                    "\x1b[0;38;5;81;48;5;234;1m Sessions "
                    "\x1b[0;38;5;24m|────────────────┐\x1b[0m\r",
                    "\x1b[3;1H│ \x1b[1m❯ WHAM\x1b[0m │\r"
                    "\x1b[4;1H│   22m ago · c9caecf5 │\r",
                    "\x1b[5;1H│   hi │\r"
                    "\x1b[6;1H│   40m ago · 451c3264 │\r",
                    "\x1b[7;1H│   1 │\r"
                    "\x1b[8;1H│   44m ago · 4a4914b9 │\r",
                    "\x1b[9;1H│   Hi │\r"
                    "\x1b[10;1H│   58m ago · f446825d │\r",
                ]
                for index, chunk in enumerate(chunks):
                    bridge.record_output(chunk)
                    bridge.flush_idle_output()
                    if index < len(chunks) - 1 and outbox.exists():
                        self.assertEqual(outbox.read_text(encoding="utf-8"), "")
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("请选择会话：\n回复数字选择"))
        self.assertIn("1. WHAM (22m ago · c9caecf5) *\n2. hi", messages[0])
        self.assertIn("3. 1 (44m ago · 4a4914b9)", messages[0])
        self.assertIn("4. Hi (58m ago · f446825d)", messages[0])

    def test_debug_bridge_sends_partial_kimi_resume_panel_as_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=999,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.record_output(
                    "SESSIONS (1 of 1)  [current directory]\n"
                    "┌──────────────────────────────────|  Sessions  |───────────────────────────────────┐\n"
                    "│                                                                                   │\n"
                    "│ ❯ Novel Team                                                                      │\n"
                    "│   31m ago · 073b7057                                                              │\n"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertIn("请选择会话", messages[0])
        self.assertIn("1. Novel Team (31m ago · 073b7057) *", messages[0])
        self.assertNotIn("SESSIONS", messages[0])
        self.assertNotIn("┌", messages[0])

    def test_debug_bridge_suppresses_kimi_sessions_redraw_after_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge._pending_menu = SelectionMenu(
                    "请选择会话：",
                    (
                        "WHAM (22m ago · c9caecf5)",
                        "hi (40m ago · 451c3264)",
                        "1 (44m ago · 4a4914b9)",
                    ),
                    0,
                )
                action = bridge.consume_menu_choice("3")
                self.assertEqual(action, TelegramInput(TelegramInputKind.KEY, "down down enter"))
                bridge.record_output(
                    " WHAM\n"
                    " 22m ago · c9caecf5\n"
                    "❯ hi\n"
                    " 40m ago · 451c32643\n"
                    " hi\n"
                    " 40m ago · 451c3264\n"
                    "❯ 1\n"
                    " 44m ago · 4a4914b94\n"
                    " Ctrl+A to show all projects · Enter to select · Esc to cancel\n"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(messages, [])

    def test_kimi_normal_reply_is_not_forwarded_as_terminal_menu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.record_output(
                    "\x1b[1;1Hagent (Kimi-k2.6 ●)  /data/lyxie/WHAM"
                    "\x1b[2;1H@: mention files | ctrl-x: toggle mode"
                    "\x1b[4;1H• Hello! You entered \"1\" — could you clarify what you'd like me to do?"
                    "\x1b[6;3HFor example, are you:"
                    "\x1b[7;3H• Selecting an option from a previous conversation?"
                    "\x1b[8;3H• Referring to a specific task or step?"
                    "\x1b[9;3H• Looking for help with something specific in this WHAM project?"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertNotIn("请选择终端菜单项", messages[0])
        self.assertIn("Hello! You entered", messages[0])

    def test_debug_bridge_sends_restored_alt_screen_resume_menu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.record_output(
                    "\x1b[?1049h\x1b[1;1HResume a previous session"
                    "\x1b[3;2HType to search"
                    "\x1b[5;3H❯ 38m ago     Motion-X"
                    "\x1b[6;5H10h ago     Hi你好"
                    "\x1b[10;1Henter resume   esc exit   ↑/↓ browse"
                    "\x1b[?1049l\x1b[4;1H› Implement {feature}"
                    "\x1b[6;3Hgpt-5.5 xhigh · /data/lyxie/Motion-X"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 1)
        self.assertIn("请选择会话", messages[0])
        self.assertIn("1. 38m ago Motion-X *", messages[0])
        self.assertIn("2. 10h ago Hi你好", messages[0])

    def test_debug_bridge_large_lifestyle_matrix_matches_real_outbox(self) -> None:
        messages = [
            "我喜欢肖邦，也很欣赏德彪西和拉赫玛尼诺夫。",
            "晚饭可以吃清淡一点，比如番茄鸡蛋面。",
            "周末去公园散步会很舒服。",
            "这本书适合慢慢读，不用急。",
            "如果你想放松，可以听一点巴赫或爵士。",
        ]
        prefixes = [
            "",
            "MM",
            "Mimprove documentation in @filenames  esc to interupt)ngg",
            "⠼ TeleAgent •Working(0s • esc to interrupt)\n",
        ]
        infixes = [
            "",
            "Write tests for @filenamesM",
            "Review code for bugsM",
            "Explain this codebase (0sM",
        ]
        suffixes = ["", "g", "ngg", "orking"]

        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            expected_texts: list[str] = []
            try:
                for message in messages:
                    split_at = max(1, len(message) // 2)
                    for prefix in prefixes:
                        for infix in infixes:
                            for suffix in suffixes:
                                raw = prefix + message[:split_at] + infix + message[split_at:] + suffix
                                bridge.record_output(raw)
                                bridge.flush_idle_output()
                                expected_texts.append(message)
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        actual_texts = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(actual_texts, expected_texts)

    def test_debug_bridge_filters_codex_tui_noise_before_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.record_output(
                    "Explored\n"
                    "  └ Search def run_wrapped in wrapper.py\n"
                    "•(53s •  · 3 background terminals running · /ps to view · /stop to close\n"
                    "我已经能看出主线：它把任意交互式终端程序当成黑盒。"
                    "Exploring  └ Search class TelegramBridge in telegram.py"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["text"],
            "我已经能看出主线：它把任意交互式终端程序当成黑盒。",
        )

    def test_debug_bridge_replays_split_reid_tui_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            history = Path(tmp) / "history.log"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(history),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=4000,
                )
            )
            try:
                bridge.mark_user_input("继续吧")
                chunks = [
                    "一句话结论：我们确认了真值事件窗口能救 acc_z。›tab to queue message",
                    "33% context left1ingngg23ingngg45ingngg67ingnggM\n",
                    "继续吧›Run /review on my current changesgpt-5.5 xhigh · /data/lyxie/ReID_imu_generation\n",
                    "king8ingngg920ingngg1我接着把刚生成的 acc_z 触发版评估完。重点看三件事。",
                    "Updated Plan\n    □ Compare worst S2 right-arm acc_z cases against baseline\n",
                    "Evaluating mocap_geom_forearm_upperarm_smooth4_best_subject_cluster_despiked_accz_triggered / S1_acting2 ...\n",
                    "Wrote outputs/sequence_calibrated_reports/low_correlation_axis_diagnostics_accz_triggered.csv\n",
                ]
                for chunk in chunks:
                    bridge.record_output(chunk)
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)
            history_text = history.read_text(encoding="utf-8")

        messages = [record["text"] for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 1)
        for text in (messages[0], history_text):
            self.assertIn("一句话结论：", text)
            self.assertIn("我接着把刚生成的 acc_z 触发版评估完。重点看三件事。", text)
            for noise in (
                "tab to queue",
                "context left",
                "Run /review",
                "gpt-5.5",
                "ingngg",
                "king",
                "Updated Plan",
                "Evaluating",
                "Wrote outputs",
            ):
                self.assertNotIn(noise, text)

    def test_debug_bridge_filters_reid_spinner_fragment_line_before_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.record_output(
                    "m•••••\n"
                    "*****Work\n"
                    "***99\n"
                    ";138;49mg•••orking•rking•kinging•ngg•••••••"
                    "orking•rking•kinging•ngg6M\n"
                    "\n"
                    "一句话结论：roll/local gravity 问题已基本打通。"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["text"],
            "一句话结论：roll/local gravity 问题已基本打通。",
        )

    def test_long_output_sends_summary_reply_when_model_returns_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=100,
                    summary_max_chars=800,
                    summary_timeout_seconds=999,
                )
            )
            try:
                bridge.record_output("这是很长的输出。" * 20)
                prompt = bridge.flush_idle_output()
                self.assertIsNotNone(prompt)
                bridge.mark_injected_prompt(prompt or "")

                bridge.record_output(
                    (prompt or "")
                    + "一句话结论：这是一段摘要。\n"
                    "- 关键点一。\n"
                    "- 关键点二。"
                )
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [str(record["text"]) for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 2)
        self.assertIn("输出较长", messages[0])
        self.assertIn("一句话结论：这是一段摘要。", messages[1])
        self.assertIn("- 关键点一。", messages[1])
        self.assertNotIn("请把你刚才过长", messages[1])

    def test_long_output_summary_timeout_sends_fallback_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=100,
                    summary_timeout_seconds=0,
                    summary_fallback_chars=120,
                )
            )
            try:
                bridge.record_output("这是很长的原始输出。" * 20)
                prompt = bridge.flush_idle_output()
                self.assertIsNotNone(prompt)
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        messages = [str(record["text"]) for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 3)
        self.assertIn("输出较长", messages[0])
        self.assertIn("摘要请求超时", messages[1])
        self.assertIn("这是很长的原始输出。", messages[2])
        self.assertIn("...[truncated]", messages[2])

    def test_auto_continue_returns_prompt_after_short_model_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.handle_command("/auto start")
                bridge.record_output("一句话结论：阶段完成。")
                prompt = bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        self.assertEqual(prompt, "请继续推进,注意在合适的时候记录进展")
        messages = [str(record["text"]) for record in records if record["type"] == "message"]
        self.assertIn("已开启自动推进模式", messages[0])
        self.assertIn("一句话结论：阶段完成。", messages[1])

    def test_auto_continue_end_stops_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.handle_command("/auto start")
                bridge.handle_command("/auto end")
                bridge.record_output("一句话结论：阶段完成。")
                prompt = bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        self.assertIsNone(prompt)
        messages = [str(record["text"]) for record in records if record["type"] == "message"]
        self.assertTrue(any("已关闭自动推进模式" in message for message in messages))
        self.assertIn("一句话结论：阶段完成。", messages[-1])

    def test_auto_continue_timer_expiry_stops_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.handle_command("/auto 7.5")
                bridge._auto_continue_until = time.monotonic() - 1
                bridge.record_output("一句话结论：阶段完成。")
                prompt = bridge.flush_idle_output()
                second_prompt = bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        self.assertIsNone(prompt)
        self.assertIsNone(second_prompt)
        messages = [str(record["text"]) for record in records if record["type"] == "message"]
        self.assertTrue(any("自动推进模式已到期" in message for message in messages))
        self.assertTrue(any("一句话结论：阶段完成。" in message for message in messages))

    def test_auto_continue_waits_for_summary_before_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=100,
                    summary_max_chars=800,
                    summary_timeout_seconds=999,
                )
            )
            try:
                bridge.handle_command("/auto start")
                bridge.record_output("这是很长的输出。" * 20)
                summary_prompt = bridge.flush_idle_output()
                self.assertIsNotNone(summary_prompt)
                assert summary_prompt is not None
                self.assertIn("总结成", summary_prompt)
                bridge.mark_injected_prompt(summary_prompt)
                bridge.record_output(summary_prompt + "一句话结论：这是摘要。")
                auto_prompt = bridge.flush_idle_output()
            finally:
                bridge.close()

        self.assertEqual(auto_prompt, "请继续推进,注意在合适的时候记录进展")

    def test_auto_continue_does_not_prompt_while_background_terminal_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                bridge.handle_command("/auto start")
                bridge.record_output(
                    "一句话结论：当前 sweep 还在跑。"
                    "\n• Working (1m 02s • esc to interrupt) · "
                    "1 background terminal running · /ps to view · /stop to close"
                )
                prompt = bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        self.assertIsNone(prompt)
        messages = [str(record["text"]) for record in records if record["type"] == "message"]
        self.assertIn("一句话结论：当前 sweep 还在跑。", messages[-1])

    def test_long_terminal_output_with_active_background_does_not_request_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=100,
                    summary_max_chars=800,
                    summary_timeout_seconds=0,
                )
            )
            try:
                bridge.handle_command("/auto start")
                bridge.record_output(
                    ("一句话结论：当前任务仍在跑，已经有阶段性结果。" * 12)
                    + "\n• Working (14m 29s • esc to interrupt) · "
                    + "1 background terminal running · /ps to view · /stop to close"
                )
                prompt = bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        self.assertIsNone(prompt)
        self.assertTrue(
            _terminal_raw_has_active_status(
                "1 background terminal running · /ps to view · /stop to close"
            )
        )
        messages = [str(record["text"]) for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 2)
        self.assertIn("已开启自动推进模式", messages[0])
        self.assertIn("暂不自动插入摘要请求", messages[1])

    def test_long_output_without_auto_summary_sends_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    idle_forward_seconds=0,
                    summary_threshold_chars=100,
                    summary_fallback_chars=120,
                    auto_summary=False,
                )
            )
            try:
                bridge.record_output("自动摘要关闭时也应该发出原始长输出预览。" * 20)
                prompt = bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        self.assertIsNone(prompt)
        messages = [str(record["text"]) for record in records if record["type"] == "message"]
        self.assertEqual(len(messages), 2)
        self.assertIn("自动摘要已关闭", messages[0])
        self.assertIn("自动摘要关闭时也应该发出原始长输出预览。", messages[1])
        self.assertIn("...[truncated]", messages[1])

    def test_forward_pattern_match_does_not_idle_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            bridge = TelegramBridge(
                TelegramConfig(
                    enabled=True,
                    debug_mode=True,
                    allowed_chat_ids=(0,),
                    debug_inbox_path=str(Path(tmp) / "inbox.txt"),
                    debug_outbox_path=str(outbox),
                    history_path=str(Path(tmp) / "history.log"),
                    raw_history_path=str(Path(tmp) / "raw.log"),
                    forward_patterns=(
                        re.compile(
                            r"Final answer:\s*(.*)",
                            re.IGNORECASE | re.MULTILINE | re.DOTALL,
                        ),
                    ),
                    idle_forward_seconds=0,
                    summary_threshold_chars=1000,
                )
            )
            try:
                raw = "Final answer: hello\nNext? "
                bridge.record_output(raw)
                self.assertTrue(bridge.maybe_forward_output(raw))
                bridge.flush_idle_output()
            finally:
                bridge.close()

            records = _read_debug_records(outbox)

        self.assertEqual(
            [record["text"] for record in records if record["type"] == "message"],
            ["hello Next?"],
        )


def _drain_debug_actions(
    bridge: TelegramBridge,
    *,
    expected_count: int,
    timeout_seconds: float = 2.0,
) -> list[object]:
    actions: list[object] = []
    deadline = time.monotonic() + timeout_seconds
    while len(actions) < expected_count and time.monotonic() < deadline:
        readable, _, _ = select.select([bridge.read_fd], [], [], 0.1)
        if bridge.read_fd in readable:
            actions.extend(bridge.drain_replies())
    return actions


def _read_debug_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _capture_written_telegram_input(
    action: TelegramInput,
    config: TelegramConfig,
) -> bytes:
    read_fd, write_fd = os.pipe()
    try:
        _write_telegram_input(write_fd, action, config)
        os.close(write_fd)
        write_fd = -1
        return os.read(read_fd, 4096)
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)


if __name__ == "__main__":
    unittest.main()

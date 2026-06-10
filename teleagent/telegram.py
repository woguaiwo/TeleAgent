from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path


INLINE_NOISE_PHRASES = (
    "Write tests for @filename",
    "Write tests for @filenames",
    "Improve documentation in @filename",
    "Improve documentation in @filenames",
    "Summarize recent commits",
    "Review code for bugs",
    "Explain this codebase",
    "Explain selected code",
    "Generate a plan",
    "Fix this bug",
    "Implement feature",
    "Implement {feature}",
    "Improve code quality",
    "Run /review on my current changes",
    "Use /skills to list available skills",
)

_CODEX_ROLLOUT_INITIAL_READ_LIMIT = 5_000_000
_CODEX_ROLLOUT_DISCOVERY_GRACE_SECONDS = 30.0
_KIMI_SESSION_INITIAL_READ_LIMIT = 5_000_000
_KIMI_SESSION_DISCOVERY_GRACE_SECONDS = 30.0
_RECENT_OUTPUT_FINGERPRINT_TTL_SECONDS = 60.0
_MENU_CHOICE_SUPPRESS_SECONDS = 2.0
_AUTO_CONTINUE_PROMPT = "请继续推进,注意在合适的时候记录进展"


class TelegramInputKind(Enum):
    SEND = "send"
    TYPE = "type"
    ENTER = "enter"
    KEY = "key"
    IGNORE = "ignore"
    COMMAND = "command"


@dataclass(frozen=True)
class TelegramInput:
    kind: TelegramInputKind
    text: str = ""


@dataclass(frozen=True)
class SelectionMenu:
    title: str
    options: tuple[str, ...]
    selected_index: int

    @property
    def fingerprint(self) -> str:
        return "\n".join([str(self.selected_index), self.title, *self.options])


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    token: str = ""
    token_env: str = "TELEGRAM_BOT_TOKEN"
    token_file: str = ""
    allowed_chat_ids: tuple[int, ...] = ()
    debug_mode: bool = False
    debug_inbox_path: str = "teleagent-debug-inbox.txt"
    debug_outbox_path: str = "teleagent-debug-outbox.jsonl"
    debug_chat_id: int = 0
    forward_patterns: tuple[re.Pattern[str], ...] = ()
    poll_timeout: int = 20
    max_message_chars: int = 3500
    history_path: str = "teleagent-history.log"
    raw_history_path: str = "teleagent-raw.log"
    output_mode: str = "summary"
    output_sources: tuple[str, ...] = ("terminal",)
    codex_state_root: str = "~/.codex"
    kimi_state_root: str = "~/.kimi"
    idle_forward_seconds: float = 3.0
    all_chunk_chars: int = 2500
    summary_threshold_chars: int = 1200
    summary_max_chars: int = 800
    auto_summary: bool = True
    summary_timeout_seconds: float = 30.0
    summary_fallback_chars: int = 3500
    input_submit_delay_seconds: float = 0.05
    input_submit_keys: tuple[str, ...] = ("enter", "linefeed")
    slash_submit_delay_seconds: float = 0.2
    slash_submit_keys: tuple[str, ...] = ("enter",)
    summary_submit_delay_seconds: float = 0.2
    summary_submit_keys: tuple[str, ...] = ("enter", "linefeed")
    summary_prompt_template: str = (
        "请把你刚才过长的回复总结成 {max_chars} 字以内。"
        "面向手机聊天阅读：先给一句话结论，再用短条目列关键点；不要复述完整原文。"
    )

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TelegramConfig":
        enabled = data.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("telegram.enabled must be a boolean")

        token = data.get("token", "")
        token_env = data.get("token_env", "TELEGRAM_BOT_TOKEN")
        token_file = data.get("token_file", "")
        if not isinstance(token, str):
            raise ValueError("telegram.token must be a string")
        if not isinstance(token_env, str) or not token_env:
            raise ValueError("telegram.token_env must be a non-empty string")
        if not isinstance(token_file, str):
            raise ValueError("telegram.token_file must be a string")

        debug_mode = data.get("debug_mode", False)
        debug_inbox_path = data.get("debug_inbox_path", "teleagent-debug-inbox.txt")
        debug_outbox_path = data.get("debug_outbox_path", "teleagent-debug-outbox.jsonl")
        debug_chat_id = data.get("debug_chat_id", 0)
        if not isinstance(debug_mode, bool):
            raise ValueError("telegram.debug_mode must be a boolean")
        if not isinstance(debug_inbox_path, str) or not debug_inbox_path:
            raise ValueError("telegram.debug_inbox_path must be a non-empty string")
        if not isinstance(debug_outbox_path, str) or not debug_outbox_path:
            raise ValueError("telegram.debug_outbox_path must be a non-empty string")
        if not isinstance(debug_chat_id, int):
            raise ValueError("telegram.debug_chat_id must be an integer")

        allowed_chat_ids_raw = data.get("allowed_chat_ids", [])
        if not isinstance(allowed_chat_ids_raw, list) or not all(
            isinstance(item, int) for item in allowed_chat_ids_raw
        ):
            raise ValueError("telegram.allowed_chat_ids must be a list of integers")
        allowed_chat_ids = tuple(allowed_chat_ids_raw)
        if enabled and debug_mode and not allowed_chat_ids:
            allowed_chat_ids = (debug_chat_id,)

        patterns_raw = data.get("forward_patterns", [])
        if not isinstance(patterns_raw, list) or not all(
            isinstance(item, str) for item in patterns_raw
        ):
            raise ValueError("telegram.forward_patterns must be a list of strings")

        poll_timeout = data.get("poll_timeout", 20)
        max_message_chars = data.get("max_message_chars", 3500)
        if not isinstance(poll_timeout, int) or poll_timeout < 1:
            raise ValueError("telegram.poll_timeout must be an integer >= 1")
        if not isinstance(max_message_chars, int) or max_message_chars < 100:
            raise ValueError("telegram.max_message_chars must be an integer >= 100")
        history_path = data.get("history_path", "teleagent-history.log")
        raw_history_path = data.get("raw_history_path", "teleagent-raw.log")
        output_mode = data.get("output_mode", "summary")
        output_sources_raw = data.get("output_sources", ["terminal"])
        codex_state_root = data.get("codex_state_root", "~/.codex")
        kimi_state_root = data.get("kimi_state_root", "~/.kimi")
        idle_forward_seconds = data.get("idle_forward_seconds", 3.0)
        all_chunk_chars = data.get("all_chunk_chars", 2500)
        summary_threshold_chars = data.get("summary_threshold_chars", 1200)
        summary_max_chars = data.get("summary_max_chars", 800)
        auto_summary = data.get("auto_summary", True)
        summary_timeout_seconds = data.get("summary_timeout_seconds", 30.0)
        summary_fallback_chars = data.get("summary_fallback_chars", 3500)
        input_submit_delay_seconds = data.get("input_submit_delay_seconds", 0.05)
        input_submit_keys = data.get("input_submit_keys", ["enter", "linefeed"])
        slash_submit_delay_seconds = data.get("slash_submit_delay_seconds", 0.2)
        slash_submit_keys = data.get("slash_submit_keys", ["enter"])
        summary_submit_delay_seconds = data.get("summary_submit_delay_seconds", 0.2)
        summary_submit_keys = data.get("summary_submit_keys", ["enter", "linefeed"])
        summary_prompt_template = data.get(
            "summary_prompt_template",
            "请把你刚才过长的回复总结成 {max_chars} 字以内。"
            "面向手机聊天阅读：先给一句话结论，再用短条目列关键点；不要复述完整原文。",
        )
        if not isinstance(history_path, str) or not history_path:
            raise ValueError("telegram.history_path must be a non-empty string")
        if not isinstance(raw_history_path, str) or not raw_history_path:
            raise ValueError("telegram.raw_history_path must be a non-empty string")
        if output_mode not in ("summary", "all"):
            raise ValueError('telegram.output_mode must be "summary" or "all"')
        if not isinstance(output_sources_raw, list) or not output_sources_raw:
            raise ValueError("telegram.output_sources must be a non-empty list of strings")
        output_sources: list[str] = []
        for item in output_sources_raw:
            if not isinstance(item, str) or not item:
                raise ValueError("telegram.output_sources must be a non-empty list of strings")
            normalized_source = _normalize_output_source_name(item)
            if normalized_source not in {"codex_rollout", "kimi_context", "kimi_wire", "terminal"}:
                raise ValueError(
                    "telegram.output_sources only supports terminal, codex_rollout, "
                    "kimi_context, and kimi_wire"
                )
            if normalized_source not in output_sources:
                output_sources.append(normalized_source)
        if not isinstance(codex_state_root, str) or not codex_state_root:
            raise ValueError("telegram.codex_state_root must be a non-empty string")
        if not isinstance(kimi_state_root, str) or not kimi_state_root:
            raise ValueError("telegram.kimi_state_root must be a non-empty string")
        if not isinstance(idle_forward_seconds, int | float) or idle_forward_seconds < 0:
            raise ValueError("telegram.idle_forward_seconds must be a number >= 0")
        if not isinstance(all_chunk_chars, int) or all_chunk_chars < 100:
            raise ValueError("telegram.all_chunk_chars must be an integer >= 100")
        if not isinstance(summary_threshold_chars, int) or summary_threshold_chars < 100:
            raise ValueError("telegram.summary_threshold_chars must be an integer >= 100")
        if not isinstance(summary_max_chars, int) or summary_max_chars < 100:
            raise ValueError("telegram.summary_max_chars must be an integer >= 100")
        if not isinstance(auto_summary, bool):
            raise ValueError("telegram.auto_summary must be a boolean")
        if not isinstance(summary_timeout_seconds, int | float) or summary_timeout_seconds < 0:
            raise ValueError("telegram.summary_timeout_seconds must be a number >= 0")
        if not isinstance(summary_fallback_chars, int) or summary_fallback_chars < 100:
            raise ValueError("telegram.summary_fallback_chars must be an integer >= 100")
        if not isinstance(input_submit_delay_seconds, int | float) or input_submit_delay_seconds < 0:
            raise ValueError("telegram.input_submit_delay_seconds must be a number >= 0")
        if not isinstance(input_submit_keys, list) or not all(
            isinstance(item, str) for item in input_submit_keys
        ):
            raise ValueError("telegram.input_submit_keys must be a list of strings")
        if not isinstance(slash_submit_delay_seconds, int | float) or slash_submit_delay_seconds < 0:
            raise ValueError("telegram.slash_submit_delay_seconds must be a number >= 0")
        if not isinstance(slash_submit_keys, list) or not all(
            isinstance(item, str) for item in slash_submit_keys
        ):
            raise ValueError("telegram.slash_submit_keys must be a list of strings")
        if not isinstance(summary_submit_delay_seconds, int | float) or summary_submit_delay_seconds < 0:
            raise ValueError("telegram.summary_submit_delay_seconds must be a number >= 0")
        if not isinstance(summary_submit_keys, list) or not all(
            isinstance(item, str) for item in summary_submit_keys
        ):
            raise ValueError("telegram.summary_submit_keys must be a list of strings")
        if not isinstance(summary_prompt_template, str) or "{max_chars}" not in summary_prompt_template:
            raise ValueError(
                "telegram.summary_prompt_template must be a string containing {max_chars}"
            )

        return cls(
            enabled=enabled,
            token=token,
            token_env=token_env,
            token_file=token_file,
            allowed_chat_ids=allowed_chat_ids,
            debug_mode=debug_mode,
            debug_inbox_path=debug_inbox_path,
            debug_outbox_path=debug_outbox_path,
            debug_chat_id=debug_chat_id,
            forward_patterns=tuple(
                re.compile(pattern, re.IGNORECASE | re.MULTILINE | re.DOTALL)
                for pattern in patterns_raw
            ),
            poll_timeout=poll_timeout,
            max_message_chars=max_message_chars,
            history_path=history_path,
            raw_history_path=raw_history_path,
            output_mode=output_mode,
            output_sources=tuple(output_sources),
            codex_state_root=codex_state_root,
            kimi_state_root=kimi_state_root,
            idle_forward_seconds=float(idle_forward_seconds),
            all_chunk_chars=all_chunk_chars,
            summary_threshold_chars=summary_threshold_chars,
            summary_max_chars=summary_max_chars,
            auto_summary=auto_summary,
            summary_timeout_seconds=float(summary_timeout_seconds),
            summary_fallback_chars=summary_fallback_chars,
            input_submit_delay_seconds=float(input_submit_delay_seconds),
            input_submit_keys=tuple(input_submit_keys),
            slash_submit_delay_seconds=float(slash_submit_delay_seconds),
            slash_submit_keys=tuple(slash_submit_keys),
            summary_submit_delay_seconds=float(summary_submit_delay_seconds),
            summary_submit_keys=tuple(summary_submit_keys),
            summary_prompt_template=summary_prompt_template,
        )

    def resolved_token(self) -> str:
        if self.debug_mode:
            return ""
        if self.token:
            return self.token
        env_token = os.environ.get(self.token_env, "")
        if env_token:
            return env_token
        if self.token_file:
            try:
                token_path = Path(os.path.expandvars(self.token_file)).expanduser()
                return token_path.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        return ""


class TelegramClient:
    def __init__(self, token: str, *, timeout: int = 30) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._timeout = timeout

    def send_message(self, chat_id: int, text: str) -> None:
        self._request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )

    def send_document(self, chat_id: int, path: Path, *, caption: str = "") -> None:
        boundary = f"teleagent-{int(time.time() * 1000)}"
        fields = {
            "chat_id": str(chat_id),
            "caption": caption,
        }
        body = _multipart_body(boundary, fields, "document", path)
        request = urllib.request.Request(
            f"{self._base_url}/sendDocument",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            raw = response.read()
        decoded = json.loads(raw.decode())
        if not isinstance(decoded, dict) or not decoded.get("ok", False):
            raise RuntimeError(f"telegram API error: {decoded!r}")

    def get_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, object]]:
        payload: dict[str, object] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        data = self._request("getUpdates", payload, timeout=timeout + 5)
        result = data.get("result", [])
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    def _request(
        self,
        method: str,
        payload: dict[str, object],
        *,
        timeout: int | None = None,
    ) -> dict[str, object]:
        body = urllib.parse.urlencode(payload).encode()
        request = urllib.request.Request(
            f"{self._base_url}/{method}",
            data=body,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:
            raw = response.read()
        decoded = json.loads(raw.decode())
        if not isinstance(decoded, dict):
            raise RuntimeError("telegram returned a non-object response")
        if not decoded.get("ok", False):
            raise RuntimeError(f"telegram API error: {decoded!r}")
        return decoded


class _DebugTelegramClient:
    def __init__(self, inbox_path: str, outbox_path: str, *, chat_id: int) -> None:
        self._inbox_path = Path(inbox_path)
        self._outbox_path = Path(outbox_path)
        self._chat_id = chat_id
        self._position = 0
        self._partial_input = ""
        self._next_update_id = 1
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._inbox_path.parent.mkdir(parents=True, exist_ok=True)
        self._outbox_path.parent.mkdir(parents=True, exist_ok=True)
        self._inbox_path.touch(exist_ok=True)
        self._position = self._inbox_path.stat().st_size

    def send_message(self, chat_id: int, text: str) -> None:
        self._append_outbox(
            {
                "type": "message",
                "time": time.time(),
                "chat_id": chat_id,
                "text": text,
            }
        )

    def send_document(self, chat_id: int, path: Path, *, caption: str = "") -> None:
        self._append_outbox(
            {
                "type": "document",
                "time": time.time(),
                "chat_id": chat_id,
                "path": str(path),
                "caption": caption,
                "exists": path.exists(),
                "size_bytes": path.stat().st_size if path.exists() else 0,
            }
        )

    def get_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, object]]:
        del offset
        deadline = time.monotonic() + timeout
        while not self._closed.is_set():
            updates = self._read_new_updates()
            if updates:
                return updates
            if time.monotonic() >= deadline:
                return []
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return []

    def close(self) -> None:
        self._closed.set()

    def _append_outbox(self, record: dict[str, object]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._outbox_path.parent.mkdir(parents=True, exist_ok=True)
            with self._outbox_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _read_new_updates(self) -> list[dict[str, object]]:
        with self._lock:
            self._inbox_path.parent.mkdir(parents=True, exist_ok=True)
            self._inbox_path.touch(exist_ok=True)
            size = self._inbox_path.stat().st_size
            if size < self._position:
                self._position = 0
                self._partial_input = ""
            with self._inbox_path.open("rb") as handle:
                handle.seek(self._position)
                raw = handle.read()
                self._position = handle.tell()
        if not raw:
            return []

        self._partial_input += raw.decode("utf-8", errors="replace")
        parts = self._partial_input.splitlines(keepends=True)
        complete_lines: list[str] = []
        self._partial_input = ""
        for part in parts:
            if part.endswith(("\n", "\r")):
                line = part.rstrip("\r\n")
                if line:
                    complete_lines.append(line)
            else:
                self._partial_input = part

        updates: list[dict[str, object]] = []
        for line in complete_lines:
            updates.append(
                {
                    "update_id": self._next_update_id,
                    "message": {
                        "text": line,
                        "chat": {"id": self._chat_id},
                    },
                }
            )
            self._next_update_id += 1
        return updates


class _CodexRolloutOutputSource:
    def __init__(self, state_root: str, *, cwd: Path, launched_at: float) -> None:
        self._state_root = Path(os.path.expandvars(state_root)).expanduser()
        self._cwd = cwd.resolve()
        self._launched_at = launched_at
        self._path: Path | None = None
        self._offset = 0
        self._partial = ""
        self._next_discovery_at = 0.0
        self._recent_fingerprints: dict[str, float] = {}

    def poll_messages(self) -> list[str]:
        if self._path is None:
            self._discover_path()
        if self._path is None:
            return []
        lines = self._read_new_lines()
        if not lines:
            return []

        messages: list[str] = []
        seen_in_poll: set[str] = set()
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            record_ts = _jsonl_record_epoch(record)
            if record_ts is not None and record_ts < self._launched_at - 5:
                continue
            for text in _extract_codex_rollout_messages(record):
                cleaned = _clean_structured_text(text)
                if not cleaned:
                    continue
                fingerprint = _fingerprint_text(cleaned)
                if fingerprint in seen_in_poll or self._was_recently_emitted(fingerprint):
                    continue
                seen_in_poll.add(fingerprint)
                self._remember_emitted(fingerprint)
                messages.append(cleaned)
        return messages

    def _discover_path(self) -> None:
        now = time.monotonic()
        if now < self._next_discovery_at:
            return
        self._next_discovery_at = now + 1.0

        sessions_root = self._state_root / "sessions"
        if not sessions_root.exists():
            return

        candidates: list[tuple[float, Path]] = []
        for path in sessions_root.rglob("rollout-*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < self._launched_at - _CODEX_ROLLOUT_DISCOVERY_GRACE_SECONDS:
                continue
            candidates.append((stat.st_mtime, path))

        for _, path in sorted(candidates, reverse=True):
            if self._path_matches_cwd(path):
                self._path = path
                self._offset = 0
                self._partial = ""
                return

    def _path_matches_cwd(self, path: Path) -> bool:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if line_number > 40:
                        return False
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = _extract_codex_record_cwd(record)
                    if cwd is None:
                        continue
                    record_ts = _jsonl_record_epoch(record)
                    if (
                        record_ts is not None
                        and record_ts < self._launched_at - _CODEX_ROLLOUT_DISCOVERY_GRACE_SECONDS
                    ):
                        continue
                    try:
                        if Path(cwd).expanduser().resolve() == self._cwd:
                            return True
                    except OSError:
                        if str(Path(cwd).expanduser()) == str(self._cwd):
                            return True
        except OSError:
            return False
        return False

    def _read_new_lines(self) -> list[str]:
        assert self._path is not None
        try:
            size = self._path.stat().st_size
        except OSError:
            self._path = None
            self._offset = 0
            self._partial = ""
            return []

        if size < self._offset:
            self._offset = 0
            self._partial = ""

        try:
            with self._path.open("rb") as handle:
                if self._offset == 0 and size > _CODEX_ROLLOUT_INITIAL_READ_LIMIT:
                    handle.seek(size - _CODEX_ROLLOUT_INITIAL_READ_LIMIT)
                    handle.readline()
                    self._offset = handle.tell()
                else:
                    handle.seek(self._offset)
                raw = handle.read()
                self._offset = handle.tell()
        except OSError:
            self._path = None
            self._offset = 0
            self._partial = ""
            return []

        if not raw:
            return []
        text = self._partial + raw.decode("utf-8", errors="replace")
        parts = text.splitlines(keepends=True)
        lines: list[str] = []
        self._partial = ""
        for part in parts:
            if part.endswith(("\n", "\r")):
                line = part.rstrip("\r\n")
                if line:
                    lines.append(line)
            else:
                self._partial = part
        return lines

    def _was_recently_emitted(self, fingerprint: str) -> bool:
        self._drop_old_fingerprints()
        return fingerprint in self._recent_fingerprints

    def _remember_emitted(self, fingerprint: str) -> None:
        self._recent_fingerprints[fingerprint] = time.monotonic()
        self._drop_old_fingerprints()

    def _drop_old_fingerprints(self) -> None:
        cutoff = time.monotonic() - _RECENT_OUTPUT_FINGERPRINT_TTL_SECONDS
        for fingerprint, timestamp in list(self._recent_fingerprints.items()):
            if timestamp < cutoff:
                self._recent_fingerprints.pop(fingerprint, None)


class _KimiSessionJsonlOutputSource:
    def __init__(
        self,
        state_root: str,
        *,
        cwd: Path,
        launched_at: float,
        file_name: str,
        start_at_end: bool,
    ) -> None:
        self._state_root = Path(os.path.expandvars(state_root)).expanduser()
        self._cwd = cwd.resolve()
        self._launched_at = launched_at
        self._file_name = file_name
        self._start_at_end = start_at_end
        self._path: Path | None = None
        self._offset = 0
        self._partial = ""
        self._next_discovery_at = 0.0
        self._recent_fingerprints: dict[str, float] = {}

    def poll_messages(self) -> list[str]:
        self._discover_path()
        if self._path is None:
            return []
        return self._dedupe_messages(self._extract_messages(self._read_new_lines()))

    def _extract_messages(self, lines: list[str]) -> list[str]:
        raise NotImplementedError

    def _discover_path(self) -> None:
        now = time.monotonic()
        if now < self._next_discovery_at:
            return
        self._next_discovery_at = now + 1.0

        candidates: list[tuple[float, Path]] = []
        for root in _kimi_workdir_session_roots(self._state_root, self._cwd):
            if not root.exists():
                continue
            try:
                session_dirs = [path for path in root.iterdir() if path.is_dir()]
            except OSError:
                continue
            for session_dir in session_dirs:
                path = session_dir / self._file_name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                candidates.append((stat.st_mtime, path))
        if not candidates:
            return

        _, best_path = max(candidates, key=lambda item: item[0])
        if self._path == best_path:
            return
        if self._path is not None:
            try:
                current_mtime = self._path.stat().st_mtime
                best_mtime = best_path.stat().st_mtime
            except OSError:
                current_mtime = 0.0
                best_mtime = 1.0
            if best_mtime <= current_mtime:
                return

        self._path = best_path
        try:
            self._offset = best_path.stat().st_size if self._start_at_end else 0
        except OSError:
            self._offset = 0
        self._partial = ""

    def _read_new_lines(self) -> list[str]:
        assert self._path is not None
        try:
            size = self._path.stat().st_size
        except OSError:
            self._path = None
            self._offset = 0
            self._partial = ""
            return []

        if size < self._offset:
            self._offset = 0
            self._partial = ""

        try:
            with self._path.open("rb") as handle:
                if self._offset == 0 and size > _KIMI_SESSION_INITIAL_READ_LIMIT:
                    handle.seek(size - _KIMI_SESSION_INITIAL_READ_LIMIT)
                    handle.readline()
                    self._offset = handle.tell()
                else:
                    handle.seek(self._offset)
                raw = handle.read()
                self._offset = handle.tell()
        except OSError:
            self._path = None
            self._offset = 0
            self._partial = ""
            return []

        if not raw:
            return []
        text = self._partial + raw.decode("utf-8", errors="replace")
        parts = text.splitlines(keepends=True)
        lines: list[str] = []
        self._partial = ""
        for part in parts:
            if part.endswith(("\n", "\r")):
                line = part.rstrip("\r\n")
                if line:
                    lines.append(line)
            else:
                self._partial = part
        return lines

    def _dedupe_messages(self, messages: list[str]) -> list[str]:
        output: list[str] = []
        seen_in_poll: set[str] = set()
        for text in messages:
            cleaned = _clean_structured_text(text)
            if not cleaned:
                continue
            fingerprint = _fingerprint_text(cleaned)
            if fingerprint in seen_in_poll or self._was_recently_emitted(fingerprint):
                continue
            seen_in_poll.add(fingerprint)
            self._remember_emitted(fingerprint)
            output.append(cleaned)
        return output

    def _was_recently_emitted(self, fingerprint: str) -> bool:
        self._drop_old_fingerprints()
        return fingerprint in self._recent_fingerprints

    def _remember_emitted(self, fingerprint: str) -> None:
        self._recent_fingerprints[fingerprint] = time.monotonic()
        self._drop_old_fingerprints()

    def _drop_old_fingerprints(self) -> None:
        cutoff = time.monotonic() - _RECENT_OUTPUT_FINGERPRINT_TTL_SECONDS
        for fingerprint, timestamp in list(self._recent_fingerprints.items()):
            if timestamp < cutoff:
                self._recent_fingerprints.pop(fingerprint, None)


class _KimiContextOutputSource(_KimiSessionJsonlOutputSource):
    def __init__(self, state_root: str, *, cwd: Path, launched_at: float) -> None:
        super().__init__(
            state_root,
            cwd=cwd,
            launched_at=launched_at,
            file_name="context.jsonl",
            start_at_end=True,
        )

    def _extract_messages(self, lines: list[str]) -> list[str]:
        messages: list[str] = []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                messages.extend(_extract_kimi_context_messages(record))
        return messages


class _KimiWireOutputSource(_KimiSessionJsonlOutputSource):
    def __init__(self, state_root: str, *, cwd: Path, launched_at: float) -> None:
        super().__init__(
            state_root,
            cwd=cwd,
            launched_at=launched_at,
            file_name="wire.jsonl",
            start_at_end=False,
        )
        self._turn_parts: list[str] = []
        self._in_turn = False

    def _extract_messages(self, lines: list[str]) -> list[str]:
        messages: list[str] = []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") == "metadata":
                continue
            timestamp = record.get("timestamp")
            if (
                isinstance(timestamp, int | float)
                and timestamp < self._launched_at - 5
            ):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            message_type = message.get("type")
            payload = message.get("payload")
            if message_type == "TurnBegin":
                self._in_turn = True
                self._turn_parts = []
                continue
            if message_type == "ContentPart" and isinstance(payload, dict):
                if payload.get("type") == "text":
                    text = payload.get("text")
                    if isinstance(text, str) and text:
                        self._turn_parts.append(text)
                continue
            if message_type == "TurnEnd":
                text = _join_kimi_wire_text_parts(self._turn_parts)
                if text:
                    messages.append(text)
                self._turn_parts = []
                self._in_turn = False
        return messages


class TelegramBridge:
    def __init__(self, config: TelegramConfig) -> None:
        token = config.resolved_token()
        if config.enabled and not config.debug_mode and not token:
            raise ValueError(
                "telegram.enabled is true but no token was provided via "
                f"token, token_file, or {config.token_env}"
            )
        if config.enabled and not config.allowed_chat_ids:
            raise ValueError("telegram.enabled is true but allowed_chat_ids is empty")

        self.config = config
        self._launched_at = time.time()
        self._client = self._make_client(token)
        self._read_fd, self._write_fd = os.pipe()
        os.set_blocking(self._read_fd, False)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sent_matches: set[str] = set()
        self._errors: queue.Queue[str] = queue.Queue()
        self._mode = config.output_mode
        self._pending_raw_output = ""
        self._pending_output = ""
        self._last_output_at: float | None = None
        self._awaiting_summary = False
        self._summary_requested_at: float | None = None
        self._summary_fallback_output = ""
        self._summary_fallback_structured = False
        self._injected_prompts: list[str] = []
        self._recent_user_inputs: list[str] = []
        self._pending_menu: SelectionMenu | None = None
        self._sent_menu_fingerprint = ""
        self._suppress_menu_until = 0.0
        self._auto_continue_enabled = False
        self._auto_continue_until: float | None = None
        self._pending_auto_continue_prompt: str | None = None
        self._structured_sources: dict[str, object] = {}
        if "codex_rollout" in config.output_sources:
            self._structured_sources["codex_rollout"] = _CodexRolloutOutputSource(
                config.codex_state_root,
                cwd=Path.cwd(),
                launched_at=self._launched_at,
            )
        if "kimi_context" in config.output_sources:
            self._structured_sources["kimi_context"] = _KimiContextOutputSource(
                config.kimi_state_root,
                cwd=Path.cwd(),
                launched_at=self._launched_at,
            )
        if "kimi_wire" in config.output_sources:
            self._structured_sources["kimi_wire"] = _KimiWireOutputSource(
                config.kimi_state_root,
                cwd=Path.cwd(),
                launched_at=self._launched_at,
            )
        self._terminal_output_fingerprints: dict[str, float] = {}
        self._structured_output_fingerprints: dict[str, tuple[str, float]] = {}

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def read_fd(self) -> int:
        return self._read_fd

    def start(self) -> None:
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        client_close = getattr(self._client, "close", None)
        if callable(client_close):
            client_close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        for fd in (self._read_fd, self._write_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def drain_replies(self) -> list[TelegramInput]:
        chunks: list[bytes] = []
        while True:
            try:
                chunks.append(os.read(self._read_fd, 4096))
            except BlockingIOError:
                break
            except OSError:
                break
            if not chunks[-1] or len(chunks[-1]) < 4096:
                break
        if not chunks:
            return []
        raw = b"".join(chunks).decode(errors="replace")
        return [parse_telegram_input(line) for line in raw.splitlines() if line]

    def maybe_forward_output(self, output_buffer: str) -> bool:
        if not self.enabled or self._client is None:
            return False

        for pattern in self.config.forward_patterns:
            match = pattern.search(output_buffer)
            if match is None:
                continue

            text = _match_text(match).strip()
            if not text or text in self._sent_matches:
                continue
            self._sent_matches.add(text)
            cleaned = self._clean_forwardable_output(text)
            if not cleaned:
                continue
            _append_history_entry(Path(self.config.history_path), cleaned)
            self._send_to_allowed_chats(_truncate(cleaned, self.config.max_message_chars))
            self._schedule_auto_continue_prompt()
            self._pending_raw_output = ""
            self._pending_output = ""
            self._last_output_at = None
            return True
        return False

    def record_output(self, text: str) -> None:
        if not self.enabled:
            return
        _append_text(Path(self.config.raw_history_path), text)
        if _terminal_chunk_has_pending_signal(text):
            self._pending_raw_output = (self._pending_raw_output + text)[
                -_pending_raw_limit(self.config) :
            ]
        self._last_output_at = time.monotonic()

    def flush_idle_output(self) -> str | None:
        if not self.enabled:
            return None
        self._refresh_auto_continue_state()
        auto_prompt = self._pop_auto_continue_prompt()
        if auto_prompt:
            return auto_prompt
        structured_polled: dict[str, str] = {}
        for source in self._structured_sources_before_terminal():
            structured_pending = self._poll_structured_output(source)
            structured_polled[source] = structured_pending
            if structured_pending:
                return self._deliver_output(structured_pending, source=source)

        if (
            self._awaiting_summary
            and not self._pending_raw_output
            and self._summary_requested_at is not None
            and time.monotonic() - self._summary_requested_at >= self.config.summary_timeout_seconds
        ):
            self._send_summary_fallback()
            return None
        if not self._pending_raw_output or self._last_output_at is None:
            for source in self.config.output_sources:
                if source == "terminal" or source not in self._structured_sources:
                    continue
                if source in structured_polled:
                    structured_pending = structured_polled[source]
                else:
                    structured_pending = self._poll_structured_output(source)
                    structured_polled[source] = structured_pending
                if structured_pending:
                    return self._deliver_output(structured_pending, source=source)
            return None
        raw_for_menu = self._remove_injected_prompt_echoes(self._pending_raw_output)
        raw_for_menu = _remove_recent_user_echoes(raw_for_menu, self._recent_user_inputs)
        if self._maybe_forward_selection_menu(raw_for_menu):
            self._pending_raw_output = ""
            self._pending_output = ""
            self._last_output_at = None
            return None
        if time.monotonic() - self._last_output_at < self.config.idle_forward_seconds:
            return None

        raw_pending = self._pending_raw_output
        self._pending_raw_output = ""
        self._pending_output = ""
        self._last_output_at = None

        raw_pending = self._remove_injected_prompt_echoes(raw_pending)
        raw_pending = _remove_recent_user_echoes(raw_pending, self._recent_user_inputs)
        if self._maybe_forward_selection_menu(raw_pending):
            return None

        terminal_pending: str | None = None
        for source in self.config.output_sources:
            if source in self._structured_sources:
                if source in structured_polled:
                    structured_pending = structured_polled[source]
                else:
                    structured_pending = self._poll_structured_output(source)
                    structured_polled[source] = structured_pending
                if structured_pending:
                    return self._deliver_output(structured_pending, source=source)
                continue
            if source == "terminal":
                if terminal_pending is None:
                    terminal_pending = self._clean_terminal_pending(raw_pending)
                if terminal_pending:
                    return self._deliver_output(
                        terminal_pending,
                        source=source,
                        defer_auto_summary=_terminal_raw_has_active_status(raw_pending),
                        raw_pending=raw_pending,
                    )
        return None

    def _structured_sources_before_terminal(self) -> list[str]:
        sources = self.config.output_sources
        if "terminal" not in sources:
            terminal_index = len(sources)
        else:
            terminal_index = sources.index("terminal")
        return [
            source
            for source in sources[:terminal_index]
            if source in self._structured_sources
        ]

    def _poll_structured_output(self, source: str) -> str:
        output_source = self._structured_sources.get(source)
        if output_source is None:
            return ""
        poll_messages = getattr(output_source, "poll_messages", None)
        if not callable(poll_messages):
            return ""
        messages = poll_messages()
        return "\n\n".join(message for message in messages if message.strip()).strip()

    def _clean_terminal_pending(self, raw_pending: str) -> str:
        pending = _clean_terminal_text(raw_pending).strip()
        pending = _remove_recent_user_echoes(pending, self._recent_user_inputs)
        pending = _clean_terminal_text(pending).strip()
        return pending

    def _after_model_output_delivered(self, *, allow_auto_continue: bool = True) -> str | None:
        if allow_auto_continue:
            self._schedule_auto_continue_prompt()
        return self._pop_auto_continue_prompt()

    def _schedule_auto_continue_prompt(self) -> None:
        if not self._auto_continue_is_active():
            return
        self._pending_auto_continue_prompt = _AUTO_CONTINUE_PROMPT

    def _pop_auto_continue_prompt(self) -> str | None:
        if not self._auto_continue_is_active():
            self._pending_auto_continue_prompt = None
            return None
        prompt = self._pending_auto_continue_prompt
        self._pending_auto_continue_prompt = None
        return prompt

    def _auto_continue_is_active(self) -> bool:
        self._refresh_auto_continue_state()
        return self._auto_continue_enabled

    def _refresh_auto_continue_state(self) -> None:
        if (
            self._auto_continue_enabled
            and self._auto_continue_until is not None
            and time.monotonic() >= self._auto_continue_until
        ):
            self._auto_continue_enabled = False
            self._auto_continue_until = None
            self._pending_auto_continue_prompt = None
            self._send_to_allowed_chats("自动推进模式已到期，已恢复等待用户回复。")

    def _deliver_output(
        self,
        pending: str,
        *,
        source: str,
        defer_auto_summary: bool = False,
        raw_pending: str = "",
    ) -> str | None:
        structured = source != "terminal"
        allow_auto_continue = not (defer_auto_summary and not structured)
        pending = _clean_structured_text(pending) if structured else pending.strip()
        if not pending:
            return None

        if self._mode == "all":
            if self._is_cross_source_duplicate(pending, source):
                return None
            self._remember_output_source(pending, source)
            _append_history_entry(Path(self.config.history_path), pending)
            self._send_long_message(
                pending,
                self.config.all_chunk_chars,
                structured=structured,
            )
            return self._after_model_output_delivered(
                allow_auto_continue=allow_auto_continue,
            )

        if self._awaiting_summary:
            summary = self._extract_summary_reply(
                pending,
                raw_pending=raw_pending,
                structured=structured,
            )
            if not summary:
                return None
            if self._is_cross_source_duplicate(summary, source):
                self._clear_pending_summary()
                return None
            self._remember_output_source(summary, source)
            _append_history_entry(Path(self.config.history_path), summary)
            self._clear_pending_summary()
            self._send_to_allowed_chats(summary, structured=structured)
            return self._after_model_output_delivered(
                allow_auto_continue=allow_auto_continue,
            )

        if self._is_cross_source_duplicate(pending, source):
            return None

        self._remember_output_source(pending, source)
        _append_history_entry(Path(self.config.history_path), pending)

        if len(pending) <= self.config.summary_threshold_chars:
            self._send_to_allowed_chats(pending, structured=structured)
            return self._after_model_output_delivered(
                allow_auto_continue=allow_auto_continue,
            )

        if not self.config.auto_summary:
            max_chars = min(self.config.summary_fallback_chars, self.config.max_message_chars)
            self._send_to_allowed_chats(
                f"输出较长，已写入 {self.config.history_path}。自动摘要已关闭，"
                f"先发送原输出前 {max_chars} 字。发送 /history 可获取完整记录。"
            )
            self._send_output_preview(pending, max_chars, structured=structured)
            return self._after_model_output_delivered(
                allow_auto_continue=allow_auto_continue,
            )
        if defer_auto_summary and not structured:
            self._send_to_allowed_chats(
                f"输出较长，已写入 {self.config.history_path}。检测到底层 CLI 仍有 "
                "background terminal 正在运行，暂不自动插入摘要请求，避免打断当前任务。"
                "发送 /history 可获取完整记录；任务结束后可以直接让模型总结。"
            )
            return None
        self._send_to_allowed_chats(
            f"输出较长，已写入 {self.config.history_path}。正在请求模型生成 "
            f"{self.config.summary_max_chars} 字以内摘要。若摘要未返回，会自动发送截断预览。"
            f"发送 /history 可获取完整记录。"
        )
        self._awaiting_summary = True
        self._summary_requested_at = time.monotonic()
        self._summary_fallback_output = pending
        self._summary_fallback_structured = structured
        return self.config.summary_prompt_template.format(
            max_chars=self.config.summary_max_chars
        )

    def _clear_pending_summary(self) -> None:
        self._awaiting_summary = False
        self._summary_requested_at = None
        self._summary_fallback_output = ""
        self._summary_fallback_structured = False

    def _extract_summary_reply(
        self,
        text: str,
        *,
        raw_pending: str = "",
        structured: bool = False,
    ) -> str:
        if raw_pending and not structured:
            raw_summary = _extract_summary_reply_from_terminal_raw(raw_pending)
            if raw_summary:
                cleaned_raw = _extract_summary_reply_section(_clean_terminal_text(raw_summary))
                if _looks_like_summary_reply(cleaned_raw):
                    return cleaned_raw
        cleaned = _clean_structured_text(text) if structured else _clean_terminal_text(text)
        cleaned = _extract_summary_reply_section(cleaned)
        if not _looks_like_summary_reply(cleaned):
            return ""
        return cleaned

    def _is_cross_source_duplicate(self, text: str, source: str) -> bool:
        fingerprint = _fingerprint_text(text)
        self._drop_old_output_fingerprints()
        if source == "terminal":
            return fingerprint in self._structured_output_fingerprints
        if source in self._structured_sources:
            existing = self._structured_output_fingerprints.get(fingerprint)
            if existing is not None and existing[0] != source:
                return True
            return fingerprint in self._terminal_output_fingerprints
        return False

    def _remember_output_source(self, text: str, source: str) -> None:
        fingerprint = _fingerprint_text(text)
        if source in self._structured_sources:
            self._structured_output_fingerprints[fingerprint] = (source, time.monotonic())
        elif source == "terminal":
            self._terminal_output_fingerprints[fingerprint] = time.monotonic()
        self._drop_old_output_fingerprints()

    def _drop_old_output_fingerprints(self) -> None:
        cutoff = time.monotonic() - _RECENT_OUTPUT_FINGERPRINT_TTL_SECONDS
        for cache in (
            self._terminal_output_fingerprints,
        ):
            for fingerprint, timestamp in list(cache.items()):
                if timestamp < cutoff:
                    cache.pop(fingerprint, None)
        for fingerprint, (_, timestamp) in list(self._structured_output_fingerprints.items()):
            if timestamp < cutoff:
                self._structured_output_fingerprints.pop(fingerprint, None)

    def mark_injected_prompt(self, prompt: str) -> None:
        if prompt:
            self._injected_prompts.append(prompt)
            self._injected_prompts = self._injected_prompts[-5:]

    def mark_user_input(self, text: str) -> None:
        text = text.strip()
        if text:
            self._clear_pending_summary()
            self._recent_user_inputs.append(text)
            self._recent_user_inputs = self._recent_user_inputs[-10:]

    def consume_menu_choice(self, text: str) -> TelegramInput | None:
        if self._pending_menu is None:
            return None
        stripped = text.strip()
        if not re.fullmatch(r"\d{1,2}", stripped):
            self._pending_menu = None
            self._sent_menu_fingerprint = ""
            return None
        choice = int(stripped)
        if choice < 1 or choice > len(self._pending_menu.options):
            self._send_to_allowed_chats(
                f"选项编号无效。请输入 1-{len(self._pending_menu.options)}。"
            )
            return TelegramInput(TelegramInputKind.IGNORE, stripped)

        target_index = choice - 1
        current_index = self._pending_menu.selected_index
        self._pending_menu = None
        self._sent_menu_fingerprint = ""
        self._suppress_menu_until = time.monotonic() + _MENU_CHOICE_SUPPRESS_SECONDS
        if target_index == current_index:
            return TelegramInput(TelegramInputKind.KEY, "enter")
        if target_index > current_index:
            keys = ["down"] * (target_index - current_index)
        else:
            keys = ["up"] * (current_index - target_index)
        keys.append("enter")
        return TelegramInput(TelegramInputKind.KEY, " ".join(keys))

    def handle_command(self, command: str) -> None:
        if not self.enabled:
            return
        normalized = command.strip()
        if normalized.startswith("/auto"):
            self._handle_auto_continue_command(normalized)
            return
        if normalized == "/all":
            self._mode = "all"
            self._send_to_allowed_chats("已切换到完整输出模式。发送 /summary 可切回摘要模式。")
            return
        if normalized == "/summary":
            self._mode = "summary"
            self._send_to_allowed_chats("已切换到摘要模式。发送 /all 可查看后续完整输出。")
            return
        if normalized == "/history":
            self.send_history(raw=False)
            return
        if normalized == "/rawhistory":
            self.send_history(raw=True)
            return
        if normalized == "/help":
            self._send_to_allowed_chats(
                "TeleAgent 控制命令：/ta all、/ta summary、/ta history、"
                "/ta rawhistory、/ta auto start、/ta auto end、/ta auto 7.5。"
                "其他 / 开头消息会发送给底层 CLI。"
            )
            return

    def _handle_auto_continue_command(self, command: str) -> None:
        argument = command[len("/auto") :].strip().lower()
        if argument in {"", "status"}:
            self._refresh_auto_continue_state()
            if not self._auto_continue_enabled:
                self._send_to_allowed_chats(
                    "自动推进模式未开启。发送 /ta auto start 开启，或 /ta auto 7.5 开启 7.5 小时。"
                )
                return
            if self._auto_continue_until is None:
                self._send_to_allowed_chats(
                    f"自动推进模式已开启。每次模型回复后会发送：{_AUTO_CONTINUE_PROMPT}"
                )
                return
            remaining_hours = max(0.0, (self._auto_continue_until - time.monotonic()) / 3600)
            self._send_to_allowed_chats(
                "自动推进模式已开启，约 "
                f"{remaining_hours:.2f} 小时后到期。每次模型回复后会发送：{_AUTO_CONTINUE_PROMPT}"
            )
            return
        if argument in {"start", "on"}:
            self._auto_continue_enabled = True
            self._auto_continue_until = None
            self._pending_auto_continue_prompt = None
            self._send_to_allowed_chats(
                f"已开启自动推进模式。模型每次回复后会自动发送：{_AUTO_CONTINUE_PROMPT}。"
                "发送 /ta auto end 可关闭。"
            )
            return
        if argument in {"end", "stop", "off"}:
            self._auto_continue_enabled = False
            self._auto_continue_until = None
            self._pending_auto_continue_prompt = None
            self._send_to_allowed_chats("已关闭自动推进模式，恢复等待用户回复。")
            return
        try:
            hours = float(argument)
        except ValueError:
            self._send_to_allowed_chats(
                "无法识别自动推进命令。用法：/ta auto start、/ta auto end、/ta auto 7.5。"
            )
            return
        if hours <= 0:
            self._send_to_allowed_chats("自动推进倒计时必须大于 0 小时。")
            return
        self._auto_continue_enabled = True
        self._auto_continue_until = time.monotonic() + hours * 3600
        self._pending_auto_continue_prompt = None
        self._send_to_allowed_chats(
            f"已开启自动推进模式，将在 {hours:g} 小时后自动停止。"
            f"模型每次回复后会自动发送：{_AUTO_CONTINUE_PROMPT}。"
            "发送 /ta auto end 可提前关闭。"
        )

    def send_history(self, *, raw: bool) -> None:
        if not self.enabled or self._client is None:
            return
        path = Path(self.config.raw_history_path if raw else self.config.history_path)
        if not path.exists():
            self._send_to_allowed_chats("还没有历史记录文件。")
            return
        try:
            send_path = path if raw else _write_clean_history_copy(path)
            size_text = _format_bytes(send_path.stat().st_size)
        except OSError as exc:
            self._send_to_allowed_chats(f"历史文件准备失败：{exc}")
            self._errors.put(str(exc))
            return
        self._send_to_allowed_chats(
            f"正在发送{'原始' if raw else '清理后'}历史文件：{send_path.name} ({size_text})。"
        )
        for chat_id in self.config.allowed_chat_ids:
            try:
                caption = "TeleAgent raw history" if raw else "TeleAgent clean history"
                self._client.send_document(chat_id, send_path, caption=caption)
            except (RuntimeError, urllib.error.URLError, OSError) as exc:
                self._send_to_allowed_chats(f"历史文件发送失败：{exc}")
                self._errors.put(str(exc))

    def pop_errors(self) -> list[str]:
        errors: list[str] = []
        while True:
            try:
                errors.append(self._errors.get_nowait())
            except queue.Empty:
                break
        return errors

    def _poll_loop(self) -> None:
        assert self._client is not None
        offset: int | None = None
        while not self._stop.is_set():
            try:
                updates = self._client.get_updates(
                    offset=offset,
                    timeout=self.config.poll_timeout,
                )
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    text, chat_id = _extract_text_message(update)
                    if text is None or chat_id is None:
                        continue
                    if chat_id not in self.config.allowed_chat_ids:
                        continue
                    os.write(self._write_fd, f"{text}\n".encode())
            except (OSError, RuntimeError, urllib.error.URLError) as exc:
                self._errors.put(str(exc))
                time.sleep(3)

    def _send_to_allowed_chats(self, text: str, *, structured: bool = False) -> None:
        assert self._client is not None
        text = _truncate(text, self.config.max_message_chars)
        text = (
            _format_structured_for_mobile(text)
            if structured
            else _format_for_mobile(text)
        )
        if not text:
            return
        for chat_id in self.config.allowed_chat_ids:
            try:
                self._client.send_message(chat_id, text)
            except (RuntimeError, urllib.error.URLError, OSError) as exc:
                self._errors.put(str(exc))

    def _send_long_message(self, text: str, chunk_chars: int, *, structured: bool = False) -> None:
        start = 0
        while start < len(text):
            chunk = text[start : start + chunk_chars]
            self._send_to_allowed_chats(
                _truncate(chunk, self.config.max_message_chars),
                structured=structured,
            )
            start += chunk_chars

    def _send_summary_fallback(self) -> None:
        fallback = self._summary_fallback_output.strip()
        structured = self._summary_fallback_structured
        self._awaiting_summary = False
        self._summary_requested_at = None
        self._summary_fallback_output = ""
        self._summary_fallback_structured = False
        if not fallback:
            return
        max_chars = min(self.config.summary_fallback_chars, self.config.max_message_chars)
        self._send_to_allowed_chats(
            f"摘要请求超时，先发送原输出前 {max_chars} 字。"
            f"完整记录可发送 /history 获取。"
        )
        self._send_output_preview(fallback, max_chars, structured=structured)
        self._schedule_auto_continue_prompt()

    def _send_output_preview(self, text: str, max_chars: int, *, structured: bool = False) -> None:
        preview = text[:max_chars].rstrip()
        if len(text) > max_chars:
            preview += "\n...[truncated]"
        self._send_to_allowed_chats(preview, structured=structured)

    def _maybe_forward_selection_menu(self, text: str) -> bool:
        menu = _extract_selection_menu(text)
        if menu is None:
            return False
        if time.monotonic() < self._suppress_menu_until:
            self._pending_menu = None
            return True
        if menu.fingerprint == self._sent_menu_fingerprint:
            self._pending_menu = menu
            return True

        self._pending_menu = menu
        self._sent_menu_fingerprint = menu.fingerprint
        lines = [
            menu.title,
            "",
            "回复数字选择；发送 /key up、/key down、/key enter 可手动控制。",
            "",
        ]
        for index, option in enumerate(menu.options, start=1):
            marker = " *" if index - 1 == menu.selected_index else ""
            lines.append(f"{index}. {option}{marker}")
        self._send_to_allowed_chats("\n".join(lines), structured=True)
        return True

    def _remove_injected_prompt_echoes(self, text: str) -> str:
        for prompt in self._injected_prompts:
            text = text.replace(prompt, "")
            text = text.replace(prompt.replace(str(self.config.summary_max_chars), ""), "")
        return text

    def _clean_forwardable_output(self, text: str) -> str:
        text = self._remove_injected_prompt_echoes(text)
        text = _remove_recent_user_echoes(text, self._recent_user_inputs)
        text = _clean_terminal_text(text)
        text = _remove_recent_user_echoes(text, self._recent_user_inputs)
        return _clean_terminal_text(text).strip()

    def _make_client(self, token: str) -> TelegramClient | _DebugTelegramClient | None:
        if not self.config.enabled:
            return None
        if self.config.debug_mode:
            chat_id = self.config.allowed_chat_ids[0]
            return _DebugTelegramClient(
                self.config.debug_inbox_path,
                self.config.debug_outbox_path,
                chat_id=chat_id,
            )
        return TelegramClient(token)


def _extract_text_message(update: dict[str, object]) -> tuple[str | None, int | None]:
    message = update.get("message")
    if not isinstance(message, dict):
        return None, None
    text = message.get("text")
    chat = message.get("chat")
    if not isinstance(text, str) or not isinstance(chat, dict):
        return None, None
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return None, None
    return text, chat_id


def _match_text(match: re.Match[str]) -> str:
    if match.lastindex:
        return match.group(1)
    return match.group(0)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + "\n...[truncated]"


def _format_bytes(size: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _normalize_output_source_name(name: str) -> str:
    normalized = name.strip().lower().replace("-", "_")
    aliases = {
        "codex": "codex_rollout",
        "codex_jsonl": "codex_rollout",
        "codex_log": "codex_rollout",
        "codex_logs": "codex_rollout",
        "codex_session": "codex_rollout",
        "codex_sessions": "codex_rollout",
        "structured": "codex_rollout",
        "structured_log": "codex_rollout",
        "structured_logs": "codex_rollout",
        "kimi": "kimi_wire",
        "kimi_log": "kimi_wire",
        "kimi_logs": "kimi_wire",
        "kimi_session": "kimi_wire",
        "kimi_sessions": "kimi_wire",
        "kimi_ui": "terminal",
        "kimi_context_log": "kimi_context",
        "kimi_context_logs": "kimi_context",
        "kimi_context_jsonl": "kimi_context",
        "kimi_wire_log": "kimi_wire",
        "kimi_wire_logs": "kimi_wire",
        "kimi_wire_jsonl": "kimi_wire",
        "codex_ui": "terminal",
        "ui": "terminal",
        "pty": "terminal",
        "terminal_read": "terminal",
        "terminal_reader": "terminal",
    }
    return aliases.get(normalized, normalized)


def _fingerprint_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip())
    return hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()


def _jsonl_record_epoch(record: dict[str, object]) -> float | None:
    timestamps: list[object] = [record.get("timestamp")]
    payload = record.get("payload")
    if isinstance(payload, dict):
        timestamps.append(payload.get("timestamp"))
    for timestamp in timestamps:
        if not isinstance(timestamp, str) or not timestamp:
            continue
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return None


def _extract_codex_record_cwd(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None

    candidates: list[object] = [payload]
    nested_payload = payload.get("payload")
    if isinstance(nested_payload, dict):
        candidates.append(nested_payload)
    turn_context = payload.get("turn_context")
    if isinstance(turn_context, dict):
        candidates.append(turn_context)

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        cwd = candidate.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def _extract_codex_rollout_messages(record: dict[str, object]) -> list[str]:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return []

    record_type = record.get("type")
    messages: list[str] = []
    if record_type == "event_msg":
        payload_type = payload.get("type")
        if payload_type in {"agent_message", "assistant_message"}:
            message = payload.get("message")
            if isinstance(message, str):
                messages.append(message)
        elif payload_type == "task_complete":
            message = payload.get("last_agent_message")
            if isinstance(message, str):
                messages.append(message)
    elif record_type == "response_item":
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            message = _flatten_codex_message_content(payload.get("content"))
            if message:
                messages.append(message)
    return messages


def _flatten_codex_message_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text.strip()
        return ""
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            if item.strip():
                parts.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        text = item.get("text")
        if item_type in {"output_text", "text"} and isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def _clean_structured_text(text: str) -> str:
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b[PX^_].*?\x1b\\", "", text, flags=re.DOTALL)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\x1b[()][A-Za-z0-9]", "", text)
    text = re.sub(r"\x1b[=>]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")
    text = _apply_backspaces(text)
    text = "".join(
        char
        for char in text
        if char == "\n" or char == "\t" or ord(char) >= 32
    )
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _paths_equal(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return str(left.expanduser()) == str(right.expanduser())


def _kimi_workdir_hash(path: str, kaos: str = "local") -> str:
    digest = hashlib.md5(path.encode(encoding="utf-8")).hexdigest()
    return digest if kaos == "local" else f"{kaos}_{digest}"


def _kimi_workdir_session_roots(state_root: Path, cwd: Path) -> list[Path]:
    sessions_root = state_root / "sessions"
    roots: list[Path] = []
    metadata_path = state_root / "kimi.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}
        work_dirs = metadata.get("work_dirs") if isinstance(metadata, dict) else None
        if isinstance(work_dirs, list):
            for item in work_dirs:
                if not isinstance(item, dict):
                    continue
                raw_path = item.get("path")
                if not isinstance(raw_path, str) or not raw_path:
                    continue
                if not _paths_equal(Path(raw_path), cwd):
                    continue
                kaos = item.get("kaos")
                root = sessions_root / _kimi_workdir_hash(
                    raw_path,
                    kaos if isinstance(kaos, str) and kaos else "local",
                )
                if root not in roots:
                    roots.append(root)

    fallback = sessions_root / _kimi_workdir_hash(str(cwd))
    if fallback not in roots:
        roots.append(fallback)
    return roots


def _extract_kimi_context_messages(record: dict[str, object]) -> list[str]:
    role = record.get("role")
    if role != "assistant":
        return []
    text = _extract_kimi_text_parts(record.get("content"))
    return [text] if text else []


def _extract_kimi_text_parts(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            if item.strip():
                parts.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        text = item.get("text")
        if item_type == "text" and isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts).strip()


def _join_kimi_wire_text_parts(parts: list[str]) -> str:
    return "\n\n".join(part.strip() for part in parts if part.strip()).strip()


def _extract_selection_menu(text: str) -> SelectionMenu | None:
    candidates: list[str] = []
    for match in re.finditer(r"\x1b\[\?1049h(?P<body>.*?)(?:\x1b\[\?1049l|$)", text, re.DOTALL):
        alt_snapshot = _terminal_snapshot_text(match.group("body"))
        if alt_snapshot.strip() and alt_snapshot not in candidates:
            candidates.append(alt_snapshot)

    snapshot = _terminal_snapshot_text(text)
    if snapshot.strip():
        candidates.append(snapshot)
    visible_stream = _visible_terminal_text(text)
    if visible_stream.strip() and visible_stream not in candidates:
        candidates.append(visible_stream)
    for visible in candidates:
        menu = _extract_selection_menu_from_visible(visible, text)
        if menu is not None:
            return menu
    return None


def _extract_selection_menu_from_visible(visible: str, raw_text: str) -> SelectionMenu | None:
    lines = [line for line in visible.splitlines() if _normalize_menu_line(line)]
    if len(lines) < 2:
        return None

    options: list[str] = []
    selected_index: int | None = None
    candidate_lines = lines[-30:]
    kimi_sessions_menu = _extract_kimi_sessions_menu(lines, raw_text=raw_text)
    if kimi_sessions_menu is not None:
        return kimi_sessions_menu
    if _has_kimi_sessions_context(lines, raw_text):
        return None
    menu_context = _has_selection_menu_context(candidate_lines)
    terminal_context = _has_terminal_control_context(raw_text)
    selected_line_positions: list[int] = []
    for position, line in enumerate(candidate_lines):
        parsed = _parse_selected_menu_option_line(line, allow_weak_markers=menu_context)
        if parsed is not None and not _is_low_value_menu_option(parsed):
            selected_line_positions.append(position)
    if not selected_line_positions:
        return None

    selected_line_position = selected_line_positions[-1]
    start = selected_line_position
    while (
        start > 0
        and _parse_plain_menu_option_line(candidate_lines[start - 1], menu_context=menu_context)
        is not None
    ):
        start -= 1
    end = selected_line_position + 1
    while (
        end < len(candidate_lines)
        and _parse_plain_menu_option_line(candidate_lines[end], menu_context=menu_context)
        is not None
    ):
        end += 1

    for position, line in enumerate(candidate_lines[start:end], start=start):
        selected_label = _parse_selected_menu_option_line(line, allow_weak_markers=menu_context)
        selected = selected_label is not None
        label = (
            selected_label
            if selected
            else _parse_plain_menu_option_line(line, menu_context=menu_context)
        )
        if label is None or _is_low_value_menu_option(label):
            continue
        if label in options:
            if selected:
                selected_index = options.index(label)
            continue
        options.append(label)
        if selected:
            selected_index = len(options) - 1

    if selected_index is None or len(options) < 2:
        return None
    if len(options) > 20:
        options = options[:20]
        if selected_index >= len(options):
            selected_index = 0
    if not menu_context and not terminal_context:
        return None

    title = _selection_menu_title(options)
    if title == "请选择终端菜单项：" and not _has_generic_terminal_menu_context(
        candidate_lines,
        raw_text,
    ):
        return None
    return SelectionMenu(title=title, options=tuple(options), selected_index=selected_index)


def _extract_kimi_sessions_menu(
    lines: list[str],
    *,
    raw_text: str = "",
) -> SelectionMenu | None:
    normalized = [_normalize_menu_line(line) for line in lines]
    normalized = [line for line in normalized if line]
    if not normalized:
        return None
    expected_count = _kimi_sessions_expected_count(normalized)
    if expected_count is None and raw_text:
        expected_count = _kimi_sessions_expected_count(
            [
                _normalize_menu_line(line)
                for line in _visible_terminal_text(raw_text).splitlines()
            ]
        )

    control_index: int | None = None
    for index, line in enumerate(normalized):
        if re.search(
            r"(?i)\bCtrl\+A\b.*\bshow all projects\b.*\bEnter to select\b.*\bEsc to cancel\b",
            line,
        ):
            control_index = index
            break

    header_index: int | None = None
    header_search_end = control_index if control_index is not None else len(normalized)
    for index in range(header_search_end - 1, -1, -1):
        if re.search(r"(?i)\bsessions\b", normalized[index]):
            header_index = index
            break
    if header_index is None:
        return None

    options: list[str] = []
    selected_index: int | None = None
    body_end = control_index if control_index is not None else len(normalized)
    body = [
        line
        for line in normalized[header_index + 1 : body_end]
        if not re.search(r"(?i)\bsessions\b", line)
    ]
    index = 0
    while index + 1 < len(body):
        title = body[index]
        meta = body[index + 1]
        selected = False
        selected_title = _parse_selected_menu_option_line(title, allow_weak_markers=True)
        if selected_title is not None:
            title = selected_title
            selected = True
        else:
            title = _normalize_menu_label(title) or ""

        if not title or not _is_kimi_session_meta_line(meta):
            index += 1
            continue

        label = f"{title} ({meta})"
        if label not in options:
            options.append(label)
            if selected:
                selected_index = len(options) - 1
        elif selected:
            selected_index = options.index(label)
        index += 2

    if not options:
        return None
    if (
        expected_count is not None
        and len(options) < min(expected_count, 20)
        and control_index is None
    ):
        return None
    if selected_index is None:
        selected_index = 0
    return SelectionMenu("请选择会话：", options=tuple(options[:20]), selected_index=selected_index)


def _kimi_sessions_expected_count(lines: list[str]) -> int | None:
    for line in lines:
        match = re.search(r"(?i)\bSESSIONS\s*\(\s*\d+\s+of\s+(\d+)\s*\)", line)
        if match:
            return int(match.group(1))
    return None


def _has_kimi_sessions_context(lines: list[str], raw_text: str) -> bool:
    normalized = [_normalize_menu_line(line) for line in lines]
    visible = _visible_terminal_text(raw_text) if raw_text else ""
    text = "\n".join(normalized)
    combined = f"{text}\n{visible}"
    return bool(
        re.search(r"(?i)\bSESSIONS\s*\(\s*\d+\s+of\s+\d+\s*\)", combined)
        or (
            re.search(r"(?i)\bSessions\b", combined)
            and re.search(
                r"(?i)\bCtrl\+A\b.*\bshow all projects\b.*\bEnter to select\b.*\bEsc to cancel\b",
                combined,
                flags=re.DOTALL,
            )
        )
    )


def _is_kimi_session_meta_line(line: str) -> bool:
    return bool(
        re.fullmatch(
            r"\d+\s*(?:s|m|h|d|w|mo|y)\s+ago\s*[·•-]\s*[0-9a-f]{6,}",
            line.strip(),
            flags=re.IGNORECASE,
        )
    )


def _visible_terminal_text(text: str) -> str:
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b[PX^_].*?\x1b\\", "", text, flags=re.DOTALL)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\x1b[()][A-Za-z0-9]", "", text)
    text = re.sub(r"\x1b[=>]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x07", "")
    text = _apply_backspaces(text)
    text = "".join(
        char
        for char in text
        if char == "\n" or char == "\t" or ord(char) >= 32
    )
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _terminal_snapshot_text(text: str) -> str:
    rows: dict[int, list[str]] = {}
    row = 0
    col = 0
    max_cols = 240
    max_rows = 200

    def get_row(index: int) -> list[str]:
        if index not in rows:
            rows[index] = []
        return rows[index]

    def write_char(char: str) -> None:
        nonlocal row, col
        if row < 0:
            row = 0
        if col < 0:
            col = 0
        if row >= max_rows:
            return
        if col >= max_cols:
            row += 1
            col = 0
            if row >= max_rows:
                return
        line = get_row(row)
        if len(line) <= col:
            line.extend(" " for _ in range(col - len(line) + 1))
        line[col] = char
        col += 1

    def erase_line(mode: int) -> None:
        line = get_row(row)
        if mode == 2:
            rows[row] = []
        elif mode == 1:
            end = min(col + 1, len(line))
            for index in range(end):
                line[index] = " "
        else:
            if col < len(line):
                del line[col:]

    def erase_display(mode: int) -> None:
        nonlocal rows
        if mode in (2, 3):
            rows = {}
            return
        if mode == 1:
            for index in list(rows):
                if index < row:
                    rows.pop(index, None)
                elif index == row:
                    line = rows[index]
                    end = min(col + 1, len(line))
                    for pos in range(end):
                        line[pos] = " "
            return
        for index in list(rows):
            if index > row:
                rows.pop(index, None)
            elif index == row:
                line = rows[index]
                if col < len(line):
                    del line[col:]

    def parse_params(raw: str) -> list[int]:
        cleaned = raw.replace("?", "").replace(">", "").replace("<", "")
        if not cleaned:
            return []
        params: list[int] = []
        for part in cleaned.split(";"):
            if not part:
                params.append(0)
                continue
            match = re.match(r"\d+", part)
            params.append(int(match.group(0)) if match else 0)
        return params

    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\x1b":
            if index + 1 >= length:
                break
            marker = text[index + 1]
            if marker == "]":
                end_bel = text.find("\x07", index + 2)
                end_st = text.find("\x1b\\", index + 2)
                candidates = [pos for pos in (end_bel, end_st) if pos != -1]
                if not candidates:
                    break
                end = min(candidates)
                index = end + (2 if end == end_st else 1)
                continue
            if marker in "PX^_":
                end = text.find("\x1b\\", index + 2)
                if end == -1:
                    break
                index = end + 2
                continue
            if marker == "[":
                match = re.match(r"\x1b\[([0-?]*[ -/]*)([@-~])", text[index:])
                if not match:
                    index += 2
                    continue
                raw_params = match.group(1).strip()
                final = match.group(2)
                params = parse_params(raw_params)
                first = params[0] if params else 0
                if final in ("H", "f"):
                    row = max((params[0] if len(params) >= 1 and params[0] else 1) - 1, 0)
                    col = max((params[1] if len(params) >= 2 and params[1] else 1) - 1, 0)
                elif final == "A":
                    row = max(row - (first or 1), 0)
                elif final == "B":
                    row = min(row + (first or 1), max_rows - 1)
                elif final == "C":
                    col = min(col + (first or 1), max_cols - 1)
                elif final == "D":
                    col = max(col - (first or 1), 0)
                elif final == "G":
                    col = max((first or 1) - 1, 0)
                elif final == "d":
                    row = max((first or 1) - 1, 0)
                elif final == "J":
                    erase_display(first)
                elif final == "K":
                    erase_line(first)
                index += len(match.group(0))
                continue
            if marker in "()":
                index += 3
                continue
            if marker == "M":
                row = max(row - 1, 0)
                index += 2
                continue
            index += 2
            continue

        if char == "\r":
            col = 0
        elif char == "\n":
            row = min(row + 1, max_rows - 1)
            col = 0
        elif char in ("\b", "\x7f"):
            col = max(col - 1, 0)
        elif char == "\x07":
            pass
        elif char == "\t":
            spaces = 4 - (col % 4)
            for _ in range(spaces):
                write_char(" ")
        elif ord(char) >= 32:
            write_char(char)
        index += 1

    rendered = ["".join(rows[index]).rstrip() for index in sorted(rows)]
    while rendered and not rendered[0].strip():
        rendered.pop(0)
    while rendered and not rendered[-1].strip():
        rendered.pop()
    return "\n".join(rendered)


def _has_terminal_control_context(text: str) -> bool:
    return bool(
        re.search(r"\x1b\[[0-?]*[ -/]*[@-~]", text)
        or re.search(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", text)
    )


def _has_selection_menu_context(lines: list[str]) -> bool:
    text = "\n".join(_normalize_menu_line(line) for line in lines)
    return bool(
        re.search(
            r"(?i)\b(?:select|choose|pick|switch)\b[^\n]{0,40}\b(?:model|session|conversation)\b",
            text,
        )
        or re.search(
            r"(?i)\b(?:model|session|conversation)\b[^\n]{0,40}\b(?:select|choose|pick)\b",
            text,
        )
        or re.search(
            r"(?i)\b(use arrow|arrow keys|press enter|enter to select|resume session|select session)\b",
            text,
        )
        or re.search(r"(?i)\bwould you like to run the following command\b", text)
        or re.search(r"(?i)\brequesting approval to\b", text)
        or re.search(r"(?i)\bpress enter to confirm or esc to cancel\b", text)
        or re.search(r"(?i)\b(?:select|choose)\b[^\n]{0,40}\b(?:reasoning|effort)\b", text)
        or re.search(r"(?i)\b(?:reasoning|effort)\b[^\n]{0,40}\b(?:select|choose)\b", text)
        or re.search(r"(选择模型|选择会话|方向键|回车选择)", text)
        or re.search(r"[↑↓]\s*(?:/|to)?", text)
    )


def _has_generic_terminal_menu_context(lines: list[str], raw_text: str) -> bool:
    text = "\n".join(_normalize_menu_line(line) for line in lines)
    return bool(
        re.search(r"(?i)\b(?:select|choose|pick one|pick an option)\b", text)
        or re.search(r"(?i)\b(?:press enter|enter to confirm|esc to cancel)\b", text)
        or re.search(r"(?i)\b(?:use arrow|arrow keys|↑/↓|方向键|回车选择)\b", text)
        or re.search(r"(?i)\bwould you like to run the following command\b", text)
        or re.search(r"(?i)\brequesting approval to\b", text)
        or re.search(r"(?i)\baction required\b", raw_text)
    )


def _parse_selected_menu_option_line(
    line: str,
    *,
    allow_weak_markers: bool = False,
) -> str | None:
    stripped = _normalize_menu_line(line)
    cursor_match = re.match(r"^(?P<marker>[❯❱→])\s*(?P<label>.+)$", stripped)
    if cursor_match:
        return _normalize_numbered_menu_label(cursor_match.group("label"))
    if not allow_weak_markers:
        return None
    match = re.match(r"^(?P<marker>[▶▸►➤➜›>●◉◆■✔✓])\s*(?P<label>.+)$", stripped)
    if not match:
        return None
    label = _normalize_numbered_menu_label(match.group("label"))
    if label is None or not _looks_like_menu_option_label(label):
        return None
    return label


def _parse_plain_menu_option_line(line: str, *, menu_context: bool) -> str | None:
    stripped = _normalize_menu_line(line)
    selected = _parse_selected_menu_option_line(stripped, allow_weak_markers=menu_context)
    if selected is not None:
        return selected
    match = re.match(r"^(?P<marker>[○◦□◇\-*•])\s*(?P<label>.+)$", stripped)
    if match:
        label = _normalize_menu_label(match.group("label"))
        if label is None:
            return None
        if _looks_like_menu_option_label(label):
            return label
        return None
    if re.match(r"^(?:Select|Choose|Pick|Use|请选择|选择)\b", stripped, re.IGNORECASE):
        return None
    if _is_terminal_ui_line(stripped):
        return None
    if re.search(r"[:：]\s*$", stripped):
        return None
    label = _normalize_numbered_menu_label(stripped)
    if label is None:
        return None
    if _looks_like_menu_option_label(label):
        return label
    return None


def _normalize_menu_line(line: str) -> str:
    stripped = line.strip()
    stripped = re.sub(r"^[│┃|]\s*", "", stripped)
    stripped = re.sub(r"\s*[│┃|]$", "", stripped).strip()
    stripped = re.sub(r"^[╭╰╮╯─━┌└┐┘├┤┬┴┼\s]+", "", stripped).strip()
    stripped = re.sub(r"[╭╰╮╯─━┌└┐┘├┤┬┴┼\s]+$", "", stripped).strip()
    return stripped


def _normalize_menu_label(label: str) -> str | None:
    label = re.sub(r"\s{2,}", " ", label)
    label = re.sub(r"\s+\((?:current|selected|default)\)$", "", label, flags=re.IGNORECASE)
    label = label.strip()
    if not label or len(label) > 160:
        return None
    return label


def _normalize_numbered_menu_label(label: str) -> str | None:
    label = _remove_menu_number(label)
    label = _compact_codex_model_option(label)
    label = _compact_codex_reasoning_option(label)
    return _normalize_menu_label(label)


def _remove_menu_number(label: str) -> str:
    match = re.match(r"^\s*(?:\d{1,2}[.)]|\[\d{1,2}\])\s*(?P<label>.+)$", label)
    return match.group("label") if match else label


def _compact_codex_model_option(label: str) -> str:
    match = re.match(
        r"^\s*(?P<model>(?:[gG][pP][tT]-\d+(?:\.\d+)*(?:-[a-z0-9]+)*|"
        r"[oO]\d+(?:-[a-z0-9]+)*))"
        r"(?P<rest>.*)$",
        label,
    )
    if not match:
        return label
    model = match.group("model")
    rest = match.group("rest").strip()
    tags = " ".join(re.findall(r"\([^)]*\)", rest))
    effort_match = re.match(r"^(?P<effort>xhigh|high|medium|low)\b", rest, flags=re.IGNORECASE)
    if effort_match:
        effort = effort_match.group("effort")
        return f"{model} {effort} {tags}".strip()
    return f"{model} {tags}".strip()


def _compact_codex_reasoning_option(label: str) -> str:
    match = re.match(
        r"^\s*(?P<level>Extra\s*high|Extrahigh|High|Medium|Low)"
        r"\s*(?P<tag>\([^)]*\))?",
        label,
        flags=re.IGNORECASE,
    )
    if not match:
        return label
    level = re.sub(r"\s+", " ", match.group("level")).strip()
    if level.lower() == "extrahigh":
        level = "Extra high"
    tag = match.group("tag") or ""
    return f"{level} {tag}".strip()


def _is_low_value_menu_option(label: str) -> bool:
    lowered = label.lower()
    if lowered in {"working", "work", "loading"}:
        return True
    if "esc to interrupt" in lowered:
        return True
    if "esc exit" in lowered or "↑/↓ browse" in lowered or "browse" in lowered:
        return True
    if lowered.startswith(("enter resume", "ctrl+o", "ctrl+t", "ctrl+e")):
        return True
    if "background terminal" in lowered:
        return True
    if "/model to change" in lowered or lowered.startswith("model:"):
        return True
    if " · /" in label or "\u00b7 /" in label:
        return True
    if lowered.startswith(("directory:", "cwd:", "path:")):
        return True
    if lowered.startswith(("token usage", "tip:", "to continue this session")):
        return True
    return False


def _looks_like_menu_option_label(label: str) -> bool:
    lowered = label.lower()
    if re.search(r"\b(gpt|claude|gemini|o[1-9]|llama|mistral|qwen|deepseek)\b", lowered):
        return True
    if re.fullmatch(r"(?:low|medium|high|extra high)(?:\s+\([^)]*\))?", lowered):
        return True
    if lowered.startswith(("approve", "reject", "allow", "deny")):
        return True
    if lowered.startswith(("yes,", "yes ", "no,", "no ")):
        return True
    if re.search(r"\b(session|resume|conversation|chat)\b", lowered):
        return bool(
            lowered.startswith(("session", "resume", "conversation", "chat"))
            or re.search(r"\b\d+\s*(?:s|m|h|d|w|mo|y)\s+ago\b", lowered)
            or re.search(r"\b\d{4}-\d{2}-\d{2}\b", lowered)
            or re.search(r"\b[0-9a-f]{8,}(?:-[0-9a-f]{4,}){1,}\b", lowered)
        )
    if re.search(r"\b\d+\s*(?:s|m|h|d|w|mo|y)\s+ago\b", lowered):
        return True
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", lowered):
        return True
    if re.search(r"\b[0-9a-f]{8,}(?:-[0-9a-f]{4,}){1,}\b", lowered):
        return True
    return False


def _selection_menu_title(options: list[str]) -> str:
    joined = " ".join(options).lower()
    if (
        "yes, proceed" in joined
        or "don't ask again" in joined
        or "tell codex" in joined
        or "approve" in joined
        or "reject" in joined
        or "allow" in joined
        or "deny" in joined
    ):
        return "请选择是否允许："
    if "gpt" in joined or "model" in joined or "claude" in joined:
        return "请选择模型："
    if re.search(r"\b(?:low|medium|high|extra high)\b", joined):
        return "请选择推理强度："
    if "resume" in joined or "session" in joined or "conversation" in joined:
        return "请选择会话："
    if re.search(r"\b\d+\s*(?:s|m|h|d|w|mo|y)\s+ago\b", joined):
        return "请选择会话："
    return "请选择终端菜单项："


def parse_telegram_input(text: str) -> TelegramInput:
    stripped = text.strip()
    stripped = _strip_telegram_bot_suffix(stripped)
    if stripped == "/start":
        return TelegramInput(TelegramInputKind.IGNORE, stripped)
    ta_command = _parse_teleagent_command(stripped)
    if ta_command is not None:
        return TelegramInput(TelegramInputKind.COMMAND, ta_command)
    if stripped in ("/all", "/summary", "/history", "/rawhistory"):
        return TelegramInput(TelegramInputKind.COMMAND, stripped)
    if stripped in ("/enter", "/submit"):
        return TelegramInput(TelegramInputKind.ENTER)
    if stripped.startswith("/type "):
        return TelegramInput(TelegramInputKind.TYPE, stripped[len("/type ") :])
    if stripped.startswith("/send "):
        return TelegramInput(TelegramInputKind.SEND, stripped[len("/send ") :])
    if stripped.startswith("/key "):
        return TelegramInput(TelegramInputKind.KEY, stripped[len("/key ") :].strip())
    if stripped != text.strip() and stripped.startswith("/"):
        return TelegramInput(TelegramInputKind.SEND, stripped)
    return TelegramInput(TelegramInputKind.SEND, text)


def _strip_telegram_bot_suffix(text: str) -> str:
    match = re.match(
        r"^/(?P<command>[A-Za-z0-9_]+)@[A-Za-z0-9_]+(?P<rest>\s.*)?$",
        text,
    )
    if not match:
        return text
    return f"/{match.group('command')}{match.group('rest') or ''}"


def _parse_teleagent_command(stripped: str) -> str | None:
    if stripped == "/ta":
        return "/help"
    if not stripped.startswith("/ta "):
        return None

    command = stripped[len("/ta ") :].strip().lower()
    if command == "auto":
        return "/auto status"
    if command.startswith("auto "):
        return "/auto " + command[len("auto ") :].strip()
    aliases = {
        "all": "/all",
        "summary": "/summary",
        "history": "/history",
        "rawhistory": "/rawhistory",
        "raw": "/rawhistory",
        "help": "/help",
    }
    return aliases.get(command)


def _append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(text)


def _append_history_entry(path: Path, text: str) -> None:
    text = text.strip()
    if not text:
        return
    _append_text(path, text + "\n\n")


def _write_clean_history_copy(path: Path) -> Path:
    cleaned = _format_for_mobile(path.read_text(encoding="utf-8", errors="replace"))
    clean_path = path.with_name(f"{path.stem}.clean{path.suffix}")
    clean_path.write_text((cleaned.rstrip() + "\n") if cleaned else "", encoding="utf-8")
    return clean_path


def _pending_raw_limit(config: TelegramConfig) -> int:
    return max(
        config.summary_threshold_chars * 8,
        config.all_chunk_chars * 4,
        config.max_message_chars * 4,
        1_000_000,
    )


def _terminal_chunk_has_pending_signal(text: str) -> bool:
    visible = _visible_terminal_text(text)
    visible = _strip_terminal_control_fragments(visible)
    if not visible.strip():
        return False
    lines = [line.strip() for line in visible.splitlines() if line.strip()]
    if not lines:
        return False
    return any(
        not _is_terminal_noise_only_line(_strip_line_edge_noise(line).strip())
        for line in lines
    )


def _terminal_raw_has_active_status(text: str) -> bool:
    visible = _visible_terminal_text(text)
    visible = _strip_terminal_control_fragments(visible)
    normalized = re.sub(r"\s+", " ", visible)
    return bool(
        re.search(
            r"(?i)\b\d+\s+background\s*terminals?\s+runn?ing\b",
            normalized,
        )
        or re.search(r"(?i)\bbackground\s*terminal\s+runn?g\b", normalized)
        or re.search(r"(?i)\b(?:esc\s+to\s+interrupt|/ps\s+to\s+view|/stop\s+to\s+close)\b", normalized)
        or re.search(r"(?i)\bW(?:aiting|aited)\s+for\s+background\s+terminal\b", normalized)
        or re.search(r"(?i)\bait(?:ing|ed)\s+for\s+background\s+terminal\b", normalized)
    )


def _is_terminal_noise_only_line(stripped: str) -> bool:
    if not stripped:
        return True
    if _is_spinner_fragment_line(stripped):
        return True
    if _is_terminal_decoration_fragment_line(stripped):
        return True
    if re.fullmatch(r"(?:\[?\d{1,3};\d{1,3}[A-Za-z]\d*|;?\d{1,3}[A-Za-z])", stripped):
        return True
    if re.fullmatch(r"\d{1,3}", stripped):
        return True
    return False


def _clean_terminal_text(text: str) -> str:
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b[PX^_].*?\x1b\\", "", text, flags=re.DOTALL)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\x1b[()][A-Za-z0-9]", "", text)
    text = re.sub(r"\x1b[=>]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x07", "")
    text = _apply_backspaces(text)
    text = _drop_terminal_ui_noise(text)
    text = "".join(
        char
        for char in text
        if char == "\n" or char == "\t" or ord(char) >= 32
    )
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\s+(?=[，。！？；：、])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff，。！？；：、])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = re.sub(r"(?m)^•\s*", "", text)
    text = re.sub(r"\s*•\s*$", "", text)
    return text.strip()


def _remove_recent_user_echoes(text: str, recent_inputs: list[str]) -> str:
    for user_input in recent_inputs:
        stripped = user_input.strip()
        if not stripped:
            continue

        escaped = re.escape(stripped)
        text = re.sub(rf"(?m)^\s*›?\s*{escaped}\s*(?:\n|$)", "\n", text)
        if _can_strip_user_echo_prefix(stripped):
            text = re.sub(
                rf"(?m)^\s*›?\s*{escaped}(?=[^\s，。！？；：、,.!?;:])",
                "",
                text,
            )

        max_prefix = min(len(stripped), 12)
        for length in range(max_prefix, 1, -1):
            prefix = stripped[:length]
            if prefix.isspace():
                continue
            text = re.sub(
                rf"(?m)^\s*›?\s*{re.escape(prefix)}(?={_inline_noise_pattern()}|M|Working|Worki|Workin)",
                "",
                text,
            )
    return text


def _can_strip_user_echo_prefix(text: str) -> bool:
    if text.startswith("/"):
        return True
    if len(text) >= 6:
        return True
    return bool(re.search(r"[A-Za-z]", text) and re.search(r"\s", text))


def _apply_backspaces(text: str) -> str:
    output: list[str] = []
    for char in text:
        if char == "\b" or char == "\x7f":
            if output and output[-1] != "\n":
                output.pop()
            continue
        output.append(char)
    return "".join(output)


def _drop_terminal_ui_noise(text: str) -> str:
    text = _strip_terminal_control_fragments(text)
    text = _strip_inline_composer_echoes(text)
    text = _strip_inline_terminal_ui_noise(text)
    text = _drop_plain_codex_menu_blocks(text)
    text = re.sub(r"(?:\d+s)?Context compacted", "", text)
    text = re.sub(r"\[(?:\d{1,3};)*\d{1,3}m", "", text)
    text = _strip_sgr_fragment_noise(text)
    text = re.sub(r"\b\d{1,4}\s+\+\s+", "", text)
    text = re.sub(r"(\.(?:json|csv|py))ing(?=[A-Za-z_])", r"\1 ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff。！？；：，、])g(?=[A-Za-z_])", "", text)
    text = re.sub(r"(?:edinged|ingeding|eding|inged)(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(
        r"\d+(?:ingngg|ingng|rking|orking|king|eding|inged|edng|runing|ngg|ing|ng|g)"
        r"(?:\d+[sm]?|M?)+",
        "",
        text,
    )
    text = re.sub(
        r"(?:ingngg|ingng|rking|orking|king|eding|inged|edng|runing|ngg|ing|ng|g)\d+"
        r"(?:(?:ingngg|ingng|rking|orking|king|eding|inged|edng|runing|ngg|ing|ng|g)|\d|[smM])+",
        "",
        text,
    )
    text = re.sub(r"(?m)^› .*(?:\n[ \t]+.*)*", "", text)
    text = re.sub(r"\s*›\s*/+\s*(?=\n|$)", "", text)
    text = re.sub(r"(?s)›\s*/{1,2}model\b.*?(?=\n\s*\n|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?sm)^\s*(?:›\s*)?/resume\b.*?(?=\n\s*\n|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?s)›\s*/resume\b.*?(?=\n\s*\n|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?s)M{2,}\s*/resume\b.*?(?=\n\s*\n|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?s)Resume a previous session.*?(?=\n\s*\n|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"⚠\s*(?:Heads up, you have less than .*?weekly limit left\. Run /status for a breakdown\.|"
        r"Selected model is at capacity\. Please try a different model\.)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"请把你刚才过长的回复总结成.*?字以内。?"
        r"面向手机聊天阅读：先给一句话结论，再用短条目列关键点；不要复述完整原文。?",
        "\n",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(r"\]?0;[^\n]*(?:TeleAgent|Working|Worki|Workin|Wor|Wo)[^\n]*", "", text)
    text = re.sub(r"(?:›\s*)?Use /skills[^\n]*(?=\n|$)", "", text)
    text = re.sub(
        r"(?:›\s*)?Run /review on my current changes(?:[•·]\s*)?(?:›\s*)?"
        r"(?:gpt-[\w.-]+\s+\w+\s+·\s+/[^\s\u4e00-\u9fff•›]+)?",
        "",
        text,
    )
    text = re.sub(r"(?:›\s*)?Implement \{feature\}(?:\s+\{feature\})?", "", text)
    text = re.sub(r"(?m)(?<!^)\s+›\s+.*?(?=\s{2,}|$)", " ", text)
    text = re.sub(r"M\s*(?=" + _inline_noise_pattern() + ")", "", text)
    text = re.sub(_inline_noise_pattern(), "", text)
    text = re.sub(r"\(\d+s\s*[•·]?\s*M?\s*(?=[\u4e00-\u9fff，。！？；：、])", "", text)
    text = re.sub(r"TeleAgent\s*[•·]?\s*(?:Working|Workin|Worki)?\s*\(\d+s\s*[•·]?", "", text)
    text = re.sub(r"(?m)^\s*TeleAgent\s*[•·]?\s*$", "", text)
    text = re.sub(
        r"(?:[›•·]\s*)?(?:gpt|(?<![A-Za-z0-9_])pt)-[\w.-]+\s+\w+\s+·\s+"
        r"/data/lyxie/(?:ReID_imu_generation|Motion-X|TeleAgent)",
        "",
        text,
    )
    text = re.sub(
        r"(?:[›•·]\s*)?(?:gpt|(?<![A-Za-z0-9_])pt)-[\w.-]+\s+\w+\s+·\s+/"
        r"[^\s\u4e00-\u9fff•›]+(?=\s|$|[›•·，。！？；：、])",
        "",
        text,
    )
    text = re.sub(r"\besc\s+to\s+interr?upt\)?", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?<=[\u4e00-\u9fff。！？；：，、])\s*(?:M\s*)?(?:ing|ngg|ng|g)\s*(?=[\u4e00-\u9fff。！？；：，、])",
        "",
        text,
    )
    text = re.sub(
        r"(?<=[\u4e00-\u9fff。！？；：，、])(?:ing|ngg|ng|g)+\s*(?=[•·]?\s*\d+\s+background)",
        "",
        text,
    )
    spinner_fragment = _spinner_fragment_pattern()
    text = re.sub(
        rf"(?:[•·*mM\d]+[ \t]*)+{spinner_fragment}(?:[•·*mM\d \t]*)*",
        "",
        text,
    )
    text = re.sub(r"\b(?:Working|Workin|Worki|Work|Wor|Wo|W)\b(?:\(\d+s[^)]*\))?", "", text)
    text = re.sub(r"TeleAgent\s*[•·]?\s*\(\d+s\s*[•·]?", "", text)
    text = re.sub(rf"(?:{spinner_fragment}){{3,}}", "", text)
    text = re.sub(
        rf"(?:(?:[•·*\sMm]|\d+[sm]?)*{spinner_fragment}(?:[•·*\sMm]|\d+[sm]?)+){{2,}}",
        "",
        text,
    )
    text = re.sub(
        r"(?:(?:\d+\s*)?(?:ingngg|ingng|rking|orking|kinging|king|eding|inged|edng|runing|"
        r"Working|Workin|Worki|Work|Wor|Wo|W)"
        r"(?:\s*\d+[sm]?)?){2,}",
        "",
        text,
    )
    text = re.sub(
        r"(?<=[\u4e00-\u9fff。！？；：，、])(?:\d+\s*){2,}(?=\s|[\u4e00-\u9fff]|$)",
        "",
        text,
    )
    text = re.sub(
        r"(?<=[\u4e00-\u9fff。！？；：，、])"
        r"(?:rking|orking|kinging|king|eding|inged|edng|runing|ingngg|ingng|ngg|ing|ng|g)\d+"
        r"(?=\s|[□✔]|\n|$)",
        "",
        text,
    )
    text = re.sub(
        r"\s*[□✔]\s+(?:Compare|Update|Inspect|Read|Run|Write|Search|List|Fix|Test)\b[^\n]*",
        "",
        text,
    )
    text = re.sub(r"(?:edinged|ingeding|eding|inged)(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(
        r"(?:Work(?:ing|in|i)?\d*s?(?:ing|ngg|ng|g)?\d*){2,}",
        "",
        text,
    )
    text = re.sub(r"\bM(?:\s*M)+\b", "", text)
    text = re.sub(r"\bM{2,}\b", "", text)
    text = re.sub(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]", "", text)

    kept_lines: list[str] = []
    dropping_prompt_continuation = False
    dropping_terminal_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept_lines.append("")
            dropping_prompt_continuation = False
            dropping_terminal_block = False
            continue
        if dropping_terminal_block and _is_terminal_ui_block_continuation(line, stripped):
            continue
        dropping_terminal_block = False
        if _is_terminal_ui_block_header(stripped):
            dropping_terminal_block = True
            continue
        if _is_terminal_ui_line(stripped):
            continue
        if _is_spinner_fragment_line(stripped):
            continue
        if _is_terminal_decoration_fragment_line(stripped):
            continue
        if stripped.startswith(("› Use /skills", "Use /skills")):
            continue
        if stripped.startswith("› "):
            dropping_prompt_continuation = True
            continue
        if dropping_prompt_continuation and line[:1].isspace():
            continue
        dropping_prompt_continuation = False
        if stripped.startswith("]0;"):
            continue
        cleaned_line = _strip_line_edge_noise(line)
        cleaned_line = _strip_inline_terminal_ui_noise(cleaned_line)
        cleaned_line = _strip_sgr_fragment_noise(cleaned_line)
        if cleaned_line.strip() in {"TeleAgent", "TeleAgent •"}:
            continue
        if _is_terminal_ui_line(cleaned_line.strip()):
            continue
        if _is_spinner_fragment_line(cleaned_line.strip()):
            continue
        if cleaned_line.strip():
            kept_lines.append(cleaned_line)
    return "\n".join(kept_lines)


def _strip_terminal_control_fragments(text: str) -> str:
    control_fragment = r"(?:\[?\d{1,3};\d{1,3}[A-Za-z]\d*|;?\d{1,3};\d{1,3}[A-Za-z]\d*)"
    text = re.sub(rf"(?m)^\s*{control_fragment}\s*$", "", text)
    text = re.sub(rf"(?m)^\s*{control_fragment}\s*(?=\S)", "", text)
    text = re.sub(rf"(?m)^\s*;?\d{{1,3}}[A-Za-z]\s*(?=\S)", "", text)
    return text


def _strip_sgr_fragment_noise(text: str) -> str:
    return re.sub(
        r"(?<![\w/])(?:;?\d{1,3})(?:;\d{1,3}){1,8}m(?=[A-Za-z•·*\s\n]|$)",
        "",
        text,
    )


def _spinner_fragment_pattern() -> str:
    return (
        r"(?:Working|Workin|Worki|Work|Wor|Wo|W|"
        r"orking|mork|ork|rking|kinging|king|ingngg|ingng|ngg|ing|ng|g)"
    )


def _is_spinner_fragment_line(stripped: str) -> bool:
    if not stripped:
        return False
    if not re.search(
        r"Working|Workin|Worki|orking|mork|ork|rking|kinging|ingngg|ingng|ngg|ng\b|[•·*]",
        stripped,
    ):
        return False
    candidate = _strip_sgr_fragment_noise(stripped)
    candidate = re.sub(_spinner_fragment_pattern(), "", candidate)
    candidate = re.sub(r"[•·*\sMm\d;:()[\],.+\-]+", "", candidate)
    return candidate == ""


def _is_terminal_decoration_fragment_line(stripped: str) -> bool:
    if not stripped:
        return False
    if re.fullmatch(r"\[?\d{1,3};\d{1,3}[A-Za-z]\d*", stripped):
        return True
    if not re.search(r"[•·*mM›]", stripped):
        return False
    return bool(re.fullmatch(r"[mM\s•·*›\-|/\\\d]+", stripped))


def _drop_plain_codex_menu_blocks(text: str) -> str:
    text = re.sub(
        r"(?is)(?:›\s*)?/{1,2}model\b.*?"
        r"(?:Select Model and Effort|Select Reasoning Level).*?"
        r"Press enter to confirm or esc to go back"
        r"(?:[^\n]*(?:Model changed to[^\n]*)?)?",
        "\n",
        text,
    )
    text = re.sub(
        r"(?is)(?:or the composer)?/"
        r"(?:experimental|approve|memories|mention|mcp|mcplist|model)[^\n]*?"
        r"(?:Select Model and Effort|Select Reasoning Level).*?"
        r"Press enter to confirm or esc to go back"
        r"(?:[^\n]*(?:Model changed to[^\n]*)?)?",
        "\n",
        text,
    )
    text = re.sub(
        r"(?is)(?:›\s*)?/+[a-z][a-z0-9_-]*[^\n]{0,120}?"
        r"(?:Select Model and Effort|Select Reasoning Level).*?"
        r"Press enter to confirm or esc to go back"
        r"(?:[^\n]*(?:Model changed to[^\n]*)?)?",
        "\n",
        text,
    )
    text = re.sub(
        r"(?is)(?:›\s*)?/resume[^\n]{0,120}?"
        r"Resume a previous session.*?"
        r"(?:enter resume|esc exit|ctrl\+c exit|↑/↓ browse)"
        r"[^\n]*",
        "\n",
        text,
    )
    text = re.sub(
        r"(?is)(?:›Shutting down\.\.\.|exit)?\s*M*\s*/?resume\s+resume a saved chat"
        r"\s*Resume a previous session.*?"
        r"(?:enter resume|esc exit|ctrl\+c exit|↑/↓ browse)"
        r"[^\n]*",
        "\n",
        text,
    )
    text = re.sub(
        r"(?is)Resume a previous session.*?"
        r"(?:enter resume|esc exit|ctrl\+c exit|↑/↓ browse)"
        r"[^\n]*",
        "\n",
        text,
    )
    return text


def _strip_inline_composer_echoes(text: str) -> str:
    status_bar = (
        r"(?:gpt|(?<![A-Za-z0-9_])pt)-[\w.-]+\s+\w+\s+·\s+/"
        r"[^\s\u4e00-\u9fff•›]+"
    )
    text = re.sub(
        rf"›[^\n]*?{status_bar}(?![^\n]*›\s*tab\s*to\s*queue)[^\n]*(?=\n|$)",
        "",
        text,
    )
    text = re.sub(
        rf"›[^\n]*?{status_bar}",
        "",
        text,
    )
    text = re.sub(
        r"›\s*tab\s*to\s*queue\s*message\s*\d+%\s*context\s*left",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"›[^\n]*(?:Run /review on my current changes|Use /skills to list available skills|"
        rf"Implement \{{feature\}}|Shutting down|/{{1,2}}model\b|/resume\b)[^\n]*(?=\n|$)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _strip_inline_terminal_ui_noise(text: str) -> str:
    text = re.sub(r"Token usage:.*?(?=\n|$)", "", text)
    text = re.sub(r"To continue this session, run codex resume[^\n]*", "", text)
    text = re.sub(r"\s*Tip:\s*New Build faster with Codex\.?", "", text)
    text = re.sub(r"(?:›\s*)?Use /skills to list available skills[^\n]*(?=\n|$)", "", text)
    text = re.sub(
        r"(?is)(?:›\s*)?/(?:model)?\s*choose what model and reasoning effort to use.*?"
        r"(?:/approveapprove one retry of a recent auto-review denial|(?=\n\s*\n|$))",
        "",
        text,
    )
    text = re.sub(
        r"(?:›\s*)?(?:tab\s*)?to queue message\s*\d+%\s*context left",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?:›\s*)?Run /review on my current changes(?:[•·]\s*)?(?:›\s*)?"
        r"(?:gpt-[\w.-]+\s+\w+\s+·\s+/[^\s\u4e00-\u9fff•›]+)?",
        "",
        text,
    )
    text = re.sub(r"(?:›\s*)?Implement \{feature\}(?:\s+\{feature\})?", "", text)
    text = re.sub(r"(?:[›•·]\s*)?\bgpt-[\w.-]+\s+\w+\s+·\s+/[^\s\u4e00-\u9fff•›]+", "", text)
    text = re.sub(
        r"M*(?:Working|Workin|Worki|Work)\(\d+s[^)\n\u4e00-\u9fff]*\)",
        "",
        text,
    )
    text = re.sub(
        r"[•·]?\s*(?:Messages\s*to\s*be\s*submitted\s*after\s*next\s*tool\s*call|"
        r"Messagestobesubmittedafternexttoolcall)\s*\([^)]*\)\s*↳?",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*↳\s*", "\n", text)
    text = re.sub(
        r"\s*[•·]?\s*\(\d+s[^)\n\u4e00-\u9fff]*(?:background terminals? running|esc to interrupt|/ps to view|/stop to close)[^)\n\u4e00-\u9fff]*\)?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s*[•·]?\s*\(\d+m\s+\d+s[^)\n\u4e00-\u9fff]*(?:background terminals? running|esc to interrupt|/ps to view|/stop to close)?[^)\n\u4e00-\u9fff]*\)?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\(\d+s[0-9A-Za-z]*", "", text)
    text = re.sub(
        r"\s*[•·]?\s*\d+\s+background terminals? running\s*[•·]?\s*/ps to view\s*[•·]?\s*/stop to close\w*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s*[•·]?\s*\d+\s+background\s*terminals?\s+runn?g\s*[•·]?\s*/[ps]?\s*to\s*view\s*[•·]?\s*/stop\w*\s*(?:to|o)?\s*close\w*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s*\d+s\s+runing\s*[•·]?\s*/ps to view\s*[•·]?\s*/stop to close\w*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?im)^\s*[•·]?\s*(?:W?ait(?:ed|ing)|ait(?:ed|ing))\s+for\s+background\s+terminal\b[^\n]*",
        "",
        text,
    )
    text = re.sub(
        r"\s*[•·]?\s*(?:W?ait(?:ed|ing)|ait(?:ed|ing))\s+for\s+background\s+terminal\b[^\n]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*─\s*Worked for [^─\n]*(?:─+)?", "", text)
    text = re.sub(r"[─━]{6,}\s*[•·]?", "", text)
    text = re.sub(
        r"(?m)(?<!^)(?:Exploring|Explored|Ran\s+|Read\s+|Search\s+|List\s+|"
        r"Updated Plan|Evaluating\s+|Wrote\s+|Edited\s+|Added\s+)[^\n]*",
        "",
        text,
    )
    return text


def _is_terminal_ui_block_header(stripped: str) -> bool:
    if re.match(
        r"^(?:Exploring|Explored|Ran\s+|Read\s+|Search\s+|List\s+|Updated Plan|"
        r"Evaluating\s+|Wrote\s+|Edited\s+|Added\s+)\b",
        stripped,
    ):
        return True
    if re.match(r"^(?:File \".*\", line \d+|ModuleNotFoundError:|Traceback\b)", stripped):
        return True
    return False


def _is_terminal_ui_block_continuation(line: str, stripped: str) -> bool:
    if line[:1].isspace():
        return True
    if stripped.startswith(("└", "├", "│", "…", "□", "✔", "+", "}", "{")):
        return True
    if "ctrl + t to view transcript" in stripped:
        return True
    if "Context compacted" in stripped:
        return True
    if re.search(r"\[(?:3[89]|0)m|\[3[89];|\[38;|\[39;|\[48;|\[49;", stripped):
        return True
    if re.match(r"^\d+\s+[+-]", stripped):
        return True
    if stripped.startswith(("□", "✔")):
        return True
    if re.fullmatch(r"[=+\-\s0-9.]+", stripped):
        return True
    return False


def _is_terminal_ui_line(stripped: str) -> bool:
    if not stripped:
        return False
    if re.fullmatch(r"[╭╮╰╯│─└├┤┬┴┼\s]+", stripped):
        return True
    if stripped.startswith(("╭", "╰", "│", "└", "├")):
        return True
    if "OpenAI Codex" in stripped:
        return True
    if re.match(r"^(?:Token usage:|To continue this session|Tip:)", stripped):
        return True
    if "model:" in stripped and "/model to change" in stripped:
        return True
    if "directory:" in stripped and "TeleAgent" in stripped:
        return True
    if "background terminal" in stripped and ("/ps to view" in stripped or "/stop to close" in stripped):
        return True
    if "Messages" in stripped and "submitted" in stripped and "tool" in stripped:
        return True
    if "Messagestobesubmittedafternexttoolcall" in stripped:
        return True
    if "ctrl + t to view transcript" in stripped:
        return True
    if "tab to queue message" in stripped or "context left" in stripped:
        return True
    if re.match(r"^M?odel changed to(?:\s|[A-Za-z0-9_.-]|$)", stripped, flags=re.IGNORECASE):
        return True
    if re.match(r"^(?:gpt|pt)-[\w.-]+\s+\w+\s+·\s+/[^\s\u4e00-\u9fff•›]+", stripped):
        return True
    if re.match(r"^(?:Wrote|Edited|Added|Evaluating|Updated Plan)\b", stripped):
        return True
    if "Run /review on my current changes" in stripped:
        return True
    if "Selected model is at capacity" in stripped:
        return True
    if "weekly limit left" in stripped and "/status" in stripped:
        return True
    if not re.search(r"[\u4e00-\u9fff]", stripped) and re.match(
        r"^[\w_().| /:-]+ in [\w./-]+$",
        stripped,
    ):
        return True
    return False


def _strip_line_edge_noise(line: str) -> str:
    line = _strip_sgr_fragment_noise(line)
    spinner_fragment = _spinner_fragment_pattern()
    line = re.sub(r"^\s*(?:" + _inline_noise_pattern() + r")\s*\(\d+s\s*M?\s*", "", line)
    line = re.sub(r"^\s*.{0,4}M*Working\([^)]*\)\s*", "", line)
    line = re.sub(
        rf"^\s*(?:[•·*mM\s]|\d+[sm]?)+{spinner_fragment}(?:[•·*mM\s]|\d+[sm]?)*"
        r"(?=[\u4e00-\u9fff])",
        "",
        line,
    )
    line = re.sub(r"^\s*[•·*mM\s]*[•·*]{2,}\s*\d+[sm]?\s*(?=[\u4e00-\u9fff])", "", line)
    line = re.sub(r"^\s*[mM]?[•·*]{2,}\s*(?=[\u4e00-\u9fff])", "", line)
    line = re.sub(r"^\s*[•·]?\s*\(\d+s[0-9A-Za-z]*", "", line)
    line = re.sub(r"^\s*(?:ing|ngg|ng|g)+(?=[\u4e00-\u9fff])", "", line)
    line = re.sub(
        r"^\s*(?:(?:\d+)?(?:ingngg|ingng|rking|orking|kinging|king|eding|inged|edng|runing|"
        r"Working|Workin|Worki|Work|Wor|Wo|W)\d*)+"
        r"(?=[\u4e00-\u9fff])",
        "",
        line,
    )
    line = re.sub(r"^\s*(?:M\s+){2,}", "", line)
    line = re.sub(r"^\s*M{2,}(?=[A-Za-z])", "M", line)
    line = re.sub(r"^\s*M+(?=[\u4e00-\u9fff，。！？；：、])", "", line)
    line = re.sub(r"^\s*M{2,}\s+(?=\S)", "", line)
    line = re.sub(r"^\s*(?:Working|Workin|Worki)+\s*", "", line)
    line = re.sub(r"\s*\(\d+s\s*M?\s*$", "", line)
    line = re.sub(r"\s*(?:Working|Workin|Worki|orking|rking|kinging|king|eding|inged|edng|runing)+\s*$", "", line)
    line = re.sub(
        r"\s*(?:\d+\s*)?(?:ingngg|ingng|rking|orking|kinging|king|eding|inged|edng|runing|"
        r"Working|Workin|Worki|Work|Wor|Wo)+\s*$",
        "",
        line,
    )
    line = re.sub(
        rf"(?<=[\u4e00-\u9fff。！？；：，、])\s*(?:[•·*mM\s]|\d+[sm]?)*"
        rf"{spinner_fragment}(?:[•·*mM\s]|\d+[sm]?)*$",
        "",
        line,
    )
    line = re.sub(r"(?<=[。！？；：，、])\s*\d{1,2}$", "", line)
    line = re.sub(
        r"(?<=[\u4e00-\u9fff。！？；：，、])\s*[•·*mM\s]*[•·*]{2,}\s*\d*[mM]?\s*$",
        "",
        line,
    )
    line = re.sub(r"(?<=[\u4e00-\u9fff。！？；：，、])\s*(?:ing|ngg|ng|g)$", "", line)
    line = re.sub(r"(?<=[\u4e00-\u9fff。！？；：，、])M\s*(?=[\u4e00-\u9fff。！？；：，、])", "", line)
    line = re.sub(r"\s*M+$", "", line)
    return line


def _inline_noise_pattern() -> str:
    escaped_phrases = sorted(
        (re.escape(phrase) for phrase in INLINE_NOISE_PHRASES),
        key=len,
        reverse=True,
    )
    command_patterns = [
        r"write\s+tests\s+for\s+@filenames?",
        r"improve\s+documentation\s+in\s+@filenames?",
        r"improve\s+docs\s+in\s+@filenames?",
        r"summarize\s+recent\s+commits",
        r"review\s+code\s+for\s+bugs",
        r"explain\s+this\s+codebase",
        r"explain\s+selected\s+code",
        r"generate\s+a\s+plan",
        r"fix\s+this\s+bug",
        r"implement\s+feature",
        r"implement\s+\{feature\}",
        r"improve\s+code\s+quality",
        r"run\s+/review\s+on\s+my\s+current\s+changes",
    ]
    return "(?i:" + "|".join(command_patterns + escaped_phrases) + ")"


def _format_for_mobile(text: str) -> str:
    text, transcript_inputs = _extract_transcript_reply(text)
    if transcript_inputs:
        text = _remove_recent_user_echoes(text, transcript_inputs)
    text = _clean_terminal_text(text)
    if not text:
        return ""
    return _format_mobile_layout(text)


def _format_structured_for_mobile(text: str) -> str:
    text = _clean_structured_text(text)
    if not text:
        return ""
    return _format_mobile_layout(text)


def _looks_like_summary_reply(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return bool(re.match(_summary_reply_marker_pattern(), stripped))


def _extract_summary_reply_section(text: str) -> str:
    text = text.strip()
    if not text:
        return ""

    marker = re.search(_summary_reply_marker_pattern(), text)
    if marker is None:
        return ""
    text = text[marker.start() :].strip()

    second_marker = re.search(r"(?s).+?(?=\s+" + _summary_reply_marker_pattern() + r")", text)
    if second_marker is not None:
        text = second_marker.group(0).strip()

    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if kept:
                kept.append("")
            continue
        if kept and (
            _looks_like_summary_reply(stripped)
            or _is_summary_reply_boundary_line(stripped)
        ):
            break
        kept.append(line)

    return "\n".join(kept).strip()


def _extract_summary_reply_from_terminal_raw(text: str) -> str:
    visible = _visible_terminal_text(text)
    lines = visible.splitlines()
    collected: list[str] = []
    collecting = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if collecting and collected:
                collected.append("")
            continue

        bullet = re.match(r"^[•●]\s*(?P<body>.+)$", stripped)
        body = bullet.group("body").strip() if bullet else stripped

        if not collecting:
            marker = re.search(_summary_reply_marker_pattern(), body)
            if marker is None:
                continue
            collected.append(body[marker.start() :].strip())
            collecting = True
            continue

        if bullet or stripped.startswith("›"):
            break
        if _is_summary_reply_boundary_line(stripped):
            break
        collected.append(stripped)

    return "\n".join(collected).strip()


def _summary_reply_marker_pattern() -> str:
    return r"(?:一句话(?:结论|总结)?|简要(?:结论|总结)|总结|结论)[：:]"


def _is_summary_reply_boundary_line(stripped: str) -> bool:
    if _is_terminal_ui_line(stripped):
        return True
    if _is_terminal_ui_block_header(stripped):
        return True
    lowered = stripped.lower()
    if any(
        token in lowered
        for token in (
            "background terminal",
            "/ps to view",
            "/stop to close",
            "esc to interrupt",
            "waiting for background terminal",
            "aiting for background terminal",
        )
    ):
        return True
    if re.match(r"^\d+\s+\d{2}:\d{2}\s+\d+(?:\.\d+)?\s+\d+(?:\.\d+)?\b", stripped):
        return True
    if re.match(r"^(?:bwrap|python(?:\d+(?:\.\d+)?)?)\s+", stripped):
        return True
    if re.match(r"^(?:outputs/|/data/|/tmp/|[\w.-]+\.(?:csv|json|log))\b", stripped):
        return True
    if stripped.count(",") >= 6 and re.search(r"\d", stripped):
        return True
    return False


def _format_mobile_layout(text: str) -> str:
    text = re.sub(r"([：:])\s*-\s*", r"\1\n- ", text)
    text = re.sub(r"([。！？.!?])\s*-\s*", r"\1\n- ", text)
    text = re.sub(r"(?<=[0-9])-\s+(?=[\u4e00-\u9fffA-Za-z])", "\n- ", text)
    text = re.sub(r"(?<!^)\s*(关键点[：:])", r"\n\1", text)
    text = re.sub(r"(?<!^)\s*(一句话(?:总结|结论)?[：:])", r"\n\1", text)
    text = re.sub(r"(?<!^)\s*(核心代码[：:])", r"\n\1", text)
    text = re.sub(r"(?<!^)\s*(主要文件[：:])", r"\n\1", text)
    text = re.sub(r"(?<!^)\s*(典型运行[：:])", r"\n\1", text)
    text = re.sub(r"(?<!^)\s-\s+", "\n- ", text)
    text = re.sub(r"\s{2,}([^\s，。,.;；]{2,16}[：:])", r"\n\1", text)
    text = re.sub(r"\s{2,}(-\s+)", r"\n\1", text)
    text = re.sub(r"([：:])\s{2,}(?=\S)", r"\1\n", text)

    lines = [line.strip() for line in text.splitlines()]
    blocks: list[str] = []
    current: list[str] = []

    for line in lines:
        if not line:
            if current:
                blocks.append(_join_wrapped_lines(current))
                current = []
            continue
        if _is_bullet_line(line):
            if current:
                blocks.append(_join_wrapped_lines(current))
                current = []
            blocks.append(_normalize_bullet(line))
            continue
        current.append(line)

    if current:
        blocks.append(_join_wrapped_lines(current))

    result = "\n\n".join(block for block in blocks if block.strip())
    result = re.sub(r"(?m)(^- .*)\n\n(?=- )", r"\1\n", result)
    result = re.sub(r"(?m)(^\d+[.)] .*)\n\n(?=\d+[.)] )", r"\1\n", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _extract_transcript_reply(text: str) -> tuple[str, list[str]]:
    blocks: list[tuple[str, list[str]]] = []
    current_label: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+):\s*$", line.strip())
        if match:
            if current_label is not None:
                blocks.append((current_label, current_lines))
            current_label = match.group(1)
            current_lines = []
            continue
        if current_label is not None:
            current_lines.append(line)

    if current_label is not None:
        blocks.append((current_label, current_lines))

    if not blocks:
        return text, []

    bot_index: int | None = None
    for index, (label, _) in enumerate(blocks):
        lowered = label.lower()
        if "bot" in lowered or lowered in {"assistant", "ai", "codex", "claude"}:
            bot_index = index

    if bot_index is None:
        return text, []

    bot_text = "\n".join(blocks[bot_index][1])
    recent_user_inputs = [
        "\n".join(lines).strip()
        for label, lines in blocks[:bot_index]
        if "bot" not in label.lower() and "\n".join(lines).strip()
    ]
    return bot_text, recent_user_inputs[-3:]


def _is_bullet_line(line: str) -> bool:
    return bool(re.match(r"^(?:[-*•]|\d+[.)]|[一二三四五六七八九十]+[、.])\s+", line))


def _normalize_bullet(line: str) -> str:
    line = re.sub(r"^[*•]\s+", "- ", line)
    return line


def _join_wrapped_lines(lines: list[str]) -> str:
    if not lines:
        return ""
    output = lines[0]
    for line in lines[1:]:
        if _is_bullet_line(line):
            output += "\n" + _normalize_bullet(line)
        elif re.match(r"^[^\s，。,.;；]{2,16}[：:]", line):
            output += "\n" + line
        elif re.search(r"[。！？.!?:：]$", output):
            output += "\n" + line
        else:
            output += " " + line
    return output


def _multipart_body(
    boundary: str,
    fields: dict[str, str],
    file_field: str,
    path: Path,
) -> bytes:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode())
        parts.append(b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{path.name}"\r\n'
        ).encode()
    )
    parts.append(b"Content-Type: text/plain\r\n\r\n")
    parts.append(path.read_bytes())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)

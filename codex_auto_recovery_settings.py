#!/usr/bin/env python3
"""Non-secret settings and Windows credential storage for the recovery TUI."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import tempfile
from pathlib import Path
from typing import Any


CREDENTIAL_TARGET = "CodexAutoRecovery/Sub2API"


class AppSettings:
    def __init__(
        self,
        base_url: str = "",
        selected_tasks: list[str] | None = None,
        stale_seconds: int = 120,
        initial_delay: float = 60.0,
        poll_interval: float = 15.0,
        loop_interval: float = 5.0,
        lookback_hours: float = 6.0,
        max_tasks: int = 20,
        api_timeout: float = 20.0,
        codex_timeout: float = 30.0,
        codex_cli: str = "",
        dry_run: bool = True,
    ) -> None:
        self.base_url = base_url
        self.selected_tasks = list(selected_tasks or [])
        self.stale_seconds = stale_seconds
        self.initial_delay = initial_delay
        self.poll_interval = poll_interval
        self.loop_interval = loop_interval
        self.lookback_hours = lookback_hours
        self.max_tasks = max_tasks
        self.api_timeout = api_timeout
        self.codex_timeout = codex_timeout
        self.codex_cli = codex_cli
        self.dry_run = dry_run

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "selected_tasks": list(self.selected_tasks),
            "stale_seconds": int(self.stale_seconds),
            "initial_delay": float(self.initial_delay),
            "poll_interval": float(self.poll_interval),
            "loop_interval": float(self.loop_interval),
            "lookback_hours": float(self.lookback_hours),
            "max_tasks": int(self.max_tasks),
            "api_timeout": float(self.api_timeout),
            "codex_timeout": float(self.codex_timeout),
            "codex_cli": self.codex_cli,
            "dry_run": bool(self.dry_run),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "AppSettings":
        if not isinstance(value, dict):
            return cls()
        defaults = cls()
        result = cls(
            base_url=str(value.get("base_url", defaults.base_url) or ""),
            selected_tasks=[str(item) for item in value.get("selected_tasks", []) if item],
            stale_seconds=_positive_int(value.get("stale_seconds"), defaults.stale_seconds),
            initial_delay=_positive_float(value.get("initial_delay"), defaults.initial_delay),
            poll_interval=_positive_float(value.get("poll_interval"), defaults.poll_interval),
            loop_interval=_positive_float(value.get("loop_interval"), defaults.loop_interval),
            lookback_hours=_positive_float(value.get("lookback_hours"), defaults.lookback_hours),
            max_tasks=_positive_int(value.get("max_tasks"), defaults.max_tasks),
            api_timeout=_positive_float(value.get("api_timeout"), defaults.api_timeout),
            codex_timeout=_positive_float(value.get("codex_timeout"), defaults.codex_timeout),
            codex_cli=str(value.get("codex_cli", defaults.codex_cli) or ""),
            dry_run=bool(value.get("dry_run", defaults.dry_run)),
        )
        return result


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def load_settings(path: Path) -> AppSettings:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return AppSettings.from_dict(json.load(handle))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return AppSettings()


def save_settings(path: Path, settings: AppSettings) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(settings.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def redact_secret(text: str, secret: str | None) -> str:
    if not secret:
        return text
    return text.replace(secret, "***")


class MemoryCredentialBackend:
    """Test backend and fallback used when Windows Credential Manager is unavailable."""

    def __init__(self) -> None:
        self._value: str | None = None

    def read(self) -> str | None:
        return self._value

    def write(self, value: str) -> None:
        self._value = value

    def delete(self) -> None:
        self._value = None


class WindowsCredentialBackend:
    def __init__(self, target_name: str = CREDENTIAL_TARGET) -> None:
        self.target_name = target_name

    def read(self) -> str | None:
        if os.name != "nt":
            return None
        credential = wintypes.LPVOID()
        if not ctypes.windll.advapi32.CredReadW(
            self.target_name, 1, 0, ctypes.byref(credential)
        ):
            return None
        try:
            class CREDENTIAL(ctypes.Structure):
                _fields_ = [
                    ("Flags", wintypes.DWORD),
                    ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", wintypes.LPVOID),
                    ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD),
                    ("Attributes", wintypes.LPVOID),
                    ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR),
                ]

            pointer = ctypes.cast(credential, ctypes.POINTER(CREDENTIAL))
            item = pointer.contents
            raw = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize)
            return raw.decode("utf-8")
        finally:
            ctypes.windll.advapi32.CredFree(credential)

    def write(self, value: str) -> None:
        if os.name != "nt":
            raise OSError("Windows Credential Manager is only available on Windows")
        blob = value.encode("utf-8")

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", wintypes.LPVOID),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", wintypes.LPVOID),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        buffer = ctypes.create_string_buffer(blob)
        item = CREDENTIAL(
            0,
            1,
            self.target_name,
            None,
            wintypes.FILETIME(),
            len(blob),
            ctypes.cast(buffer, wintypes.LPVOID),
            2,
            0,
            None,
            None,
            "CodexAutoRecovery",
        )
        if not ctypes.windll.advapi32.CredWriteW(ctypes.byref(item), 0):
            raise ctypes.WinError()

    def delete(self) -> None:
        if os.name == "nt":
            ctypes.windll.advapi32.CredDeleteW(self.target_name, 1, 0)


def default_settings_path() -> Path:
    app_data = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(app_data) / "CodexAutoRecovery" / "settings.json"


def default_credential_backend() -> WindowsCredentialBackend:
    return WindowsCredentialBackend()

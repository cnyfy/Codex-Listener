#!/usr/bin/env python3
"""Monitor local Codex tasks and resume them around transient channel failures."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from codex_task_health_monitor import (  # noqa: E402
    UUID_RE,
    clear_screen,
    collect_summaries,
    discover_session_files,
    parse_timestamp,
    read_session_summary,
    load_session_titles,
    render,
)


BAD_STATUSES = {"failed", "error", "unknown"}
CONTINUE_MESSAGE = "继续"
DEFAULT_LOOP_INTERVAL_SECONDS = 5.0
DEFAULT_INITIAL_DELAY_SECONDS = 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 15.0


def normalize_channel_status(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    return value.strip().lower()


def is_channel_recovered(status: Any) -> bool:
    """Only failed/error/unknown are unavailable; every other status is recovered."""
    return normalize_channel_status(status) not in BAD_STATUSES


def _retry_after_seconds(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        seconds = float(str(value).strip())
        return max(0.0, seconds)
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(str(value))
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 0.0


class ChannelStatus:
    __slots__ = ("status", "retry_after_seconds", "detail")

    def __init__(
        self,
        status: Any,
        retry_after_seconds: float = 0.0,
        detail: str = "",
    ) -> None:
        self.status = normalize_channel_status(status)
        self.retry_after_seconds = max(0.0, float(retry_after_seconds or 0.0))
        self.detail = detail


class ChannelStatusClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 20.0,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("base_url is required")
        if not api_key or not api_key.strip():
            raise ValueError("api_key is required")
        normalized = base_url.strip().rstrip("/")
        if normalized.endswith("/v1/sub2api/channel-status"):
            self.url = normalized
        elif normalized.endswith("/v1/sub2api"):
            self.url = normalized + "/channel-status"
        else:
            self.url = normalized + "/v1/sub2api/channel-status"
        self._api_key = api_key
        self._timeout = max(1.0, float(timeout))
        self._opener = opener

    def get(self) -> ChannelStatus:
        request = Request(
            self.url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw_body = response.read()
                try:
                    payload = json.loads(raw_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return ChannelStatus("unknown", detail="invalid_json")
                if not isinstance(payload, dict):
                    return ChannelStatus("unknown", detail="invalid_payload")
                status = normalize_channel_status(payload.get("status"))
                if status == "unknown":
                    return ChannelStatus("unknown", detail="missing_status")
                return ChannelStatus(status, detail="http_200")
        except HTTPError as exc:
            headers = getattr(exc, "headers", None)
            retry_after = _retry_after_seconds(headers.get("Retry-After") if headers else None)
            return ChannelStatus(
                "unknown",
                retry_after_seconds=retry_after,
                detail=f"http_{getattr(exc, 'code', 'error')}",
            )
        except (OSError, TimeoutError, URLError, ValueError):
            return ChannelStatus("unknown", detail="network_error")


class CodexQueueSender:
    def __init__(
        self,
        command: str | None = None,
        timeout: float = 30.0,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.command = command or self._find_command()
        self.timeout = max(1.0, float(timeout))
        self._runner = runner

    @staticmethod
    def _find_command() -> str:
        configured = os.environ.get("CODEX_CLI")
        if configured:
            return configured
        for name in ("codex", "codex.exe"):
            found = shutil.which(name)
            if found:
                return found
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates = list((Path(local_app_data) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"))
            candidates.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
            if candidates:
                return str(candidates[0])
        return "codex"

    def send(self, thread_id: str, message: str) -> bool:
        command = [self.command, "queue", "--thread", thread_id, "--message", message]
        try:
            completed = self._runner(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0


def load_state(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"tickets": {}, "resolved": {}}
    if not isinstance(state, dict):
        return {"tickets": {}, "resolved": {}}
    tickets = state.get("tickets")
    resolved = state.get("resolved")
    return {
        "tickets": dict(tickets) if isinstance(tickets, dict) else {},
        "resolved": dict(resolved) if isinstance(resolved, dict) else {},
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class RecoveryManager:
    """State machine for one immediate retry plus one recovery retry per failure."""

    def __init__(
        self,
        status_reader: Callable[[], ChannelStatus],
        sender: Callable[[str, str], bool],
        initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        state: dict[str, Any] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        state = state or {"tickets": {}, "resolved": {}}
        self.status_reader = status_reader
        self.sender = sender
        self.initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        self.poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self.tickets: dict[str, dict[str, Any]] = dict(state.get("tickets") or {})
        self.resolved: dict[str, str] = dict(state.get("resolved") or {})
        self.logger = logger or (lambda message: None)
        self.last_channel = ChannelStatus("unknown", detail="尚未检查")

    def export_state(self) -> dict[str, Any]:
        return {
            "tickets": self.tickets,
            "resolved": self.resolved,
        }

    @staticmethod
    def _next_check(ticket: dict[str, Any]) -> datetime | None:
        value = ticket.get("next_check_at")
        if not isinstance(value, str):
            return None
        return parse_timestamp(value)

    def _record_resolved(self, thread_id: str, failure_key: str) -> None:
        if failure_key:
            self.resolved[thread_id] = failure_key
        if len(self.resolved) > 256:
            oldest_ids = list(self.resolved)[:-256]
            for old_id in oldest_ids:
                self.resolved.pop(old_id, None)

    def _send_continue(self, thread_id: str, task_name: str, reason: str) -> None:
        exception_name = ""
        try:
            sent = bool(self.sender(thread_id, CONTINUE_MESSAGE))
        except Exception as exc:  # Keep one task's CLI failure from stopping monitoring.
            sent = False
            exception_name = exc.__class__.__name__
        if sent:
            self.logger(f"{task_name} {reason}已发送继续")
        elif exception_name:
            self.logger(f"{task_name} {reason}发送失败: {exception_name}")
        else:
            self.logger(f"{task_name} {reason}发送失败")

    def _coerce_status(self, result: Any) -> ChannelStatus:
        if isinstance(result, ChannelStatus):
            return result
        if isinstance(result, str):
            return ChannelStatus(result)
        status = getattr(result, "status", None)
        retry_after = getattr(result, "retry_after_seconds", 0.0)
        detail = getattr(result, "detail", "")
        return ChannelStatus(status, retry_after, detail)

    def tick(self, summaries: Iterable[dict[str, Any]], now: datetime) -> None:
        now = _as_utc(now)
        summary_list = list(summaries)
        observed_ids = {
            str(summary.get("session_id") or "")
            for summary in summary_list
            if summary.get("session_id")
        }

        for summary in summary_list:
            thread_id = str(summary.get("session_id") or "")
            if not thread_id:
                continue
            task_name = str(summary.get("task_name") or thread_id)
            ticket = self.tickets.get(thread_id)
            failure_key = str(summary.get("failure_key") or "")
            status = str(summary.get("status") or "")
            user_aborted = bool(summary.get("user_aborted")) or status == "用户已停止"

            if user_aborted or status == "已完成":
                if ticket is not None:
                    self._record_resolved(thread_id, str(ticket.get("failure_key") or ""))
                    self.tickets.pop(thread_id, None)
                continue

            if status != "错误停止" or not failure_key:
                continue
            if self.resolved.get(thread_id) == failure_key:
                continue
            if ticket is not None and ticket.get("failure_key") == failure_key:
                continue

            if ticket is not None:
                self._record_resolved(thread_id, str(ticket.get("failure_key") or ""))
            self.tickets[thread_id] = {
                "failure_key": failure_key,
                "task_name": task_name,
                "phase": "waiting_first_check",
                "detected_at": now.isoformat(),
                "next_check_at": (now + timedelta(seconds=self.initial_delay_seconds)).isoformat(),
            }
            self._send_continue(thread_id, task_name, "故障后的立即")

        due = [
            (thread_id, ticket)
            for thread_id, ticket in self.tickets.items()
            if thread_id in observed_ids
            if (next_check := self._next_check(ticket)) is not None and next_check <= now
        ]
        if not due:
            return

        try:
            channel = self._coerce_status(self.status_reader())
        except Exception as exc:  # An unavailable monitor must be treated as unknown.
            channel = ChannelStatus("unknown", detail=f"reader_{exc.__class__.__name__}")
        self.last_channel = channel
        detail = f" ({channel.detail})" if channel.detail else ""
        self.logger(f"渠道状态: {channel.status}{detail}")

        if is_channel_recovered(channel.status):
            for thread_id, ticket in due:
                self._send_continue(
                    thread_id,
                    str(ticket.get("task_name") or thread_id),
                    "渠道恢复后的",
                )
                self._record_resolved(thread_id, str(ticket.get("failure_key") or ""))
                self.tickets.pop(thread_id, None)
            return

        delay = max(self.poll_interval_seconds, channel.retry_after_seconds)
        for thread_id, ticket in due:
            ticket["phase"] = "polling"
            ticket["next_check_at"] = (now + timedelta(seconds=delay)).isoformat()


def _session_ids_in_name(path: Path) -> set[str]:
    return set(UUID_RE.findall(path.name))


def find_session_paths(session_root: Path, session_ids: Iterable[str]) -> dict[str, Path]:
    wanted = {str(value).lower() for value in session_ids if value}
    found: dict[str, Path] = {}
    if not wanted:
        return found
    try:
        paths = session_root.rglob("*.jsonl")
        for path in paths:
            ids = {value.lower() for value in _session_ids_in_name(path)}
            for session_id in wanted.intersection(ids):
                previous = found.get(session_id)
                if previous is None or path.stat().st_mtime > previous.stat().st_mtime:
                    found[session_id] = path
    except OSError:
        pass
    return found


def should_monitor_summary(summary: dict[str, Any], tracked_ids: set[str]) -> bool:
    if summary.get("status") in {"已完成", "用户已停止"} or summary.get("user_aborted"):
        return False
    return bool(
        summary.get("has_lock")
        or summary.get("session_id") in tracked_ids
        or summary.get("status") in {"运行中", "错误停止"}
    )


class AutoRecoveryApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.messages: list[str] = []
        state = load_state(args.state_file) if not args.dry_run else {"tickets": {}, "resolved": {}}

        base_url = args.base_url or os.environ.get("SUB2API_BASE_URL", "")
        api_key = os.environ.get(args.api_key_env, "")
        if base_url and api_key:
            client = ChannelStatusClient(base_url, api_key, timeout=args.api_timeout)
            status_reader: Callable[[], ChannelStatus] = client.get
        elif args.dry_run:
            status_reader = lambda: ChannelStatus("unknown", detail="missing_api_config")
        else:
            raise ValueError(
                f"需要设置 SUB2API_BASE_URL 或 --base-url，以及环境变量 {args.api_key_env}"
            )

        if args.dry_run:
            def sender(thread_id: str, message: str) -> bool:
                self._log(f"[dry-run] 将向 {thread_id} 发送 {message}")
                return True
        else:
            queue_sender = CodexQueueSender(args.codex_cli, timeout=args.codex_timeout)
            sender = queue_sender.send

        self.manager = RecoveryManager(
            status_reader=status_reader,
            sender=sender,
            initial_delay_seconds=args.initial_delay,
            poll_interval_seconds=args.poll_interval,
            state=state,
            logger=self._log,
        )
        self.explicit_paths = find_session_paths(args.session_root, args.task_ids)

    def _log(self, message: str) -> None:
        self.messages.append(message)

    def _read_explicit_summaries(self, now: datetime) -> list[dict[str, Any]]:
        _, lock_ids = discover_session_files(
            self.args.session_root,
            self.args.lock_dir,
            now=now,
            lookback_hours=self.args.lookback_hours,
        )
        summaries = []
        session_titles = load_session_titles(self.args.session_root.parent / "session_index.jsonl")
        for task_id in self.args.task_ids:
            path = self.explicit_paths.get(task_id.lower())
            if path is None:
                self._log(f"未找到任务日志: {task_id}")
                continue
            summaries.append(
                read_session_summary(
                    path,
                    lock_ids=lock_ids,
                    now=now,
                    stale_after_seconds=self.args.stale_seconds,
                    session_title=session_titles.get(task_id.lower()),
                )
            )
        return summaries

    def _read_default_summaries(self, now: datetime) -> list[dict[str, Any]]:
        paths, lock_ids = discover_session_files(
            self.args.session_root,
            self.args.lock_dir,
            now=now,
            lookback_hours=self.args.lookback_hours,
        )
        summaries = []
        session_titles = load_session_titles(self.args.session_root.parent / "session_index.jsonl")
        tracked_ids = set(self.manager.tickets)
        for path in paths:
            session_match = UUID_RE.search(path.name)
            summary = read_session_summary(
                path,
                lock_ids=lock_ids,
                now=now,
                stale_after_seconds=self.args.stale_seconds,
                session_title=session_titles.get(
                    session_match.group(1).lower() if session_match else ""
                ),
            )
            if should_monitor_summary(summary, tracked_ids):
                summaries.append(summary)
        summaries.sort(key=lambda item: item.get("age_seconds") or 10**12)
        return summaries[: max(1, self.args.max_tasks)]

    def collect(self, now: datetime) -> list[dict[str, Any]]:
        if self.args.task_ids:
            return self._read_explicit_summaries(now)
        return self._read_default_summaries(now)

    def run_once(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = _as_utc(now or datetime.now(timezone.utc))
        self.messages = []
        summaries = self.collect(now)
        self.manager.tick(summaries, now)
        if not self.args.dry_run:
            try:
                save_state(self.args.state_file, self.manager.export_state())
            except OSError as exc:
                self._log(f"状态文件保存失败: {exc.__class__.__name__}")
        return summaries

    def render(self, summaries: list[dict[str, Any]], now: datetime) -> str:
        output = render(summaries, now, self.args.session_root)
        output = output.replace("Codex 任务健康监控（只读）", "Codex 自动恢复监控")
        output += f"\n\n待恢复故障: {len(self.manager.tickets)}"
        if self.args.dry_run:
            output += "    模式: dry-run（不会发送继续）"
        else:
            output += "    模式: live"
        return output


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def acquire_pid_file(path: Path, stop_file: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing_pid = int(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            existing_pid = 0
        if _pid_is_alive(existing_pid):
            raise RuntimeError(f"自动恢复脚本已在运行，PID={existing_pid}")
        try:
            path.unlink()
        except OSError:
            pass
    try:
        stop_file.unlink()
    except FileNotFoundError:
        pass
    path.write_text(str(os.getpid()), encoding="ascii")


def release_pid_file(path: Path, stop_file: Path) -> None:
    try:
        if path.read_text(encoding="ascii").strip() == str(os.getpid()):
            path.unlink()
    except (OSError, ValueError):
        pass
    try:
        stop_file.unlink()
    except FileNotFoundError:
        pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser = argparse.ArgumentParser(
        description="Monitor local Codex task failures and resume after channel recovery."
    )
    parser.add_argument("--base-url", default="", help="中转站 BASE_URL；也可使用 SUB2API_BASE_URL")
    parser.add_argument(
        "--api-key-env",
        default="SUB2API_API_KEY",
        help="读取 API Key 的环境变量名，不接受命令行中的 Key",
    )
    parser.add_argument("--task", dest="task_ids", action="append", default=[], help="指定线程 ID，可重复")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要执行的动作，不发送继续")
    parser.add_argument("--once", action="store_true", help="只检查一次后退出")
    parser.add_argument("--no-clear", action="store_true", help="不清屏，适合重定向日志")
    parser.add_argument("--loop-interval", type=float, default=DEFAULT_LOOP_INTERVAL_SECONDS)
    parser.add_argument("--initial-delay", type=float, default=DEFAULT_INITIAL_DELAY_SECONDS)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--stale-seconds", type=int, default=120)
    parser.add_argument("--lookback-hours", type=float, default=6.0)
    parser.add_argument("--max-tasks", type=int, default=20)
    parser.add_argument("--api-timeout", type=float, default=20.0)
    parser.add_argument("--codex-timeout", type=float, default=30.0)
    parser.add_argument("--codex-cli", default=os.environ.get("CODEX_CLI", ""))
    parser.add_argument("--session-root", type=Path, default=codex_home / "sessions")
    parser.add_argument("--lock-dir", type=Path, default=codex_home / "thread-writer-locks")
    parser.add_argument(
        "--state-file",
        type=Path,
        default=codex_home / "codex_auto_recovery_state.json",
    )
    parser.add_argument(
        "--pid-file",
        type=Path,
        default=SCRIPT_DIR / "codex_auto_recovery.pid",
    )
    parser.add_argument(
        "--stop-file",
        type=Path,
        default=SCRIPT_DIR / "codex_auto_recovery.stop",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pid_acquired = False
    try:
        app = AutoRecoveryApp(args)
        if not args.once:
            acquire_pid_file(args.pid_file, args.stop_file)
            pid_acquired = True
        while True:
            if args.stop_file.exists():
                print("收到停止请求，自动恢复监控已停止。", flush=True)
                return 0
            now = datetime.now(timezone.utc)
            summaries = app.run_once(now)
            if not args.no_clear:
                clear_screen()
            print(app.render(summaries, now), flush=True)
            for message in app.messages:
                print(f"动作: {message}", flush=True)
            if args.once:
                return 0
            time.sleep(max(0.5, args.loop_interval))
    except KeyboardInterrupt:
        print("\n自动恢复监控已停止。", flush=True)
        return 0
    except (ValueError, RuntimeError) as exc:
        print(f"启动失败: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        if pid_acquired:
            release_pid_file(args.pid_file, args.stop_file)


if __name__ == "__main__":
    sys.exit(main())

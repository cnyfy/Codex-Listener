#!/usr/bin/env python3
"""Read-only health monitor for active local Codex sessions."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


UUID_RE = re.compile(
    r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
SECRET_PATTERNS = (
    (re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b"), "sk-***"),
    (re.compile(r"(?i)(bearer\s+)[^\s]+"), r"\1***"),
    (re.compile(r"(?i)(x-api-key\s*[:=]\s*)[^\s]+"), r"\1***"),
    (re.compile(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s]+"), r"\1***"),
)
MAX_TAIL_BYTES = 4 * 1024 * 1024
USER_ABORT_MARKERS = {
    "turn_aborted",
    "task_aborted",
    "user_cancelled",
    "user_canceled",
    "turn_cancelled",
    "turn_canceled",
    "cancelled",
    "canceled",
    "cancel",
}


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y/%m/%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def redact_text(value: str) -> str:
    result = value
    for pattern, replacement in SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def extract_user_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def task_name_from_text(text: str, max_chars: int = 48) -> str:
    first_line = ""
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if candidate.startswith(
            (
                "<in-app-browser-context",
                "<computer-use-task-summary",
                "This block is automatically supplied",
                "The user interrupted the previous turn",
                "# In app browser:",
                "# In-app browser:",
                "- The user has the in-app browser open",
                "- Current URL:",
                "# My request:",
                "## My request:",
            )
        ):
            continue
        if re.fullmatch(r"</?[A-Za-z0-9_:-]+(?:\s+[^>]*)?/?>", candidate):
            continue
        first_line = candidate
        break
    if not first_line:
        return ""
    first_line = redact_text(re.sub(r"\s+", " ", first_line))
    if len(first_line) <= max_chars:
        return first_line
    return first_line[: max_chars - 1] + "…"


def _event_details(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    item = payload.get("item")
    if not isinstance(item, dict):
        item = {}
    return {
        "top_type": str(event.get("type") or ""),
        "payload_type": str(payload.get("type") or ""),
        "item_type": str(item.get("type") or ""),
        "item_status": str(item.get("status") or payload.get("status") or ""),
        "role": str(payload.get("role") or ""),
    }


def _is_error_event(details: dict[str, str]) -> bool:
    values = " ".join(
        details.get(key, "")
        for key in ("payload_type", "item_type", "item_status")
    ).lower()
    return any(marker in values for marker in ("error", "failed", "failure"))


def _is_user_abort_event(event: dict[str, Any], details: dict[str, str]) -> bool:
    values = [
        details.get("top_type", ""),
        details.get("payload_type", ""),
        details.get("item_type", ""),
        details.get("item_status", ""),
    ]
    for value in values:
        normalized = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
        if normalized in USER_ABORT_MARKERS:
            return True
        if normalized.endswith("_aborted") or normalized.endswith("_cancelled") or normalized.endswith("_canceled"):
            return True

    payload = event.get("payload")
    if isinstance(payload, dict):
        for key in ("reason", "stop_reason", "cancel_reason"):
            value = payload.get(key)
            if isinstance(value, str):
                normalized = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
                if normalized in USER_ABORT_MARKERS or normalized.endswith("_aborted"):
                    return True
        if payload.get("role") == "user":
            text = extract_user_text(payload).strip().lower()
            if text in {"<turn_aborted>", "<task_aborted>", "<cancelled>", "<canceled>"}:
                return True
    return False


def _is_completion_event(details: dict[str, str]) -> bool:
    return details.get("payload_type", "").lower() in {
        "task_complete",
        "turn_complete",
    }


def _is_turn_start(details: dict[str, str]) -> bool:
    if details.get("payload_type", "").lower() == "task_started":
        return True
    return details.get("item_type", "").lower() == "usermessage"


def _sampled_log_lines(path: Path) -> list[bytes]:
    """Read session metadata and a bounded tail, not the full conversation history."""
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        first_line = handle.readline().rstrip(b"\r\n")
        if size <= MAX_TAIL_BYTES:
            handle.seek(0)
            return handle.read().splitlines()
        handle.seek(max(0, size - MAX_TAIL_BYTES))
        handle.readline()
        tail_lines = handle.read().splitlines()
    return [first_line, *tail_lines]


def _format_reason(
    details: dict[str, str],
    error_seen: bool,
    activity_after_error: bool = False,
) -> str:
    if error_seen:
        if activity_after_error:
            return "检测到步骤失败，但失败后仍有后续活动"
        return "检测到失败事件"
    payload_type = details.get("payload_type") or details.get("top_type") or "未知事件"
    item_type = details.get("item_type")
    if item_type:
        return f"最近事件: {payload_type}/{item_type}"
    return f"最近事件: {payload_type}"


def _empty_summary(path: Path) -> dict[str, Any]:
    return {
        "session_id": "",
        "task_name": "未命名任务",
        "cwd": "",
        "status": "未知",
        "health": "未知",
        "reason": "没有可解析事件",
        "last_activity": None,
        "age_seconds": None,
        "stale": True,
        "has_lock": False,
        "user_aborted": False,
        "failure_key": "",
        "file": str(path),
    }


def load_session_titles(index_path: Path) -> dict[str, str]:
    """Load the same session titles shown in Codex's left sidebar."""
    titles: dict[str, str] = {}
    try:
        with index_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(entry, dict):
                    continue
                session_id = str(entry.get("id") or entry.get("session_id") or "").strip()
                title = entry.get("thread_name")
                if session_id and isinstance(title, str) and title.strip():
                    titles[session_id.lower()] = title.strip()
    except (OSError, UnicodeError):
        pass
    return titles


def read_session_summary(
    path: Path,
    lock_ids: set[str],
    now: datetime | None = None,
    stale_after_seconds: int = 120,
    session_title: str | None = None,
) -> dict[str, Any]:
    """Parse one JSONL session without exposing message bodies."""
    now = now or datetime.now(timezone.utc)
    result = _empty_summary(path)
    last_user_text = ""
    last_activity: datetime | None = None
    latest_details: dict[str, str] = {}
    turn_start_index = -1
    latest_error_index = -1
    latest_error_timestamp = ""
    latest_error_details: dict[str, str] = {}
    latest_abort_index = -1
    latest_abort_timestamp = ""
    latest_completion_index = -1
    latest_event_index = -1
    cwd = ""
    session_id = ""

    try:
        raw_lines = _sampled_log_lines(path)
    except OSError as exc:
        result["reason"] = f"无法读取日志: {exc.__class__.__name__}"
        return result

    for index, raw_line in enumerate(raw_lines):
        try:
            event = json.loads(raw_line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue

        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if event.get("type") == "session_meta":
            session_id = str(payload.get("session_id") or session_id)
            cwd = str(payload.get("cwd") or cwd)

        timestamp = parse_timestamp(event.get("timestamp"))
        if timestamp is not None:
            last_activity = timestamp

        details = _event_details(event)
        if details["top_type"] == "response_item" and details["role"] == "user":
            text = extract_user_text(payload)
            if text:
                last_user_text = text
                turn_start_index = index

        if _is_turn_start(details):
            turn_start_index = index
        if _is_user_abort_event(event, details):
            latest_abort_index = index
            latest_abort_timestamp = str(event.get("timestamp") or "")
        if _is_error_event(details):
            latest_error_index = index
            latest_error_timestamp = str(event.get("timestamp") or "")
            latest_error_details = details
        if _is_completion_event(details):
            latest_completion_index = index
        latest_details = details
        latest_event_index = index

    if not session_id:
        match = UUID_RE.search(path.name)
        session_id = match.group(1) if match else path.stem

    if last_activity is None:
        try:
            last_activity = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            last_activity = None

    if last_activity is None:
        age_seconds = None
        stale = True
    else:
        age_seconds = max(0.0, (now - last_activity).total_seconds())
        stale = age_seconds > stale_after_seconds

    has_lock = session_id in lock_ids
    error_after_start = latest_error_index > turn_start_index
    completion_after_error = latest_completion_index > latest_error_index
    activity_after_error = latest_event_index > latest_error_index
    completion_after_abort = latest_completion_index > latest_abort_index
    user_aborted = (
        latest_abort_index > turn_start_index
        and latest_abort_index > latest_error_index
        and not completion_after_abort
    )
    completed_current_turn = (
        latest_completion_index >= turn_start_index
        and latest_completion_index >= latest_error_index
        and latest_completion_index >= latest_abort_index
    )
    current_error = error_after_start and not completion_after_error and not user_aborted
    if completed_current_turn:
        status = "已完成"
        health = "正常"
    elif user_aborted:
        status = "用户已停止"
        health = "已停止"
    elif current_error and stale and not activity_after_error:
        status = "错误停止"
        health = "异常"
    elif stale:
        status = "等待/无响应"
        health = "注意"
    else:
        status = "运行中"
        health = "注意" if current_error else "正常"

    task_name = session_title.strip() if isinstance(session_title, str) and session_title.strip() else ""
    if not task_name:
        task_name = task_name_from_text(last_user_text)
    if not task_name and cwd:
        task_name = Path(cwd.rstrip("\\/")).name or cwd
    if not task_name:
        task_name = "未命名任务"

    failure_key = ""
    if current_error:
        error_identity = latest_error_timestamp or str(latest_error_index)
        failure_key = ":".join(
            [
                session_id,
                error_identity,
                latest_error_details.get("payload_type", ""),
                latest_error_details.get("item_type", ""),
                latest_error_details.get("item_status", ""),
            ]
        )

    if user_aborted:
        reason = "用户主动中止任务"
    else:
        reason = _format_reason(latest_details, current_error, activity_after_error)

    result.update(
        {
            "session_id": session_id,
            "task_name": task_name,
            "cwd": cwd,
            "status": status,
            "health": health,
            "reason": reason,
            "last_activity": last_activity,
            "age_seconds": age_seconds,
            "stale": stale,
            "has_lock": has_lock,
            "user_aborted": user_aborted,
            "failure_key": failure_key,
            "file": str(path),
        }
    )
    return result


def _session_id_from_name(path: Path) -> str | None:
    match = UUID_RE.search(path.name)
    return match.group(1) if match else None


def discover_session_files(
    session_root: Path,
    lock_dir: Path,
    now: datetime | None = None,
    lookback_hours: float = 6,
) -> tuple[list[Path], set[str]]:
    now = now or datetime.now(timezone.utc)
    try:
        lock_ids = {
            path.stem
            for path in lock_dir.glob("*.lock")
            if path.stem != ".coordination" and UUID_RE.fullmatch(path.stem)
        }
    except OSError:
        lock_ids = set()

    cutoff = now - timedelta(hours=max(0.0, lookback_hours))
    newest_by_session: dict[str, tuple[float, Path]] = {}
    try:
        paths: Iterable[Path] = session_root.rglob("*.jsonl")
        for path in paths:
            session_id = _session_id_from_name(path)
            if not session_id:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            is_recent = datetime.fromtimestamp(mtime, tz=timezone.utc) >= cutoff
            if session_id not in lock_ids and not is_recent:
                continue
            previous = newest_by_session.get(session_id)
            if previous is None or mtime > previous[0]:
                newest_by_session[session_id] = (mtime, path)
    except OSError:
        pass

    paths = [item[1] for item in newest_by_session.values()]
    paths.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    return paths, lock_ids


def collect_summaries(
    session_root: Path,
    lock_dir: Path,
    now: datetime | None = None,
    lookback_hours: float = 6,
    stale_after_seconds: int = 120,
    max_tasks: int = 20,
) -> list[dict[str, Any]]:
    paths, lock_ids = discover_session_files(session_root, lock_dir, now, lookback_hours)
    session_titles = load_session_titles(session_root.parent / "session_index.jsonl")
    summaries = [
        read_session_summary(
            path,
            lock_ids=lock_ids,
            now=now,
            stale_after_seconds=stale_after_seconds,
            session_title=session_titles.get((_session_id_from_name(path) or "").lower()),
        )
        for path in paths
    ]
    summaries.sort(
        key=lambda item: (
            item["status"] == "已完成",
            -(item["age_seconds"] if item["age_seconds"] is not None else 10**12),
        )
    )
    return summaries[: max(1, max_tasks)]


def display_width(value: str) -> int:
    width = 0
    for char in value:
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def fit(value: str, width: int) -> str:
    result = ""
    current = 0
    for char in value:
        char_width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if current + char_width > width:
            break
        result += char
        current += char_width
    return result + " " * max(0, width - current)


def format_age(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "未知"
    seconds = int(age_seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def render(summaries: list[dict[str, Any]], now: datetime, session_root: Path) -> str:
    lines = [
        "Codex 任务健康监控（只读）",
        f"更新时间: {now.astimezone().strftime('%Y-%m-%d %H:%M:%S')}    会话目录: {session_root}",
        "按 Ctrl+C 停止；状态依据最近事件和最后活动时间综合判断。",
        "锁文件仅作线程关联参考，不代表任务仍在运行。",
        "",
    ]
    headers = ["任务名", "状态", "健康", "最后活动", "距今", "锁文件", "线程 ID"]
    widths = [38, 12, 8, 19, 8, 8, 36]
    lines.append(" ".join(fit(header, width) for header, width in zip(headers, widths)))
    lines.append("-" * (sum(widths) + len(widths) - 1))
    if not summaries:
        lines.append("未找到最近会话或活跃任务锁。")
    for summary in summaries:
        last_activity = summary["last_activity"]
        last_text = last_activity.astimezone().strftime("%Y-%m-%d %H:%M:%S") if last_activity else "未知"
        lines.append(
            " ".join(
                [
                    fit(summary["task_name"], widths[0]),
                    fit(summary["status"], widths[1]),
                    fit(summary["health"], widths[2]),
                    fit(last_text, widths[3]),
                    fit(format_age(summary["age_seconds"]), widths[4]),
                    fit("是" if summary["has_lock"] else "否", widths[5]),
                    fit(summary["session_id"], widths[6]),
                ]
            )
        )
        lines.append(f"  判断: {summary['reason']}")
    return "\n".join(lines)


def clear_screen() -> None:
    if os.name == "nt":
        os.system("cls")
    else:
        print("\033[2J\033[H", end="")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser = argparse.ArgumentParser(description="Monitor local Codex session health without modifying sessions.")
    parser.add_argument("--interval", type=float, default=5.0, help="刷新间隔秒数")
    parser.add_argument("--lookback-hours", type=float, default=6.0, help="无活跃锁时纳入的最近会话时长")
    parser.add_argument("--stale-seconds", type=int, default=120, help="超过此时间没有事件则视为无响应")
    parser.add_argument("--max-tasks", type=int, default=20, help="最多显示的会话数")
    parser.add_argument("--once", action="store_true", help="只打印一次后退出")
    parser.add_argument("--session-root", type=Path, default=codex_home / "sessions")
    parser.add_argument("--lock-dir", type=Path, default=codex_home / "thread-writer-locks")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        while True:
            now = datetime.now(timezone.utc)
            summaries = collect_summaries(
                args.session_root,
                args.lock_dir,
                now=now,
                lookback_hours=args.lookback_hours,
                stale_after_seconds=args.stale_seconds,
                max_tasks=args.max_tasks,
            )
            clear_screen()
            print(render(summaries, now, args.session_root), flush=True)
            if args.once:
                return 0
            time.sleep(max(0.5, args.interval))
    except KeyboardInterrupt:
        print("\n监控已停止。")
        return 0


if __name__ == "__main__":
    sys.exit(main())

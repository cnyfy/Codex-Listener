#!/usr/bin/env python3
"""Textual terminal UI for Codex auto recovery."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
VENDOR_DIR = SCRIPT_DIR / "vendor"
if VENDOR_DIR.is_dir() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from codex_auto_recovery import (  # noqa: E402
    AutoRecoveryApp,
    ChannelStatus,
    RecoveryManager,
    _as_utc,
    collect_summaries,
    parse_args as recovery_parse_args,
)
from codex_auto_recovery_settings import (  # noqa: E402
    AppSettings,
    DEFAULT_BASE_URL,
    default_credential_backend,
    default_settings_path,
    load_settings,
    redact_secret,
    save_settings,
)


def missing_configuration(settings: AppSettings, api_key: str | None) -> list[str]:
    missing: list[str] = []
    if not settings.base_url.strip():
        missing.append("中转站地址")
    if not api_key or not api_key.strip():
        missing.append("API Key")
    return missing


def filter_selected_summaries(
    summaries: list[dict[str, Any]], selected_tasks: set[str]
) -> list[dict[str, Any]]:
    eligible = [
        summary
        for summary in summaries
        if summary.get("status") in {"运行中", "错误停止"}
        and not summary.get("user_aborted")
    ]
    if not selected_tasks:
        return eligible
    return [summary for summary in eligible if summary.get("session_id") in selected_tasks]


def build_recovery_namespace(settings: AppSettings, codex_home: Path) -> argparse.Namespace:
    arguments = [
            "--base-url",
            settings.base_url,
            "--initial-delay",
            str(settings.initial_delay),
            "--poll-interval",
            str(settings.poll_interval),
            "--loop-interval",
            str(settings.loop_interval),
            "--stale-seconds",
            str(settings.stale_seconds),
            "--lookback-hours",
            str(settings.lookback_hours),
            "--max-tasks",
            str(settings.max_tasks),
            "--api-timeout",
            str(settings.api_timeout),
            "--codex-timeout",
            str(settings.codex_timeout),
            "--codex-cli",
            settings.codex_cli,
            "--session-root",
            str(codex_home / "sessions"),
            "--lock-dir",
            str(codex_home / "thread-writer-locks"),
            "--state-file",
            str(codex_home / "codex_auto_recovery_state.json"),
        ]
    if settings.dry_run:
        arguments.append("--dry-run")
    return recovery_parse_args(arguments)


try:
    from textual.app import App, ComposeResult
    from textual.containers import Container, Horizontal, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Log, Static
    TEXTUAL_AVAILABLE = True
except ImportError:
    TEXTUAL_AVAILABLE = False


if TEXTUAL_AVAILABLE:

    class ConfirmScreen(ModalScreen[bool]):
        CSS = """
        ConfirmScreen { align: center middle; }
        #dialog { width: 62; height: 12; padding: 1 2; border: thick $accent; background: $surface; }
        #buttons { height: 3; align: center middle; }
        Button { margin: 0 1; }
        """

        def __init__(self, question: str) -> None:
            super().__init__()
            self.question = question

        def compose(self) -> ComposeResult:
            with Container(id="dialog"):
                yield Label(self.question)
                with Horizontal(id="buttons"):
                    yield Button("确认", id="yes", variant="error")
                    yield Button("取消", id="no")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.dismiss(event.button.id == "yes")


    class SettingsScreen(ModalScreen[AppSettings | None]):
        CSS = """
        SettingsScreen { align: center middle; }
        #settings {
            width: 92%;
            max-width: 90;
            height: 90%;
            max-height: 32;
            min-height: 14;
            padding: 1 2;
            border: thick $accent;
            background: $surface;
        }
        #settings-body {
            width: 1fr;
            height: 1fr;
            overflow-y: auto;
            overflow-x: hidden;
        }
        .field { height: 3; margin: 0 0 1 0; }
        #buttons { width: 1fr; height: 3; align: right middle; }
        Button { margin-left: 1; }
        """
        BINDINGS = [
            ("escape", "cancel_settings", "取消"),
            ("ctrl+s", "save_settings", "保存"),
            ("up", "scroll_settings_up", "向上滚动"),
            ("down", "scroll_settings_down", "向下滚动"),
            ("pageup", "page_settings_up", "上翻页"),
            ("pagedown", "page_settings_down", "下翻页"),
        ]

        def __init__(self, settings: AppSettings, has_key: bool, required_fields: list[str] | None = None) -> None:
            super().__init__()
            self.settings = settings
            self.has_key = has_key
            self.required_fields = required_fields or []

        def compose(self) -> ComposeResult:
            with Container(id="settings"):
                if self.required_fields:
                    yield Label("首次启动需要补充：" + "、".join(self.required_fields))
                yield Label("自动恢复设置（常用项在前，保存后立即生效）")
                with VerticalScroll(id="settings-body"):
                    yield Label("中转站地址（例如 https://example.com）")
                    yield Input(value=self.settings.base_url, placeholder=DEFAULT_BASE_URL, id="base_url", classes="field")
                    yield Label("API Key（留空表示不修改；只保存到 Windows 凭据管理器）")
                    yield Input(placeholder="已保存" if self.has_key else "尚未设置", password=True, id="api_key", classes="field")
                    yield Label("任务报错后，多久开始检查渠道（秒）")
                    yield Input(value=str(self.settings.initial_delay), id="initial_delay", classes="field")
                    yield Label("渠道异常时，每隔多久再检查（秒）")
                    yield Input(value=str(self.settings.poll_interval), id="poll_interval", classes="field")
                    yield Label("界面刷新间隔（秒）")
                    yield Input(value=str(self.settings.loop_interval), id="loop_interval", classes="field")
                    yield Label("任务扫描范围（最近多少小时）")
                    yield Input(value=str(self.settings.lookback_hours), id="lookback_hours", classes="field")
                with Horizontal(id="buttons"):
                    yield Button("保存并关闭", id="save", variant="success")
                    yield Button("取消", id="cancel")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel":
                self.action_cancel_settings()
                return
            if event.button.id != "save":
                return
            self.action_save_settings()

        def _read_settings(self) -> AppSettings | None:
            try:
                updated = AppSettings(
                    base_url=self.query_one("#base_url", Input).value.strip(),
                    selected_tasks=list(self.settings.selected_tasks),
                    initial_delay=float(self.query_one("#initial_delay", Input).value),
                    poll_interval=float(self.query_one("#poll_interval", Input).value),
                    loop_interval=float(self.query_one("#loop_interval", Input).value),
                    lookback_hours=float(self.query_one("#lookback_hours", Input).value),
                    stale_seconds=self.settings.stale_seconds,
                    max_tasks=self.settings.max_tasks,
                    api_timeout=self.settings.api_timeout,
                    codex_timeout=self.settings.codex_timeout,
                    codex_cli=self.settings.codex_cli,
                    dry_run=self.settings.dry_run,
                )
            except ValueError:
                self.notify("数值设置无效", severity="error")
                return
            entered_key = self.query_one("#api_key", Input).value.strip()
            updated.pending_api_key = entered_key
            return updated

        def action_save_settings(self) -> None:
            updated = self._read_settings()
            if updated is not None:
                self.dismiss(updated)

        def action_cancel_settings(self) -> None:
            self.dismiss(None)

        def _scroll_settings(self, method: str) -> None:
            body = self.query_one("#settings-body", VerticalScroll)
            getattr(body, method)()

        def action_scroll_settings_up(self) -> None:
            self._scroll_settings("scroll_up")

        def action_scroll_settings_down(self) -> None:
            self._scroll_settings("scroll_down")

        def action_page_settings_up(self) -> None:
            self._scroll_settings("scroll_page_up")

        def action_page_settings_down(self) -> None:
            self._scroll_settings("scroll_page_down")


    class RecoveryTui(App[None]):
        TITLE = "Codex 自动恢复控制台"
        SUB_TITLE = "任务健康、渠道状态与自动续跑"
        CSS = """
        Screen { background: $background; }
        #top { height: 3; padding: 0 1; }
        #channel { width: 1fr; border: round $panel; padding: 0 1; }
        #mode { width: 32; border: round $panel; padding: 0 1; }
        #tasks { height: 1fr; border: round $panel; }
        #log { height: 12; border: round $panel; }
        #actions { height: 3; align: center middle; }
        #actions Button { margin: 0 1; }
        DataTable { scrollbar-size: 1 1; }
        """
        BINDINGS = [
            ("s", "settings", "设置"),
            ("r", "refresh", "刷新"),
            ("p", "pause", "暂停/继续"),
            ("d", "dry_run", "Dry-run"),
            ("a", "select_all", "全选"),
            ("n", "select_none", "清空选择"),
            ("q", "quit", "退出"),
        ]

        def __init__(self, settings: AppSettings, credential_backend: Any, settings_path: Path) -> None:
            super().__init__()
            self.settings = settings
            self.credential_backend = credential_backend
            self.settings_path = settings_path
            self.selected_tasks = set(settings.selected_tasks)
            self.summaries: list[dict[str, Any]] = []
            self.paused = False
            self.live_enabled = False
            self.channel = ChannelStatus("unknown", detail="尚未检查")
            self.recovery_app: AutoRecoveryApp | None = None
            self.recovery_namespace = build_recovery_namespace(settings, Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal(id="top"):
                yield Static("渠道状态：unknown", id="channel")
                yield Static("模式：dry-run | 运行中", id="mode")
            yield DataTable(id="tasks", cursor_type="row")
            yield Log(id="log", highlight=False)
            with Horizontal(id="actions"):
                yield Button("设置 [S]", id="settings")
                yield Button("刷新 [R]", id="refresh")
                yield Button("暂停 [P]", id="pause")
                yield Button("启用 Live", id="live", variant="error")
                yield Button("退出 [Q]", id="quit")
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#tasks", DataTable)
            table.add_columns("监测", "任务名", "状态", "健康", "距今", "线程 ID")
            self.set_interval(max(0.5, self.settings.loop_interval), self.refresh_data)
            self.refresh_data()
            self.write_log("TUI 已启动，默认 dry-run；空选择表示监测全部符合条件的任务。")
            missing = missing_configuration(self.settings, self.credential_backend.read())
            if missing:
                self.write_log("配置不完整，已打开设置界面：" + "、".join(missing))
                self.push_screen(
                    SettingsScreen(self.settings, bool(self.credential_backend.read()), missing),
                    self._finish_initial_settings,
                )

        def _finish_initial_settings(self, updated: AppSettings | None) -> None:
            if updated is None:
                self.write_log("尚未完成必要配置；请按 S 打开设置。")
                return
            entered_key = getattr(updated, "pending_api_key", "")
            if entered_key:
                self.credential_backend.write(entered_key)
            self.settings = updated
            self.settings.selected_tasks = sorted(self.selected_tasks)
            save_settings(self.settings_path, self.settings)
            self.recovery_namespace = build_recovery_namespace(
                self.settings,
                Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
            )
            remaining = missing_configuration(self.settings, self.credential_backend.read())
            if remaining:
                self.write_log("仍缺少：" + "、".join(remaining))
                self.push_screen(
                    SettingsScreen(self.settings, bool(self.credential_backend.read()), remaining),
                    self._finish_initial_settings,
                )
            else:
                self.write_log("必要配置已保存。")

        def write_log(self, message: str) -> None:
            self.query_one("#log", Log).write_line(message)

        def refresh_data(self) -> None:
            if self.paused:
                return
            now = datetime.now(timezone.utc)
            try:
                self.summaries = collect_summaries(
                    self.recovery_namespace.session_root,
                    self.recovery_namespace.lock_dir,
                    now=now,
                    lookback_hours=self.settings.lookback_hours,
                    stale_after_seconds=self.settings.stale_seconds,
                    max_tasks=self.settings.max_tasks,
                )
                self.render_tasks()
                self.update_recovery(now)
            except Exception as exc:
                self.write_log(f"刷新失败：{exc.__class__.__name__}")

        def render_tasks(self) -> None:
            table = self.query_one("#tasks", DataTable)
            table.clear(columns=False)
            for summary in self.summaries:
                task_id = summary.get("session_id", "")
                selected = "✓" if not self.selected_tasks or task_id in self.selected_tasks else " "
                age = summary.get("age_seconds")
                age_text = "未知" if age is None else f"{int(age)}s"
                table.add_row(
                    selected,
                    summary.get("task_name", "未命名任务"),
                    summary.get("status", "未知"),
                    summary.get("health", "未知"),
                    age_text,
                    task_id,
                    key=task_id,
                )

        def update_recovery(self, now: datetime) -> None:
            chosen = filter_selected_summaries(self.summaries, self.selected_tasks)
            if self.paused:
                return
            if not self.live_enabled:
                self.query_one("#mode", Static).update("模式：dry-run | 运行中")
                return
            if self.recovery_app is None:
                api_key = self.credential_backend.read()
                if not api_key:
                    self.write_log("未配置 API Key，无法启用 Live。")
                    self.live_enabled = False
                    return
                os.environ["SUB2API_API_KEY"] = api_key
                live_settings = AppSettings.from_dict(self.settings.to_dict())
                live_settings.dry_run = False
                self.recovery_namespace = build_recovery_namespace(
                    live_settings,
                    Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
                )
                self.recovery_app = AutoRecoveryApp(self.recovery_namespace)
            self.recovery_app.manager.tick(chosen, now)
            latest_channel = self.recovery_app.manager.last_channel
            self.query_one("#channel", Static).update(
                f"渠道状态：{latest_channel.status}"
                + (f" ({latest_channel.detail})" if latest_channel.detail else "")
            )
            for message in self.recovery_app.messages:
                self.write_log(redact_secret(message, self.credential_backend.read()))
            self.recovery_app.messages = []
            self.query_one("#mode", Static).update(
                f"模式：live | 运行中 | 待恢复 {len(self.recovery_app.manager.tickets)}"
            )

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            task_id = str(event.row_key.value)
            if task_id in self.selected_tasks:
                self.selected_tasks.remove(task_id)
            else:
                self.selected_tasks.add(task_id)
            self.settings.selected_tasks = sorted(self.selected_tasks)
            self.render_tasks()

        def action_select_all(self) -> None:
            self.selected_tasks = {str(item.get("session_id")) for item in self.summaries if item.get("session_id")}
            self.settings.selected_tasks = sorted(self.selected_tasks)
            self.render_tasks()

        def action_select_none(self) -> None:
            self.selected_tasks.clear()
            self.settings.selected_tasks = []
            self.render_tasks()

        def action_pause(self) -> None:
            self.paused = not self.paused
            self.query_one("#pause", Button).label = "继续 [P]" if self.paused else "暂停 [P]"
            self.write_log("已暂停" if self.paused else "已继续")

        def action_dry_run(self) -> None:
            self.live_enabled = False
            self.query_one("#mode", Static).update("模式：dry-run | 运行中")
            self.write_log("已切换到 dry-run，不会发送继续。")

        def action_settings(self) -> None:
            def finished(updated: AppSettings | None) -> None:
                if updated is None:
                    return
                entered_key = getattr(updated, "pending_api_key", "")
                if entered_key:
                    self.credential_backend.write(entered_key)
                self.settings = updated
                self.settings.selected_tasks = sorted(self.selected_tasks)
                save_settings(self.settings_path, self.settings)
                self.recovery_namespace = build_recovery_namespace(
                    self.settings,
                    Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
                )
                self.write_log("设置已保存。")

            self.push_screen(
                SettingsScreen(self.settings, bool(self.credential_backend.read())),
                finished,
            )

        def action_live(self) -> None:
            if self.live_enabled:
                self.action_dry_run()
                return
            self.push_screen(
                ConfirmScreen("启用 Live 后，检测到恢复会向 Codex 发送“继续”。确认启用？"),
                self._finish_live_confirmation,
            )

        def _finish_live_confirmation(self, confirmed: bool) -> None:
            if confirmed:
                if not self.credential_backend.read():
                    self.write_log("未配置 API Key，请先在设置中保存。")
                    return
                self.live_enabled = True
                self.write_log("Live 已启用。")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            actions = {
                "settings": self.action_settings,
                "refresh": self.refresh_data,
                "pause": self.action_pause,
                "live": self.action_live,
                "quit": self.action_quit,
            }
            action = actions.get(event.button.id)
            if action:
                action()

        def action_refresh(self) -> None:
            self.refresh_data()

        def action_quit(self) -> None:
            self.settings.selected_tasks = sorted(self.selected_tasks)
            save_settings(self.settings_path, self.settings)
            self.exit()


def parse_tui_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex auto recovery Textual TUI")
    parser.add_argument("--settings-file", type=Path, default=default_settings_path())
    parser.add_argument("--dry-run", action="store_true", help="强制 dry-run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_tui_args(argv)
    if not TEXTUAL_AVAILABLE:
        print("未安装 Textual，请先运行：python -m pip install textual", file=sys.stderr)
        return 2
    settings = load_settings(args.settings_file)
    if args.dry_run:
        settings.dry_run = True
    credential_backend = default_credential_backend()
    app = RecoveryTui(settings, credential_backend, args.settings_file)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

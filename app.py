from __future__ import annotations

import datetime as _dt
import json
import subprocess
import time
import math
from dataclasses import dataclass
from typing import Callable, Optional

from rich.text import Text

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widget import Widget
from textual.message import Message
from textual.widgets import (
    Button,
    Collapsible,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)

from .db import StatusbarDB
from .http_client import create_api_key, fetch_server_info, format_server_url
from .models import FinishedTask, LiveTask, Preferences, StateSnapshot, TaskStatus, TodosCache, ViewType
from .net_models import WebsocketOp
from . import os_integration
from .task_utils import (
    apply_operation,
    current_time_millis,
    parse_deadline_input,
    parse_due_command,
    parse_move_command,
    parse_move_to_end_command,
    parse_restore_command,
    parse_reverse_command,
    random_string,
)
from .ws_client import WebsocketClient


DEFAULT_SERVER_URL = "http://localhost:8080/public/"

GRUVBOX_THEME = Theme(
    name="statusbar-gruvbox",
    primary="#458588",
    secondary="#504945",
    accent="#d65d0e",
    foreground="#ebdbb2",
    background="#282828",
    success="#98971a",
    warning="#d79921",
    error="#dc3545",
    surface="#3c3836",
    panel="#504945",
    dark=True,
    variables={
        "text": "#ebdbb2",
        "text-muted": "#a89984",
        "text-disabled": "#7c6f64",
        # Match Bootstrap's contrast choice with the gruvbox palette.
        "button-color-foreground": "#000000",
    },
)

_MONTH_ABBR = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]
_MONTH_FULL = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def _now_unix_seconds() -> float:
    return time.time()


def _same_ymd(a: _dt.datetime, b: _dt.datetime) -> bool:
    return a.year == b.year and a.month == b.month and a.day == b.day


def _format_deadline(deadline: int, *, countdown: bool, now: Optional[_dt.datetime] = None) -> str:
    """
    Port of the TS DeadlineBadge formatting, but implemented in a cross-platform
    way (no %-d / %-I directives).
    """
    if now is None:
        now = _dt.datetime.now()

    date = _dt.datetime.fromtimestamp(deadline)
    if countdown:
        diff_seconds = math.floor(deadline - now.timestamp())
        if diff_seconds < 0:
            return "Overdue"

        days = diff_seconds // (24 * 60 * 60)
        hours = (diff_seconds % (24 * 60 * 60)) // (60 * 60)
        minutes = (diff_seconds % (60 * 60)) // 60
        seconds = diff_seconds % 60

        days_padded = str(days).rjust(2, " ")
        hours_padded = str(hours).rjust(2, " ")
        minutes_padded = str(minutes).rjust(2, " ")
        seconds_padded = str(seconds).rjust(2, " ")

        if days > 0:
            return f"{days_padded}d {hours_padded}h left"
        if hours > 0:
            return f"{hours_padded}h {minutes_padded}m left"
        if minutes > 0:
            return f"{minutes_padded}m {seconds_padded}s left"
        return f"{seconds_padded}s left"

    hour_12 = str(date.hour % 12 or 12)
    minute = f"{date.minute:02d}"
    ampm = "AM" if date.hour < 12 else "PM"

    if _same_ymd(date, now):
        return f"{hour_12}:{minute} {ampm}"

    month = _MONTH_ABBR[date.month - 1]
    day = date.day
    year = date.year
    return f"{month} {day}, {year} {hour_12}:{minute} {ampm}"


def _format_time_label(hour: int, minute: int) -> str:
    hour_12 = hour % 12 or 12
    ampm = "AM" if hour < 12 else "PM"
    return f"{hour_12}:{minute:02d} {ampm}"


def _deadline_variant(deadline: int, *, now: Optional[_dt.datetime] = None) -> str:
    """success / warning / danger, aligned with the TS logic."""
    if now is None:
        now = _dt.datetime.now()
    date = _dt.datetime.fromtimestamp(deadline)
    today_midnight = _dt.datetime(now.year, now.month, now.day)

    if date < now:
        return "danger"
    if _same_ymd(date, today_midnight):
        return "warning"
    return "success"


class DeadlineBadge(Static):
    """
    A simple badge-like widget that displays a deadline.

    - If countdown=True, updates once per second.
    - Colors mimic Bootstrap variants (success/warning/danger) using Rich styles.
    """

    def __init__(self, deadline: Optional[int], *, countdown: bool = False, classes: str = "") -> None:
        super().__init__(classes=classes)
        self.deadline: Optional[int] = deadline
        self.countdown = countdown
        self._timer = None

    def on_mount(self) -> None:
        if self.countdown:
            self._timer = self.set_interval(1.0, self._refresh_badge)
        self._refresh_badge()

    def set_deadline(self, deadline: Optional[int]) -> None:
        self.deadline = deadline
        self._refresh_badge()

    def set_countdown(self, countdown: bool) -> None:
        self.countdown = countdown
        if self._timer:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None
        if self.countdown:
            self._timer = self.set_interval(1.0, self._refresh_badge)
        self._refresh_badge()

    def _refresh_badge(self) -> None:
        if self.deadline is None:
            self.update("")
            return

        label = _format_deadline(int(self.deadline), countdown=self.countdown)
        variant = _deadline_variant(int(self.deadline))

        if variant == "danger":
            style = "bold black on #dc3545"
        elif variant == "warning":
            style = "bold black on #d79921"
        else:
            style = "bold black on #98971a"

        self.update(Text(f" {label} ", style=style))

    def on_click(self, event: events.Click) -> None:
        if self.has_class("collapsed_deadline"):
            try:
                self.app.expand_dock()  # type: ignore[attr-defined]
            except Exception:
                pass


class StatusBadge(Static):
    """Badge for finished task status."""

    def __init__(self, status: TaskStatus, *, classes: str = "") -> None:
        super().__init__(classes=classes)
        self.status = status

    def on_mount(self) -> None:
        self.refresh_badge()

    def refresh_badge(self) -> None:
        status = self.status
        if status == "Succeeded":
            style = "bold black on #98971a"
        elif status == "Failed":
            style = "bold black on #dc3545"
        else:
            style = "bold black on #504945"
        self.update(Text(f" {status.upper()} ", style=style))


class NewTaskInput(Input):
    def on_focus(self, event: events.Focus) -> None:
        try:
            self.app.set_active_task(None)  # type: ignore[attr-defined]
        except Exception:
            pass


class FrequencySlider(Widget):
    """Simple interactive slider for vocal reminder frequency."""

    class Changed(Message):
        def __init__(self, slider: "FrequencySlider", value: int) -> None:
            super().__init__()
            self.slider = slider
            self.value = value

    can_focus = True

    def __init__(self, *, value: int = 5, min_value: int = 1, max_value: int = 60, **kwargs) -> None:
        super().__init__(**kwargs)
        self.value = value
        self.min_value = min_value
        self.max_value = max_value

    def set_value(self, value: int, *, notify: bool = True) -> None:
        value = max(self.min_value, min(self.max_value, int(value)))
        if value == self.value:
            return
        self.value = value
        self.refresh()
        if notify:
            self.post_message(self.Changed(self, self.value))

    def _set_from_ratio(self, ratio: float) -> None:
        span = self.max_value - self.min_value
        value = self.min_value + round(span * ratio)
        self.set_value(value, notify=True)

    def on_click(self, event: events.Click) -> None:
        if self.disabled:
            return
        width = max(4, self.size.width or 10)
        bar_width = max(4, width - 2)
        x = max(0, min(bar_width - 1, event.x - 1))
        ratio = x / float(bar_width - 1)
        self._set_from_ratio(ratio)

    def on_key(self, event: events.Key) -> None:
        if self.disabled:
            return
        if event.key in {"left", "h"}:
            self.set_value(self.value - 1, notify=True)
            event.stop()
        elif event.key in {"right", "l"}:
            self.set_value(self.value + 1, notify=True)
            event.stop()

    def render(self) -> Text:
        width = max(10, self.size.width or 10)
        bar_width = max(4, width - 2)
        span = self.max_value - self.min_value
        ratio = 0.0 if span == 0 else (self.value - self.min_value) / span
        pos = int(round(ratio * (bar_width - 1)))
        bar = ["-"] * bar_width
        bar[pos] = "|"
        text = Text("[" + "".join(bar) + "]")
        if self.disabled:
            text.stylize("dim")
        elif self.has_focus:
            text.stylize("bold")
        return text

class CollapsedBar(Widget):
    """Collapsed (compact) UI, shown when the dock is 'collapsed'."""

    DEFAULT_CSS = """
    CollapsedBar {
        height: auto;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Horizontal(id="collapsed_row")

    def refresh_bar(self) -> None:
        row = self.query_one("#collapsed_row", Horizontal)

        # Remove existing children
        for child in list(row.children):
            child.remove()

        app: "Statusbar2App" = self.app  # type: ignore[assignment]

        if app.state_type == "NotLoggedIn":
            row.mount(Button("Click to Log In", id="btn_expand_login", variant="default", classes="btn_link"))
            return

        if app.state_type == "Restored":
            row.mount(Button("Resume Session", id="btn_resume_session", variant="default", classes="btn_link"))
            return

        if app.state_type == "NotConnected":
            if app.error:
                label = Text(str(app.error), style="#dc3545")
            else:
                label = "Connecting..."
            row.mount(Button(label, id="btn_expand_connecting", variant="default", classes="btn_link"))
            return

        if app.state_type != "Connected":
            row.mount(Button("...", id="btn_expand_unknown", variant="default", classes="btn_link"))
            return

        # Connected state
        if not app.snapshot.live:
            row.mount(Button("Click to Add Task", id="btn_expand_add", variant="default", classes="btn_link"))
            return

        first = app.snapshot.live[0]
        # Action buttons
        row.mount(Button("Succeeded", id=f"btn_succeed_{first.id}", variant="success"))
        row.mount(Button(first.value, id="btn_expand_main", variant="default", classes="collapsed_main"))
        if first.deadline is not None:
            row.mount(DeadlineBadge(first.deadline, countdown=True, classes="collapsed_deadline"))
        row.mount(Button("Failed", id=f"btn_fail_{first.id}", variant="error"))
        row.mount(Button("Obsoleted", id=f"btn_obsolete_{first.id}", variant="default", classes="btn_obsoleted"))


class LoginPanel(Widget):
    """Expanded Login UI."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="login_root"):
            with Vertical(id="login_sidebar"):
                yield Button("Collapse", id="btn_collapse", variant="default", classes="btn_secondary")
            with Vertical(id="login_main"):
                yield Label("Login", id="login_title")
                yield Label("Email")
                yield Input(id="login_email")
                yield Label("Password")
                with Horizontal(classes="login_password_row"):
                    yield Input(password=True, id="login_password")
                    yield Button("[eye]", id="btn_toggle_password", variant="default", classes="btn_outline_secondary")
                with Collapsible(title="Server API URL", collapsed=True, id="login_server_collapse"):
                    yield Input(placeholder=DEFAULT_SERVER_URL, id="login_server_url")
                yield Button("Submit", id="btn_login_submit", variant="primary")
                yield Label("", id="login_error", classes="error_text")

    def show_error(self, message: str) -> None:
        self.query_one("#login_error", Label).update(message)

    def clear_error(self) -> None:
        self.query_one("#login_error", Label).update("")


class RestoredPanel(Widget):
    def compose(self) -> ComposeResult:
        yield Button("Resume Session", id="btn_resume_session_expanded", variant="default", classes="btn_link")


class NotConnectedPanel(Widget):
    def compose(self) -> ComposeResult:
        with Vertical(id="notconnected_root"):
            yield Button("Connecting...", id="btn_expand_connecting", variant="default", classes="btn_link")
            with Horizontal(id="notconnected_actions", classes="hidden"):
                yield Button("Retry", id="btn_retry", variant="default", classes="btn_secondary")
                yield Button("Return to Login", id="btn_return_login", variant="default", classes="btn_secondary")
            yield Label("", id="notconnected_error", classes="error_text")

    def set_error(self, message: str | None) -> None:
        actions = self.query_one("#notconnected_actions", Horizontal)
        err_label = self.query_one("#notconnected_error", Label)
        main_button = self.query_one("#btn_expand_connecting", Button)
        if message:
            actions.remove_class("hidden")
            main_button.label = Text(str(message), style="#dc3545")
            err_label.update("")
        else:
            actions.add_class("hidden")
            main_button.label = "Connecting..."
            err_label.update("")


@dataclass(frozen=True)
class DeadlinePickResult:
    action: str
    timestamp: Optional[int] = None


class DeadlinePickerScreen(ModalScreen[DeadlinePickResult]):
    TIME_INTERVAL_MINUTES = 15
    WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]

    DEFAULT_CSS = """
    DeadlinePickerScreen {
        align: center middle;
    }
    #deadline_picker {
        width: 80;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $panel;
    }
    #deadline_header {
        height: auto;
        padding: 0 0 1 0;
        content-align: center middle;
    }
    #deadline_month_label {
        width: 1fr;
        content-align: center middle;
        text-style: bold;
    }
    #deadline_month_prev, #deadline_month_next {
        min-width: 3;
        width: 3;
    }
    #deadline_calendar {
        height: auto;
        padding: 1 0;
    }
    .calendar_header {
        height: auto;
        content-align: center middle;
    }
    .calendar_row {
        height: auto;
        content-align: center middle;
    }
    .calendar_day_header {
        width: 4;
        content-align: center middle;
        color: $text-muted;
    }
    .calendar_day {
        width: 4;
        min-width: 4;
        height: auto;
        padding: 0 1;
    }
    .calendar_today {
        text-style: underline;
    }
    #deadline_body {
        height: auto;
    }
    #deadline_calendar_block {
        width: 1fr;
    }
    #deadline_time_block {
        width: 12;
        padding-left: 2;
    }
    #deadline_time_title {
        content-align: center middle;
    }
    #deadline_time_list {
        height: 10;
        border: round $surface;
    }
    .deadline_row {
        height: auto;
        padding: 1 0;
        content-align: center middle;
    }
    """

    def __init__(
        self,
        initial_deadline: Optional[int] = None,
        *,
        on_change: Optional[Callable[[Optional[int]], None]] = None,
    ) -> None:
        super().__init__()
        self._on_change = on_change
        now = _dt.datetime.now()
        if initial_deadline is not None:
            initial_dt = _dt.datetime.fromtimestamp(int(initial_deadline))
            self._date = initial_dt.date()
            self._hour = initial_dt.hour
            self._minute = initial_dt.minute
        else:
            self._date = now.date()
            self._hour = now.hour
            interval = self.TIME_INTERVAL_MINUTES
            self._minute = (now.minute // interval) * interval
        self._calendar_map: dict[str, _dt.date] = {}
        self._time_slots = [
            (hour, minute)
            for hour in range(24)
            for minute in range(0, 60, self.TIME_INTERVAL_MINUTES)
        ]
        self._normalize_time()

    def _emit_change(self, deadline: Optional[int]) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(deadline)
        except Exception:
            pass

    def _emit_current_selection(self) -> None:
        selected = _dt.datetime.combine(self._date, _dt.time(self._hour, self._minute, 0))
        self._emit_change(int(selected.timestamp()))

    def compose(self) -> ComposeResult:
        with Vertical(id="deadline_picker"):
            with Horizontal(id="deadline_header"):
                yield Button("<", id="deadline_month_prev", variant="default", classes="btn_nav")
                yield Label("", id="deadline_month_label")
                yield Button(">", id="deadline_month_next", variant="default", classes="btn_nav")
            with Horizontal(id="deadline_body"):
                with Vertical(id="deadline_calendar_block"):
                    with Horizontal(classes="calendar_header"):
                        for day in self.WEEKDAYS:
                            yield Label(day, classes="calendar_day_header")
                    with Vertical(id="deadline_calendar"):
                        for row in range(6):
                            with Horizontal(classes="calendar_row"):
                                for col in range(7):
                                    yield Button(
                                        "",
                                        id=f"cal_day_{row}_{col}",
                                        variant="default",
                                        classes="calendar_day",
                                    )
                with Vertical(id="deadline_time_block"):
                    yield Label("Time", id="deadline_time_title")
                    items = []
                    for idx, (hour, minute) in enumerate(self._time_slots):
                        label = _format_time_label(hour, minute)
                        items.append(ListItem(Label(label), id=f"time_{idx}"))
                    yield ListView(*items, id="deadline_time_list")
            with Horizontal(classes="deadline_row"):
                yield Button("Clear", id="deadline_clear", variant="warning")
                yield Button("Close", id="deadline_cancel", variant="default", classes="btn_secondary")

    def on_mount(self) -> None:
        self._refresh_preview()

    def _normalize_time(self) -> None:
        interval = self.TIME_INTERVAL_MINUTES
        now = _dt.datetime.now()
        if self._date == now.date():
            current_minutes = self._hour * 60 + self._minute
            now_minutes = now.hour * 60 + now.minute
            if current_minutes <= now_minutes:
                next_minutes = ((now_minutes // interval) + 1) * interval
                if next_minutes >= 24 * 60:
                    self._date = self._date + _dt.timedelta(days=1)
                    next_minutes = 0
                self._hour = next_minutes // 60
                self._minute = next_minutes % 60

    def _refresh_preview(self) -> None:
        try:
            today = _dt.datetime.now().date()
            month = _MONTH_FULL[self._date.month - 1]
            year = self._date.year
            self.query_one("#deadline_month_label", Label).update(f"{month} {year}")
            try:
                prev_button = self.query_one("#deadline_month_prev", Button)
                prev_button.disabled = (self._date.year, self._date.month) <= (today.year, today.month)
            except Exception:
                pass
            self._refresh_calendar()
            self._refresh_time_list()
        except Exception:
            pass

    def _refresh_calendar(self) -> None:
        try:
            year = self._date.year
            month = self._date.month
            first_day = _dt.date(year, month, 1)
            first_weekday = (first_day.weekday() + 1) % 7  # Sunday=0
            import calendar

            days_in_month = calendar.monthrange(year, month)[1]
            self._calendar_map = {}
            day_num = 1 - first_weekday

            today = _dt.datetime.now().date()

            for row in range(6):
                for col in range(7):
                    button_id = f"cal_day_{row}_{col}"
                    button = self.query_one(f"#{button_id}", Button)
                    if 1 <= day_num <= days_in_month:
                        date = _dt.date(year, month, day_num)
                        button.label = str(day_num)
                        is_selected = date == self._date
                        if date < today:
                            button.disabled = True
                            button.variant = "primary" if is_selected else "default"
                            button.remove_class("calendar_today")
                        else:
                            self._calendar_map[button_id] = date
                            button.disabled = False
                            button.variant = "primary" if is_selected else "default"
                            if date == today:
                                button.add_class("calendar_today")
                            else:
                                button.remove_class("calendar_today")
                    else:
                        button.label = ""
                        button.disabled = True
                        button.variant = "default"
                        button.remove_class("calendar_today")
                    day_num += 1
        except Exception:
            pass

    def _refresh_time_list(self) -> None:
        try:
            list_view = self.query_one("#deadline_time_list", ListView)
            today = _dt.datetime.now().date()
            now = _dt.datetime.now()

            selected_index: int | None = None
            if self._minute % self.TIME_INTERVAL_MINUTES == 0:
                selected_index = (self._hour * (60 // self.TIME_INTERVAL_MINUTES)) + (
                    self._minute // self.TIME_INTERVAL_MINUTES
                )

            for idx, (hour, minute) in enumerate(self._time_slots):
                item = list_view.children[idx]
                if not isinstance(item, ListItem):
                    continue
                if self._date == today:
                    slot_dt = _dt.datetime.combine(today, _dt.time(hour, minute))
                    item.disabled = slot_dt <= now
                else:
                    item.disabled = False

            list_view.index = selected_index
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""

        if button_id.startswith("cal_day_"):
            picked = self._calendar_map.get(button_id)
            if picked is not None:
                self._date = picked
                self._normalize_time()
                self._refresh_preview()
                self._emit_current_selection()
            return

        if button_id in {"deadline_month_prev", "deadline_month_next"}:
            delta_months = -1 if button_id == "deadline_month_prev" else 1
            delta_years = 0
            year = self._date.year + delta_years
            month = self._date.month + delta_months
            while month < 1:
                month += 12
                year -= 1
            while month > 12:
                month -= 12
                year += 1
            import calendar

            days_in_month = calendar.monthrange(year, month)[1]
            day = min(self._date.day, days_in_month)
            self._date = _dt.date(year, month, day)
            if self._date < _dt.datetime.now().date():
                self._date = _dt.datetime.now().date()
            self._normalize_time()
            self._refresh_preview()
            return

        if button_id == "deadline_clear":
            self._emit_change(None)
            self.dismiss(DeadlinePickResult(action="clear", timestamp=None))
            return
        if button_id == "deadline_cancel":
            self.dismiss(DeadlinePickResult(action="cancel", timestamp=None))
            return

    def _set_time_from_index(self, index: int | None, *, emit: bool = False) -> None:
        if index is None:
            return
        if index < 0 or index >= len(self._time_slots):
            return
        hour, minute = self._time_slots[index]
        self._hour = hour
        self._minute = minute
        self._normalize_time()
        self._refresh_preview()
        if emit:
            self._emit_current_selection()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if (event.list_view.id or "") != "deadline_time_list":
            return
        index = event.list_view.index
        self._set_time_from_index(index, emit=False)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if (event.list_view.id or "") != "deadline_time_list":
            return
        self._set_time_from_index(event.index, emit=True)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(DeadlinePickResult(action="cancel", timestamp=None))
            event.stop()
            return
        focused = self.app.focused
        if isinstance(focused, ListView) and (focused.id or "") == "deadline_time_list":
            return
        if event.key in {"left", "h"}:
            self._date = self._date - _dt.timedelta(days=1)
        elif event.key in {"right", "l"}:
            self._date = self._date + _dt.timedelta(days=1)
        elif event.key in {"up", "k"}:
            self._date = self._date - _dt.timedelta(days=7)
        elif event.key in {"down", "j"}:
            self._date = self._date + _dt.timedelta(days=7)
        elif event.key == "pageup":
            month = self._date.month - 1
            year = self._date.year
            if month < 1:
                month = 12
                year -= 1
            import calendar

            day = min(self._date.day, calendar.monthrange(year, month)[1])
            self._date = _dt.date(year, month, day)
        elif event.key == "pagedown":
            month = self._date.month + 1
            year = self._date.year
            if month > 12:
                month = 1
                year += 1
            import calendar

            day = min(self._date.day, calendar.monthrange(year, month)[1])
            self._date = _dt.date(year, month, day)
        else:
            return

        if self._date < _dt.datetime.now().date():
            self._date = _dt.datetime.now().date()
        self._normalize_time()
        self._refresh_preview()
        event.stop()

class LiveTaskRow(Widget):
    def __init__(
        self,
        task: LiveTask,
        index: int,
        *,
        active: bool,
        active_value: str,
        active_deadline_text: str,
    ) -> None:
        super().__init__(classes="task_row")
        self.task = task
        self.index = index
        self.active = active
        self.active_value = active_value
        self.active_deadline_text = active_deadline_text

    def compose(self) -> ComposeResult:
        with Horizontal(classes="task_row_inner"):
            yield Label(f"{self.index}|", classes="task_index")

            if not self.active:
                yield Label(self.task.value, classes="task_value")
                if self.task.deadline is not None:
                    yield DeadlineBadge(self.task.deadline, countdown=False, classes="task_deadline")
                return

            # Active edit row
            yield Button("Task Succeeded", id=f"btn_succeed_{self.task.id}", variant="success", classes="task_action")
            yield Input(value=self.active_value, id=f"input_value_{self.task.id}", classes="task_value_input")
            yield Input(
                value=self.active_deadline_text,
                placeholder="Select date and time",
                id=f"input_deadline_{self.task.id}",
                classes="task_deadline_input",
            )
            yield Button("[cal]", id=f"btn_pick_deadline_{self.task.id}", variant="default", classes="task_action")
            yield Button("Done", id=f"btn_done_{self.task.id}", variant="default", classes="btn_dark task_action")
            yield Button("Task Failed", id=f"btn_fail_{self.task.id}", variant="error", classes="task_action")
            yield Button(
                "Task Obsoleted",
                id=f"btn_obsolete_{self.task.id}",
                variant="default",
                classes="btn_obsoleted task_action",
            )

    def on_click(self, event: events.Click) -> None:
        if not self.active:
            try:
                self.app.set_active_task(self.task.id)  # type: ignore[attr-defined]
            except Exception:
                pass


class OverdueTaskRow(Widget):
    def __init__(self, task: LiveTask, index: int) -> None:
        super().__init__(classes="task_row overdue")
        self.task = task
        self.index = index

    def compose(self) -> ComposeResult:
        with Horizontal(classes="task_row_inner"):
            yield Label(f"{self.index}|", classes="task_index")
            yield Label(self.task.value, classes="task_value")
            yield DeadlineBadge(self.task.deadline, countdown=True, classes="task_deadline")
            yield Button("Succeeded", id=f"btn_succeed_{self.task.id}", variant="success", classes="task_action")
            yield Button("Failed", id=f"btn_fail_{self.task.id}", variant="error", classes="task_action")
            yield Button(
                "Obsoleted",
                id=f"btn_obsolete_{self.task.id}",
                variant="default",
                classes="btn_obsoleted task_action",
            )


class FinishedTaskRow(Widget):
    def __init__(self, task: FinishedTask, index: int) -> None:
        super().__init__(classes="task_row finished")
        self.task = task
        self.index = index

    def compose(self) -> ComposeResult:
        with Horizontal(classes="task_row_inner"):
            yield Label(f"{self.index}|", classes="task_index")
            yield StatusBadge(self.task.status, classes="task_status_badge")
            yield Label(self.task.value, classes="task_value")
            if self.task.deadline is not None:
                yield DeadlineBadge(self.task.deadline, countdown=False, classes="task_deadline")


class ConnectedPanel(Widget):
    """Expanded connected UI with tabs."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="connected_root"):
            with Vertical(id="connected_sidebar"):
                yield Button("Collapse", id="btn_collapse_connected", variant="default", classes="btn_secondary")
                yield Button("Log Out", id="btn_logout", variant="default", classes="btn_secondary")
            with Vertical(id="connected_main"):
                with TabbedContent(id="tabs"):
                    with TabPane("Live Tasks", id=ViewType.LIVE.value):
                        yield NewTaskInput(placeholder="What needs to be done?", id="input_new_task")
                        yield Vertical(id="live_list")
                    with TabPane("Overdue Tasks (0)", id=ViewType.OVERDUE.value):
                        yield Vertical(id="overdue_list")
                    with TabPane("Finished Tasks", id=ViewType.FINISHED.value):
                        yield Vertical(id="finished_list")
                    with TabPane("Preferences", id=ViewType.PREFERENCES.value):
                        yield Label("Vocal Reminders", classes="pref_title")
                        with Horizontal(classes="pref_row"):
                            yield Switch(value=False, id="pref_vocal_enabled")
                            yield Label("Enable vocal reminders (requires espeak-ng to be installed)")
                        yield Label("Frequency: 5 minutes", id="pref_vocal_frequency_label", classes="pref_value")
                        yield FrequencySlider(id="pref_vocal_frequency_slider", classes="pref_slider")

    def set_overdue_count(self, count: int) -> None:
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            tab = tabs.get_tab(ViewType.OVERDUE.value)
            tab.label = Text(f"Overdue Tasks ({count})", style="#dc3545")
        except Exception:
            pass

    def set_tabs_lockdown(self, *, lockdown: bool) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        reason = "Please resolve overdue tasks first"
        muted_tabs = {ViewType.LIVE.value, ViewType.FINISHED.value, ViewType.PREFERENCES.value}
        for pane_id in (
            ViewType.LIVE.value,
            ViewType.OVERDUE.value,
            ViewType.FINISHED.value,
            ViewType.PREFERENCES.value,
        ):
            try:
                tabs.enable_tab(pane_id)
            except Exception:
                pass
            try:
                tab = tabs.get_tab(pane_id)
            except Exception:
                continue

            tab.disabled = False
            if lockdown and pane_id in muted_tabs:
                tab.add_class("tab-muted")
                tab.tooltip = reason
            else:
                tab.remove_class("tab-muted")
                tab.tooltip = None

    def set_active_tab(self, view: ViewType) -> None:
        self.query_one("#tabs", TabbedContent).active = view.value

    def set_collapse_disabled(self, disabled: bool) -> None:
        button = self.query_one("#btn_collapse_connected", Button)
        button.disabled = disabled
        button.tooltip = "Please resolve overdue tasks first" if disabled else None

    def focus_new_task(self) -> None:
        try:
            self.query_one("#input_new_task", Input).focus()
        except Exception:
            pass

    def update_preferences_controls(self, prefs: Preferences) -> None:
        switch = self.query_one("#pref_vocal_enabled", Switch)
        switch.value = prefs.vocal_enabled
        minutes = max(1, int(round(prefs.vocal_frequency / 60)))
        label = f"Frequency: {minutes} minute" + ("s" if minutes != 1 else "")
        self.query_one("#pref_vocal_frequency_label", Label).update(label)
        slider = self.query_one("#pref_vocal_frequency_slider", FrequencySlider)
        slider.set_value(minutes, notify=False)
        slider.disabled = not prefs.vocal_enabled

    def update_lists(
        self,
        *,
        snapshot: StateSnapshot,
        active_id: Optional[str],
        active_value: str,
        active_deadline_text: str,
    ) -> None:
        # Live list
        live_list = self.query_one("#live_list", Vertical)
        for child in list(live_list.children):
            child.remove()

        if not snapshot.live:
            live_list.mount(Label("You have not created a task yet...", classes="muted big"))
        else:
            for i, task in enumerate(snapshot.live):
                is_active = active_id == task.id
                row = LiveTaskRow(
                    task,
                    i,
                    active=is_active,
                    active_value=active_value if is_active else task.value,
                    active_deadline_text=active_deadline_text if is_active else "",
                )
                live_list.mount(row)

        # Overdue list
        overdue_list = self.query_one("#overdue_list", Vertical)
        for child in list(overdue_list.children):
            child.remove()

        overdue_tasks = [
            t for t in snapshot.live
            if t.deadline is not None and _now_unix_seconds() > int(t.deadline)
        ]
        if not overdue_tasks:
            overdue_list.mount(Label("No overdue tasks", classes="muted big"))
        else:
            for i, task in enumerate(overdue_tasks):
                overdue_list.mount(OverdueTaskRow(task, i))

        # Finished list
        finished_list = self.query_one("#finished_list", Vertical)
        for child in list(finished_list.children):
            child.remove()

        if not snapshot.finished:
            finished_list.mount(Label("No finished tasks yet...", classes="muted big"))
        else:
            for i, task in enumerate(snapshot.finished):
                finished_list.mount(FinishedTaskRow(task, i))


class Statusbar2App(App):
    """
    Textual port of the original Tauri + React "Statusbar2" UI.

    - Stores cache/session + preferences in SQLite (stdlib sqlite3).
    - Tasks are sourced from the server via WebSocket operations.
    - Preserves the original command syntax (c/t/s/f/o/r/q/mv/rev/d ...) and tab behaviors.
    """

    CSS_PATH = "styles.tcss"

    def __init__(self, db_path: str | None = None) -> None:
        super().__init__()
        self.db = StatusbarDB(db_path)
        self.expanded: bool = False

        # App state (mirrors the TS union-ish types)
        self.state_type: str = "NotLoggedIn"  # NotLoggedIn | Restored | NotConnected | Connected
        self.error: Optional[str] = None

        self.server_api_url: str = ""
        self.api_key: str = ""

        self.session_id: str = ""
        self.view_type: ViewType = ViewType.LIVE

        self.snapshot: StateSnapshot = StateSnapshot(live=[], finished=[])
        self.preferences: Preferences = Preferences()

        # WebSocket session (network-backed)
        self._ws_client: Optional[WebsocketClient] = None

        # OS focus tracking (best-effort)
        self._mouse_inside: bool = False
        self._window_focused: bool = False
        self._suppress_deadline_change: bool = False

        # Active edit state
        self.active_task_id: Optional[str] = None
        self.active_task_value: str = ""
        self.active_deadline_text: str = ""
        self.active_deadline_value: Optional[int] = None

        # Timers
        self._overdue_timer = None
        self._vocal_timer = None
        self._last_vocal_time: float = 0.0
        self._prev_overdue_count: int = 0

    def compose(self) -> ComposeResult:
        yield CollapsedBar(id="collapsed_view")
        with Container(id="expanded_view", classes="hidden"):
            yield LoginPanel(id="panel_login")
            yield RestoredPanel(id="panel_restored", classes="hidden")
            yield NotConnectedPanel(id="panel_notconnected", classes="hidden")
            yield ConnectedPanel(id="panel_connected", classes="hidden")

    def on_mount(self) -> None:
        # Match the TS theme palette (gruvbox-inspired).
        self.register_theme(GRUVBOX_THEME)
        self.theme = GRUVBOX_THEME.name

        # Load cache: if present, go to Restored state (like TS)
        cache = self.db.load_cache()
        if cache:
            self.server_api_url = cache.server_api_url
            self.api_key = cache.api_key
            self.preferences = cache.preferences
            self.state_type = "Restored"
            self.error = None
        else:
            self.state_type = "NotLoggedIn"
            self.error = None

        self._update_visible_panel()
        self._update_collapsed_bar()

        # periodic overdue check (also handles auto-expand)
        self._overdue_timer = self.set_interval(1.0, self._overdue_tick)

        # OS-level attention + dock hint (best-effort)
        os_integration.maybe_dock_window()
        os_integration.request_user_attention()

    # -------------------------
    # UI helpers
    # -------------------------
    def _notify(self, message: str, *, severity: str = "info") -> None:
        self.log(f"[{severity}] {message}")

    def _set_window_focus(self, focused: bool) -> None:
        if self._window_focused == focused:
            return
        success = os_integration.set_focus_state(focused)
        self._window_focused = focused
        try:
            self.screen.set_class(focused, "focused")
        except Exception:
            pass
        if not success:
            self.log("Failed to change window focus state.")

    def _set_expanded(self, expanded: bool) -> None:
        self.expanded = expanded
        os_integration.set_expand_state(expanded)
        collapsed = self.query_one("#collapsed_view", CollapsedBar)
        expanded_view = self.query_one("#expanded_view", Container)
        if expanded:
            collapsed.add_class("hidden")
            expanded_view.remove_class("hidden")
        else:
            expanded_view.add_class("hidden")
            collapsed.remove_class("hidden")

    def _show_panel(self, panel_id: str) -> None:
        panels = {
            "login": self.query_one("#panel_login", LoginPanel),
            "restored": self.query_one("#panel_restored", RestoredPanel),
            "notconnected": self.query_one("#panel_notconnected", NotConnectedPanel),
            "connected": self.query_one("#panel_connected", ConnectedPanel),
        }
        for p in panels.values():
            p.add_class("hidden")
        panels[panel_id].remove_class("hidden")

    def _update_visible_panel(self) -> None:
        if self.state_type == "NotLoggedIn":
            self._show_panel("login")
            self._sync_login_panel()
        elif self.state_type == "Restored":
            self._show_panel("restored")
        elif self.state_type == "NotConnected":
            self._show_panel("notconnected")
            self.query_one("#panel_notconnected", NotConnectedPanel).set_error(self.error)
        elif self.state_type == "Connected":
            self._show_panel("connected")
            self._refresh_connected_panel()
        else:
            self._show_panel("login")

    def _sync_login_panel(self) -> None:
        panel = self.query_one("#panel_login", LoginPanel)
        if self.error:
            panel.show_error(self.error)
        else:
            panel.clear_error()

        try:
            panel.query_one("#login_server_url", Input).value = self.server_api_url
        except Exception:
            pass

    def _update_collapsed_bar(self) -> None:
        self.query_one("#collapsed_view", CollapsedBar).refresh_bar()

    def _refresh_connected_panel(self) -> None:
        panel = self.query_one("#panel_connected", ConnectedPanel)
        panel.update_lists(
            snapshot=self.snapshot,
            active_id=self.active_task_id,
            active_value=self.active_task_value,
            active_deadline_text=self.active_deadline_text,
        )
        panel.update_preferences_controls(self.preferences)

        overdue_count = self._compute_overdue_count()
        panel.set_overdue_count(overdue_count)

        lockdown = overdue_count > 0
        panel.set_tabs_lockdown(lockdown=lockdown)
        panel.set_collapse_disabled(lockdown)

        if lockdown:
            self.view_type = ViewType.OVERDUE
        panel.set_active_tab(self.view_type)

        if self.view_type == ViewType.LIVE and self.active_task_id is None and self.expanded:
            panel.focus_new_task()

    # -------------------------
    # State transitions
    # -------------------------
    def expand_dock(self) -> None:
        self._set_expanded(True)
        self._update_visible_panel()

        if self._mouse_inside and not self._window_focused:
            self._set_window_focus(True)

        if self.state_type == "NotLoggedIn":
            try:
                self.query_one("#login_email", Input).focus()
            except Exception:
                pass
        elif self.state_type == "Connected":
            try:
                self.query_one("#panel_connected", ConnectedPanel).focus_new_task()
            except Exception:
                pass

    def collapse_dock(self) -> None:
        if self.state_type == "Connected":
            self._clear_active_edit(commit=False)
            self.view_type = ViewType.LIVE
            try:
                self.query_one("#input_new_task", Input).value = ""
            except Exception:
                pass

        if self._window_focused:
            self._set_window_focus(False)

        self._set_expanded(False)
        self._update_collapsed_bar()

    def logout(self) -> None:
        self._disconnect_ws(reason="User logged out")
        self.db.clear_cache()
        self.state_type = "NotLoggedIn"
        self.error = None
        self.api_key = ""
        self.session_id = ""
        self.view_type = ViewType.LIVE
        self.snapshot = StateSnapshot(live=[], finished=[])
        self.preferences = Preferences()
        self._clear_active_edit(commit=False)

        self._cancel_vocal_timer()

        self._update_visible_panel()
        self._update_collapsed_bar()
        try:
            self.query_one("#login_email", Input).value = ""
            self.query_one("#login_password", Input).value = ""
        except Exception:
            pass

    # -------------------------
    # Login / Resume
    # -------------------------
    def attempt_login(self, *, email: str, password: str, server_api_url: str) -> None:
        if self.state_type != "NotLoggedIn":
            return
        try:
            formatted_url = format_server_url(server_api_url, default_url=DEFAULT_SERVER_URL)
            info = fetch_server_info(formatted_url)
            api_key = create_api_key(info, email=email, password=password)

            prefs = Preferences(vocal_enabled=False, vocal_frequency=300)
            self.preferences = prefs

            cache = TodosCache(
                server_api_url=formatted_url,
                api_key=api_key,
                preferences=prefs,
            )
            self.db.save_cache(cache)

            self.api_key = api_key

            self.connect_session(api_key=api_key)
            self._notify("Login successful. Connecting...", severity="info")
        except Exception as e:
            self.state_type = "NotLoggedIn"
            self.error = str(e)
            self._update_visible_panel()
            self._update_collapsed_bar()
            self._notify(f"Login failed: {self.error}", severity="error")

    def connect_session(self, *, api_key: str) -> None:
        self._disconnect_ws(reason="New connection requested")
        self.state_type = "NotConnected"
        self.error = None
        self._update_visible_panel()
        self._update_collapsed_bar()

        self.api_key = api_key
        self.view_type = ViewType.LIVE
        self._clear_active_edit(commit=False)
        self._prev_overdue_count = 0

        server_api_url = format_server_url(self.server_api_url, default_url=DEFAULT_SERVER_URL)
        self._ws_client = WebsocketClient(self, api_key=api_key, server_api_url=server_api_url)
        self.run_worker(
            self._ws_client.run(),
            name="ws-client",
            group="ws",
            exclusive=True,
            exit_on_error=False,
        )

    def _disconnect_ws(self, *, reason: str) -> None:
        if self._ws_client is None:
            return
        try:
            self.run_worker(
                self._ws_client.close(reason=reason),
                name="ws-close",
                group="ws",
                exclusive=False,
                exit_on_error=False,
            )
        except Exception:
            pass

    # -------------------------
    # WebSocket lifecycle
    # -------------------------
    def on_ws_open(self, api_key: str) -> None:
        cache = self.db.load_cache()
        prefs = cache.preferences if cache else Preferences(vocal_enabled=False, vocal_frequency=300)

        self.preferences = prefs
        self.snapshot = StateSnapshot(live=[], finished=[])
        self.state_type = "Connected"
        self.error = None
        self.view_type = ViewType.LIVE
        self.session_id = random_string()
        self._clear_active_edit(commit=False)
        self._prev_overdue_count = 0

        self._update_visible_panel()
        self._update_collapsed_bar()
        self._configure_vocal_timer()

        try:
            self.query_one("#input_new_task", Input).value = ""
        except Exception:
            pass

        if self.expanded:
            try:
                self.query_one("#panel_connected", ConnectedPanel).focus_new_task()
            except Exception:
                pass

        self._notify("Connected.", severity="info")

    def on_ws_message(self, raw: str) -> None:
        if self.state_type != "Connected":
            return
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
            ws_op = WebsocketOp.from_dict(payload)
        except Exception as e:
            self.log(f"WebSocket message parse error: {e}")
            return

        try:
            new_snapshot = apply_operation(self.snapshot, ws_op.kind)
        except Exception as e:
            self.log(f"WebSocket op apply error: {e}")
            return

        self.snapshot = new_snapshot
        self._refresh_connected_panel()
        self._update_collapsed_bar()
        # Run overdue handling immediately to mirror the TS effect timing.
        self._overdue_tick()
        # Mirror the TS effect that reconfigures vocal reminders when live tasks change.
        self._configure_vocal_timer()

    def on_ws_close(self, reason: str) -> None:
        cache = self.db.load_cache()
        unauthorized = reason == "Unauthorized"

        self._cancel_vocal_timer()
        self._ws_client = None

        if unauthorized or not cache:
            self.state_type = "NotLoggedIn"
            self.error = "Session expired. Please log in again." if unauthorized else None
            self.api_key = ""
        else:
            self.state_type = "NotConnected"
            self.error = reason or "Connection closed"

        self.snapshot = StateSnapshot(live=[], finished=[])
        self.view_type = ViewType.LIVE
        self.session_id = ""
        self._clear_active_edit(commit=False)

        self._update_visible_panel()
        self._update_collapsed_bar()
        if self.state_type == "NotLoggedIn" and self.error:
            self._notify(self.error, severity="error")
        elif self.state_type == "NotConnected" and self.error:
            self._notify(f"Connection lost: {self.error}", severity="error")
        if self.state_type == "NotLoggedIn":
            try:
                self.query_one("#login_email", Input).value = ""
                self.query_one("#login_password", Input).value = ""
            except Exception:
                pass

    def on_ws_error(self, error: Exception) -> None:
        self.log(f"WebSocket error: {error}")
        self._notify(f"WebSocket error: {error}", severity="error")

    def send_ws_op(self, op_kind: dict) -> bool:
        if self.state_type != "Connected" or self._ws_client is None or not self._ws_client.is_open:
            self._notify("WebSocket not connected.", severity="error")
            return False

        ws_op = {
            "alleged_time": current_time_millis(),
            "kind": op_kind,
        }
        try:
            raw = json.dumps(ws_op)
        except Exception as e:
            self._notify(f"Failed to encode operation: {e}", severity="error")
            return False

        ok = self._ws_client.send(raw)
        if not ok:
            self._notify("Failed to send WebSocket operation.", severity="error")
        return ok

    # -------------------------
    # Commands / operations
    # -------------------------
    def submit_task_text(self, text: str) -> None:
        if self.state_type != "Connected":
            return
        text = text or ""
        if not text.strip():
            return

        first = text.split(" ")[0]

        if first == "c":
            self.collapse_dock()
            return

        if first == "t":
            self._clear_active_edit(commit=False)
            self.view_type = ViewType.LIVE if self.view_type == ViewType.FINISHED else ViewType.FINISHED
            self._refresh_connected_panel()
            try:
                self.query_one("#input_new_task", Input).value = ""
            except Exception:
                pass
            return

        if first in {"s", "f", "o"}:
            if not self.snapshot.live:
                return
            status: TaskStatus = "Succeeded" if first == "s" else "Failed" if first == "f" else "Obsoleted"
            task = self.snapshot.live[0]
            self.finish_task(task.id, status)
            try:
                self.query_one("#input_new_task", Input).value = ""
            except Exception:
                pass
            self._clear_active_edit(commit=False)
            return

        if first == "r":
            idx = parse_restore_command(text)
            if idx is None:
                return
            if 0 <= idx < len(self.snapshot.finished):
                self.restore_finished_task(self.snapshot.finished[idx].id)
                try:
                    self.query_one("#input_new_task", Input).value = ""
                except Exception:
                    pass
                self._clear_active_edit(commit=False)
            return

        if first == "q":
            idx = parse_move_to_end_command(text)
            if idx is None:
                return
            if len(self.snapshot.live) > 1 and 0 <= idx < len(self.snapshot.live):
                from_task = self.snapshot.live[idx]
                to_task = self.snapshot.live[-1]
                self.move_task(from_task.id, to_task.id)
                try:
                    self.query_one("#input_new_task", Input).value = ""
                except Exception:
                    pass
                self._clear_active_edit(commit=False)
            return

        if first == "mv":
            indices = parse_move_command(text)
            if indices is None:
                return
            from_index, to_index = indices
            if (
                from_index != to_index
                and 0 <= from_index < len(self.snapshot.live)
                and 0 <= to_index < len(self.snapshot.live)
            ):
                self.move_task(self.snapshot.live[from_index].id, self.snapshot.live[to_index].id)
                try:
                    self.query_one("#input_new_task", Input).value = ""
                except Exception:
                    pass
                self._clear_active_edit(commit=False)
            return

        if first == "rev":
            indices = parse_reverse_command(text)
            if indices is None:
                return
            from_index, to_index = indices
            if (
                from_index != to_index
                and 0 <= from_index < len(self.snapshot.live)
                and 0 <= to_index < len(self.snapshot.live)
            ):
                self.reverse_task(self.snapshot.live[from_index].id, self.snapshot.live[to_index].id)
                try:
                    self.query_one("#input_new_task", Input).value = ""
                except Exception:
                    pass
                self._clear_active_edit(commit=False)
            return

        if first == "d":
            deadline = parse_due_command(text)
            if deadline is None:
                return
            if self.snapshot.live:
                task = self.snapshot.live[0]
                self.edit_task(task.id, task.value, deadline)
                try:
                    self.query_one("#input_new_task", Input).value = ""
                except Exception:
                    pass
                self._clear_active_edit(commit=False)
            return

        self.add_new_task(text)

    def add_new_task(self, value: str, deadline: Optional[int] = None) -> None:
        if self.state_type != "Connected":
            return
        task_id = random_string()
        if self.send_ws_op(
            {
                "InsLiveTask": {
                    "id": task_id,
                    "value": value,
                    "deadline": deadline,
                }
            }
        ):
            try:
                self.query_one("#input_new_task", Input).value = ""
            except Exception:
                pass

    def finish_task(self, task_id: str, status: TaskStatus) -> None:
        if self.state_type != "Connected":
            return
        self.send_ws_op(
            {
                "FinishLiveTask": {
                    "id": task_id,
                    "status": status,
                }
            }
        )

    def restore_finished_task(self, task_id: str) -> None:
        if self.state_type != "Connected":
            return
        self.send_ws_op(
            {
                "RestoreFinishedTask": {
                    "id": task_id,
                }
            }
        )

    def move_task(self, from_id: str, to_id: str) -> None:
        if self.state_type != "Connected":
            return
        self.send_ws_op(
            {
                "MvLiveTask": {
                    "id_del": from_id,
                    "id_ins": to_id,
                }
            }
        )

    def reverse_task(self, id1: str, id2: str) -> None:
        if self.state_type != "Connected":
            return
        self.send_ws_op(
            {
                "RevLiveTask": {
                    "id1": id1,
                    "id2": id2,
                }
            }
        )

    def edit_task(self, task_id: str, value: str, deadline: Optional[int]) -> None:
        if self.state_type != "Connected":
            return
        self.send_ws_op(
            {
                "EditLiveTask": {
                    "id": task_id,
                    "value": value,
                    "deadline": deadline,
                }
            }
        )

    # -------------------------
    # Active task editing
    # -------------------------
    def set_active_task(self, task_id: Optional[str]) -> None:
        if self.state_type != "Connected":
            return

        # If there was an active task being edited, try to commit it first.
        # If the edit is invalid (e.g. deadline can't be parsed), keep the user in edit mode.
        if self.active_task_id is not None:
            if not self._commit_active_edit():
                return

        if task_id is None:
            self._clear_active_edit(commit=False)
            self._refresh_connected_panel()
            try:
                self.query_one("#panel_connected", ConnectedPanel).focus_new_task()
            except Exception:
                pass
            return

        task = next((t for t in self.snapshot.live if t.id == task_id), None)
        if task is None:
            return

        self.active_task_id = task.id
        self.active_task_value = task.value
        self.active_deadline_text = self._deadline_text_for_edit(task.deadline)
        self.active_deadline_value = task.deadline

        self._refresh_connected_panel()
        try:
            self.query_one(f"#input_value_{task_id}", Input).focus()
        except Exception:
            pass

    def _deadline_text_for_edit(self, deadline: Optional[int]) -> str:
        return self._format_deadline_input(deadline)

    def _format_deadline_input(self, deadline: Optional[int]) -> str:
        if deadline is None:
            return ""
        dt_obj = _dt.datetime.fromtimestamp(int(deadline))
        month = _MONTH_ABBR[dt_obj.month - 1]
        day = dt_obj.day
        year = dt_obj.year
        hour_12 = dt_obj.hour % 12
        if hour_12 == 0:
            hour_12 = 12
        ampm = "AM" if dt_obj.hour < 12 else "PM"
        return f"{month} {day}, {year} {hour_12}:{dt_obj.minute:02d} {ampm}"

    def _open_deadline_picker(self, task_id: str) -> None:
        if self.state_type != "Connected":
            return
        task = next((t for t in self.snapshot.live if t.id == task_id), None)
        if task is None:
            return
        initial_deadline = task.deadline
        if self.active_task_id == task_id:
            initial_deadline = self.active_deadline_value
        screen = DeadlinePickerScreen(
            initial_deadline=initial_deadline,
            on_change=lambda deadline: self._apply_deadline_selection(task_id, deadline),
        )
        self.push_screen(screen)

    def _handle_deadline_pick(self, task_id: str, result: DeadlinePickResult | None) -> None:
        if result is None or result.action == "cancel":
            return

        task = next((t for t in self.snapshot.live if t.id == task_id), None)
        if task is None:
            return

        deadline = None if result.action == "clear" else result.timestamp
        value = task.value

        if self.active_task_id == task_id:
            self.active_task_value = value
            self.active_deadline_text = self._deadline_text_for_edit(deadline)
            self.active_deadline_value = deadline
            self._refresh_connected_panel()

        self.edit_task(task_id, value, deadline)

    def _apply_deadline_selection(self, task_id: str, deadline: Optional[int]) -> None:
        task = next((t for t in self.snapshot.live if t.id == task_id), None)
        if task is None:
            return
        value = task.value

        if self.active_task_id == task_id:
            self.active_task_value = value
            self.active_deadline_text = self._deadline_text_for_edit(deadline)
            self.active_deadline_value = deadline
            self._refresh_connected_panel()

        self.edit_task(task_id, value, deadline)

    def _commit_active_edit(self) -> bool:
        if self.active_task_id is None:
            return True

        self.send_ws_op(
            {
                "EditLiveTask": {
                    "id": self.active_task_id,
                    "value": self.active_task_value,
                    "deadline": self.active_deadline_value,
                }
            }
        )
        return True

    def _clear_active_edit(self, *, commit: bool) -> bool:
        if commit and self.active_task_id is not None:
            if not self._commit_active_edit():
                return False
        self.active_task_id = None
        self.active_task_value = ""
        self.active_deadline_text = ""
        self.active_deadline_value = None
        return True

    # -------------------------
    # Overdue handling
    # -------------------------
    def _compute_overdue_count(self) -> int:
        if self.state_type != "Connected":
            return 0
        now = _now_unix_seconds()
        return sum(1 for t in self.snapshot.live if t.deadline is not None and now > int(t.deadline))

    def _overdue_tick(self) -> None:
        if self.state_type != "Connected":
            return

        overdue_count = self._compute_overdue_count()

        if overdue_count > 0:
            if not self.expanded:
                self.expand_dock()
            if self.view_type != ViewType.OVERDUE:
                self.view_type = ViewType.OVERDUE
                self._refresh_connected_panel()

        if self._prev_overdue_count > 0 and overdue_count == 0 and self.view_type == ViewType.OVERDUE:
            self.view_type = ViewType.LIVE
            self._refresh_connected_panel()
            try:
                self.query_one("#panel_connected", ConnectedPanel).focus_new_task()
            except Exception:
                pass

        self._prev_overdue_count = overdue_count

    # -------------------------
    # Vocal reminders
    # -------------------------
    def _configure_vocal_timer(self) -> None:
        self._cancel_vocal_timer()
        if self.state_type != "Connected" or not self.preferences.vocal_enabled:
            return

        self._speak_top_task()
        seconds = max(1, int(self.preferences.vocal_frequency))
        self._vocal_timer = self.set_interval(float(seconds), self._speak_top_task)

    def _persist_preferences(self) -> None:
        cache = self.db.load_cache()
        if cache is None:
            return
        self.db.save_cache(
            TodosCache(
                server_api_url=cache.server_api_url,
                api_key=cache.api_key,
                preferences=self.preferences,
            )
        )

    def _cancel_vocal_timer(self) -> None:
        if self._vocal_timer is not None:
            try:
                self._vocal_timer.stop()
            except Exception:
                pass
            self._vocal_timer = None

    def _speak_top_task(self) -> None:
        if self.state_type != "Connected" or not self.preferences.vocal_enabled or not self.snapshot.live:
            return

        now = time.time()
        if now - self._last_vocal_time < 5.0:
            return

        message = self.snapshot.live[0].value
        try:
            subprocess.Popen(["espeak-ng", message])
            self._last_vocal_time = now
        except FileNotFoundError:
            self._notify("espeak-ng not found; disable vocal reminders or install espeak-ng.", severity="warning")
        except Exception as e:
            self._notify(f"Failed to speak task: {e}", severity="error")

    # -------------------------
    # Event handlers
    # -------------------------
    def on_mouse_move(self, event: events.MouseMove) -> None:
        self._mouse_inside = True
        if self.expanded and not self._window_focused:
            self._set_window_focus(True)

    def on_enter(self, event: events.Enter) -> None:
        self._mouse_inside = True
        if self.expanded and not self._window_focused:
            self._set_window_focus(True)

    def on_leave(self, event: events.Leave) -> None:
        if self.mouse_over is not None:
            return
        self._mouse_inside = False
        if self._window_focused:
            self._set_window_focus(False)

    def on_app_blur(self, event: events.AppBlur) -> None:
        self._mouse_inside = False
        if self._window_focused:
            self._set_window_focus(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""

        if button_id in {"btn_expand_login", "btn_expand_add", "btn_expand_main", "btn_expand_connecting", "btn_expand_unknown"}:
            self.expand_dock()
            return

        if button_id in {"btn_collapse", "btn_collapse_connected"}:
            self.collapse_dock()
            return

        if button_id == "btn_login_submit":
            email = self.query_one("#login_email", Input).value
            password = self.query_one("#login_password", Input).value
            server_url = self.query_one("#login_server_url", Input).value
            self.attempt_login(email=email, password=password, server_api_url=server_url)
            return

        if button_id == "btn_toggle_password":
            try:
                password_input = self.query_one("#login_password", Input)
                password_input.password = not password_input.password
                event.button.label = "[eye-slash]" if not password_input.password else "[eye]"
            except Exception:
                pass
            return

        if button_id in {"btn_resume_session", "btn_resume_session_expanded"}:
            cache = self.db.load_cache()
            if cache:
                self.server_api_url = cache.server_api_url
                self.api_key = cache.api_key
                self.preferences = cache.preferences
                self.connect_session(api_key=cache.api_key)
            else:
                self.state_type = "NotLoggedIn"
                self.error = None
                self._update_visible_panel()
                self._update_collapsed_bar()
            return

        if button_id == "btn_retry":
            if self.api_key:
                self.connect_session(api_key=self.api_key)
                return
            cache = self.db.load_cache()
            if cache:
                self.connect_session(api_key=cache.api_key)
            return

        if button_id == "btn_return_login":
            self.logout()
            return

        if button_id == "btn_logout":
            self.logout()
            return

        if button_id.startswith("btn_succeed_"):
            self.finish_task(button_id.removeprefix("btn_succeed_"), "Succeeded")
            return

        if button_id.startswith("btn_fail_"):
            self.finish_task(button_id.removeprefix("btn_fail_"), "Failed")
            return

        if button_id.startswith("btn_obsolete_"):
            self.finish_task(button_id.removeprefix("btn_obsolete_"), "Obsoleted")
            return

        if button_id.startswith("btn_pick_deadline_"):
            self._open_deadline_picker(button_id.removeprefix("btn_pick_deadline_"))
            return

        if button_id.startswith("btn_done_"):
            self.set_active_task(None)
            return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        widget_id = event.input.id or ""

        if widget_id == "login_email":
            try:
                self.query_one("#login_password", Input).focus()
            except Exception:
                pass
            return

        if widget_id == "login_password":
            email = self.query_one("#login_email", Input).value
            password = self.query_one("#login_password", Input).value
            server_url = self.query_one("#login_server_url", Input).value
            if email and password:
                self.attempt_login(email=email, password=password, server_api_url=server_url)
            return

        if widget_id == "input_new_task":
            self.submit_task_text(event.value)
            try:
                event.input.value = ""
            except Exception:
                pass
            return

        if widget_id.startswith("input_value_") or widget_id.startswith("input_deadline_"):
            self.set_active_task(None)
            return

    def on_input_changed(self, event: Input.Changed) -> None:
        widget_id = event.input.id or ""

        if widget_id in {"login_server_url", "login_email", "login_password"}:
            if self.state_type == "NotLoggedIn":
                self.error = None
                self._sync_login_panel()
            if widget_id == "login_server_url":
                self.server_api_url = event.value
            return

        if widget_id.startswith("input_value_"):
            task_id = widget_id.removeprefix("input_value_")
            if self.active_task_id == task_id:
                self.active_task_value = event.value
            return

        if widget_id.startswith("input_deadline_"):
            task_id = widget_id.removeprefix("input_deadline_")
            if self._suppress_deadline_change:
                return
            if self.active_task_id == task_id:
                self.active_deadline_text = event.value
                task = next((t for t in self.snapshot.live if t.id == task_id), None)
                if task is None:
                    return

                raw = event.value or ""
                if raw.strip() == "":
                    self.active_deadline_value = None
                    self.active_task_value = task.value
                    try:
                        value_input = self.query_one(f"#input_value_{task_id}", Input)
                        if value_input.value != task.value:
                            value_input.value = task.value
                    except Exception:
                        pass
                    self.edit_task(task_id, task.value, None)
                    return

                parsed = parse_deadline_input(raw)
                if parsed is not None:
                    self.active_deadline_value = parsed
                    self.active_task_value = task.value
                    formatted = self._deadline_text_for_edit(parsed)
                    if formatted != event.value:
                        self._suppress_deadline_change = True
                        try:
                            deadline_input = self.query_one(f"#input_deadline_{task_id}", Input)
                            deadline_input.value = formatted
                        except Exception:
                            pass
                        finally:
                            self._suppress_deadline_change = False
                        self.active_deadline_text = formatted
                    try:
                        value_input = self.query_one(f"#input_value_{task_id}", Input)
                        if value_input.value != task.value:
                            value_input.value = task.value
                    except Exception:
                        pass
                    self.edit_task(task_id, task.value, parsed)
            return

    def on_switch_changed(self, event: Switch.Changed) -> None:
        widget_id = event.switch.id or ""

        if widget_id == "pref_vocal_enabled":
            if self.state_type != "Connected":
                return
            enabled = bool(event.value)
            self.preferences = Preferences(
                vocal_enabled=enabled,
                vocal_frequency=self.preferences.vocal_frequency,
            )
            self._persist_preferences()
            self._configure_vocal_timer()
            try:
                self.query_one("#panel_connected", ConnectedPanel).update_preferences_controls(self.preferences)
            except Exception:
                pass
            return

    def on_frequency_slider_changed(self, event: FrequencySlider.Changed) -> None:
        if self.state_type != "Connected":
            return
        minutes = max(1, min(60, int(event.value)))
        self.preferences = Preferences(
            vocal_enabled=self.preferences.vocal_enabled,
            vocal_frequency=minutes * 60,
        )
        self._persist_preferences()
        self._configure_vocal_timer()
        try:
            self.query_one("#panel_connected", ConnectedPanel).update_preferences_controls(self.preferences)
        except Exception:
            pass

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if self.state_type != "Connected":
            return

        pane_id = event.pane.id or ""
        overdue_count = self._compute_overdue_count()
        if overdue_count > 0 and pane_id != ViewType.OVERDUE.value:
            try:
                self.query_one("#panel_connected", ConnectedPanel).set_active_tab(ViewType.OVERDUE)
            except Exception:
                pass
            self.view_type = ViewType.OVERDUE
            return

        try:
            self.view_type = ViewType(pane_id)
        except Exception:
            self.view_type = ViewType.LIVE

        if self.view_type == ViewType.LIVE:
            try:
                self.query_one("#panel_connected", ConnectedPanel).focus_new_task()
            except Exception:
                pass

    def on_exit(self) -> None:
        self._disconnect_ws(reason="App exiting")
        try:
            self.db.close()
        except Exception:
            pass

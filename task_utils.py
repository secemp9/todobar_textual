from __future__ import annotations

import calendar
import datetime as dt
import re
import time
from typing import Dict, Optional, Tuple

# Random session / task IDs (mirrors the TS randomString)
import random

from .models import FinishedTask, LiveTask, StateSnapshot, TaskStatus


def random_string() -> str:
    # Mirror Math.random().toString(36).substring(2, 18)
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    value = random.random()
    out = []
    for _ in range(16):
        value *= 36
        digit = int(value)
        out.append(chars[digit])
        value -= digit
    return "".join(out)


def current_time_millis() -> int:
    return int(time.time() * 1000)


# ----------------------------
# Command parsing (mirrors TS)
# ----------------------------
def parse_restore_command(input_text: str) -> Optional[int]:
    m = re.fullmatch(r"r\s*(\d+)?", input_text)
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else 0


def parse_move_to_end_command(input_text: str) -> Optional[int]:
    m = re.fullmatch(r"q\s*(\d+)?", input_text)
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else 0


def parse_move_command(input_text: str) -> Optional[Tuple[int, int]]:
    m = re.fullmatch(r"mv\s+(\d+)(?:\s+(\d+))?", input_text)
    if not m:
        return None
    from_index = int(m.group(1))
    to_index = int(m.group(2)) if m.group(2) else 0
    return (from_index, to_index)


def parse_reverse_command(input_text: str) -> Optional[Tuple[int, int]]:
    m = re.fullmatch(r"rev\s+(\d+)(?:\s+(\d+))?", input_text)
    if not m:
        return None
    from_index = int(m.group(1))
    to_index = int(m.group(2)) if m.group(2) else 0
    return (from_index, to_index)


# ----------------------------
# Deadline parsing (mirrors TS)
# ----------------------------
_MONTHS: Dict[str, int] = {
    "january": 0, "jan": 0,
    "february": 1, "feb": 1,
    "march": 2, "mar": 2,
    "april": 3, "apr": 3,
    "may": 4,
    "june": 5, "jun": 5,
    "july": 6, "jul": 6,
    "august": 7, "aug": 7,
    "september": 8, "sep": 8,
    "october": 9, "oct": 9,
    "november": 10, "nov": 10,
    "december": 11, "dec": 11,
}
_MONTHS_ABBR: Dict[str, int] = {k: v for k, v in _MONTHS.items() if len(k) == 3}

_DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]


def _now() -> dt.datetime:
    return dt.datetime.now()


def _today_midnight(now: dt.datetime) -> dt.datetime:
    return dt.datetime(now.year, now.month, now.day)


def parse_due_command(input_text: str) -> Optional[int]:
    """
    Parses the original TS `d ...` command.
    Returns unix timestamp in seconds, or None if not parsable.
    """
    parts = input_text.strip().split(" ")
    if len(parts) < 2:
        return None
    if parts[0] != "d":
        return None

    time_str = " ".join(parts[1:]).lower()

    # 1) minutes (e.g. 30m)
    m = re.fullmatch(r"(\d+)m", time_str)
    if m:
        minutes = int(m.group(1))
        return int(_now().timestamp()) + minutes * 60

    # 2) hours (e.g. 2h)
    m = re.fullmatch(r"(\d+)h", time_str)
    if m:
        hours = int(m.group(1))
        return int(_now().timestamp()) + hours * 3600

    now = _now()
    today = _today_midnight(now)

    # 3) absolute times (e.g. 8am, 3:30 pm)
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", time_str)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or "0")
        mer = m.group(3)

        if mer == "pm" and hh != 12:
            hh += 12
        if mer == "am" and hh == 12:
            hh = 0

        target = today + dt.timedelta(hours=hh, minutes=mm)
        if target <= now:
            target += dt.timedelta(days=1)
        return int(target.timestamp())

    # 4) dates (e.g. Jan 17)
    m = re.fullmatch(r"(" + "|".join(map(re.escape, _MONTHS.keys())) + r")\s+(\d{1,2})", time_str)
    if m:
        month = _MONTHS[m.group(1)]
        day = int(m.group(2))
        year = today.year

        days_in_month = calendar.monthrange(year, month + 1)[1]
        if day < 1 or day > days_in_month:
            return None

        target = today.replace(month=month + 1, day=day, hour=0, minute=0, second=0, microsecond=0)
        if target <= now:
            year += 1
            days_in_month = calendar.monthrange(year, month + 1)[1]
            if day > days_in_month:
                return None
            target = target.replace(year=year)
        return int(target.timestamp())

    # 5) dates + times (e.g. Jan 15 5 pm, Nov 30 1:05 am)
    m = re.fullmatch(
        r"(" + "|".join(map(re.escape, _MONTHS.keys())) + r")\s+(\d{1,2})\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
        time_str,
    )
    if m:
        month = _MONTHS[m.group(1)]
        day = int(m.group(2))
        hh = int(m.group(3))
        mm = int(m.group(4) or "0")
        mer = m.group(5)

        if hh < 1 or hh > 12 or mm < 0 or mm > 59:
            return None

        if mer == "pm" and hh != 12:
            hh += 12
        if mer == "am" and hh == 12:
            hh = 0

        year = today.year
        days_in_month = calendar.monthrange(year, month + 1)[1]
        if day < 1 or day > days_in_month:
            return None

        target = today.replace(month=month + 1, day=day, hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            year += 1
            days_in_month = calendar.monthrange(year, month + 1)[1]
            if day > days_in_month:
                return None
            target = target.replace(year=year)
        return int(target.timestamp())

    # 6) days of week (e.g. Monday)
    if time_str in _DAYS:
        day_index = _DAYS.index(time_str)
        current_day = today.weekday()  # Monday=0
        # Convert Sunday-based index to Monday-based
        # _DAYS is Sunday=0, so:
        target_weekday = (day_index - 1) % 7  # Sunday -> 6, Monday -> 0
        days_to_add = target_weekday - current_day
        if days_to_add <= 0:
            days_to_add += 7
        target = today + dt.timedelta(days=days_to_add)
        return int(target.timestamp())

    # 7) day of week + time (e.g. Monday 5pm)
    m = re.fullmatch(r"(" + "|".join(_DAYS) + r")\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", time_str)
    if m:
        day_str = m.group(1)
        hh = int(m.group(2))
        mm = int(m.group(3) or "0")
        mer = m.group(4)

        if mer == "pm" and hh != 12:
            hh += 12
        if mer == "am" and hh == 12:
            hh = 0

        day_index = _DAYS.index(day_str)
        target_weekday = (day_index - 1) % 7
        current_day = today.weekday()
        days_to_add = target_weekday - current_day
        if days_to_add <= 0:
            days_to_add += 7
        target = (today + dt.timedelta(days=days_to_add)) + dt.timedelta(hours=hh, minutes=mm)
        return int(target.timestamp())

    return None


def parse_deadline_input(raw: str) -> Optional[int]:
    """
    Parse the DatePicker-style input used in the TS app.
    Accepts:
      - 'MMM d, yyyy h:mm AM/PM'
      - Blank (caller treats as clear)
    """
    raw = (raw or "").strip()
    if raw == "":
        return None

    m = re.fullmatch(
        r"([A-Za-z]{3})\s+(\d{1,2}),\s*(\d{4})\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])",
        raw,
    )
    if m:
        month_str = m.group(1).lower()
        if month_str not in _MONTHS_ABBR:
            return None
        month = _MONTHS_ABBR[month_str] + 1
        day = int(m.group(2))
        year = int(m.group(3))
        hour = int(m.group(4))
        minute = int(m.group(5))
        ampm = m.group(6).lower()

        if hour < 1 or hour > 12 or minute < 0 or minute > 59:
            return None

        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0

        try:
            dt_obj = dt.datetime(year, month, day, hour, minute, 0)
        except ValueError:
            return None

        now = dt.datetime.now()
        today = dt.date(now.year, now.month, now.day)
        if dt_obj.date() < today:
            return None
        if dt_obj.date() == today and dt_obj <= now:
            return None

        return int(dt_obj.timestamp())

    return None


# ----------------------------
# WebSocket operation handling
# ----------------------------
def apply_operation(snapshot: StateSnapshot, op_kind: Dict) -> StateSnapshot:
    """Apply a WebSocket operation to a snapshot (mirrors TS applyOperation)."""
    if "OverwriteState" in op_kind:
        overwrite = op_kind["OverwriteState"]
        if isinstance(overwrite, StateSnapshot):
            return overwrite
        return snapshot

    if "InsLiveTask" in op_kind:
        payload = op_kind["InsLiveTask"]
        task = LiveTask(
            id=str(payload["id"]),
            value=str(payload["value"]),
            deadline=payload.get("deadline"),
            managed=None,
        )
        return StateSnapshot(live=[task, *snapshot.live], finished=list(snapshot.finished))

    if "RestoreFinishedTask" in op_kind:
        task_id = op_kind["RestoreFinishedTask"]["id"]
        finished_idx = next((i for i, t in enumerate(snapshot.finished) if t.id == task_id), -1)
        if finished_idx == -1:
            return snapshot
        finished_task = snapshot.finished[finished_idx]
        new_finished = [t for t in snapshot.finished if t.id != task_id]
        new_live_task = LiveTask(
            id=finished_task.id,
            value=finished_task.value,
            deadline=finished_task.deadline,
            managed=finished_task.managed,
        )
        return StateSnapshot(live=[new_live_task, *snapshot.live], finished=new_finished)

    if "EditLiveTask" in op_kind:
        payload = op_kind["EditLiveTask"]

        def _edit(task: LiveTask) -> LiveTask:
            if task.id != payload["id"]:
                return task
            return LiveTask(
                id=task.id,
                value=str(payload["value"]),
                deadline=payload.get("deadline"),
                managed=task.managed,
            )

        new_live = [_edit(task) for task in snapshot.live]
        return StateSnapshot(live=new_live, finished=list(snapshot.finished))

    if "DelLiveTask" in op_kind:
        task_id = op_kind["DelLiveTask"]["id"]
        new_live = [task for task in snapshot.live if task.id != task_id]
        return StateSnapshot(live=new_live, finished=list(snapshot.finished))

    if "MvLiveTask" in op_kind:
        payload = op_kind["MvLiveTask"]
        from_index = next((i for i, t in enumerate(snapshot.live) if t.id == payload["id_del"]), -1)
        to_index = next((i for i, t in enumerate(snapshot.live) if t.id == payload["id_ins"]), -1)
        if from_index == -1 or to_index == -1 or from_index == to_index:
            return snapshot
        new_live = list(snapshot.live)
        task = new_live.pop(from_index)
        new_live.insert(to_index, task)
        return StateSnapshot(live=new_live, finished=list(snapshot.finished))

    if "RevLiveTask" in op_kind:
        payload = op_kind["RevLiveTask"]
        index1 = next((i for i, t in enumerate(snapshot.live) if t.id == payload["id1"]), -1)
        index2 = next((i for i, t in enumerate(snapshot.live) if t.id == payload["id2"]), -1)
        if index1 == -1 or index2 == -1:
            return snapshot
        start = min(index1, index2)
        end = max(index1, index2)
        new_live = list(snapshot.live)
        section = list(reversed(new_live[start : end + 1]))
        new_live[start : end + 1] = section
        return StateSnapshot(live=new_live, finished=list(snapshot.finished))

    if "FinishLiveTask" in op_kind:
        payload = op_kind["FinishLiveTask"]
        task_index = next((i for i, t in enumerate(snapshot.live) if t.id == payload["id"]), -1)
        if task_index == -1:
            return snapshot
        task = snapshot.live[task_index]
        new_live = [t for t in snapshot.live if t.id != payload["id"]]
        status: TaskStatus = payload["status"]
        finished_task = FinishedTask(
            id=task.id,
            value=task.value,
            deadline=task.deadline,
            managed=task.managed,
            status=status,
        )
        return StateSnapshot(live=new_live, finished=[finished_task, *snapshot.finished])

    return snapshot

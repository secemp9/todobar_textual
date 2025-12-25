from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Literal


TaskStatus = Literal["Succeeded", "Failed", "Obsoleted"]


@dataclass(frozen=True)
class LiveTask:
    id: str
    value: str
    deadline: Optional[int]  # Unix timestamp, seconds
    managed: Optional[str] = None


@dataclass(frozen=True)
class FinishedTask(LiveTask):
    status: TaskStatus = "Succeeded"


@dataclass(frozen=True)
class StateSnapshot:
    live: List[LiveTask]
    finished: List[FinishedTask]


@dataclass(frozen=True)
class Preferences:
    # whether to periodically speak aloud the topmost live task
    vocal_enabled: bool = False
    # delay in seconds between vocal reminders
    vocal_frequency: int = 300  # 5 minutes


@dataclass(frozen=True)
class TodosCache:
    preferences: Preferences
    server_api_url: str
    api_key: str


class ViewType(str, Enum):
    LIVE = "live"
    FINISHED = "finished"
    OVERDUE = "overdue"
    PREFERENCES = "preferences"

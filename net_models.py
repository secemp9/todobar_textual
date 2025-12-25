from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .models import FinishedTask, LiveTask, StateSnapshot, TaskStatus


@dataclass(frozen=True)
class ServerInfo:
    service: str
    version_major: int
    version_minor: int
    version_rev: int
    app_pub_origin: str
    auth_pub_api_href: str
    auth_authenticator_href: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ServerInfo":
        if not isinstance(data, dict):
            raise ValueError("ServerInfo must be an object")

        service = _expect_str(data, "service")
        version_major = _expect_int(data, "versionMajor")
        version_minor = _expect_int(data, "versionMinor")
        version_rev = _expect_int(data, "versionRev")
        app_pub_origin = _expect_url(data, "appPubOrigin")
        auth_pub_api_href = _expect_url(data, "authPubApiHref")
        auth_authenticator_href = _expect_url(data, "authAuthenticatorHref")

        return cls(
            service=service,
            version_major=version_major,
            version_minor=version_minor,
            version_rev=version_rev,
            app_pub_origin=app_pub_origin,
            auth_pub_api_href=auth_pub_api_href,
            auth_authenticator_href=auth_authenticator_href,
        )


@dataclass(frozen=True)
class WebsocketOp:
    alleged_time: int
    kind: Dict[str, Any]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WebsocketOp":
        if not isinstance(data, dict):
            raise ValueError("WebsocketOp must be an object")

        alleged_time_raw = data.get("alleged_time")
        if not isinstance(alleged_time_raw, (int, float)):
            raise ValueError("Invalid alleged_time in WebsocketOp")
        alleged_time = int(alleged_time_raw)

        kind_raw = data.get("kind")
        kind = parse_websocket_op_kind(kind_raw)

        return cls(alleged_time=alleged_time, kind=kind)


_ALLOWED_OPS = {
    "OverwriteState",
    "InsLiveTask",
    "RestoreFinishedTask",
    "EditLiveTask",
    "DelLiveTask",
    "MvLiveTask",
    "RevLiveTask",
    "FinishLiveTask",
}


def parse_websocket_op_kind(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("WebsocketOp kind must be an object")

    present = [key for key in data.keys() if key in _ALLOWED_OPS and data.get(key) is not None]
    if len(present) != 1:
        raise ValueError("WebsocketOp kind must contain exactly one operation")

    op_key = present[0]
    payload = data.get(op_key)

    if op_key == "OverwriteState":
        return {"OverwriteState": _parse_state_snapshot(payload)}

    if op_key == "InsLiveTask":
        return {"InsLiveTask": _parse_live_task_payload(payload)}

    if op_key == "RestoreFinishedTask":
        return {"RestoreFinishedTask": _parse_id_only_payload(payload)}

    if op_key == "EditLiveTask":
        return {"EditLiveTask": _parse_live_task_payload(payload)}

    if op_key == "DelLiveTask":
        return {"DelLiveTask": _parse_id_only_payload(payload)}

    if op_key == "MvLiveTask":
        return {"MvLiveTask": _parse_move_payload(payload)}

    if op_key == "RevLiveTask":
        return {"RevLiveTask": _parse_reverse_payload(payload)}

    if op_key == "FinishLiveTask":
        return {"FinishLiveTask": _parse_finish_payload(payload)}

    raise ValueError(f"Unsupported WebsocketOp kind: {op_key}")


def _parse_state_snapshot(data: Any) -> StateSnapshot:
    if not isinstance(data, dict):
        raise ValueError("OverwriteState must be an object")

    live_raw = data.get("live")
    finished_raw = data.get("finished")
    if not isinstance(live_raw, list) or not isinstance(finished_raw, list):
        raise ValueError("OverwriteState requires live and finished arrays")

    live = [_parse_live_task(item) for item in live_raw]
    finished = [_parse_finished_task(item) for item in finished_raw]

    return StateSnapshot(live=live, finished=finished)


def _parse_live_task_payload(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Live task payload must be an object")
    task_id = _expect_str(data, "id")
    value = _expect_str(data, "value")
    deadline = _expect_nullable_int(data, "deadline")
    return {"id": task_id, "value": value, "deadline": deadline}


def _parse_id_only_payload(data: Any) -> Dict[str, str]:
    if not isinstance(data, dict):
        raise ValueError("Payload must be an object")
    return {"id": _expect_str(data, "id")}


def _parse_move_payload(data: Any) -> Dict[str, str]:
    if not isinstance(data, dict):
        raise ValueError("Move payload must be an object")
    return {"id_del": _expect_str(data, "id_del"), "id_ins": _expect_str(data, "id_ins")}


def _parse_reverse_payload(data: Any) -> Dict[str, str]:
    if not isinstance(data, dict):
        raise ValueError("Reverse payload must be an object")
    return {"id1": _expect_str(data, "id1"), "id2": _expect_str(data, "id2")}


def _parse_finish_payload(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Finish payload must be an object")
    task_id = _expect_str(data, "id")
    status = _expect_str(data, "status")
    if status not in {"Succeeded", "Failed", "Obsoleted"}:
        raise ValueError("Invalid status in FinishLiveTask")
    return {"id": task_id, "status": status}


def _parse_live_task(data: Any) -> LiveTask:
    if not isinstance(data, dict):
        raise ValueError("LiveTask must be an object")

    task_id = _expect_str(data, "id")
    value = _expect_str(data, "value")
    deadline = _expect_nullable_int(data, "deadline")
    managed = _expect_nullable_str(data, "managed")
    return LiveTask(id=task_id, value=value, deadline=deadline, managed=managed)


def _parse_finished_task(data: Any) -> FinishedTask:
    if not isinstance(data, dict):
        raise ValueError("FinishedTask must be an object")

    task_id = _expect_str(data, "id")
    value = _expect_str(data, "value")
    deadline = _expect_nullable_int(data, "deadline")
    managed = _expect_nullable_str(data, "managed")

    status_raw = _expect_str(data, "status")
    if status_raw not in {"Succeeded", "Failed", "Obsoleted"}:
        raise ValueError("Invalid status in FinishedTask")

    status: TaskStatus = status_raw  # type: ignore[assignment]
    return FinishedTask(id=task_id, value=value, deadline=deadline, managed=managed, status=status)


def _expect_str(data: Dict[str, Any], key: str) -> str:
    if key not in data:
        raise ValueError(f"Invalid or missing {key}")
    value = data[key]
    if not isinstance(value, str):
        raise ValueError(f"Invalid or missing {key}")
    return value


def _expect_int(data: Dict[str, Any], key: str) -> int:
    if key not in data:
        raise ValueError(f"Invalid or missing {key}")
    value = data[key]
    if not isinstance(value, (int, float)):
        raise ValueError(f"Invalid or missing {key}")
    return int(value)

def _expect_nullable_int(data: Dict[str, Any], key: str) -> Optional[int]:
    if key not in data:
        raise ValueError(f"Invalid or missing {key}")
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise ValueError(f"Invalid {key}")
    return int(value)


def _expect_nullable_str(data: Dict[str, Any], key: str) -> Optional[str]:
    if key not in data:
        raise ValueError(f"Invalid or missing {key}")
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Invalid {key}")
    return value


def _expect_url(data: Dict[str, Any], key: str) -> str:
    value = _expect_str(data, key)
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid or missing {key}")
    return value

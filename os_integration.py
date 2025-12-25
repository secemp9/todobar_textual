from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional


def set_focus_state(focused: bool) -> bool:
    window_id = _get_window_id()
    if window_id is None:
        return False

    helper = _find_helper()
    if helper is None:
        return False

    cmd = "focus" if focused else "unfocus"
    return _run_helper(helper, [cmd, str(window_id)])


def set_expand_state(expanded: bool) -> bool:
    window_id = _get_window_id()
    if window_id is None:
        return False

    helper = _find_helper()
    if helper is None:
        return False

    width = _env_int("STATUSBAR_WINDOW_WIDTH", 500)
    if expanded:
        height = _env_int("STATUSBAR_WINDOW_HEIGHT_EXPANDED", 500)
    else:
        height = _env_int("STATUSBAR_WINDOW_HEIGHT_COLLAPSED", 80)

    return _run_helper(helper, ["resize", str(window_id), str(width), str(height)])


def request_user_attention() -> bool:
    window_id = _get_window_id()
    if window_id is None:
        return False

    helper = _find_helper()
    if helper is None:
        return False

    return _run_helper(helper, ["request-attention", str(window_id)])


def maybe_dock_window() -> bool:
    height = _env_int("STATUSBAR_DOCK_HEIGHT", 500)

    window_id = _get_window_id()
    if window_id is None:
        return False

    helper = _find_helper()
    if helper is None:
        return False

    return _run_helper(helper, ["dock", str(window_id), str(height)])


def _run_helper(helper: str, args: list[str]) -> bool:
    try:
        subprocess.run([helper, *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _find_helper() -> Optional[str]:
    env_path = os.environ.get("STATUSBAR_WM_HELPER")
    if env_path and Path(env_path).exists():
        return env_path

    in_path = shutil.which("statusbar_wm_helper")
    if in_path:
        return in_path

    repo_root = Path(__file__).resolve().parent.parent
    candidate = repo_root / "statusbar_wm_helper" / "target" / "release" / "statusbar_wm_helper"
    if candidate.exists():
        return str(candidate)

    return None


def _get_window_id() -> Optional[int]:
    env = os.environ.get("STATUSBAR_WINDOW_ID")
    if env:
        return _parse_window_id(env)

    env_window = os.environ.get("WINDOWID")
    if env_window:
        return _parse_window_id(env_window)

    # Best-effort fallbacks
    window_id = _from_xdotool()
    if window_id is not None:
        return window_id

    return _from_xprop()


def _parse_window_id(value: str) -> Optional[int]:
    value = value.strip()
    if not value:
        return None
    try:
        if value.lower().startswith("0x"):
            return int(value, 16)
        return int(value, 10)
    except ValueError:
        return None


def _from_xdotool() -> Optional[int]:
    try:
        output = subprocess.check_output(["xdotool", "getactivewindow"], stderr=subprocess.DEVNULL)
    except Exception:
        return None
    return _parse_window_id(output.decode("utf-8").strip())


def _from_xprop() -> Optional[int]:
    try:
        output = subprocess.check_output(["xprop", "-root", "_NET_ACTIVE_WINDOW"], stderr=subprocess.DEVNULL)
    except Exception:
        return None

    text = output.decode("utf-8").strip()
    if "#" in text:
        candidate = text.split("#", 1)[1].strip()
        return _parse_window_id(candidate)
    return None

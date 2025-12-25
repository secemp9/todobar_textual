from __future__ import annotations

import asyncio
from urllib.parse import urlparse, urlunparse


class WebsocketClient:
    def __init__(self, app: "Statusbar2App", *, api_key: str, server_api_url: str) -> None:
        self.app = app
        self.api_key = api_key
        self.server_api_url = server_api_url
        self._ws = None
        self._open = False
        self._closing = False
        self._outbox: asyncio.Queue[str] = asyncio.Queue()

    @property
    def is_open(self) -> bool:
        return bool(self._open and self._ws is not None and not getattr(self._ws, "closed", False))

    def send(self, message: str) -> bool:
        if not self.is_open:
            return False
        try:
            self._outbox.put_nowait(message)
            return True
        except asyncio.QueueFull:
            return False

    async def close(self, reason: str = "Client closing") -> None:
        self._closing = True
        ws = self._ws
        if ws is None:
            return
        if getattr(ws, "closed", False):
            return
        try:
            await ws.close(code=1000, reason=reason)
        except Exception:
            pass

    async def run(self) -> None:
        try:
            import websockets  # type: ignore
            from websockets.exceptions import ConnectionClosed  # type: ignore
        except Exception as e:  # pragma: no cover - depends on env
            self.app.on_ws_error(e)
            self.app.on_ws_close("websockets package not available")
            return

        ws_url = build_ws_url(self.server_api_url, self.api_key)

        try:
            async with websockets.connect(ws_url) as ws:  # type: ignore[attr-defined]
                self._ws = ws
                self._open = True
                self.app.on_ws_open(self.api_key)

                receiver = asyncio.create_task(self._recv_loop(ws))
                sender = asyncio.create_task(self._send_loop(ws))
                done, pending = await asyncio.wait(
                    {receiver, sender},
                    return_when=asyncio.FIRST_EXCEPTION,
                )

                for task in pending:
                    task.cancel()

                for task in done:
                    exc = task.exception()
                    if exc:
                        raise exc

                reason = getattr(ws, "close_reason", "") or "Connection closed"
                self.app.on_ws_close(reason)
        except Exception as e:
            if isinstance(e, ConnectionClosed):
                reason = e.reason or "Connection closed"
                if not self._closing and e.code not in (1000, 1001):
                    self.app.on_ws_error(e)
                self.app.on_ws_close(reason)
            else:
                if self._closing:
                    reason = "Connection closed"
                else:
                    reason = str(e) or "Connection failed"
                    self.app.on_ws_error(e)
                self.app.on_ws_close(reason)
        finally:
            self._open = False
            self._ws = None

    async def _recv_loop(self, ws) -> None:
        async for message in ws:
            self.app.on_ws_message(message)

    async def _send_loop(self, ws) -> None:
        while True:
            message = await self._outbox.get()
            await ws.send(message)


def build_ws_url(server_api_url: str, api_key: str) -> str:
    parsed = urlparse(server_api_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path or "/"
    if not path.endswith("/"):
        path += "/"
    path = f"{path}ws/task_updates"
    return urlunparse(
        (
            scheme,
            parsed.netloc,
            path,
            parsed.params,
            f"api_key={api_key}",
            parsed.fragment,
        )
    )

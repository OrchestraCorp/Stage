import asyncio
import traceback

import pytest
from starlette.requests import ClientDisconnect
from starlette.types import Message, Receive, Scope, Send

from app.core.middleware import RequestIdMiddleware, ResponseStreamError


async def receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


@pytest.mark.parametrize("failure", (asyncio.CancelledError, ClientDisconnect))
async def test_cancellation_and_client_disconnect_are_not_swallowed(failure: type[BaseException]) -> None:
    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        raise failure()

    async def send(message: Message) -> None:
        pytest.fail("Disconnected/cancelled requests must not send a fabricated response")

    with pytest.raises(failure):
        await RequestIdMiddleware(application)({"type": "http", "headers": []}, receive, send)


async def test_failed_stream_does_not_restart_response_or_expose_exception() -> None:
    messages: list[Message] = []

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("synthetic-private-stream-data")

    async def send(message: Message) -> None:
        messages.append(message)

    with pytest.raises(ResponseStreamError) as failure:
        await RequestIdMiddleware(application)({"type": "http", "headers": []}, receive, send)
    assert len(messages) == 1
    assert messages[0]["type"] == "http.response.start"
    assert "synthetic-private-stream-data" not in "".join(traceback.format_exception(failure.value))

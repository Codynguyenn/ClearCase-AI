"""Reject over-size uploads while the request body is still arriving.

Why this exists: FastAPI parses a multipart upload *before* our route runs, so the size check in
``validate_pdf`` only fires after the whole body has been received (measured earlier: a 120 MB body was
accepted in full against a 20 MB limit). This middleware looks at the body as it streams in and stops
early, so the server never stores more than the limit plus a little multipart framing.

Two cases are covered:

* an honest ``Content-Length`` header above the limit is refused immediately, before reading anything;
* a body with no header (chunked transfer) or a lying header is counted as it arrives and cut off at the limit.

It is plain ASGI (not Starlette's ``BaseHTTPMiddleware``) because it has to wrap ``receive``/``send`` itself.
The exact limit and its friendly message stay in the services (``validate_pdf``, ``PolicyService.ingest``);
this layer is only a resource cap, so it allows a little headroom for multipart boundaries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Multipart boundaries, part headers and small form fields add a few hundred bytes; 64 KiB is generous.
MULTIPART_HEADROOM_BYTES = 64 * 1024


@dataclass(frozen=True)
class UploadRule:
    path: str  # exact request path, e.g. "/api/documents"
    max_file_bytes: int
    code: str
    noun: str  # used in the message: "PDF", "policy file"

    @property
    def max_body_bytes(self) -> int:
        return self.max_file_bytes + MULTIPART_HEADROOM_BYTES

    @property
    def message(self) -> str:
        mb = self.max_file_bytes / (1024 * 1024)
        size = f"{mb:g} MB" if mb >= 1 else f"{self.max_file_bytes // 1024} KB"
        return f"That {self.noun} is larger than the {size} limit."


class _BodyTooLarge(Exception):
    """Raised from the wrapped ``receive`` to stop the application reading further."""


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                parsed = int(value)
            except ValueError:
                return None
            return parsed if parsed >= 0 else None
    return None


async def _send_413(send: Send, rule: UploadRule) -> None:
    body = json.dumps({"error": {"code": rule.code, "message": rule.message}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class UploadSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, rules: list[UploadRule]) -> None:
        self.app = app
        self._rules = {rule.path: rule for rule in rules}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        rule = self._rules.get(scope.get("path", "")) if scope["type"] == "http" and scope["method"] == "POST" else None
        if rule is None:
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > rule.max_body_bytes:
            await _send_413(send, rule)
            return

        received = 0
        exceeded = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > rule.max_body_bytes:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            # When we cut the body off, the framework reports its own "could not parse the body" error.
            # Swap it for a plain 413 so the caller learns the real reason.
            nonlocal response_started
            if exceeded:
                if not response_started:
                    response_started = True
                    await _send_413(send, rule)
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            if not response_started:
                await _send_413(send, rule)

"""The upload size cap is enforced while the body streams in, not after it has all arrived."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

from fastapi.testclient import TestClient

from app.api.upload_limit import MULTIPART_HEADROOM_BYTES, UploadRule, UploadSizeLimitMiddleware
from app.db import DOCUMENTS
from tests.conftest import make_text_pdf

RULE = UploadRule("/api/documents", 1000, "file_too_large", "PDF")
LIMIT = RULE.max_body_bytes  # the file limit plus multipart headroom


def run_asgi(app, *, path="/api/documents", method="POST", headers=None, chunks=()):
    """Drive an ASGI app by hand and return (status, json_body, bytes_read_by_app)."""
    sent: list[dict] = []
    queue = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    queue = queue or [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": method, "path": path, "headers": headers or []}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body) if body else None


def make_inner(read_all=True):
    """An app that reads the whole body like a form parser would, then answers 200."""
    state = {"called": False, "read": 0}

    async def inner(scope, receive, send):
        state["called"] = True
        try:
            while True:
                message = await receive()
                state["read"] += len(message.get("body", b""))
                if not message.get("more_body"):
                    break
        except Exception:  # noqa: BLE001 - mimic a framework that turns any parse failure into a 400
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b'{"detail":"There was an error parsing the body"}'})
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    return inner, state


def wrapped():
    inner, state = make_inner()
    return UploadSizeLimitMiddleware(inner, rules=[RULE]), state


def test_declared_length_over_limit_is_refused_without_calling_the_app():
    app, state = wrapped()
    status, body = run_asgi(app, headers=[(b"content-length", str(LIMIT + 1).encode())], chunks=[b"x"])
    assert status == 413
    assert body["error"]["code"] == "file_too_large"
    assert "limit" in body["error"]["message"]
    assert state["called"] is False


def test_body_without_a_length_header_is_cut_off_at_the_limit():
    app, state = wrapped()
    chunk = b"x" * 4096
    chunks = [chunk] * (LIMIT // 4096 + 20)  # well over the limit, no Content-Length
    status, body = run_asgi(app, chunks=chunks)
    assert status == 413 and body["error"]["code"] == "file_too_large"
    # The app was stopped near the limit, not after all ~70 KB: at most one chunk past it.
    assert state["read"] <= LIMIT + len(chunk)


def test_a_lying_content_length_is_still_caught():
    app, state = wrapped()
    status, body = run_asgi(app, headers=[(b"content-length", b"10")], chunks=[b"x" * 5000] * 20)
    assert status == 413 and body["error"]["code"] == "file_too_large"


def test_the_frameworks_own_400_is_replaced_by_the_413():
    # make_inner() answers 400 when its receive() raises; the caller must see 413 instead.
    app, _ = wrapped()
    status, body = run_asgi(app, chunks=[b"x" * 5000] * 20)
    assert status == 413 and "detail" not in body


def test_a_body_within_the_limit_passes_through_untouched():
    app, state = wrapped()
    status, body = run_asgi(app, headers=[(b"content-length", b"1500")], chunks=[b"x" * 500] * 3)
    assert status == 200 and body == {"ok": True}
    assert state["read"] == 1500


def test_headroom_allows_multipart_framing_around_a_file_at_the_limit():
    app, _ = wrapped()
    size = RULE.max_file_bytes + MULTIPART_HEADROOM_BYTES - 1
    status, _ = run_asgi(app, headers=[(b"content-length", str(size).encode())], chunks=[b"x" * size])
    assert status == 200


def test_only_post_to_a_listed_path_is_limited():
    app, state = wrapped()
    assert run_asgi(app, path="/api/other", chunks=[b"x" * (LIMIT + 5000)])[0] == 200
    assert run_asgi(app, method="GET", chunks=[b"x" * (LIMIT + 5000)])[0] == 200
    assert state["called"] is True


def test_the_message_names_the_limit_in_mb_or_kb():
    assert "2 MB" in UploadRule("/p", 2 * 1024 * 1024, "c", "PDF").message
    assert "200 KB" in UploadRule("/p", 200 * 1024, "c", "policy file").message


# ----------------------------------------------------------------------- through the real app
def _chunked(total: int, piece: int = 64 * 1024) -> Iterator[bytes]:
    # A generator body is sent with chunked transfer encoding, i.e. with no Content-Length header.
    sent = 0
    while sent < total:
        yield b"A" * min(piece, total - sent)
        sent += piece


def test_oversized_pdf_with_content_length_is_refused_and_not_stored(client: TestClient, mongo_client, test_settings):
    big = make_text_pdf([["x"]]) + b"\n%" + b"A" * (3 * 1024 * 1024)  # test limit is 2 MB
    r = client.post("/api/documents", files={"file": ("big.pdf", big, "application/pdf")})
    assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"
    assert mongo_client[test_settings.mongodb_db][DOCUMENTS].count_documents({}) == 0


def test_oversized_chunked_upload_is_refused_and_not_stored(client: TestClient, mongo_client, test_settings):
    r = client.post(
        "/api/documents",
        content=_chunked(4 * 1024 * 1024),
        headers={"content-type": "multipart/form-data; boundary=x"},
    )
    assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"
    assert mongo_client[test_settings.mongodb_db][DOCUMENTS].count_documents({}) == 0


def test_the_413_carries_cors_headers_so_the_browser_can_read_it(client: TestClient):
    big = b"%PDF-" + b"A" * (3 * 1024 * 1024)
    r = client.post(
        "/api/documents",
        files={"file": ("big.pdf", big, "application/pdf")},
        headers={"Origin": "http://localhost:5173"},
    )
    assert r.status_code == 413
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_a_normal_upload_still_works(client: TestClient):
    r = client.post("/api/documents", files={"file": ("ok.pdf", make_text_pdf([["hello"]]), "application/pdf")})
    assert r.status_code == 202

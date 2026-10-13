"""Document upload, listing, page text, original file download, retry and delete."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Path, Query, UploadFile, status
from fastapi.responses import StreamingResponse

from app.api.deps import DocService, OwnerId
from app.models.documents import (
    DeleteResponse,
    DocumentListResponse,
    DocumentRecord,
    PageSummary,
    PageText,
)
from app.models.search import SearchResult
from app.services.search import MAX_QUERY_CHARS, search_pages

router = APIRouter(prefix="/documents", tags=["documents"])

DocumentId = Annotated[str, Path(min_length=1, max_length=64)]


@router.post("", response_model=DocumentRecord, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    background: BackgroundTasks,
    service: DocService,
    owner_id: OwnerId,
    file: Annotated[UploadFile, File(description="A text-based PDF")],
) -> DocumentRecord:
    """Store the PDF and schedule extraction.

    Returns 202 immediately with ``status: uploaded``; the client polls
    ``GET /documents/{id}`` until the status is ready/unsupported/error.
    Extraction runs as a background task in this process, which is enough for a
    local single-user MVP and keeps the state machine identical to a future queue.
    """
    data = await file.read()
    record = service.upload(owner_id=owner_id, filename=file.filename, data=data)
    background.add_task(service.process, record.id, owner_id=owner_id)
    return record


@router.get("", response_model=DocumentListResponse)
def list_documents(
    service: DocService,
    owner_id: OwnerId,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> DocumentListResponse:
    items = service.documents.list(owner_id=owner_id, limit=limit)
    return DocumentListResponse(items=items, total=service.documents.count(owner_id=owner_id))


@router.get("/{document_id}", response_model=DocumentRecord)
def get_document(document_id: DocumentId, service: DocService, owner_id: OwnerId) -> DocumentRecord:
    return service.get(document_id, owner_id=owner_id)


@router.post("/{document_id}/reprocess", response_model=DocumentRecord)
def reprocess_document(document_id: DocumentId, service: DocService, owner_id: OwnerId) -> DocumentRecord:
    """Explicit retry path. Runs synchronously so the caller sees the final state."""
    return service.process(document_id, owner_id=owner_id)


@router.get("/{document_id}/pages", response_model=list[PageSummary])
def list_pages(document_id: DocumentId, service: DocService, owner_id: OwnerId) -> list[PageSummary]:
    service.get(document_id, owner_id=owner_id)  # 404 if not owned
    return service.pages.list_summaries(document_id, owner_id=owner_id)


@router.get("/{document_id}/pages/{page_number}", response_model=PageText)
def get_page(
    document_id: DocumentId,
    page_number: Annotated[int, Path(ge=1)],
    service: DocService,
    owner_id: OwnerId,
) -> PageText:
    service.get(document_id, owner_id=owner_id)
    return service.pages.get_page(document_id, page_number, owner_id=owner_id)


@router.get("/{document_id}/search", response_model=SearchResult)
def search_document(
    document_id: DocumentId,
    service: DocService,
    owner_id: OwnerId,
    q: Annotated[str, Query(min_length=1, max_length=MAX_QUERY_CHARS)],
    case_sensitive: bool = False,
    whole_word: bool = False,
) -> SearchResult:
    """Literal search across all extracted pages, with rectangles per occurrence."""
    record = service.get(document_id, owner_id=owner_id)
    pages = service.pages.list_full(document_id, owner_id=owner_id)
    return search_pages(
        pages,
        q,
        document_id=document_id,
        document_version=record.version,
        case_sensitive=case_sensitive,
        whole_word=whole_word,
    )


@router.get("/{document_id}/file")
def download_original(document_id: DocumentId, service: DocService, owner_id: OwnerId) -> StreamingResponse:
    """Stream the untouched original PDF for the viewer."""
    record = service.get(document_id, owner_id=owner_id)
    file_id = service.original_file_id(document_id, owner_id=owner_id)
    stream = service.files.open_stream(file_id, owner_id=owner_id)

    def iterate():
        try:
            while chunk := stream.read(256 * 1024):
                yield chunk
        finally:
            stream.close()

    # The filename was sanitised to a safe ASCII subset at upload time.
    headers = {
        "Content-Disposition": f'inline; filename="{record.filename}"',
        "Content-Length": str(record.size_bytes),
        "Cache-Control": "private, max-age=3600",
    }
    return StreamingResponse(iterate(), media_type="application/pdf", headers=headers)


@router.delete("/{document_id}", response_model=DeleteResponse)
def delete_document(document_id: DocumentId, service: DocService, owner_id: OwnerId) -> DeleteResponse:
    counts = service.delete(document_id, owner_id=owner_id)
    return DeleteResponse(document_id=document_id, deleted=counts)

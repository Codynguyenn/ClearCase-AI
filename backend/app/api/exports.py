"""Export endpoints (reviewer-facing): authorize, export, list, download."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, status
from fastapi.responses import StreamingResponse

from app.api.deps import ExportSvc, OwnerId
from app.models.findings import ExportAuthorization, ExportRecord, ExportRequest

router = APIRouter(prefix="/documents/{document_id}", tags=["export"])

DocumentId = Annotated[str, Path(min_length=1, max_length=64)]
ExportId = Annotated[str, Path(min_length=1, max_length=40)]


@router.post("/export-authorizations", response_model=ExportAuthorization, status_code=status.HTTP_201_CREATED)
def authorize_export(document_id: DocumentId, service: ExportSvc, owner_id: OwnerId) -> ExportAuthorization:
    """The reviewer's explicit go-ahead. 409 if anything is unresolved."""
    return service.issue_authorization(document_id, owner_id)


@router.post("/exports", response_model=ExportRecord, status_code=status.HTTP_201_CREATED)
def create_export(document_id: DocumentId, body: ExportRequest, service: ExportSvc, owner_id: OwnerId) -> ExportRecord:
    """Redact, verify, and (only if verification passes) store the output.

    Re-sending the same authorization returns the same export (idempotent).
    """
    return service.export(document_id, owner_id, body)


@router.get("/exports", response_model=list[ExportRecord])
def list_exports(document_id: DocumentId, service: ExportSvc, owner_id: OwnerId) -> list[ExportRecord]:
    return service.list_exports(document_id, owner_id)


@router.get("/exports/{export_id}", response_model=ExportRecord)
def get_export(document_id: DocumentId, export_id: ExportId, service: ExportSvc, owner_id: OwnerId) -> ExportRecord:
    return service.get_export(document_id, owner_id, export_id)


@router.get("/exports/{export_id}/file")
def download_export(
    document_id: DocumentId, export_id: ExportId, service: ExportSvc, owner_id: OwnerId
) -> StreamingResponse:
    record, stream = service.open_download(document_id, owner_id, export_id)

    def iterate():
        try:
            while chunk := stream.read(256 * 1024):
                yield chunk
        finally:
            stream.close()

    headers = {
        "Content-Disposition": f'attachment; filename="{record.filename}"',
        "Content-Length": str(record.output_size_bytes),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(iterate(), media_type="application/pdf", headers=headers)

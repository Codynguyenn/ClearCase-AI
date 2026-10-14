"""Policy library endpoints: upload, versions, passages (citations) and policy search."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Path as PathParam, UploadFile, status
from pydantic import BaseModel

from app.api.deps import OwnerId, PolicySvc, Retriever
from app.config import Settings, get_settings
from app.models.policies import (
    PolicyPassage,
    PolicyRecord,
    PolicySearchRequest,
    ResolveRequest,
    ResolveResult,
    RetrievalResult,
)
from fastapi import Depends

router = APIRouter(prefix="/policies", tags=["policies"])

PolicyId = Annotated[str, PathParam(min_length=1, max_length=64)]
Version = Annotated[str, PathParam(min_length=1, max_length=20)]
CitationId = Annotated[str, PathParam(min_length=1, max_length=120)]


class SeedRequest(BaseModel):
    reset: bool = False


class DeleteResponse(BaseModel):
    policies: int
    chunks: int


@router.get("", response_model=list[PolicyRecord])
def list_policies(service: PolicySvc, owner_id: OwnerId) -> list[PolicyRecord]:
    return service.list_policies(owner_id)


@router.post("", response_model=PolicyRecord, status_code=status.HTTP_201_CREATED)
async def upload_policy(
    service: PolicySvc,
    owner_id: OwnerId,
    file: Annotated[UploadFile, File(description="A Markdown (.md) or plain-text (.txt) policy")],
    policy_id: Annotated[str | None, Form(max_length=64)] = None,
    version: Annotated[str | None, Form(max_length=20)] = None,
    title: Annotated[str | None, Form(max_length=200)] = None,
) -> PolicyRecord:
    """Store, chunk and embed a policy; the new version becomes the active one."""
    data = await file.read()
    return service.ingest(owner_id, filename=file.filename, data=data, policy_id=policy_id, version=version, title=title)


@router.post("/seed-samples", response_model=list[PolicyRecord])
def seed_samples(
    body: SeedRequest,
    service: PolicySvc,
    owner_id: OwnerId,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[PolicyRecord]:
    """Load the fictional example policies shipped with the project (idempotent)."""
    return service.seed_samples(owner_id, Path(settings.sample_data_dir) / "policies", reset=body.reset)


@router.post("/reindex", response_model=list[PolicyRecord])
def reindex_all(service: PolicySvc, owner_id: OwnerId) -> list[PolicyRecord]:
    """Embed again anything missing or embedded with a different model revision."""
    service.reindex_all(owner_id)
    return service.list_policies(owner_id)


@router.post("/search", response_model=RetrievalResult)
def search_policies(body: PolicySearchRequest, retriever: Retriever, owner_id: OwnerId) -> RetrievalResult:
    """Ask the active policies a question. Returns passages above the similarity cutoff, or says there are none."""
    return retriever.retrieve(owner_id, body.query, top_k=body.top_k)


@router.post("/passages/resolve", response_model=ResolveResult)
def resolve_passages(body: ResolveRequest, service: PolicySvc, owner_id: OwnerId) -> ResolveResult:
    """Look up citation ids; unknown ids are reported as missing and never shown as citations."""
    return service.resolve(owner_id, body.citation_ids)


@router.get("/passages/{citation_id}", response_model=PolicyPassage)
def get_passage(citation_id: CitationId, service: PolicySvc, owner_id: OwnerId) -> PolicyPassage:
    return service.passage(owner_id, citation_id)


@router.post("/{policy_id}/versions/{version}/activate", response_model=PolicyRecord)
def activate_version(policy_id: PolicyId, version: Version, service: PolicySvc, owner_id: OwnerId) -> PolicyRecord:
    return service.activate(owner_id, policy_id, version)


@router.post("/{policy_id}/versions/{version}/reindex", response_model=PolicyRecord)
def reindex_version(policy_id: PolicyId, version: Version, service: PolicySvc, owner_id: OwnerId) -> PolicyRecord:
    return service.reindex(owner_id, policy_id, version)


@router.delete("/{policy_id}/versions/{version}", response_model=DeleteResponse)
def delete_version(policy_id: PolicyId, version: Version, service: PolicySvc, owner_id: OwnerId) -> DeleteResponse:
    return DeleteResponse(**service.delete(owner_id, policy_id, version))


@router.delete("/{policy_id}", response_model=DeleteResponse)
def delete_policy(policy_id: PolicyId, service: PolicySvc, owner_id: OwnerId) -> DeleteResponse:
    return DeleteResponse(**service.delete(owner_id, policy_id, None))

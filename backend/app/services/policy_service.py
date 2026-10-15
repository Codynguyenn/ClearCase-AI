"""Policy ingestion, versioning, embedding and citation lookup.

The life of a policy
--------------------
1. **Ingest**: a Markdown/plain-text file is validated, split into chunks
   (``policy_chunking``) and stored with stable citation ids such as
   ``northwind-redaction-policy:1.0:3``.
2. **Embed**: each chunk is turned into a vector by the local embedding model and
   stored *with the model name and revision that produced it*. If the model is not
   available, the policy is still saved and honestly marked "unavailable".
3. **Activate**: exactly one version of a policy is "active"; only active versions
   are searched. Uploading a new version supersedes the old one; the old one stays
   readable (so old citations still resolve) and can be re-activated.
4. **Re-index**: if the model changes, or an earlier embedding step failed, chunks
   without a current embedding are embedded again. Stale vectors are never mixed
   with current ones (vectors from different models are not comparable).

Policies are a *reference corpus*. They are stored apart from the documents being
reviewed, and deleting a document never touches them.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import Mongo
from app.models.common import utcnow
from app.models.policies import (
    EmbeddingState,
    EmbeddingSummary,
    PolicyPassage,
    PolicyRecord,
    PolicyStatus,
    ResolveResult,
)
from app.repositories.policies import PolicyChunkRepository, PolicyRepository, citation_id, policy_key
from app.repositories.review import FindingRepository
from app.services.embeddings import Embedder, EmbeddingUnavailable, get_embedder
from app.services.errors import ConflictError, NotFoundError, ValidationFailed
from app.services.policy_chunking import (
    CHUNKER_VERSION,
    MAX_CHUNKS_PER_POLICY,
    chunk_markdown,
    first_heading,
    has_control_characters,
    is_valid_policy_id,
    is_valid_version,
    parse_front_matter,
    slugify,
)

logger = logging.getLogger(__name__)

ALLOWED_SUFFIXES = {".md", ".markdown", ".txt"}
MAX_TITLE_CHARS = 200
_CITATION_FORMAT = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}:[A-Za-z0-9][A-Za-z0-9._-]{0,19}:[1-9][0-9]{0,5}$")

EmbedderProvider = Callable[[], Embedder]


def embedding_text(policy_title: str, section: str, text: str) -> str:
    """What gets embedded: the passage plus its title and section, so a short clause keeps its context."""
    return f"{policy_title}. {section}.\n{text}"


def is_citation_format(value: str) -> bool:
    return bool(_CITATION_FORMAT.match(value))


def _safe_source(filename: str | None) -> str:
    name = Path(filename or "policy.md").name
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"/\\|?*')
    return (name or "policy.md")[:100]


class PolicyService:
    def __init__(self, mongo: Mongo, settings: Settings, embedder_provider: EmbedderProvider | None = None) -> None:
        self._settings = settings
        self.policies = PolicyRepository(mongo.db)
        self.chunks = PolicyChunkRepository(mongo.db)
        self.findings = FindingRepository(mongo.db)
        self._embedder_provider = embedder_provider or (lambda: get_embedder(settings))

    # ----------------------------------------------------------------- ingest
    def ingest(
        self,
        owner_id: str,
        *,
        filename: str | None,
        data: bytes,
        policy_id: str | None = None,
        version: str | None = None,
        title: str | None = None,
        fictional: bool | None = None,
        activate: bool = True,
    ) -> PolicyRecord:
        if len(data) > self._settings.max_policy_kb * 1024:
            raise ValidationFailed(
                f"Policy files may be at most {self._settings.max_policy_kb} KB.", code="policy_too_large", status_code=413
            )
        suffix = Path(filename or "policy.md").suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise ValidationFailed("Policies must be Markdown (.md) or plain text (.txt).", code="unsupported_policy_type")
        if b"\x00" in data:
            raise ValidationFailed("That file contains binary data, not text.", code="invalid_policy")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValidationFailed("Policies must be UTF-8 text.", code="invalid_policy") from exc
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if has_control_characters(text):
            raise ValidationFailed("That file contains control characters.", code="invalid_policy")

        meta, body = parse_front_matter(text)
        source = _safe_source(filename)
        pid = (policy_id or meta.get("policy_id") or slugify(Path(source).stem)).strip()
        ver = (version or meta.get("version") or "1").strip()
        policy_title = (title or meta.get("title") or first_heading(body) or pid).strip()
        if not is_valid_policy_id(pid):
            raise ValidationFailed(
                "policy_id must be 1-64 characters: lowercase letters, digits and hyphens.", code="invalid_policy_id"
            )
        if not is_valid_version(ver):
            raise ValidationFailed("version must be 1-20 letters, digits, dots, hyphens or underscores.", code="invalid_version")
        if not policy_title or len(policy_title) > MAX_TITLE_CHARS:
            raise ValidationFailed(f"A title of 1-{MAX_TITLE_CHARS} characters is required.", code="invalid_title")
        is_fictional = fictional if fictional is not None else ("fictional" in meta.get("status", "").lower())

        drafts = chunk_markdown(body)
        if not drafts:
            raise ValidationFailed("That file has no policy text to index.", code="empty_policy")
        if len(drafts) > MAX_CHUNKS_PER_POLICY:
            raise ValidationFailed(f"A policy may have at most {MAX_CHUNKS_PER_POLICY} passages.", code="policy_too_large")

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing = self.policies.find(owner_id, pid, ver)
        if existing is not None:
            if existing["content_hash"] != content_hash:
                raise ConflictError(
                    f"Version {ver} of '{pid}' already exists with different content. Upload the changes as a new version.",
                    code="policy_version_exists",
                )
            # Same file again: do not duplicate anything; just finish any unfinished work.
            self.reindex(owner_id, pid, ver)
            if activate and existing["status"] != PolicyStatus.ACTIVE.value:
                self.activate(owner_id, pid, ver)
            return self.get(owner_id, pid, ver)

        now = utcnow()
        key = policy_key(pid, ver)
        # Staged as "archived" so the database never sees two active versions, even for a moment.
        self.policies.insert(
            {
                "owner_id": owner_id,
                "policy_id": pid,
                "version": ver,
                "title": policy_title,
                "source": source,
                "is_fictional_example": is_fictional,
                "status": PolicyStatus.ARCHIVED.value,
                "content_hash": content_hash,
                "chunk_count": len(drafts),
                "word_count": sum(d.word_count for d in drafts),
                "truncated_chunks": 0,
                "chunker_version": CHUNKER_VERSION,
                "embedding_note": "Not embedded yet.",
                "created_at": now,
                "updated_at": now,
                "activated_at": None,
            }
        )
        self.chunks.insert_many(
            [
                {
                    "owner_id": owner_id,
                    "policy_id": pid,
                    "policy_version": ver,
                    "policy_key": key,
                    "citation_id": citation_id(pid, ver, d.ordinal),
                    "ordinal": d.ordinal,
                    "section": d.section,
                    "text": d.text,
                    "text_hash": hashlib.sha256(d.text.encode("utf-8")).hexdigest(),
                    "word_count": d.word_count,
                    "embedding": None,
                    "embedding_model": None,
                    "embedding_revision": None,
                    "embedded_at": None,
                    "truncated_by_model": False,
                    "created_at": now,
                }
                for d in drafts
            ]
        )
        self.reindex(owner_id, pid, ver)
        if activate:
            self.activate(owner_id, pid, ver)
        logger.info("Ingested policy %s v%s (%d chunks)", pid, ver, len(drafts))
        return self.get(owner_id, pid, ver)

    # --------------------------------------------------------------- embedding
    def reindex(self, owner_id: str, policy_id: str, version: str) -> PolicyRecord:
        """Embed every chunk that has no embedding from the *current* model revision."""
        doc = self.policies.get(owner_id, policy_id, version)
        chunks = self.chunks.list_for_policy(owner_id, policy_id, version)
        revision = self._settings.embedding_revision
        todo = [c for c in chunks if c.get("embedding_revision") != revision or not c.get("embedding")]
        if todo:
            try:
                embedder = self._embedder_provider()
                vectors = embedder.embed([embedding_text(doc["title"], c["section"], c["text"]) for c in todo])
                limit = embedder.max_tokens
                now = utcnow()
                for chunk, vector in zip(todo, vectors, strict=True):
                    self.chunks.set_embedding(
                        chunk["_id"], vector, model=embedder.model_id, revision=embedder.revision, embedded_at=now
                    )
                    over = bool(limit) and embedder.count_tokens(chunk["text"]) > limit
                    self.chunks.set_truncated(chunk["_id"], over)
                note: str | None = None
            except EmbeddingUnavailable as exc:
                note = str(exc)
                logger.warning("Policy %s v%s saved without embeddings: %s", policy_id, version, note)
            truncated = sum(
                1 for c in self.chunks.list_for_policy(owner_id, policy_id, version) if c.get("truncated_by_model")
            )
            self.policies.update_fields(
                owner_id, policy_id, version, {"embedding_note": note, "truncated_chunks": truncated, "updated_at": utcnow()}
            )
        else:
            self.policies.update_fields(owner_id, policy_id, version, {"embedding_note": None})
        return self.get(owner_id, policy_id, version)

    def reindex_all(self, owner_id: str) -> int:
        """Re-embed anything stale or missing for this owner (e.g. after the model revision changed)."""
        count = 0
        for doc in self.policies.list(owner_id):
            before = self.chunks.count_by_state(owner_id, policy_key(doc["policy_id"], doc["version"]), self._settings.embedding_revision)
            if before[1] or before[2]:
                self.reindex(owner_id, doc["policy_id"], doc["version"])
                count += 1
        return count

    # --------------------------------------------------------------- versions
    def activate(self, owner_id: str, policy_id: str, version: str) -> PolicyRecord:
        self.policies.get(owner_id, policy_id, version)  # 404 if missing
        now = utcnow()
        for other in self.policies.versions(owner_id, policy_id):
            if other["version"] != version and other["status"] == PolicyStatus.ACTIVE.value:
                self.policies.set_status(owner_id, policy_id, other["version"], PolicyStatus.ARCHIVED.value, now)
        self.policies.set_status(owner_id, policy_id, version, PolicyStatus.ACTIVE.value, now)
        return self.get(owner_id, policy_id, version)

    def delete(self, owner_id: str, policy_id: str, version: str | None = None) -> dict[str, int]:
        if not self.policies.versions(owner_id, policy_id):
            raise NotFoundError("Policy not found.", code="policy_not_found")
        chunks = self.chunks.delete_for_policy(owner_id, policy_id, version)
        policies = self.policies.delete(owner_id, policy_id, version)
        return {"policies": policies, "chunks": chunks}

    # ------------------------------------------------------------------- read
    def _summary(self, owner_id: str, doc: dict[str, Any]) -> EmbeddingSummary:
        revision = self._settings.embedding_revision
        current, stale, missing = self.chunks.count_by_state(owner_id, policy_key(doc["policy_id"], doc["version"]), revision)
        total = doc["chunk_count"]
        note = doc.get("embedding_note")
        if current == total:
            state = EmbeddingState.READY
        elif current == 0 and stale == 0:
            state = EmbeddingState.UNAVAILABLE if note else EmbeddingState.PENDING
        elif current == 0:
            state = EmbeddingState.STALE
        else:
            state = EmbeddingState.PARTIAL
        return EmbeddingSummary(
            state=state,
            model=self._settings.embedding_model,
            revision=revision,
            chunks_current=current,
            chunks_stale=stale,
            chunks_missing=missing,
            detail=note,
        )

    def _record(self, owner_id: str, doc: dict[str, Any]) -> PolicyRecord:
        return PolicyRecord(
            id=str(doc["_id"]),
            policy_id=doc["policy_id"],
            version=doc["version"],
            title=doc["title"],
            source=doc["source"],
            is_fictional_example=doc["is_fictional_example"],
            status=PolicyStatus(doc["status"]),
            content_hash=doc["content_hash"],
            chunk_count=doc["chunk_count"],
            word_count=doc["word_count"],
            truncated_chunks=doc.get("truncated_chunks", 0),
            # Versions saved before this field existed were split by the current rules (no change has happened yet).
            chunker_version=doc.get("chunker_version", 1),
            chunker_outdated=doc.get("chunker_version", 1) != CHUNKER_VERSION,
            cited_by_findings=self.findings.count_citing(
                owner_id, [citation_id(doc["policy_id"], doc["version"], n) for n in range(1, doc["chunk_count"] + 1)]
            ),
            embedding=self._summary(owner_id, doc),
            created_at=doc["created_at"],
            updated_at=doc["updated_at"],
            activated_at=doc.get("activated_at"),
        )

    def get(self, owner_id: str, policy_id: str, version: str) -> PolicyRecord:
        return self._record(owner_id, self.policies.get(owner_id, policy_id, version))

    def list_policies(self, owner_id: str) -> list[PolicyRecord]:
        return [self._record(owner_id, d) for d in self.policies.list(owner_id)]

    # --------------------------------------------------------------- citations
    def _passages(self, owner_id: str, chunk_docs: list[dict[str, Any]]) -> list[PolicyPassage]:
        policies = {(d["policy_id"], d["version"]): d for d in self.policies.list(owner_id)}
        out: list[PolicyPassage] = []
        for c in chunk_docs:
            parent = policies.get((c["policy_id"], c["policy_version"]))
            if parent is None:
                continue  # orphaned chunk: never presented as a citation
            out.append(passage_from_docs(c, parent))
        return out

    def passage(self, owner_id: str, cite: str) -> PolicyPassage:
        if not is_citation_format(cite):
            raise NotFoundError("Policy passage not found.", code="citation_not_found")
        found = self._passages(owner_id, [d for d in [self.chunks.get_by_citation(owner_id, cite)] if d])
        if not found:
            raise NotFoundError("Policy passage not found.", code="citation_not_found")
        return found[0]

    def resolve(self, owner_id: str, cites: list[str]) -> ResolveResult:
        """Look up citation ids. Anything malformed or unknown is reported as missing, never displayed."""
        wanted = list(dict.fromkeys(c for c in cites if is_citation_format(c)))
        passages = self._passages(owner_id, self.chunks.get_many_by_citation(owner_id, wanted)) if wanted else []
        by_id = {p.citation_id: p for p in passages}
        return ResolveResult(
            found=[by_id[c] for c in wanted if c in by_id],
            missing=[c for c in dict.fromkeys(cites) if c not in by_id],
        )

    def invalid_citations(self, owner_id: str, cites: list[str]) -> list[str]:
        return self.resolve(owner_id, cites).missing

    # ------------------------------------------------------------------- seed
    def seed_samples(self, owner_id: str, directory: Path, *, reset: bool = False) -> list[PolicyRecord]:
        """Load the fictional example policies shipped in ``sample_data/policies``."""
        files = sorted(directory.glob("*.md"))
        if not files:
            raise NotFoundError(f"No sample policies found in {directory}.", code="no_sample_policies")
        records: list[PolicyRecord] = []
        for path in files:
            data = path.read_bytes()
            if reset:
                meta, _ = parse_front_matter(data.decode("utf-8-sig"))
                pid = meta.get("policy_id") or slugify(path.stem)
                if self.policies.versions(owner_id, pid):
                    self.delete(owner_id, pid)
            records.append(self.ingest(owner_id, filename=path.name, data=data))
        return records


def passage_from_docs(chunk: dict[str, Any], parent: dict[str, Any]) -> PolicyPassage:
    return PolicyPassage(
        citation_id=chunk["citation_id"],
        policy_id=chunk["policy_id"],
        policy_title=parent["title"],
        policy_version=chunk["policy_version"],
        policy_status=PolicyStatus(parent["status"]),
        section=chunk["section"],
        source=parent["source"],
        ordinal=chunk["ordinal"],
        text=chunk["text"],
        is_fictional_example=parent["is_fictional_example"],
    )

"""Small shared model building blocks."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp (MongoDB stores datetimes as UTC)."""
    return datetime.now(UTC)


class ApiModel(BaseModel):
    """Base class for API-facing models.

    ``from_attributes`` lets us build models from plain objects; ``populate_by_name``
    lets internal code use ``id`` while JSON uses the same key (we never expose the
    raw Mongo ``_id`` key to clients).
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class BBox(BaseModel):
    """Axis-aligned rectangle in PDF points, top-left origin, unrotated page space.

    This is exactly what PyMuPDF returns. The frontend converts to screen pixels
    using the page's width/height/rotation plus the current zoom.
    """

    x0: float = Field(description="left")
    y0: float = Field(description="top")
    x1: float = Field(description="right")
    y1: float = Field(description="bottom")

    def width(self) -> float:
        return self.x1 - self.x0

    def height(self) -> float:
        return self.y1 - self.y0

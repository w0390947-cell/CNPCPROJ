"""Pure retention rules for terminal job directories; no business-model imports."""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RetentionPolicy(BaseModel):
    """Decimal bytes; elapsed wall time starts at terminal publication."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_age_seconds: int = Field(default=86_400, gt=0)
    max_bytes: int = Field(default=5_000_000_000, gt=0)
    interval_seconds: int = Field(default=60, gt=0)
    batch_size: int = Field(default=32, gt=0)

    def reason(
        self, finished_at: datetime, now: datetime, total_bytes: int
    ) -> Literal["expired", "capacity"] | None:
        if finished_at.utcoffset() is None or now.utcoffset() is None:
            raise ValueError("retention timestamps must include a timezone")
        if now - finished_at >= timedelta(seconds=self.max_age_seconds):
            return "expired"
        if total_bytes > self.max_bytes:
            return "capacity"
        return None


class RetentionCapacityError(RuntimeError):
    """Admission refused because managed storage cannot currently meet its budget."""

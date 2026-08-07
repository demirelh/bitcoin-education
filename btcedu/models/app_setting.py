"""Runtime settings that operators can change from the dashboard.

``Settings`` (pydantic) is read from ``.env`` at process start and is therefore
not writable at runtime. This table holds the few values that need to be
switchable while the pipeline is running -- currently the render execution
mode. Values stored here take precedence over the corresponding ``.env`` value.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AppSetting(Base):
    """A single operator-editable key/value pair."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"<AppSetting(key='{self.key}', value='{self.value}')>"


def get_setting(session, key: str, default: str = "") -> str:
    """Read a runtime setting, falling back to ``default`` when unset."""
    row = session.get(AppSetting, key)
    return row.value if row is not None else default


def set_setting(session, key: str, value: str) -> None:
    """Create or update a runtime setting."""
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = _utcnow()
    session.commit()

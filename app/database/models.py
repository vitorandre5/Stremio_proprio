from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.db import Base


class MetadataItem(Base):
    __tablename__ = "metadata_items"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(16), index=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    imdb_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    poster_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    overview: Mapped[str] = mapped_column(Text, default="")


class Addon(Base):
    __tablename__ = "addons"

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    manifest_url: Mapped[str] = mapped_column(Text, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    manifest_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class LibraryItem(Base):
    __tablename__ = "library_items"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(16), index=True)
    imdb_id: Mapped[str] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(String(300))
    path: Mapped[str] = mapped_column(Text, unique=True)
    stream_url: Mapped[str] = mapped_column(Text)


class TemporaryMedia(Base):
    __tablename__ = "temporary_media"

    info_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    imdb_id: Mapped[str] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(String(300))
    path: Mapped[str] = mapped_column(Text, unique=True)
    jellyfin_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    jellyfin_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    playback_started: Mapped[bool] = mapped_column(Boolean, default=False)
    downloaded_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="downloaded", index=True)


class LinkCheck(Base):
    __tablename__ = "link_checks"

    library_item_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    checked_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    quality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(String(240), default="Aguardando primeira verificacao.")


class Preference(Base):
    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    preferred_quality: Mapped[str] = mapped_column(String(16), default="1080p")
    preferred_provider: Mapped[str] = mapped_column(String(160), default="automatic")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    owner_id: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(String(500), default="Na fila")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))

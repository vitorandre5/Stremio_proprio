from sqlalchemy import Boolean, Integer, String, Text
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


class Preference(Base):
    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    preferred_quality: Mapped[str] = mapped_column(String(16), default="1080p")
    preferred_provider: Mapped[str] = mapped_column(String(160), default="automatic")

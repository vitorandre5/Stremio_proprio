from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL


class Base(DeclarativeBase):
    pass


database_url = DATABASE_URL
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
elif database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    database_url,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=not DATABASE_URL.startswith("sqlite"),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

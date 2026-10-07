import asyncio
from datetime import datetime, timezone
import json
import logging
from uuid import uuid4

from app.database.db import SessionLocal
from app.database.models import Job


logger = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(kind: str, owner_id: str, total: int = 0, message: str = "Na fila") -> dict:
    job_id = str(uuid4())
    now = _now()
    with SessionLocal() as session:
        job = Job(
            id=job_id,
            kind=kind,
            owner_id=owner_id,
            status="queued",
            total=total,
            completed=0,
            failed=0,
            message=message,
            result_json="{}",
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.commit()
        return serialize_job(job)


def update_job(job_id: str, **changes) -> dict | None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            return None
        for field in ("status", "total", "completed", "failed", "message"):
            if field in changes:
                setattr(job, field, changes[field])
        if "result" in changes:
            job.result_json = json.dumps(changes["result"], ensure_ascii=False)
        job.updated_at = _now()
        session.commit()
        return serialize_job(job)


def get_job(job_id: str) -> dict | None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        return serialize_job(job) if job else None


def serialize_job(job: Job) -> dict:
    try:
        result = json.loads(job.result_json or "{}")
    except json.JSONDecodeError:
        result = {}
    return {
        "id": job.id,
        "kind": job.kind,
        "owner_id": job.owner_id,
        "status": job.status,
        "total": job.total,
        "completed": job.completed,
        "failed": job.failed,
        "message": job.message,
        "result": result,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def mark_interrupted_jobs() -> None:
    with SessionLocal() as session:
        interrupted = session.query(Job).filter(Job.status.in_(["queued", "uploading", "running"])).all()
        for job in interrupted:
            job.status = "failed"
            job.message = "O processo foi interrompido durante uma reinicializacao."
            job.updated_at = _now()
        session.commit()
        if interrupted:
            logger.warning("Marked %s interrupted jobs", len(interrupted))


def start_task(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def can_access_job(job: dict, user: dict) -> bool:
    return bool(user.get("is_admin") or job["owner_id"] == user.get("user_id"))

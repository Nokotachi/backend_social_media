from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from content_service.auth import AdminAuth
from content_service.core import errors
from content_service.core.pagination import decode_cursor, encode_cursor
from content_service.database import get_db
from content_service.models import PostReport

router = APIRouter(prefix="/admin", tags=["admin"])

Db = Annotated[AsyncSession, Depends(get_db)]


class ReportOut(BaseModel):
    id: int
    post_id: int
    reporter_username: str | None
    description: str
    created_at: datetime


class ReportPage(BaseModel):
    items: list[ReportOut]
    next_cursor: str | None


@router.get("/reports", response_model=ReportPage)
async def list_reports(
    db: Db,
    admin: AdminAuth,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ReportPage:
    query = select(PostReport)
    cur = decode_cursor(cursor)
    if cur is not None:
        query = query.where(PostReport.id < cur["id"])
    rows = list(await db.scalars(query.order_by(PostReport.id.desc()).limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]
    reporters = await admin.users.authors({r.reporter_id for r in rows})
    return ReportPage(
        items=[
            ReportOut(
                id=r.id,
                post_id=r.post_id,
                reporter_username=reporters[r.reporter_id].username
                if r.reporter_id in reporters
                else None,
                description=r.description,
                created_at=r.created_at,
            )
            for r in rows
        ],
        next_cursor=encode_cursor({"id": rows[-1].id}) if has_more else None,
    )


@router.delete("/reports/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_report(report_id: int, db: Db, _admin: AdminAuth) -> None:
    result = await db.execute(delete(PostReport).where(PostReport.id == report_id))
    if not result.rowcount:
        raise errors.not_found("ReportNotFound")
    await db.commit()

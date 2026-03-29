"""
Session management endpoints.

Provides:
  GET    /sessions              — List sessions (optionally by user_id)
  GET    /sessions/{id}         — Get a specific session
  DELETE /sessions/{id}         — Soft-close a session
  GET    /sessions/{id}/messages — Get messages for a session
"""

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import ICRMSession, ICRMMessage

router = APIRouter()


class SessionResponse(BaseModel):
    id: UUID
    user_id: str
    channel: str
    status: str
    created_at: datetime
    updated_at: datetime
    metadata: dict = Field(default_factory=dict)

    model_config = {"from_attributes": True}


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse] = Field(default_factory=list)
    total: int = 0


class MessageResponse(BaseModel):
    id: UUID
    session_id: UUID
    role: str
    content: str
    token_count: Optional[int] = None
    model: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageListResponse(BaseModel):
    messages: list[MessageResponse] = Field(default_factory=list)
    total: int = 0


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    user_id: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """List chat sessions, optionally filtered by user_id."""
    query = select(ICRMSession).order_by(ICRMSession.created_at.desc())
    count_query = select(func.count(ICRMSession.id))

    if user_id:
        query = query.where(ICRMSession.user_id == user_id)
        count_query = count_query.where(ICRMSession.user_id == user_id)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    rows = result.scalars().all()

    sessions = [
        SessionResponse(
            id=row.id,
            user_id=row.user_id,
            channel=row.channel,
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
            metadata=row.metadata_ or {},
        )
        for row in rows
    ]

    return SessionListResponse(sessions=sessions, total=total)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: UUID, db: AsyncSession = Depends(get_db)):
    """Get a specific session by ID."""
    result = await db.execute(
        select(ICRMSession).where(ICRMSession.id == session_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    return SessionResponse(
        id=row.id,
        user_id=row.user_id,
        channel=row.channel,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        metadata=row.metadata_ or {},
    )


@router.delete("/{session_id}")
async def delete_session(session_id: UUID, db: AsyncSession = Depends(get_db)):
    """Soft-close a session (set status to 'closed')."""
    result = await db.execute(
        select(ICRMSession).where(ICRMSession.id == session_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    await db.execute(
        update(ICRMSession)
        .where(ICRMSession.id == session_id)
        .values(status="closed", updated_at=datetime.now(timezone.utc))
    )
    await db.commit()

    return {"status": "closed", "session_id": str(session_id)}


@router.get("/{session_id}/messages", response_model=MessageListResponse)
async def get_session_messages(
    session_id: UUID,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Get all messages for a session."""
    # Verify session exists
    sess_result = await db.execute(
        select(ICRMSession.id).where(ICRMSession.id == session_id)
    )
    if sess_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")

    count_result = await db.execute(
        select(func.count(ICRMMessage.id)).where(ICRMMessage.session_id == session_id)
    )
    total = count_result.scalar() or 0

    query = (
        select(ICRMMessage)
        .where(ICRMMessage.session_id == session_id)
        .order_by(ICRMMessage.created_at.asc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(query)
    rows = result.scalars().all()

    messages = [
        MessageResponse(
            id=row.id,
            session_id=row.session_id,
            role=row.role.value if hasattr(row.role, "value") else row.role,
            content=row.content,
            token_count=row.token_count,
            model=row.model,
            created_at=row.created_at,
        )
        for row in rows
    ]

    return MessageListResponse(messages=messages, total=total)

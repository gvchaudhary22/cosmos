import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    Column, String, Integer, SmallInteger, Float, Boolean, Text, DateTime,
    Enum, ForeignKey, Index, UniqueConstraint,
)
from sqlalchemy.types import JSON as JSONB  # MySQL-compatible (was PostgreSQL JSONB)
from sqlalchemy import String as _Str
from sqlalchemy.orm import DeclarativeBase, relationship, Mapped, mapped_column
import enum


class Base(DeclarativeBase):
    pass


# --- Enums ---

class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"


class ReasoningPhase(str, enum.Enum):
    reason = "reason"
    act = "act"
    observe = "observe"
    evaluate = "evaluate"
    reflect = "reflect"


class ExecutionStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"


class RiskLevel(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class ApprovalMode(str, enum.Enum):
    auto = "auto"
    manual = "manual"
    escalated = "escalated"


# --- Models ---

class ICRMSession(Base):
    __tablename__ = "icrm_sessions"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(255), nullable=False, index=True)
    company_id = Column(String(255), nullable=True, index=True)
    channel = Column(String(50), nullable=False, default="web")
    status = Column(String(50), nullable=False, default="active")
    metadata_ = Column("metadata", JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    messages = relationship("ICRMMessage", back_populates="session", cascade="all, delete-orphan")
    context = relationship("ConversationContext", back_populates="session", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_sessions_user_status", "user_id", "status"),
    )


class ICRMMessage(Base):
    __tablename__ = "icrm_messages"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(36), ForeignKey("icrm_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(Enum(MessageRole), nullable=False)
    content = Column(Text, nullable=False)
    token_count = Column(Integer, nullable=True)
    model = Column(String(100), nullable=True)
    metadata_ = Column("metadata", JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    session = relationship("ICRMSession", back_populates="messages")

    __table_args__ = (
        Index("idx_messages_session_created", "session_id", "created_at"),
    )


class ConversationContext(Base):
    __tablename__ = "icrm_conversation_context"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(36), ForeignKey("icrm_sessions.id", ondelete="CASCADE"), nullable=False, unique=True)
    intent = Column(String(255), nullable=True)
    entities = Column(JSONB, nullable=True)
    tool_state = Column(JSONB, nullable=True)
    user_profile = Column(JSONB, nullable=True)
    conversation_summary = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    session = relationship("ICRMSession", back_populates="context")


class ReasoningTrace(Base):
    __tablename__ = "icrm_reasoning_traces"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(36), ForeignKey("icrm_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    message_id = Column(String(36), ForeignKey("icrm_messages.id", ondelete="SET NULL"), nullable=True)
    phase = Column(Enum(ReasoningPhase), nullable=False)
    content = Column(Text, nullable=False)
    duration_ms = Column(Integer, nullable=True)
    metadata_ = Column("metadata", JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_traces_session_phase", "session_id", "phase"),
    )


class ToolExecution(Base):
    __tablename__ = "icrm_tool_executions"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(36), ForeignKey("icrm_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    tool_name = Column(String(255), nullable=False, index=True)
    input_params = Column(JSONB, nullable=True)
    output_result = Column(JSONB, nullable=True)
    status = Column(Enum(ExecutionStatus), nullable=False, default=ExecutionStatus.pending)
    duration_ms = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_tool_exec_session_status", "session_id", "status"),
    )


class ActionApproval(Base):
    __tablename__ = "icrm_action_approvals"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(36), ForeignKey("icrm_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    tool_execution_id = Column(String(36), ForeignKey("icrm_tool_executions.id", ondelete="SET NULL"), nullable=True)
    action_type = Column(String(255), nullable=False)
    risk_level = Column(Enum(RiskLevel), nullable=False, default=RiskLevel.low)
    approval_mode = Column(Enum(ApprovalMode), nullable=False, default=ApprovalMode.auto)
    approved = Column(Boolean, nullable=True)
    approved_by = Column(String(255), nullable=True)
    reason = Column(Text, nullable=True)
    metadata_ = Column("metadata", JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    resolved_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_approvals_session_risk", "session_id", "risk_level"),
    )


class AuditLog(Base):
    """Immutable audit log — no updates or deletes."""
    __tablename__ = "icrm_audit_log"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(36), nullable=True, index=True)
    user_id = Column(String(255), nullable=True, index=True)
    action = Column(String(255), nullable=False)
    resource_type = Column(String(255), nullable=True)
    resource_id = Column(String(255), nullable=True)
    details = Column(JSONB, nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_audit_action_created", "action", "created_at"),
    )


class Analytics(Base):
    __tablename__ = "icrm_analytics"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(36), ForeignKey("icrm_sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    event_type = Column(String(255), nullable=False, index=True)
    event_data = Column(JSONB, nullable=True)
    user_id = Column(String(255), nullable=True)
    company_id = Column(String(255), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    token_count = Column(Integer, nullable=True)
    model = Column(String(100), nullable=True)
    cost_usd = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_analytics_event_created", "event_type", "created_at"),
    )


class Feedback(Base):
    __tablename__ = "icrm_feedback"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(36), ForeignKey("icrm_sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    message_id = Column(String(36), ForeignKey("icrm_messages.id", ondelete="SET NULL"), nullable=True)
    user_id = Column(String(255), nullable=True)
    rating = Column(Integer, nullable=True)
    comment = Column(Text, nullable=True)
    tags = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_feedback_session_rating", "session_id", "rating"),
    )


class ToolRegistry(Base):
    __tablename__ = "icrm_tool_registry"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, unique=True)
    display_name = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    category = Column(String(100), nullable=True, index=True)
    parameters = Column(JSONB, nullable=True)
    returns = Column(JSONB, nullable=True)
    requires_approval = Column(Boolean, default=False)
    risk_level = Column(Enum(RiskLevel), nullable=False, default=RiskLevel.low)
    enabled = Column(Boolean, default=True)
    version = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_tool_registry_category_enabled", "category", "enabled"),
    )


# --- Phase 3: Intelligence & Learning Models ---


class KnowledgeCategory(str, enum.Enum):
    faq = "faq"
    policy = "policy"
    process = "process"
    troubleshooting = "troubleshooting"


class DistillationRecord(Base):
    """Training data collected from query-response pairs for future model distillation."""
    __tablename__ = "icrm_distillation_records"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(36), nullable=False, index=True)
    user_query = Column(Text, nullable=False)
    intent = Column(String(255), nullable=True)
    entity = Column(String(255), nullable=True)
    tools_used = Column(JSONB, nullable=True)
    tool_results = Column(JSONB, nullable=True)
    llm_prompt = Column(Text, nullable=True)
    llm_response = Column(Text, nullable=True)
    final_response = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)
    feedback_score = Column(Integer, nullable=True)
    feedback_text = Column(Text, nullable=True)
    model_used = Column(String(100), nullable=True)
    token_count_input = Column(Integer, default=0)
    token_count_output = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index("idx_distillation_session", "session_id"),
        Index("idx_distillation_confidence", "confidence"),
        Index("idx_distillation_created", "created_at"),
    )


class KnowledgeEntry(Base):
    """Knowledge base entries for context enrichment."""
    __tablename__ = "icrm_knowledge_entries"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    category = Column(Enum(KnowledgeCategory), nullable=False, index=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    source = Column(String(255), nullable=True)
    confidence = Column(Float, default=1.0)
    usage_count = Column(Integer, default=0)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                       onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index("idx_knowledge_category_enabled", "category", "enabled"),
    )


class KBFileIndex(Base):
    """Persistent file-hash tracker for KB YAML files.

    Replaces KBUpdatePipeline._file_hashes (in-memory) with a DB-backed table.
    Survives restarts, enables PR-triggered re-index, and drives UI observability.

    status:
      0 = pending  (file changed, awaiting re-embed)
      1 = indexed  (current, embedding is up to date)
      2 = failed   (parse or embed error)
    """
    __tablename__ = "cosmos_kb_file_index"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    repo_id = Column(String(255), nullable=False, default="")
    file_path = Column(String(500), nullable=False)   # relative path inside KB root
    file_hash = Column(String(64), nullable=False, default="")   # MD5 of raw file bytes
    entity_id = Column(String(191), nullable=False, default="")  # table:orders / api:mc_get_order
    entity_type = Column(String(100), nullable=False, default="")
    # 0=pending, 1=indexed, 2=failed
    status = Column(SmallInteger, nullable=False, default=0)
    # S3 fields — populated when S3 sync is active
    s3_key = Column(String(500), nullable=True)
    s3_etag = Column(String(64), nullable=True)
    last_indexed_at = Column(DateTime, nullable=True)
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("repo_id", "file_path", name="uq_kb_file_repo_path"),
        Index("idx_kb_file_status", "status"),
        Index("idx_kb_file_repo", "repo_id"),
        Index("idx_kb_file_entity", "entity_type"),
    )


class S3ExportRecord(Base):
    """Tracks training data (DPO/SFT) exports pushed to S3."""
    __tablename__ = "cosmos_s3_exports"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    export_type = Column(String(50), nullable=False)    # dpo | sft | embedding_backup
    s3_key = Column(String(500), nullable=False)
    s3_bucket = Column(String(255), nullable=False)
    record_count = Column(Integer, nullable=False, default=0)
    size_bytes = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, default="uploaded")  # uploaded | failed
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_s3_exports_type", "export_type"),
        Index("idx_s3_exports_created", "created_at"),
    )


class QueryAnalytics(Base):
    """Per-query metrics for analytics dashboard."""
    __tablename__ = "icrm_query_analytics"

    id = Column(String(36), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(36), nullable=True, index=True)
    intent = Column(String(255), nullable=True, index=True)
    entity = Column(String(255), nullable=True)
    confidence = Column(Float, nullable=True)
    latency_ms = Column(Float, nullable=True)
    tools_used = Column(JSONB, nullable=True)
    escalated = Column(Boolean, default=False)
    model = Column(String(100), nullable=True)
    cost_usd = Column(Float, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index("idx_query_analytics_intent", "intent"),
        Index("idx_query_analytics_created", "created_at"),
        Index("idx_query_analytics_model", "model"),
    )


# --- Goal 5: Continuous Learning Models ---

class StagedImprovementStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    applied = "applied"


class StagedImprovementType(str, enum.Enum):
    missing_action_candidate = "missing_action_candidate"
    add_negative_example = "add_negative_example"
    add_clarification_rule = "add_clarification_rule"
    kb_correction = "kb_correction"
    new_qa_pair = "new_qa_pair"


class StagedImprovement(Base):
    __tablename__ = "cosmos_staged_improvements"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    improvement_type: Mapped[StagedImprovementType] = mapped_column(String(50))
    status: Mapped[StagedImprovementStatus] = mapped_column(
        String(20), default=StagedImprovementStatus.pending, index=True
    )

    # Source trace
    source_query: Mapped[Optional[str]] = mapped_column(Text)
    source_confidence: Mapped[Optional[float]] = mapped_column(Float)
    source_domain: Mapped[Optional[str]] = mapped_column(String(100))
    source_record_id: Mapped[Optional[str]] = mapped_column(String(36))  # DistillationRecord.id

    # Proposed content (what goes into KB)
    proposed_entity_id: Mapped[Optional[str]] = mapped_column(String(200))
    proposed_pillar: Mapped[Optional[str]] = mapped_column(String(50))
    proposed_content: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    proposed_rationale: Mapped[Optional[str]] = mapped_column(Text)  # Why this helps

    # Eval context
    eval_recall_before: Mapped[Optional[float]] = mapped_column(Float)
    eval_domain: Mapped[Optional[str]] = mapped_column(String(100))

    # Review
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(100))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[Optional[str]] = mapped_column(Text)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

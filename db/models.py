"""
SQLAlchemy models — all tables from doc 02.

CRITICAL: The UniqueConstraint("user_id", "jd_hash", "form_id") on the
`applications` table is the DB-level idempotency guard. It is enforced here,
not in application logic, so no race condition can produce a duplicate row.

Phase 2 additions:
- ContextVersion already exists (used from Phase 1 schema).
- ApplicationAnswer gains source + context_keys_used for doc 04 traceability.
- Template exists and is now actively used by /template command.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    """One row per Telegram user. telegram_id is the external key."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    applications = relationship("Application", back_populates="user")
    context_versions = relationship("ContextVersion", back_populates="user")
    templates = relationship("Template", back_populates="user", uselist=False)


class ContextVersion(Base):
    """
    Versioned store of user context (resume, GitHub, LinkedIn, portfolio).
    Latest-version-wins for generation; old versions kept for audit.
    Phase 1: not populated — replaced by static test profile.
    Phase 2: resume upload populates this.
    """

    __tablename__ = "context_versions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    type = Column(String(50), nullable=False)  # resume | github | linkedin | portfolio
    content = Column(Text, nullable=False)
    version = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="context_versions")


class Application(Base):
    """
    One row per (user, job-posting, form) triplet.

    Idempotency: the UniqueConstraint on (user_id, jd_hash, form_id) is the
    non-negotiable guard from doc 02. jd_hash = sha256 of normalised JD text;
    form_id = canonical form URL (query-param-stripped, redirect-resolved).
    The INSERT in idempotency.py catches IntegrityError as the hard backstop
    even if the application-level check was somehow bypassed.

    status flow: drafted → pending_approval → submitted | failed | cancelled
    """

    __tablename__ = "applications"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    jd_hash = Column(String(64), nullable=False)
    form_id = Column(String(512), nullable=False)
    platform = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False, default="drafted")
    receipt_path = Column(String(512), nullable=True)  # path to confirmation screenshot
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    submitted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "jd_hash",
            "form_id",
            name="uq_application_idempotency",
        ),
    )

    user = relationship("User", back_populates="applications")
    answers = relationship("ApplicationAnswer", back_populates="application")
    outcomes = relationship("Outcome", back_populates="application")


class ApplicationAnswer(Base):
    """
    One row per form field per application.
    Phase 1: drafted_answer = value from static profile.
    Phase 2: drafted_answer = LLM-generated, user_edited tracks edits.
    """

    __tablename__ = "application_answers"

    id = Column(Integer, primary_key=True)
    application_id = Column(
        Integer, ForeignKey("applications.id"), nullable=False, index=True
    )
    question_text = Column(Text, nullable=False)
    drafted_answer = Column(Text, nullable=True)   # None = flagged for manual review
    user_edited = Column(Boolean, default=False, nullable=False)
    # Phase 2: traceability (doc 04 — every answer must reference its source)
    source = Column(String(64), nullable=True)      # static_profile | groq | manual_required
    context_keys_used = Column(Text, nullable=True) # JSON array of context keys used

    application = relationship("Application", back_populates="answers")


class Outcome(Base):
    """
    User-reported outcome per application (Phase 4 population, schema built now).
    outcome: response | interview | no_response
    """

    __tablename__ = "outcomes"

    id = Column(Integer, primary_key=True)
    application_id = Column(
        Integer, ForeignKey("applications.id"), nullable=False, index=True
    )
    outcome = Column(String(50), nullable=False)
    noted_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    application = relationship("Application", back_populates="outcomes")


class Template(Base):
    """
    User-editable tone/style template for answer generation (Phase 2 use).
    One row per user (unique constraint). Editable via /template command.
    """

    __tablename__ = "templates"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    template_text = Column(Text, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    user = relationship("User", back_populates="templates")

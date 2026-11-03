import datetime as dt
import os

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

DATA_DIR = os.getenv("DATA_DIR", "data")
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{DATA_DIR}/grievance.db"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgresql://"):]
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Citizen(Base):
    __tablename__ = "citizen"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    phone_hash: Mapped[str] = mapped_column(String)
    token_hash: Mapped[str] = mapped_column(String)
    user_lang: Mapped[str] = mapped_column(String)
    voice_lang: Mapped[str | None] = mapped_column(String, nullable=True)


class Consent(Base):
    __tablename__ = "consent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    citizen_id: Mapped[str] = mapped_column(ForeignKey("citizen.id"))
    purpose: Mapped[str] = mapped_column(String)
    text_version: Mapped[str] = mapped_column(String)
    given_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    withdrawn_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class Case(Base):
    __tablename__ = "case"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    citizen_id: Mapped[str] = mapped_column(ForeignKey("citizen.id"))
    status: Mapped[str] = mapped_column(String, default="active")
    forum: Mapped[str | None] = mapped_column(String, nullable=True)
    ministry: Mapped[str | None] = mapped_column(String, nullable=True)
    department: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    route_confidence: Mapped[float | None] = mapped_column(nullable=True)
    state: Mapped[dict] = mapped_column(JSON, default=dict)  # asked groups, pending group, confirmed sections, screen result
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Segment(Base):
    __tablename__ = "segment"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("case.id"))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String)
    audio_key: Mapped[str | None] = mapped_column(String, nullable=True)
    lang: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    text_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Slot(Base):
    __tablename__ = "slot"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("case.id"))
    name: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String, default="segment")
    source_id: Mapped[str] = mapped_column(String)
    confirmed: Mapped[bool] = mapped_column(default=False)


class Draft(Base):
    __tablename__ = "draft"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("case.id"))
    version: Mapped[int] = mapped_column(Integer)
    json: Mapped[dict] = mapped_column(JSON)
    text_en: Mapped[str] = mapped_column(Text)
    text_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_key: Mapped[str | None] = mapped_column(String, nullable=True)


class Submission(Base):
    __tablename__ = "submission"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("case.id"))
    forum: Mapped[str] = mapped_column(String)
    registration_no: Mapped[str] = mapped_column(String)
    filed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    filed_by: Mapped[str] = mapped_column(String, default="citizen")


class StatusEvent(Base):
    __tablename__ = "status_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("case.id"))
    type: Mapped[str] = mapped_column(String)
    date: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str | None] = mapped_column(String, nullable=True)
    actor: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    Base.metadata.create_all(engine)


def get_db():
    with SessionLocal() as s:
        yield s


def audit(db: Session, case_id: str | None, action: str, actor: str = "citizen"):
    db.add(AuditLog(case_id=case_id, actor=actor, action=action))
    db.commit()

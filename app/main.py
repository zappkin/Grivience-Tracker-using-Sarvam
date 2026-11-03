import base64
import datetime as dt
import hashlib
import os
import secrets
import uuid

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import pipeline as pl
from . import sarvam as sv
from .db import (DATA_DIR, AuditLog, Case, Citizen, Consent, Draft, Segment, Slot, StatusEvent, Submission, audit, get_db,
                 init_db, now)

app = FastAPI(title="Grievance Drafter")
init_db()
SALT = os.getenv("PHONE_HASH_SALT", "")
PURPOSES = ["drafting", "storage", "reminders"]
CONSENT_VERSION = "v1"
CONSENT_TEXT = ("We will record your voice and what you say, turn it into a written complaint, and keep it for up to one year. "
                "We use Sarvam AI services in India to do this. We will send you reminders about your complaint. "
                "You can stop and delete everything at any time.")


def h(s: str) -> str:
    return hashlib.sha256((SALT + s).encode()).hexdigest()


def citizen_auth(citizen_id: str, token: str | None, db: Session) -> Citizen:
    c = db.get(Citizen, citizen_id)
    if not c or not token or h(token) != c.token_hash:
        raise HTTPException(401, "Invalid citizen token")
    return c


def active_consent(db: Session, citizen_id: str, purposes=("drafting", "storage")) -> bool:
    got = {c.purpose for c in db.scalars(select(Consent).where(Consent.citizen_id == citizen_id, Consent.withdrawn_at.is_(None)))}
    return all(p in got for p in purposes)


def get_case(case_id: str, x_citizen_token: str | None = Header(None), db: Session = Depends(get_db)) -> Case:
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    citizen_auth(case.citizen_id, x_citizen_token, db)
    if not active_consent(db, case.citizen_id):
        raise HTTPException(403, "Consent withdrawn")
    return case


# ---- consent ----------------------------------------------------------------------------------------------
@app.get("/consents/notice")
def consent_notice(lang: str = "en-IN"):
    if lang not in sv.LANGS:
        raise HTTPException(400, "Unsupported language")
    text = sv.translate(CONSENT_TEXT, "en-IN", lang)
    return {"version": CONSENT_VERSION, "purposes": PURPOSES, "lang": lang, "text": text, "audio_b64": sv.tts(text, lang)}


class ConsentIn(BaseModel):
    phone: str
    user_lang: str
    voice_lang: str | None = None
    purposes: list[str]


@app.post("/consents")
def create_consent(body: ConsentIn, db: Session = Depends(get_db)):
    if body.user_lang not in sv.LANGS:
        raise HTTPException(400, "Unsupported language")
    if not {"drafting", "storage"} <= set(body.purposes):
        raise HTTPException(400, "Consent to drafting and storage is required")
    token = secrets.token_urlsafe(24)
    c = Citizen(id=str(uuid.uuid4()), phone_hash=h(body.phone), token_hash=h(token), user_lang=body.user_lang, voice_lang=body.voice_lang)
    db.add(c)
    for p in body.purposes:
        if p in PURPOSES:
            db.add(Consent(citizen_id=c.id, purpose=p, text_version=CONSENT_VERSION))
    db.commit()
    audit(db, None, "consent_given")
    return {"citizen_id": c.id, "token": token}


@app.post("/consents/withdraw")
def withdraw(citizen_id: str, x_citizen_token: str | None = Header(None), db: Session = Depends(get_db)):
    citizen_auth(citizen_id, x_citizen_token, db)
    for c in db.scalars(select(Consent).where(Consent.citizen_id == citizen_id, Consent.withdrawn_at.is_(None))):
        c.withdrawn_at = now()
    db.commit()
    audit(db, None, "consent_withdrawn")
    return {"ok": True}


@app.delete("/citizens/{citizen_id}")
def erase(citizen_id: str, x_citizen_token: str | None = Header(None), db: Session = Depends(get_db)):
    citizen_auth(citizen_id, x_citizen_token, db)
    ids = list(db.scalars(select(Case.id).where(Case.citizen_id == citizen_id)))
    for cid in ids:
        for m in (Segment, Slot, Draft, Submission, StatusEvent):
            db.execute(delete(m).where(m.case_id == cid))
        adir = os.path.join(DATA_DIR, "audio", cid)
        for f in os.listdir(adir) if os.path.isdir(adir) else []:
            os.remove(os.path.join(adir, f))
    db.execute(delete(Case).where(Case.citizen_id == citizen_id))
    db.execute(delete(Consent).where(Consent.citizen_id == citizen_id))
    db.execute(delete(Citizen).where(Citizen.id == citizen_id))
    db.commit()
    audit(db, None, "citizen_erased")
    return {"ok": True}


# ---- cases ------------------------------------------------------------------------------------------------
class CaseIn(BaseModel):
    citizen_id: str


@app.post("/cases")
def create_case(body: CaseIn, x_citizen_token: str | None = Header(None), db: Session = Depends(get_db)):
    citizen_auth(body.citizen_id, x_citizen_token, db)
    if not active_consent(db, body.citizen_id):
        raise HTTPException(403, "Consent required")
    case = Case(id=str(uuid.uuid4()), citizen_id=body.citizen_id, state={})
    db.add(case)
    db.commit()
    audit(db, case.id, "case_created")
    return {"case_id": case.id}


def clock(sub: Submission | None):
    if not sub:
        return None
    d7, d21 = sub.filed_at + dt.timedelta(days=7), sub.filed_at + dt.timedelta(days=21)
    n = now()
    return {"registration_no": sub.registration_no, "filed_at": sub.filed_at.isoformat(), "day7": d7.isoformat(), "day21": d21.isoformat(),
            "days_left": (d21 - n).days,
            "reminders": [{"label": "Day 7 check-in", "due": d7.isoformat(), "due_now": n >= d7},
                          {"label": "Day 21: reply deadline. Ask for an interim reply or appeal", "due": d21.isoformat(), "due_now": n >= d21}]}


@app.get("/cases/{case_id}")
def read_case(case: Case = Depends(get_case), db: Session = Depends(get_db)):
    audit(db, case.id, "case_viewed")
    sub = db.scalars(select(Submission).where(Submission.case_id == case.id)).first()
    events = [{"type": e.type, "date": e.date.isoformat(), "raw_text": e.raw_text} for e in db.scalars(select(StatusEvent).where(StatusEvent.case_id == case.id))]
    draft = db.scalars(select(Draft).where(Draft.case_id == case.id).order_by(Draft.version.desc())).first()
    return {"id": case.id, "status": case.status, "route": {"forum": case.forum, "ministry": case.ministry, "department": case.department, "category": case.category, "confidence": case.route_confidence},
            "slots": pl.slots_dict(db, case.id), "questions_asked": len(case.state.get("asked", [])), "confirmed": case.state.get("confirmed", []),
            "draft": draft.json if draft else None, "tracking": clock(sub), "status_events": events}


def citizen_of(db, case) -> Citizen:
    return db.get(Citizen, case.citizen_id)


def case_lang(db, case) -> str:
    return case.state.get("lang") or citizen_of(db, case).user_lang


def ingest_audio(db: Session, case: Case, kind: str, data: bytes, filename: str, mime: str):
    r = sv.transcribe(data, filename, mime)
    if not r["text"].strip():
        raise HTTPException(422, "Could not hear any speech. Please try again.")
    key = os.path.join(DATA_DIR, "audio", case.id)
    os.makedirs(key, exist_ok=True)
    path = os.path.join(key, f"{len(pl.segments(db, case.id)) + 1}_{uuid.uuid4().hex[:6]}_{os.path.basename(filename)}")
    with open(path, "wb") as fh:
        fh.write(data)
    return pl.add_segment(db, case, r["text"], r["lang"], kind, path, r["start_ms"], r["end_ms"])


def after_narrative(db: Session, case: Case, seg: Segment):
    if not case.state.get("lang") and seg.lang in sv.LANGS:
        case.state = {**case.state, "lang": seg.lang}
        db.commit()
    result = pl.screen(db, case)
    if case.status == "active":
        pl.extract_slots(db, case)
    return {"segment": {"id": pl.seg_id(seg), "lang": seg.lang, "text": seg.text}, "status": case.status, "screen": result}


@app.post("/cases/{case_id}/audio")
async def post_audio(file: UploadFile = File(...), case: Case = Depends(get_case), db: Session = Depends(get_db)):
    seg = ingest_audio(db, case, "narrative", await file.read(), file.filename or "audio.webm", file.content_type or "audio/webm")
    return after_narrative(db, case, seg)


class TextIn(BaseModel):
    text: str


@app.post("/cases/{case_id}/text")
def post_text(body: TextIn, case: Case = Depends(get_case), db: Session = Depends(get_db)):
    if not body.text.strip():
        raise HTTPException(422, "Empty text")
    seg = pl.add_segment(db, case, body.text, "unknown", "narrative")
    return after_narrative(db, case, seg)


@app.get("/cases/{case_id}/next-question")
def get_next_question(case: Case = Depends(get_case), db: Session = Depends(get_db)):
    c = citizen_of(db, case)
    return pl.next_question(db, case, case_lang(db, case), c.voice_lang)


@app.post("/cases/{case_id}/answers")
async def post_answer(text: str = Form(""), skip: bool = Form(False), correction: bool = Form(False), file: UploadFile | None = File(None),
                      case: Case = Depends(get_case), db: Session = Depends(get_db)):
    kind = "correction" if correction else "answer"
    if skip and not correction:
        pl.mark_asked(db, case, skip=True)
        return {"ok": True, "skipped": True}
    if file is not None:
        seg = ingest_audio(db, case, kind, await file.read(), file.filename or "audio.webm", file.content_type or "audio/webm")
    elif text.strip():
        seg = pl.add_segment(db, case, text, "unknown", kind)
    else:
        raise HTTPException(422, "Provide text, audio, or skip")
    pl.extract_slots(db, case)
    if not correction:
        pl.mark_asked(db, case, skip=False)
    return {"ok": True, "segment": {"id": pl.seg_id(seg), "lang": seg.lang, "text": seg.text}}


class DraftIn(BaseModel):
    route_id: str | None = None


@app.post("/cases/{case_id}/draft")
def make_draft(body: DraftIn | None = None, case: Case = Depends(get_case), db: Session = Depends(get_db)):
    if case.status in ("emergency", "out_of_scope"):
        raise HTTPException(409, f"Case is {case.status}")
    c = citizen_of(db, case)
    chosen = body.route_id if body else None
    if chosen:
        dept = next((d for d in pl.DEPARTMENTS if d["id"] == chosen), None)
        if not dept:
            raise HTTPException(400, "Unknown route_id")
        pl.apply_route(db, case, dept, 1.0)
    elif case.route_confidence is None or case.route_confidence < 0.7:
        r = pl.route(db, case)
        if r["confidence"] < 0.7:
            return {"needs_route_choice": True, "options": [{k: o[k] for k in ("id", "ministry", "department", "category")} for o in r["options"]]}
        pl.apply_route(db, case, r["options"][0], r["confidence"])
    draft, readback = pl.create_draft(db, case, case_lang(db, case), c.voice_lang)
    audit(db, case.id, "draft_created")
    return {"version": draft.version, "draft": draft.json, "readback": readback}


class ConfirmIn(BaseModel):
    sections: list[str] | str = "all"


@app.post("/cases/{case_id}/confirm")
def confirm(body: ConfirmIn, case: Case = Depends(get_case), db: Session = Depends(get_db)):
    keys = [k for k, _ in pl.SECTIONS] if body.sections == "all" else body.sections
    allowed = {k for k, _ in pl.SECTIONS}
    if not set(keys) <= allowed:
        raise HTTPException(400, "Unknown section")
    got = sorted(set(case.state.get("confirmed", [])) | set(keys))
    case.state = {**case.state, "confirmed": got}
    if set(got) == allowed:
        case.status = "ready"
        for s in db.scalars(select(Slot).where(Slot.case_id == case.id)):
            s.confirmed = True
    db.commit()
    return {"confirmed": got, "all_confirmed": set(got) == allowed}


@app.get("/cases/{case_id}/packet")
def packet(format: str = "json", case: Case = Depends(get_case), db: Session = Depends(get_db)):
    if not active_consent(db, case.citizen_id, ("drafting",)):
        raise HTTPException(403, "Consent withdrawn")
    allowed = {k for k, _ in pl.SECTIONS}
    if set(case.state.get("confirmed", [])) != allowed:
        raise HTTPException(409, "Every section must be confirmed first")
    draft = db.scalars(select(Draft).where(Draft.case_id == case.id).order_by(Draft.version.desc())).first()
    if not draft:
        raise HTTPException(404, "No draft")
    audit(db, case.id, f"packet_exported_{format}")
    if format == "pdf":
        return Response(pl.make_pdf(pl.sections_en(draft.json), case.id, draft.json.get("warnings", [])), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="grievance-{case.id[:8]}.pdf"'})
    if format == "html":
        return Response(pl.printable_html(draft, case_lang(db, case)), media_type="text/html")
    return {"text_en": draft.text_en, "text_hi": draft.text_hi, "text_user": draft.text_user, "version": draft.version,
            "portal": "https://pgportal.gov.in", "warnings": draft.json.get("warnings", [])}


class SubmissionIn(BaseModel):
    registration_no: str
    filed_at: dt.datetime | None = None


@app.post("/cases/{case_id}/submission")
def submission(body: SubmissionIn, case: Case = Depends(get_case), db: Session = Depends(get_db)):
    if case.status not in ("ready", "filed"):
        raise HTTPException(409, "Confirm the draft first")
    sub = db.scalars(select(Submission).where(Submission.case_id == case.id)).first()
    if sub:
        sub.registration_no = body.registration_no
        if body.filed_at:
            sub.filed_at = body.filed_at.replace(tzinfo=None)
    else:
        sub = Submission(case_id=case.id, forum=case.forum or "CPGRAMS", registration_no=body.registration_no.strip(),
                         filed_at=(body.filed_at.replace(tzinfo=None) if body.filed_at else now()))
        db.add(sub)
    case.status = "filed"
    db.commit()
    return clock(sub)


class EventIn(BaseModel):
    type: str
    date: dt.datetime | None = None
    raw_text: str | None = None


@app.post("/cases/{case_id}/status-events")
def status_event(body: EventIn, case: Case = Depends(get_case), db: Session = Depends(get_db)):
    db.add(StatusEvent(case_id=case.id, type=body.type, date=(body.date.replace(tzinfo=None) if body.date else now()), raw_text=pl.scrub(body.raw_text or "")))
    db.commit()
    return {"ok": True}


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), "public"), html=True), name="static")

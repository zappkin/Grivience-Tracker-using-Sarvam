import datetime as dt
import hashlib
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

from fpdf import FPDF
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import sarvam as sv
from .db import Case, Draft, Segment, Slot

DEPARTMENTS = json.load(open(os.path.join(os.path.dirname(__file__), "departments.json")))
SLOT_NAMES = ["issue_summary", "reference_type", "reference_number", "key_dates", "prior_attempts", "relief_sought",
              "grievant_name", "state", "district", "pin_code", "mobile"]
REQUIRED = ["issue_summary", "reference_number", "prior_attempts", "relief_sought", "grievant_name", "state", "district",
            "pin_code", "mobile"]
GROUPS = [
    ("issue", ["issue_summary"], "what exactly went wrong and with which government service or scheme"),
    ("refs", ["reference_number", "key_dates"], "the application, registration, PPO, UAN or account number, and the main dates"),
    ("attempts", ["prior_attempts"], "whom they already contacted about this, when, and what happened"),
    ("relief", ["relief_sought"], "what action they want the office to take"),
    ("who", ["grievant_name", "state", "district", "pin_code", "mobile"], "their full name, state, district, PIN code and mobile number"),
]
MAX_QUESTIONS = 6
AADHAAR = re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")
SECTIONS = [("route", "Ministry / Department / Category"), ("subject", "Subject"), ("grievant", "Grievant"),
            ("facts", "Facts"), ("references", "Reference numbers"), ("prior_attempts", "Prior attempts"),
            ("relief_sought", "Relief sought")]
BASE = ("You help Indian citizens prepare complaints to government offices. Use only facts present in the supplied "
        "segments. Never invent names, dates, numbers or offices. Keep a calm, neutral, factual tone.")


def scrub(text: str) -> str:
    return AADHAAR.sub("[REDACTED]", text)


def seg_id(s: Segment) -> str:
    return f"seg_{s.seq}"


def segments(db: Session, case_id: str) -> list[Segment]:
    return list(db.scalars(select(Segment).where(Segment.case_id == case_id).order_by(Segment.seq)))


def reason_text(s: Segment) -> str:
    return s.text_en or s.text


def slots_dict(db: Session, case_id: str) -> dict[str, str]:
    return {s.name: s.value for s in db.scalars(select(Slot).where(Slot.case_id == case_id))}


def add_segment(db: Session, case: Case, text: str, lang: str, kind: str, audio_key=None, start=None, end=None) -> Segment:
    text = scrub(text.strip())
    if lang in ("unknown", "", None):
        lang = sv.detect_lang(text)
    seq = len(segments(db, case.id)) + 1
    text_en = None if lang in sv.LLM_LANGS else sv.translate(text, lang, "en-IN")
    s = Segment(case_id=case.id, seq=seq, kind=kind, audio_key=audio_key, lang=lang, text=text, text_en=text_en,
                start_ms=start, end_ms=end)
    db.add(s)
    db.commit()
    return s


def transcript_block(segs: list[Segment]) -> str:
    return "\n".join(f"{seg_id(s)} [{s.kind}, {s.lang}]: {reason_text(s)}" for s in segs)


# ---- screen -----------------------------------------------------------------------------------------------
EXCLUDED_RULES = [
    (r"\brti\b|right to information", "RTI matter"),
    (r"high court|supreme court|sub[- ]?judice|court case|case is pending in court|court order", "Court or sub-judice matter"),
    (r"religio|temple dispute|mosque|church", "Religious matter"),
]
EMERGENCY_RULES = r"kill me|suicide|being beaten|domestic violence|attack|fire broke|accident|child abuse|trafficking"
REDIRECTS = {
    "RTI matter": "File an RTI application at rtionline.gov.in. A separate RTI Copilot is planned.",
    "Court or sub-judice matter": "CPGRAMS does not take up matters that are before a court. Contact your lawyer or the legal-services authority (NALSA 15100).",
    "Religious matter": "CPGRAMS does not take up religious matters.",
    "Government employee service matter": "Service matters of government employees go to the department's own grievance process.",
    "Suggestion": "CPGRAMS does not take up general suggestions as grievances.",
    "Territorial integrity or foreign relations": "CPGRAMS does not take up this kind of matter.",
}
EMERGENCY_MSG = "This may be an emergency. Call 112 now. Women in distress: 181. Children: 1098."


def screen(db: Session, case: Case) -> dict:
    segs = segments(db, case.id)
    text = " ".join(reason_text(s) for s in segs).lower()
    result = {"in_scope": True, "excluded_category": None, "emergency": False, "forum_hint": "CPGRAMS"}
    for pat, cat in EXCLUDED_RULES:
        if re.search(pat, text):
            result.update(in_scope=False, excluded_category=cat)
            break
    if re.search(EMERGENCY_RULES, text):
        result["emergency"] = True
    schema = {"type": "object", "additionalProperties": False, "required": ["in_scope", "excluded_category", "emergency", "forum_hint"],
              "properties": {"in_scope": {"type": "boolean"},
                             "excluded_category": {"type": "string", "enum": ["none"] + list(REDIRECTS)},
                             "emergency": {"type": "boolean"},
                             "forum_hint": {"type": "string", "enum": ["CPGRAMS", "state portal", "RBI ombudsman", "consumer helpline", "railway", "other"]}}}
    try:
        llm = sv.llm_json(BASE + " Classify the complaint. excluded_category is 'none' unless CPGRAMS would not take it up "
                          "(RTI, court/sub-judice, religious, government employee's own service matter, suggestion, territorial integrity). "
                          "emergency is true only for immediate danger to life or safety.", transcript_block(segs), "screen", schema)
        if llm["emergency"]:
            result["emergency"] = True
        if llm["excluded_category"] != "none" and result["excluded_category"] is None:
            result.update(in_scope=False, excluded_category=llm["excluded_category"])
        result["forum_hint"] = llm["forum_hint"]
    except Exception:
        pass
    if result["emergency"]:
        case.status = "emergency"
    elif not result["in_scope"]:
        case.status = "out_of_scope"
    case.state = {**case.state, "screen": result}
    db.commit()
    result["message"] = EMERGENCY_MSG if result["emergency"] else REDIRECTS.get(result["excluded_category"])
    return result


# ---- interview --------------------------------------------------------------------------------------------
def extract_slots(db: Session, case: Case):
    segs = segments(db, case.id)
    valid = {seg_id(s) for s in segs}
    schema = {"type": "object", "additionalProperties": False, "required": ["slots"],
              "properties": {"slots": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False, "required": ["name", "value", "source"],
                  "properties": {"name": {"type": "string", "enum": SLOT_NAMES}, "value": {"type": "string"},
                                 "source": {"type": "string"}}}}}}
    system = (BASE + " Extract these fields from the citizen's statements: " + ", ".join(SLOT_NAMES) + ". "
              "reference_type is one of PPO, UAN, application, consumer, account, registration. Write values in English (names and places "
              "in Latin letters). Include a field only if it is explicitly stated. Give the id of the segment where it appears as source. "
              "If a later correction segment contradicts an earlier value, use the correction. Do not output Aadhaar numbers.")
    out = sv.llm_json(system, transcript_block(segs), "slots", schema)
    existing = {s.name: s for s in db.scalars(select(Slot).where(Slot.case_id == case.id))}
    for item in out["slots"]:
        name, value, src = item["name"], scrub(item["value"].strip()), item["source"]
        if src not in valid or not value:
            continue
        if name == "mobile":
            digits = re.sub(r"\D", "", value)[-10:]
            if not re.fullmatch(r"[6-9]\d{9}", digits):
                continue
            value = digits
        if name == "pin_code":
            m = re.search(r"\d{6}", value.replace(" ", ""))
            if not m:
                continue
            value = m.group()
        if name in existing:
            existing[name].value, existing[name].source_id, existing[name].confirmed = value, src, False
        else:
            db.add(Slot(case_id=case.id, name=name, value=value, source_id=src))
    db.commit()


def missing_required(db: Session, case: Case) -> list[str]:
    have = slots_dict(db, case.id)
    return [n for n in REQUIRED if n not in have]


def next_question(db: Session, case: Case, user_lang: str, voice_lang: str | None) -> dict:
    st = case.state
    asked = st.get("asked", [])
    have = slots_dict(db, case.id)
    if case.status in ("emergency", "out_of_scope"):
        return {"done": True, "status": case.status, "screen": st.get("screen")}
    if len(asked) >= MAX_QUESTIONS:
        return {"done": True, "status": case.status, "missing": missing_required(db, case)}
    for key, names, desc in GROUPS:
        if key in asked or all(n in have for n in names if n in REQUIRED):
            continue
        need = [n for n in names if n in REQUIRED and n not in have]
        lang_name = sv.LANGS.get(user_lang, "English")
        if user_lang in sv.LLM_LANGS:
            q = sv.llm_json(BASE + f" Write ONE short, polite, simple question in {lang_name} (native script) asking the citizen for {desc}. "
                            "Output only the question.", f"Already known: {json.dumps(have)}", "question",
                            {"type": "object", "additionalProperties": False, "required": ["question"],
                             "properties": {"question": {"type": "string"}}}, 600)["question"]
        else:
            q_en = sv.llm_json(BASE + f" Write ONE short, polite, simple question in English asking the citizen for {desc}. Output only the question.",
                               f"Already known: {json.dumps(have)}", "question",
                               {"type": "object", "additionalProperties": False, "required": ["question"],
                                "properties": {"question": {"type": "string"}}}, 600)["question"]
            q = sv.translate(q_en, "en-IN", user_lang)
        audio = None
        if user_lang in sv.LLM_LANGS:
            audio = sv.tts(q, user_lang)
        elif voice_lang:
            audio = sv.tts(sv.translate(q, user_lang, voice_lang), voice_lang)
        case.state = {**st, "pending": key}
        db.commit()
        return {"done": False, "question": q, "group": key, "slots_needed": need, "audio_b64": audio,
                "questions_asked": len(asked), "max_questions": MAX_QUESTIONS, "skippable": True}
    return {"done": True, "status": case.status, "missing": missing_required(db, case)}


def mark_asked(db: Session, case: Case, skip: bool):
    st = case.state
    key = st.get("pending")
    if key:
        case.state = {**st, "asked": st.get("asked", []) + [key], "pending": None,
                      "skipped": st.get("skipped", []) + ([key] if skip else [])}
        db.commit()


# ---- routing ----------------------------------------------------------------------------------------------
def candidates(issue: str, n: int = 10) -> list[dict]:
    words = set(re.findall(r"[a-z0-9\-]+", issue.lower()))
    scored = []
    for d in DEPARTMENTS:
        score = sum(2 for k in d["keywords"] if k in issue.lower()) + len(words & set(re.findall(r"[a-z0-9\-]+", " ".join(d["keywords"]).lower())))
        scored.append((score, d))
    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored[:n]]


def route(db: Session, case: Case) -> dict:
    have = slots_dict(db, case.id)
    issue = have.get("issue_summary") or " ".join(reason_text(s) for s in segments(db, case.id))
    cands = candidates(issue)
    schema = {"type": "object", "additionalProperties": False, "required": ["ids", "confidence"],
              "properties": {"ids": {"type": "array", "items": {"type": "string", "enum": [c["id"] for c in cands]}},
                             "confidence": {"type": "number"}}}
    out = sv.llm_json(BASE + " Choose which of these departments should handle the complaint. ids = up to 3 best choices, best first. "
                      "confidence = 0 to 1 for the first choice.",
                      f"Complaint: {issue}\nOptions:\n" + "\n".join(f"{c['id']}: {c['ministry']} / {c['department']} / {c['category']}" for c in cands),
                      "route", schema, 800)
    by_id = {c["id"]: c for c in cands}
    top = [by_id[i] for i in out["ids"] if i in by_id][:3] or cands[:3]
    return {"options": top, "confidence": max(0.0, min(1.0, float(out["confidence"])))}


def apply_route(db: Session, case: Case, dept: dict, confidence: float):
    case.forum, case.ministry, case.department, case.category, case.route_confidence = (
        "CPGRAMS", dept["ministry"], dept["department"], dept["category"], confidence)
    db.commit()


# ---- draft ------------------------------------------------------------------------------------------------
def mask(value: str) -> str:
    v = re.sub(r"\s", "", value)
    if len(v) <= 4:
        return "*" * len(v)
    return v[:2] + "*" * (len(v) - 4) + v[-2:]


def build_draft(db: Session, case: Case) -> dict:
    segs = segments(db, case.id)
    valid = {seg_id(s): reason_text(s) for s in segs}
    have = slots_dict(db, case.id)
    schema = {"type": "object", "additionalProperties": False, "required": ["subject", "facts", "references", "prior_attempts", "relief_sought"],
              "properties": {
                  "subject": {"type": "string"},
                  "facts": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["text", "date", "sources"],
                            "properties": {"text": {"type": "string"}, "date": {"type": "string"}, "sources": {"type": "array", "items": {"type": "string"}}}}},
                  "references": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["type", "value", "source"],
                                 "properties": {"type": {"type": "string", "enum": ["PPO", "UAN", "application", "consumer", "account", "registration"]},
                                                "value": {"type": "string"}, "source": {"type": "string"}}}},
                  "prior_attempts": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["office", "date", "outcome", "source"],
                                     "properties": {"office": {"type": "string"}, "date": {"type": "string"}, "outcome": {"type": "string"}, "source": {"type": "string"}}}},
                  "relief_sought": {"type": "string"}}}
    system = (BASE + " Write a grievance for the government. subject: one line under 100 characters naming the service and the failure. "
              "facts: numbered-style first-person statements in English, each with a date in YYYY-MM-DD if known (else empty string) and the "
              "source segment ids that support it. Rewrite abuse or accusations against named people as a factual description of conduct. "
              "relief_sought: a specific action the office should take (investigate, correct the record, release the payment). Never ask to "
              "'give the scheme'. Do not include Aadhaar numbers. Use empty arrays when nothing is stated.")
    d = sv.llm_json(system, f"Known fields: {json.dumps(have)}\n\nSegments:\n{transcript_block(segs)}", "draft", schema, 6000, model=sv.DRAFT_MODEL)

    warnings = []
    facts = []
    for f in d["facts"]:
        srcs = [s for s in f["sources"] if s in valid]
        if not srcs:
            warnings.append(f"Removed a statement with no source: {f['text'][:60]}")
            continue
        facts.append({"text": scrub(f["text"]), "date": f["date"] or None, "sources": srcs, "needs_confirmation": False})
    if facts:
        chk = sv.llm_json("For each numbered fact, say whether the cited segment text clearly supports it.",
                          "\n".join(f"{i}. FACT: {f['text']}\n   SEGMENTS: " + " | ".join(valid[s] for s in f["sources"]) for i, f in enumerate(facts)),
                          "support", {"type": "object", "additionalProperties": False, "required": ["results"],
                                      "properties": {"results": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["i", "supported"],
                                                                "properties": {"i": {"type": "integer"}, "supported": {"type": "boolean"}}}}}}, 1500)
        for r in chk["results"]:
            if 0 <= r["i"] < len(facts) and not r["supported"]:
                facts[r["i"]]["needs_confirmation"] = True
    for i, f in enumerate(facts, 1):
        f["n"] = i
    refs = [{"type": r["type"], "value_masked": mask(r["value"]), "source": r["source"]} for r in d["references"] if r["source"] in valid]
    attempts = [{"office": a["office"], "date": a["date"] or None, "outcome": a["outcome"], "source": a["source"]}
                for a in d["prior_attempts"] if a["source"] in valid]
    for n in REQUIRED:
        if n not in have:
            warnings.append(f"Not provided: {n.replace('_', ' ')}")
    grievant = {k: have.get(k) for k in ("grievant_name", "state", "district", "pin_code", "mobile")}
    subject = scrub(d["subject"]).strip()
    if len(subject) > 100:
        subject = subject[:97].rsplit(" ", 1)[0] + "..."
    return {
        "subject": subject,
        "route": {"forum": case.forum, "ministry": case.ministry, "department": case.department, "category": case.category,
                  "confidence": case.route_confidence},
        "grievant": grievant, "facts": facts, "references": refs, "prior_attempts": attempts,
        "relief_sought": scrub(d["relief_sought"]), "citations": [], "annexes": [], "warnings": warnings,
    }


def sections_en(d: dict) -> list[tuple[str, str, list[str]]]:
    r, g = d["route"], d["grievant"]
    out = [
        ("route", SECTIONS[0][1], [f"{r['ministry']} -> {r['department']} -> {r['category']}"]),
        ("subject", "Subject", [d["subject"]]),
        ("grievant", "Grievant", [f"Name: {g.get('grievant_name') or 'not provided'}",
                                  f"Address: {', '.join(x for x in (g.get('district'), g.get('state'), g.get('pin_code')) if x) or 'not provided'}",
                                  f"Mobile: {g.get('mobile') or 'not provided'}"]),
        ("facts", "Facts", [f"{f['n']}. {f['text']}" + (" (please confirm)" if f["needs_confirmation"] else "") for f in d["facts"]] or ["None stated."]),
        ("references", "Reference numbers", [f"{x['type']}: {x['value_masked']}" for x in d["references"]] or ["Not provided."]),
        ("prior_attempts", "Prior attempts", [f"{a['office']}" + (f" ({a['date']})" if a["date"] else "") + f": {a['outcome']}" for a in d["prior_attempts"]] or ["None stated."]),
        ("relief_sought", "Relief sought", [d["relief_sought"]]),
    ]
    return out


def render_text(secs: list[tuple[str, str, list[str]]], header: str | None = None) -> str:
    parts = [header] if header else []
    parts += [f"{label}\n" + "\n".join(lines) for _, label, lines in secs]
    return "\n\n".join(parts)


def translate_sections(secs, tgt: str):
    flat = [label for _, label, _ in secs] + [ln for _, _, lines in secs for ln in lines]
    done = sv.translate_many(flat, "en-IN", tgt)
    labels, rest = done[:len(secs)], iter(done[len(secs):])
    return [(k, labels[i], [next(rest) for _ in lines]) for i, (k, _, lines) in enumerate(secs)]


def make_pdf(secs, case_id: str, warnings: list[str]) -> bytes:
    def clean(t: str) -> str:
        return t.replace("->", "to").encode("latin-1", "replace").decode("latin-1")
    pdf = FPDF()
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "Grievance packet - Index", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, f"Case: {case_id}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, "Annexes: none attached (evidence upload is not in this release).", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, "Page 2 onward: grievance text (English).", new_x="LMARGIN", new_y="NEXT")
    pdf.add_page()
    for _, label, lines in secs:
        pdf.set_font("Helvetica", "B", 11)
        pdf.multi_cell(0, 6, clean(label), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 11)
        for ln in lines:
            pdf.multi_cell(0, 6, clean(ln), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
    return bytes(pdf.output())


def create_draft(db: Session, case: Case, user_lang: str, voice_lang: str | None) -> tuple[Draft, list[dict]]:
    d = build_draft(db, case)
    en = sections_en(d)
    hi = translate_sections(en, "hi-IN")
    if user_lang == "en-IN":
        usr = en
    elif user_lang == "hi-IN":
        usr = hi
    else:
        usr = translate_sections(en, user_lang)
    prev = db.scalars(select(Draft).where(Draft.case_id == case.id)).all()
    readback = []
    for (k, _, _), (_, label_u, lines_u) in zip(en, usr):
        readback.append({"key": k, "label": label_u, "text": "\n".join(lines_u)})
    d["readback"] = readback
    draft = Draft(case_id=case.id, version=len(prev) + 1, json=d, text_en=render_text(en), text_hi=render_text(hi),
                  text_user=render_text(usr))
    db.add(draft)
    case.status = "drafted"
    case.state = {**case.state, "confirmed": []}
    db.commit()

    def speak(item):
        spoken = item["label"] + ". " + item["text"]
        if user_lang in sv.LLM_LANGS:
            return sv.tts(spoken, user_lang)
        if voice_lang:
            return sv.tts(sv.translate(spoken, user_lang, voice_lang), voice_lang)
        return None
    with ThreadPoolExecutor(7) as ex:
        audios = list(ex.map(speak, readback))
    audio_out = [{**r, "audio_b64": a} for r, a in zip(readback, audios)]
    return draft, audio_out


def printable_html(draft: Draft, user_lang: str) -> str:
    import html
    blocks = [("English", draft.text_en), ("Hindi", draft.text_hi)]
    if user_lang not in ("en-IN", "hi-IN"):
        blocks.append((sv.LANGS.get(user_lang, user_lang), draft.text_user))
    body = "".join(f"<section><h2>{html.escape(n)}</h2><pre>{html.escape(t or '')}</pre></section>" for n, t in blocks)
    return ("<!doctype html><meta charset='utf-8'><title>Grievance packet</title>"
            "<style>body{font-family:system-ui,'Noto Sans',sans-serif;max-width:760px;margin:24px auto;padding:0 16px}"
            "pre{white-space:pre-wrap;font:inherit;line-height:1.5}section{page-break-after:always}</style>" + body)

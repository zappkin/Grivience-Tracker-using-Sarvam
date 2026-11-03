# Grievance Tracker using Sarvam

Speak your problem in your own language. Get a complete, CPGRAMS-ready complaint.

Built on [Sarvam AI](https://docs.sarvam.ai) for India's government grievance portal (CPGRAMS). A citizen records a voice note in any of 23 Indian languages. The app asks a few follow-up questions in the same language, picks the right ministry and department, drafts the complaint, reads it back for confirmation, and then tracks the 21-day reply clock.

## How it works

| Step | What happens | Sarvam API |
| --- | --- | --- |
| 1. Speak | Voice or text, language detected automatically | Saaras v4 |
| 2. Check | Catches RTI, court and emergency cases before filing | Sarvam-105B |
| 3. Interview | Up to 6 follow-up questions, spoken in the citizen's language | Sarvam-105B, Bulbul |
| 4. Route | Chooses ministry, department and category | Sarvam-105B |
| 5. Draft | English and Hindi complaint, every fact linked to what was said | Sarvam-105B, Sarvam-Translate |
| 6. Read back | Each part is read aloud for confirmation | Bulbul |
| 7. Track | Registration number, day 7 and day 21 reminders | |

The output is a packet to paste into pgportal.gov.in: copyable text, a PDF, and a printable page in all languages. There is no public CPGRAMS filing API, so the citizen or an operator files it.

## Run it

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
echo "SARVAM_API_KEY=your_key" > .env
set -a && . ./.env && set +a
PHONE_HASH_SALT=any-random-string uvicorn app.main:app --port 8010
```

Open http://localhost:8010.

## Configuration

| Variable | Purpose |
| --- | --- |
| `SARVAM_API_KEY` | Required |
| `PHONE_HASH_SALT` | Salt for hashing phone numbers |
| `DATABASE_URL` | Postgres URL. Defaults to local SQLite |
| `SARVAM_FAST_MODEL` | Default `sarvam-105b-conversations` |
| `SARVAM_DRAFT_MODEL` | Default is the fast model. Set `sarvam-105b` for deeper drafting (slower) |

## Status

MVP covering the P0 scope of the product spec: consent, voice and text intake, safety check, interview, routing, drafting, read-back, packet export and tracking. Not yet built: evidence extraction from photos, operator mode, WhatsApp and phone channels, and automatic reminder delivery.

The department list in `app/departments.json` is a seed list and should be checked against CPGRAMS before real use.

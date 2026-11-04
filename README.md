# LIMS Copilot

An AI assistant for a Laboratory Information Management System (LIMS). Users ask questions in plain language and the Copilot answers from live LIMS data (MongoDB) and from the company's manuals and procedures. It can build Excel, PDF, chart and Power BI exports, accept voice input, and email or schedule reports after the user confirms. Access is controlled by user role.

Built with Python, FastAPI and Google Gemini (tool calling), with a Chroma vector store for the manuals.

It can be used in three ways:

- **Embedded chat** in a web portal. An Angular chat component talks to the HTTP API through the portal's server.
- **HTTP API** (`api.py`) for any other application.
- **Streamlit app** (`app.py`) for local use and testing.

## What it can do

| Capability | How it works |
| --- | --- |
| Answer questions from LIMS data | The model calls read-only query tools over MongoDB: list, filter, distinct values, count, sum, first/last, parameter durations |
| Answer "how do I..." questions | Semantic search over the manuals and procedure documents (RAG), plus multi-hop search for questions that need several lookups |
| Export to Excel | Single-sheet, multi-sheet, joined and summary exports. Nested records are split into linked sheets |
| Export to PDF | Record tables or plain-text documents |
| Charts | PNG (works offline) or interactive HTML |
| Power BI | Power BI file export, plus a data feed registered by `export_powerbi.register_feed` |
| Voice input | The browser records audio and the API transcribes it (and translates it to English if needed) |
| Email a report | The user asks, the Copilot prepares the email, and nothing is sent until the user clicks **Send** |
| Scheduled emails | Send a report later, or on a repeat. The user confirms once, then a background worker sends it automatically and a confirmation appears in the chat |
| Role-based access | Collections, export tools, email and scheduling are limited by the user's role |
| Answer cache | Standalone questions that closely match an earlier question are answered from the cache, which saves model calls |
| Learning from corrections | When the user corrects an answer, the correction is stored and used later |

## How it works

```
User message
   |
Checks (needs earlier context? cached answer available?)
   |
Gemini model  <->  Tools
                    |- MongoDB queries (read-only, limited by role)
                    |- Document search / multi-hop search
                    |- Excel, PDF, chart and Power BI export
                    |- prepare_report_email   (prepare only, never sends)
                    '- schedule_report_email  (saves a draft only)
   |
Reply + generated files + pending email / pending schedule
   |
User clicks Send / Schedule  ->  confirm endpoint
   |
Background worker (every 30 s) builds the report with fresh data and sends it
   |
A notification is saved; the chat picks it up and shows "sent"
```

Tools are offered to the model only when they are relevant, to save tokens: export tools when the conversation mentions a file or export, scheduling tools when it mentions scheduling or a time.

## Roles

The calling application sends the user's roles in the `X-User-Roles` header (comma separated). Supported roles are `lab`, `accounts` and `admin`.

| Resource | Allowed roles |
| --- | --- |
| `el_orders` collection | lab, accounts, admin |
| `el_order_test_parameter` collection | lab, admin |
| `app_invoice` collection | accounts, admin |
| Export tools (Excel, PDF, chart, Power BI) | lab, accounts, admin |
| `prepare_report_email` | accounts, admin |
| Scheduling tools | accounts, admin |
| `cache_stats` | admin |

Data access is read-only and limited to the collections listed in `ALLOWED_COLLECTIONS` in `agent.py`. The model also gets only the schema of the collections the user's role may read.

**Important:** if the `X-User-Roles` header is missing, no role restriction is applied. The calling application must always send it.

## Project structure

| File / folder | Purpose |
| --- | --- |
| `api.py` | FastAPI service: conversations, files, email and schedule confirmation, voice, notifications, API keys |
| `app.py` | Streamlit chat interface |
| `agent.py` | Conversation loop: model calls and fallbacks, tool dispatch, role checks, caching, history trimming, voice transcription |
| `system_prompt.py` | System prompt for the model |
| `tool_definitions.py` | Tool schemas the model can call |
| `db.py` | MongoDB connection |
| `mongo_discovery.py` | Builds the schema catalog from sample documents |
| `mongo_query.py` | Query, count, sum, duration and first/last tools |
| `rag_pipeline.py` | Ingests documents, searches them, stores corrections |
| `multi_hop.py` | Multi-step document search |
| `answer_cache.py` | Answer cache lookup and storage |
| `export_excel.py`, `export_pdf.py`, `export_chart.py`, `export_powerbi.py` | File generation |
| `email_report.py` | Contact lookup, email preparation, sending, audit log |
| `email_scheduler.py` | Scheduled and repeating report emails, background sender, notifications |
| `keystore.py` | API key creation, hashing, verification and revocation |
| `documents/`, `manual_sources/` | Source manuals and schema notes (not in Git) |
| `vector_store/`, `cache/` | Generated search index and caches (not in Git) |
| `exports/`, `charts/` | Generated reports (not in Git) |

The Angular front end is a separate project. Its chat component, `copilot.service.ts` and `wav.util.ts` live there.

## Setup

**Requirements:** Python 3.10 or newer, access to the MongoDB instance, a Gemini API key, and an SMTP account if you want email.

```
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
pip install croniter              # needed by email_scheduler.py
```

Copy `.env.example` to `.env` and fill it in:

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Google Gemini API key |
| `MODEL` | Primary Gemini model name |
| `FALLBACK_MODELS` | Optional. Comma-separated models to try if the primary is busy, rate limited or unavailable |
| `MONGO_URI`, `DB_NAME` | MongoDB connection for the LIMS data (use a read-only user) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` | Outgoing mail account. For Gmail use an app password |
| `INTERNAL_DOMAIN` | Optional. Company domain, used to flag external recipients |
| `ADMIN_API_KEY` | Master key for managing API keys. Needed only if `ENABLE_ADMIN_API=1` |
| `ENABLE_ADMIN_API` | Optional. `1` turns on the `/admin/keys` endpoints (off by default) |
| `ENABLE_DOCS` | Optional. `1` turns on `/docs` (keep off in production) |
| `KEYSTORE_PATH` | Optional. Location of the API key database (default `api_keys.db`) |
| `SCHEDULER_DB_NAME` | Optional. Database that stores scheduled jobs and notifications. Defaults to the LIMS database. It must be writable, so point it at a separate database if the LIMS user is read-only |
| `SCHEDULER_TZ` | Optional. Time zone for scheduled times (default `Asia/Kolkata`) |

Never commit `.env`.

### Build the document index

Put the manuals and schema notes in `documents/` (and `manual_sources/` if used), then:

```
python rag_pipeline.py
```

To rebuild from scratch, delete only the `vector_store/` folder and run it again.

## Running

**API:**

```
uvicorn api:app --host 0.0.0.0 --port 8000
```

Run the API as a single worker for now: conversations and pending emails are held in memory. Starting the API also starts the scheduled-email worker.

**Streamlit app:**

```
streamlit run app.py
```

## Using the API

Create a key for each calling application:

```
python keystore.py create portal
python keystore.py list
python keystore.py revoke <key_id>
```

The key is shown once when created. Only its hash is stored.

Every request needs these headers:

- `X-API-Key`: the key for the calling application.
- `X-User-Id`: the logged-in user, set by the calling application's server. Users are namespaced by application, for example `portal:user-1`.
- `X-User-Roles`: the user's roles, for example `accounts,lab`. Always send it (see Roles above).

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/conversations` | Start a conversation |
| `POST` | `/v1/conversations/{id}/messages` | Send a message. Returns the reply, files, `pending_email` and `pending_schedule` |
| `POST` | `/v1/transcribe` | Send a WAV recording (16 kHz, up to 5 MB). Returns `{"text": "..."}` |
| `GET` | `/v1/artifacts/{id}` | Download a generated file |
| `GET` | `/v1/conversations/{id}/email/pending` | Read the pending email |
| `POST` | `/v1/conversations/{id}/email/confirm` | The user clicked Send |
| `POST` | `/v1/conversations/{id}/email/cancel` | The user clicked Cancel |
| `POST` | `/v1/conversations/{id}/schedule/confirm` | The user clicked Schedule |
| `POST` | `/v1/conversations/{id}/schedule/cancel` | The user cancelled the schedule |
| `GET` | `/v1/notifications` | Messages from the background worker, such as "scheduled email sent". Each is returned once |
| `DELETE` | `/v1/conversations/{id}` | End a conversation |
| `GET` | `/health` | Uptime check |
| `POST`, `GET`, `DELETE` | `/admin/keys` | Manage API keys (needs `X-Admin-Key`, only when enabled) |

Limits: messages up to 4,000 characters, and idle conversations (and their files) are dropped after 2 hours. A request can take 20 to 60 seconds, so use a client timeout of at least 120 seconds.

Example:

```
curl -X POST http://localhost:8000/v1/conversations \
  -H "X-API-Key: <key>" -H "X-User-Id: user-1" -H "X-User-Roles: accounts"

curl -X POST http://localhost:8000/v1/conversations/<id>/messages \
  -H "X-API-Key: <key>" -H "X-User-Id: user-1" -H "X-User-Roles: accounts" \
  -H "Content-Type: application/json" \
  -d '{"message": "how many orders were received last month?"}'
```

The calling application's **server** holds the API key. Do not put it in browser code. For the Angular front end, call a proxy (the dev proxy, or the portal's backend) that adds the three headers.

## Voice input

The browser records audio and converts it to 16 kHz WAV, then sends it to `/v1/transcribe`. The server transcribes it with Gemini (trying the fallback models if needed) and, if that fails, with a local Whisper model. Speech in other languages is translated to English. The text is put in the input box so the user can edit it before sending.

## Emailing reports

1. The user generates a report and asks to email it.
2. The recipient is either an address the user typed in the chat, or a name looked up in the contact collections listed in `CONTACT_SOURCES` in `email_report.py`.
3. The Copilot prepares the email and shows a confirmation with the address, file, subject and message. External addresses and addresses the user did not type are flagged.
4. Nothing is sent until the user clicks **Send**. Each send is written to an audit log, and there is a per-user hourly limit.

Rules built into the code: only files generated in the current conversation can be attached, and the model cannot send to an address the user did not type or that was not found in the contacts.

## Scheduled emails

Example requests:

- "Send this report to name@company.com at 2 pm today"
- "Email me last month's invoices every Monday at 9"
- "Do I have any scheduled emails?"
- "Cancel the weekly one"

How it works:

1. The model calls `schedule_report_email`, which only saves a **draft** and shows a confirmation card in the chat.
2. When the user clicks **Schedule**, the job becomes active.
3. A background worker checks every 30 seconds. At the due time it rebuilds the report with fresh data and emails it.
4. A notification is saved, and the chat shows it within about a minute.

Details:

- **Timing:** exactly one of `run_at` (a date and time), `in_minutes`, or `cron` (5 fields, for example `0 9 * * 1` for Mondays at 9:00). Repeating jobs can run at most once a day.
- **Moving date ranges:** a repeating report uses presets instead of fixed dates: `today`, `yesterday`, `last_7_days`, `last_30_days`, `last_week`, `month_to_date`, `last_month`.
- **Reports that can be scheduled:** Excel (records, joined, summary) and PDF records.
- **Limits:** 10 active jobs per user, a draft expires after 60 minutes, a one-time job more than 24 hours late is not sent, and a repeating job stops after 3 failures in a row.
- **Roles:** `accounts` and `admin` only.
- **Storage:** jobs are saved in the `scheduled_emails` collection and notifications in `scheduler_notifications`, in `SCHEDULER_DB_NAME` or the LIMS database. This is the only part of the system that writes to MongoDB.
- **The worker only runs while the API runs.** If the server is down at the due time, a repeating job runs once when it is back, and a one-time job that is more than 24 hours late is dropped.
- **Management:** the model can list, pause, resume and cancel a user's scheduled emails.

## Security notes

- Database access to LIMS data is read-only, and only the listed collections are exposed to the model.
- The contact collections are not exposed to the query tools.
- API keys are stored hashed, can expire, and can be revoked.
- Roles limit which collections, tools, emails and schedules a user can use. Send `X-User-Roles` on every request.
- Emails and schedules require an explicit confirmation step.
- Scheduled jobs are re-checked against the owner's role each time they run.
- Keep the API on an internal network, behind HTTPS.
- Reports are not yet filtered by branch, department or customer. Until they are, only give a role to people who may see all the data in the collections it unlocks.

## Known limitations and planned work

- Conversations, generated-file records and pending emails are held in memory. Move them to Redis or a database before running several workers.
- Filtering of query results by branch, department and customer.
- Streaming responses.
- Notifications are polled by the chat every 20 seconds. Server-sent events or WebSockets would make them instant.

## Troubleshooting

- **Search returns poor or outdated results:** delete `vector_store/` and run `python rag_pipeline.py` again.
- **Email fails to send:** check the SMTP values in `.env` and restart the app. Gmail needs an app password, not the account password.
- **A name returns no contact:** check the field names in `CONTACT_SOURCES`. A wrong field name returns no results and no error.
- **API returns 401:** the key is wrong, expired or revoked.
- **API returns 403:** the user's role is not allowed to do that.
- **API returns 409:** the previous message in that conversation is still being processed.
- **`400 INVALID_ARGUMENT: Duplicate function declaration found: <tool>`:** the same tool name appears twice in the tool list sent to Gemini. Look for a tool defined in `tool_definitions.py` and also added from `email_scheduler.SCHEDULER_TOOLS`.
- **"Send at 2 pm" sends immediately instead of scheduling:** the scheduling tools are only offered to the model when the message matches `SCHEDULE_WORDS` in `agent.py`. Add patterns for clock times (for example `\d{1,2}(:\d{2})?\s*(am|pm)`) and words such as `tomorrow`.
- **`ModuleNotFoundError: croniter`:** run `pip install croniter`.
- **Scheduled email was sent but no message appeared in the chat:** the chat polls `/v1/notifications`. Check that the endpoint uses the same authentication as the other routes. A notification is saved under the same user id the job was created with, and it is returned only to that user.
- **Scheduled jobs are not saved:** the database user is read-only. Set `SCHEDULER_DB_NAME` to a database it can write to.
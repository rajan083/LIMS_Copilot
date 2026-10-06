# LIMS Copilot

An AI assistant for a Laboratory Information Management System (LIMS). Users ask questions in plain language and the Copilot answers from live LIMS data and from the company's manuals and procedures. It can also build Excel exports and charts, and email a generated report after the user confirms.

It can be used in two ways:

- **Streamlit chat app** (`app.py`) for local use and testing.
- **HTTP API** (`api.py`) so a portal or any other application can embed it.

## What it can do

| Capability | How it works |
|---|---|
| Answer questions from LIMS data | The model calls read-only query tools over MongoDB (list, filter, count, sum, first/last, parameter durations) |
| Answer "how do I..." questions | Semantic search over the manuals and procedure documents (RAG), plus multi-hop search for questions that need several lookups |
| Export to Excel | Single-sheet, multi-sheet and joined exports; nested records are split into linked sheets |
| Charts | PNG (works offline) or interactive HTML |
| Email a report | The user asks, the Copilot prepares the email, and nothing is sent until the user clicks **Send** |
| Answer cache | Standalone questions with a close match to an earlier question are answered from the cache, which saves model calls |
| Learning from corrections | When the user corrects an answer, the correction is stored and used later |

## How it works

```
User message
   ↓
Checks (needs earlier context? cached answer available?)
   ↓
Gemini model  ←→  Tools
                   ├─ MongoDB queries (read-only)
                   ├─ Document search / multi-hop search
                   ├─ Excel export, charts
                   └─ prepare_report_email (prepare only, never sends)
   ↓
Reply + generated files + pending email (if any)
   ↓
User confirms the email → report_email.confirm_and_send
```

Data access is read-only and limited to the collections listed in `ALLOWED_COLLECTIONS` in `agent.py` (`el_orders`, `el_order_test_parameter`, `app_invoice`).

## Project structure

| File / folder | Purpose |
|---|---|
| `app.py` | Streamlit chat interface |
| `api.py` | FastAPI service for other applications |
| `agent.py` | Conversation loop: model calls, tool dispatch, caching, history trimming |
| `system_prompt.py` | System prompt for the model |
| `tool_definitions.py` | Tool schemas the model can call |
| `db.py` | MongoDB connection |
| `mongo_discovery.py` | Builds the schema catalog from sample documents |
| `mongo_query.py` | Query, count, sum, duration and first/last tools |
| `rag_pipeline.py` | Ingests documents and searches them; stores corrections |
| `multi_hop.py` | Multi-step document search |
| `answer_cache.py` | Answer cache lookup and storage |
| `excel_export.py`, `chart_export.py` | Excel and chart generation |
| `report_email.py` | Contact lookup, email preparation, sending, audit log |
| `keystore.py` | API key creation, hashing, verification and revocation |
| `documents/`, `manual_sources/` | Source manuals and schema notes (not in Git) |
| `vector_store/`, `cache/` | Generated search index and caches (not in Git) |
| `exports/`, `charts/` | Generated reports (not in Git) |

## Setup

**Requirements:** Python 3.10 or newer, access to the MongoDB instance, a Gemini API key, and an SMTP account if you want email.

```
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill it in:

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Google Gemini API key |
| `MONGO_URI`, `DB_NAME` | MongoDB connection (use a read-only user) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` | Outgoing mail account. For Gmail use an app password |
| `ADMIN_API_KEY` | Master key that manages API keys (API only) |
| `INTERNAL_DOMAIN` | Optional. Company domain, used to flag external recipients |
| `KEYSTORE_PATH` | Optional. Location of the API key database (default `api_keys.db`) |

Never commit `.env`.

### Build the document index

Put the manuals and schema notes in `documents/` (and `manual_sources/` if used), then:

```
python rag_pipeline.py
```

To rebuild from scratch, delete only the `vector_store/` folder and run it again.

## Running

**Streamlit app:**

```
streamlit run app.py
```

**API:**

```
uvicorn api:app --host 0.0.0.0 --port 8000
```

Run the API as a single worker for now: conversations and email state are held in memory.

## Using the API

Create a key for each calling application:

```
python keystore.py create portal
python keystore.py list
python keystore.py revoke <key_id>
```

The key is shown once when created. Only its hash is stored.

Every request needs two headers:

- `X-API-Key`: the key for the calling application.
- `X-User-Id`: the logged-in user, set by the calling application's server.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/conversations` | Start a conversation |
| `POST` | `/v1/conversations/{id}/messages` | Send a message; returns the reply, files and any pending email |
| `GET` | `/v1/artifacts/{id}` | Download a generated file |
| `POST` | `/v1/conversations/{id}/email/confirm` | The user clicked Send |
| `POST` | `/v1/conversations/{id}/email/cancel` | The user clicked Cancel |
| `DELETE` | `/v1/conversations/{id}` | End a conversation |
| `GET` | `/health` | Uptime check |
| `POST`, `GET`, `DELETE` | `/admin/keys` | Manage API keys (needs `X-Admin-Key`) |

A request can take 20 to 60 seconds, so use a client timeout of at least 120 seconds. Interactive documentation is available at `/docs`; turn it off or restrict it in production.

Example:

```
curl -X POST http://localhost:8000/v1/conversations \
  -H "X-API-Key: <key>" -H "X-User-Id: user-1"

curl -X POST http://localhost:8000/v1/conversations/<id>/messages \
  -H "X-API-Key: <key>" -H "X-User-Id: user-1" -H "Content-Type: application/json" \
  -d '{"message": "how many orders were received last month?"}'
```

The calling application's **server** holds the API key. Do not put it in browser code.

## Emailing reports

1. The user generates a report (Excel or chart) and asks to email it.
2. The recipient can be:
   - an email address the user typed in the chat, or
   - a name, looked up in the contact collections listed in `CONTACT_SOURCES` in `report_email.py`.
3. The Copilot prepares the email and shows a confirmation with the address, file, subject and message.
4. Nothing is sent until the user confirms. Each send is written to `email_audit.jsonl`, and there is a per-user hourly limit.

Rules built into the code: only files generated in the current conversation can be attached, and the model cannot send to an address the user did not type or that was not found in the contacts.

## Security notes

- Database access is read-only, and only the listed collections are exposed to the model.
- The contact collections are not exposed to the query tools.
- API keys are stored hashed, can expire, and can be revoked.
- Emails require an explicit confirmation step.
- Keep the API on an internal network, behind HTTPS.
- Reports are not yet filtered by the user's role, branch or customer. Until they are, only give access to people who may see all of the data the Copilot can reach.

## Known limitations and planned work

- Conversations, generated-file records and pending emails are held in memory. Move them to Redis or a database before running several workers.
- Role-based filtering of query results (role, branch, department, customer).
- PDF reports.
- Scheduled and automatic report sending.
- Streaming responses.

## Troubleshooting

- **Search returns poor or outdated results:** delete `vector_store/` and run `python rag_pipeline.py` again.
- **Email fails to send:** check the SMTP values in `.env` and restart the app. Gmail needs an app password, not the account password.
- **A name returns no contact:** check the field names in `CONTACT_SOURCES`. A wrong field name returns no results and no error.
- **API returns 401:** the key is wrong, expired or revoked.
- **API returns 409:** the previous message in that conversation is still being processed.
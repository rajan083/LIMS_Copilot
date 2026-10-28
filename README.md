# LIMS Copilot

An AI assistant for a Laboratory Information Management System (LIMS). Users ask questions in plain language and the Copilot answers from live LIMS data and from the company's manuals and procedures. It can also build Excel, PDF, chart and Power BI outputs, take voice input, and email a generated report after the user confirms.

It can be used in three ways:

- **Streamlit chat app** (`app.py`) for local use and testing, with voice input.
- **Terminal chat** (`main.py`) with typed or voice input.
- **HTTP API** (`api.py`) so a portal or any other application can embed it.

## What it can do

| Capability | How it works |
|---|---|
| Answer questions from LIMS data | The model calls read-only query tools over MongoDB (list, filter, count, sum, first/last, parameter durations) |
| Answer "how do I..." questions | Semantic search over the manuals and procedure documents (RAG), plus multi-hop search for questions that need several lookups |
| Export to Excel | Single-sheet, multi-sheet, joined and summary exports; nested records are split into linked sheets |
| Export to PDF | Three layouts: `table`, `profile` (one client, order or invoice) and `cards` (one text block per record, no table). Optional de-duplication for unique lists. Limited to 300 rows (ReportLab) |
| PDF from chat text | A definition or explanation already in the chat (for example from the manual) can be turned into a formatted PDF with headings, bullets and bold text |
| Charts | bar, horizontal bar, stacked bar, line, area, pie, donut, scatter, histogram. PNG (works offline) or interactive HTML |
| Power BI | Creates a `.pbids` file that points to a signed, expiring CSV feed. Power BI refreshes from live data |
| Email a report | The user asks, the Copilot prepares the email, and nothing is sent until the user clicks **Send** |
| Voice input | Audio is transcribed (and translated to English) by Gemini, with a local Whisper fallback |
| Role-based access | The calling application passes the user's roles; collections, exports and email are restricted by role |
| Answer cache | Standalone questions are answered from a cache when an earlier question means the same thing, even if worded differently. Small typos also match when the text is almost identical. A cache hit skips the model call completely |
| Learning from corrections | When the user corrects an answer, the correction is stored and used later |

## How it works

```
User message
   ↓
Checks (needs earlier context? cached answer available?)
   ↓
Gemini model  ←→  Tools
                   ├─ MongoDB queries (read-only, role-checked)
                   ├─ Document search / multi-hop search
                   ├─ Excel, PDF, chart and Power BI export
                   └─ prepare_report_email (prepare only, never sends)
   ↓
Reply + generated files + pending email (if any)
   ↓
User confirms the email → email_report.confirm_and_send
```

Data access is read-only and limited to the collections listed in `ALLOWED_COLLECTIONS` in `agent.py` (`el_orders`, `el_order_test_parameter`, `app_invoice`).

## Request-to-response workflow

This is the path of one API request through the code. The Streamlit app and terminal chat call `new_conversation()` and `handle_turn()` directly, so they skip the HTTP and authentication steps.

### 1. Startup (once)

1. `api.py` calls `keystore.init()` to open the API-key database.
2. `warm_up()` loads the embedding model (`all-MiniLM-L6-v2`) and the Chroma collection, so the first real question is not slow.
3. Importing `agent.py` runs `build_schema_catalog(ALLOWED_COLLECTIONS)`, which samples MongoDB documents to learn the fields.
4. `register_feed(app)` adds the `/powerbi/feed.csv` route.

### 2. Start a conversation: `POST /v1/conversations`

1. `authenticate()` checks `X-API-Key` with `keystore.verify_key()` (hash lookup; rejects unknown, expired or revoked keys), validates `X-User-Id`, and builds the user as `<app_name>:<user_id>`, so users from different applications never collide.
2. `get_roles()` reads the optional `X-User-Roles` header (comma-separated).
3. `new_conversation(roles)` creates the message list: the system prompt from `build_system_prompt()` plus the schema of only the collections those roles may see.
4. The API stores the conversation in memory and returns `conversation_id`.

### 3. Send a message: `POST /v1/conversations/{id}/messages`

1. `authenticate()` and `get_roles()` run again. `_get_conv()` returns 404 unless the conversation belongs to this user.
2. A per-conversation lock allows one turn at a time (409 if the previous message is still running).
3. `handle_turn()` runs the steps below.

```
handle_turn(messages, text, session_id, user, roles)
 │
 ├─ 1. note_user_message()                       (kept for the email feature)
 ├─ 2. Fresh chat + text that needs earlier context
 │       → ask the user to clarify, stop
 ├─ 3. Answer cache (standalone, non-repeated, non-export questions only)
 │       lookup_cached_answer(): drop filler words → embed → Chroma query (top 3)
 │       hit if similarity ≥ threshold, or ≥ 0.50 and the text is ≥ 93% identical
 │       (typos); expired entries are skipped → return, no model call
 ├─ 4. _call_llm(messages)
 │       trim to the last 3 turns, replace old results of document search,
 │       describe_field and list_collections with a stub,
 │       add the schema to the system text (skipped for "how do I..." questions),
 │       offer only the document tools for "how do I..." questions; otherwise offer
 │       export tools only if this or the previous message asks for a file,
 │       generate_content with automatic function calling disabled,
 │       retry on rate limit / server errors, then try FALLBACK_MODELS
 ├─ 5. Tool loop (maximum 12 rounds)
 │       for each function call the model returns:
 │         run_tool_call()
 │           a. role checks: TOOL_ROLES and COLLECTION_ROLES → "Access denied"
 │           b. export gate (export_ok, worked out once per turn): this or the previous
│              user message mentions a file or export word, or this one is a short "yes"
 │           c. dispatch to the tool (table below)
 │         the result goes back to the model, then _call_llm() runs again
 │       • prepare_report_email returns "awaiting_user_confirmation"
 │         → reply "files are ready, confirm below" and stop
 │       • 12 rounds reached → one last call with tools disabled
 ├─ 6. Empty reply → up to 2 retries, then a fallback message
 ├─ 7. store_cached_answer(), only if the turn used only document search,
 │       created no files, was not an export request, and started a fresh conversation
 └─ 8. note_assistant_message(), learn_if_correction()
```

4. Back in `api.py`, `_public_artifacts()` replaces server file paths with random download IDs (paths are never sent to the client) and `_public_pending()` adds the email draft, if any.
5. The response is `{reply, artifacts[], pending_email}`.

### 4. What each tool calls

| Tool | Internal path |
|---|---|
| `list_collections`, `describe_field`, `distinct_values`, `query_collection`, `count_by_field`, `sum_by_field`, `first_or_last_by_field`, `analyze_parameter_durations` | `mongo_query.*` → `db.py` (read-only MongoDB) |
| `export_records_to_excel`, `export_joined_records_to_excel`, `export_summary_to_excel` | `_fetch_records` → `mongo_query` → `_build_normalized_export` / `_apply_columns` → `export_excel` → `.xlsx` artifact |
| `export_records_to_pdf` | `_fetch_records` → `_apply_columns` (with `dedupe`) or `_build_normalized_export` → `create_pdf` (`export_pdf.py`) → `.pdf` artifact |
| `export_text_to_pdf` | `create_text_pdf` (`export_pdf.py`) → `.pdf` artifact. Takes a title and text from the chat, not database records |
| `generate_chart` | `export_chart.generate_chart` → PNG or HTML artifact |
| `create_powerbi_file` | `sign_spec()` → `.pbids` artifact pointing to `/powerbi/feed.csv?t=<token>`. When Power BI refreshes, `verify_token()` → `fetch_rows()` → `rows_to_csv()` |
| `search_documents` | `rag_pipeline.search_documents` → search cache. On a miss: embed the query → Chroma query → relevance filter → `_fetch_related` (linked documents) |
| `multi_hop_search` | `_decompose` (model) → `_retrieve` → `_find_gaps` (model) → `_retrieve` → merged, ranked chunks |
| `prepare_report_email` | `email_report`: resolve the recipient, validate attachments, store a pending email |
| `cache_stats` | `get_cache_stats` (admin only) |

### 5. Other API calls in the same flow

- **Download a file:** `GET /v1/artifacts/{id}` checks the file belongs to this user, then returns it.
- **Send the email:** `POST .../email/confirm` checks the role, then `confirm_and_send()` applies the hourly limit, calls `_smtp_send()`, and appends to `email_audit.jsonl`.
- **Cancel the email:** `POST .../email/cancel`.
- **Voice:** `POST /v1/transcribe` runs `transcribe_bytes()`: Gemini first (with retries and fallback models), then local Whisper.
- **Cleanup:** conversations and files idle for 2 hours are dropped, or end one with `DELETE /v1/conversations/{id}`.

## Project structure

| File / folder | Purpose |
|---|---|
| `app.py` | Streamlit chat interface with voice input |
| `main.py` | Terminal chat (type, or `/v` for voice) |
| `api.py` | FastAPI service for other applications |
| `agent.py` | Conversation loop: model calls, tool dispatch, role checks, caching, history trimming, transcription |
| `system_prompt.py` | System prompt for the model |
| `tool_definitions.py` | Tool schemas the model can call |
| `db.py` | MongoDB connection |
| `mongo_discovery.py` | Builds the schema catalog from sample documents |
| `mongo_query.py` | Query, count, sum, duration and first/last tools |
| `rag_pipeline.py` | Ingests documents and searches them; stores corrections |
| `multi_hop.py` | Multi-step document search |
| `answer_cache.py` | Answer cache lookup and storage |
| `manual_qa.py` | Turns manual PDFs in `manual_sources/` into per-page documents in `documents/manuals/` using the model |
| `schema_docs.py` | Generates schema documents for the allowed collections |
| `export_excel.py`, `export_pdf.py`, `export_chart.py` | Excel, PDF (table, profile and cards layouts, and text PDFs) and chart generation |
| `export_powerbi.py` | Signed Power BI feed and `.pbids` file creation |
| `email_report.py` | Contact lookup, email preparation, sending, audit log |
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

PDF export uses `reportlab` and PNG charts use `kaleido`; both are listed in `requirements.txt`.

Optional: to print symbols such as the rupee sign in PDFs, put `DejaVuSans.ttf` and `DejaVuSans-Bold.ttf` in a `fonts/` folder next to `export_pdf.py`. Without them the PDFs use Helvetica.

Create a `.env` file in the project folder:

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Google Gemini API key |
| `MODEL` | Main Gemini model name (also used for voice transcription) |
| `FALLBACK_MODELS` | Optional. Comma-separated models to try if the main one is rate limited or unavailable |
| `MONGO_URI`, `DB_NAME` | MongoDB connection (use a read-only user) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` | Outgoing mail account. For Gmail use an app password |
| `INTERNAL_DOMAIN` | Optional. Company domain, used to flag external recipients |
| `MAX_ATTACH_MB` | Optional. Maximum email attachment size in MB (default 20) |
| `POWERBI_FEED_BASE_URL` | Public base URL of this API as Power BI will reach it (required at startup) |
| `POWERBI_FEED_SECRET` | Secret used to sign Power BI feed links |
| `POWERBI_TOKEN_TTL_DAYS` | Optional. How long a Power BI link stays valid (default 30) |
| `ADMIN_API_KEY`, `ENABLE_ADMIN_API` | Enable the `/admin/keys` endpoints (set `ENABLE_ADMIN_API=1` and a key) |
| `ENABLE_DOCS` | Optional. Set to `1` to serve `/docs` (keep off in production) |
| `KEYSTORE_PATH` | Optional. Location of the API key database (default `api_keys.db`) |

Never commit `.env`.

Note: `api.py` imports `export_powerbi`, which reads `POWERBI_FEED_BASE_URL` at import time, so the API will not start without it.

### Build the document index

Put the manuals and schema notes in `documents/` (and `manual_sources/` if used), then:

```
python rag_pipeline.py
```

To convert manual PDFs placed in `manual_sources/` into searchable pages, run `python manual_qa.py`. To rebuild the index from scratch, delete only the `vector_store/` folder and run `rag_pipeline.py` again.

## Running

**Streamlit app:**

```
streamlit run app.py
```

**Terminal chat:**

```
python main.py
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

Every request needs these headers:

- `X-API-Key`: the key for the calling application.
- `X-User-Id`: the logged-in user, set by the calling application's server.
- `X-User-Roles`: comma-separated roles of that user, for example `accounts` or `lab,admin`. **Always send it.** If it is missing, no role restrictions are applied.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/conversations` | Start a conversation |
| `POST` | `/v1/conversations/{id}/messages` | Send a message; returns the reply, files and any pending email |
| `POST` | `/v1/transcribe` | Send raw WAV audio (up to 5 MB); returns the transcript |
| `GET` | `/v1/artifacts/{id}` | Download a generated file |
| `GET` | `/v1/conversations/{id}/email/pending` | Show the pending email, if any |
| `POST` | `/v1/conversations/{id}/email/confirm` | The user clicked Send |
| `POST` | `/v1/conversations/{id}/email/cancel` | The user clicked Cancel |
| `DELETE` | `/v1/conversations/{id}` | End a conversation |
| `GET` | `/health` | Uptime check |
| `GET` | `/powerbi/feed.csv?t=<token>` | Live data for a Power BI file (signed link, no API key) |
| `POST`, `GET`, `DELETE` | `/admin/keys` | Manage API keys (needs `X-Admin-Key`) |

A request can take 20 to 60 seconds, so use a client timeout of at least 120 seconds. Messages are limited to 4000 characters.

Example:

```
curl -X POST http://localhost:8000/v1/conversations \
  -H "X-API-Key: <key>" -H "X-User-Id: user-1" -H "X-User-Roles: accounts"

curl -X POST http://localhost:8000/v1/conversations/<id>/messages \
  -H "X-API-Key: <key>" -H "X-User-Id: user-1" -H "X-User-Roles: accounts" \
  -H "Content-Type: application/json" \
  -d '{"message": "how many orders were received last month?"}'
```

The calling application's **server** holds the API key. Do not put it in browser code.

## Roles and permissions

The calling application decides who the user is and sends their roles. The Copilot enforces these rules:

| Collection | Roles allowed |
|---|---|
| `el_orders` | `lab`, `accounts`, `admin` |
| `el_order_test_parameter` | `lab`, `admin` |
| `app_invoice` | `accounts`, `admin` |

| Action | Roles allowed |
|---|---|
| Excel, PDF (records and text) and Power BI exports | `accounts`, `lab`, `admin` |
| Prepare and send report email | `accounts`, `admin` |
| Cache statistics | `admin` |

A user without the right role gets an "Access denied" result, and the model is told not to look for another way to the data. The schema shown to the model also leaves out collections the user cannot access. Roles are defined in `COLLECTION_ROLES`, `EXPORT_ROLES` and `TOOL_ROLES` in `agent.py`.

## Emailing reports

1. The user generates a report (Excel, PDF or chart) and asks to email it.
2. The recipient can be:
   - an email address the user typed in the chat, or
   - a name, looked up in the contact collections listed in `CONTACT_SOURCES` in `email_report.py`.
3. The Copilot prepares the email and shows a confirmation with the address, file, subject and message.
4. Nothing is sent until the user confirms. Each send is written to `email_audit.jsonl`, and there is a limit of 10 emails per user per hour.

Rules built into the code: only files generated in the current conversation can be attached, and the model cannot send to an address the user did not type or that was not found in the contacts.

## PDF output

Two tools create PDFs, both with a coloured title block, the generation time, and page numbers on every page.

**`export_records_to_pdf`** builds a PDF from a database query. Pick the layout by what you ask for:

| Layout | Use it for | Page |
|---|---|---|
| `table` | A plain list of records | Landscape |
| `profile` | The details of ONE client, order or invoice. Fields that are the same in every record are printed as `Label: value` lines, the rest go in a table | Portrait |
| `cards` | Details of several records as text, not a table. One block per record, the first column is the heading, and empty values (`0`, `NA`) are hidden | Portrait |

Set `dedupe` to true for a unique list. Client details come from order records, so without it each client appears once per order. Rows are only removed when every selected column is identical.

Examples: "give me all the details of all the clients in a pdf as text, not a table" uses `cards` with `dedupe`.

**`export_text_to_pdf`** turns text that is already in the chat into a formatted PDF. The model can use `## Heading`, `- bullet` lines and `**bold**`. Example: "give me the definition of app resource list in a pdf".

If a PDF tool call fails, the model is told not to switch layouts on its own, but to say what failed and ask.

## Power BI

1. The user asks for a Power BI file. The Copilot creates a `.pbids` file.
2. The user opens it in Power BI Desktop and chooses **Anonymous** if asked.
3. Power BI reads `/powerbi/feed.csv?t=<token>`. The token contains the query (records, count or sum), is signed with `POWERBI_FEED_SECRET`, and expires after `POWERBI_TOKEN_TTL_DAYS`.

Anyone who has the link can read that data until it expires, so treat `.pbids` files like the data itself.

## Logging and token usage

Everything is logged to `lims_copilot.log` (UTF-8) and the terminal:

- `[tokens]` lines: input and output tokens for every model call, and a `TURN TOTAL` per message.
- `[tool_call]` and `[tool_result]` lines: which tool was called, with which arguments, and what came back.
- `[answer_cache]` lines: `HIT` (with similarity and text ratio), `MISS` (with the nearest question), `STORED` or `SKIPPED` and why.
- `[multi_hop] tokens` lines: the extra model calls inside multi-hop search. These are **not** included in `TURN TOTAL`.

The log contains the questions users type and the data returned, so keep it private. `*.log` is in `.gitignore`, so it is not committed.

**Where the tokens go.** Each model call resends the system prompt, the schema and the tool definitions, so that fixed part is most of the cost of a request. To keep it down:

- "How do I..." questions get only the document tools, and no database schema.
- Export tools are offered only when the user asks for a file.
- Old results of document search, `describe_field` and `list_collections` are replaced by a short note in later turns.
- The `describe_field` result leaves out bookkeeping fields (`allSelected`, `select`, `selected`, `version`).
- Only the last 3 turns are sent.
- Cached answers skip the model entirely.

Multi-hop search makes two extra model calls, so it costs more than a plain document search. Its tool description tells the model to use it only when the answer needs information from more than one place, and to use plain document search for single-fact lookups.

## Security notes

- Database access is read-only, and only the listed collections are exposed to the model.
- Role checks run on every tool call, not only in the prompt.
- The contact collections are not exposed to the query tools.
- API keys are stored hashed, can expire, and can be revoked.
- Emails require an explicit confirmation step.
- Power BI links are signed and expire, but they do not use the API key.
- If `X-User-Roles` is not sent, role restrictions are skipped. The calling application must always send it.
- Keep the API on an internal network, behind HTTPS.
- Roles limit which collections a user can reach, but results are not yet filtered by branch, department or customer.

## Known limitations and planned work

- Conversations, generated-file records and pending emails are held in memory. Move them to Redis or a database before running several workers.
- Filtering of query results by branch, department or customer.
- Defaulting to no access when roles are missing.
- Scheduled and automatic report sending.
- Streaming responses.
- A unique client list is built from order records, so near-duplicates stay separate (for example the same client with a different name suffix, or `NA`, `na` and `zzs` as the GST value). Reading from a clients collection, or cleaning those entries, would give a more exact list.

## Troubleshooting

- **API does not start, `KeyError: POWERBI_FEED_BASE_URL`:** add the Power BI variables to `.env`.
- **Search returns poor or outdated results:** delete `vector_store/` and run `python rag_pipeline.py` again.
- **Email fails to send:** check the SMTP values in `.env` and restart the app. Gmail needs an app password, not the account password.
- **A name returns no contact:** check the field names in `CONTACT_SOURCES`. A wrong field name returns no results and no error.
- **"Access denied" for a user:** check the roles sent in `X-User-Roles` against the tables above.
- **API returns 401:** the key is wrong, expired or revoked.
- **API returns 409:** the previous message in that conversation is still being processed.
- **PDF export fails with `ModuleNotFoundError: reportlab`:** run `pip install -r requirements.txt` again.
- **A PDF shows a table when you wanted text:** ask for it "as text, not a table". The model then uses the `cards` layout.
- **Symbols like the rupee sign show as boxes in a PDF:** add the DejaVu fonts to a `fonts/` folder (see Setup).
- **Voice transcription is slow:** the Gemini call failed and the local Whisper model is being used.
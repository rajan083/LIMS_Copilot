import os
import json
import re
import time
import uuid
import tempfile
import logging
from dotenv import load_dotenv
from google import genai
from google.genai import types

from system_prompt import build_system_prompt
from excel_export import export_excel_file, export_excel_file_multi_sheet
from chart_export import generate_chart
from tool_definitions import tools
from report_email import (note_user_message, note_assistant_message, note_artifact, prepare_report_email)
from rag_pipeline import (search_documents as run_search_documents, get_cache_stats, learn_if_correction, _looks_like_correction, _pending_turns, _normalize_query)
from answer_cache import lookup_cached_answer, store_cached_answer
from multi_hop import  multi_hop_search as run_multi_hop_search
from mongo_discovery import build_schema_catalog, get_shallow_summary, describe_field as describe_field_lookup
from mongo_query import (query_collection as run_query_collection, distinct_values as run_distinct_values, MAX_RESULTS, first_or_last_by_field as run_first_or_last_by_field,
    analyze_parameter_durations as run_analyze_parameter_durations, count_by_field as run_count_by_field, sum_by_field as run_sum_by_field,)


load_dotenv()

genai_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

MODEL= os.environ.get("MODEL")

ALLOWED_COLLECTIONS = ["el_orders", "el_order_test_parameter", "app_invoice"]
SCHEMA_CATALOG = build_schema_catalog(collection_names=ALLOWED_COLLECTIONS)

MAX_ROUND_TOOLS = 12

ANSWER_CACHEABLE_TOOLS = {"search_documents", "multi_hop_search", "list_collections", "describe_field"}

MIDCHAT_CACHE_THRESHOLD = 0.75

_FOLLOWUP_RE = re.compile(
    r"\b(it|its|that|this|those|these|them|they|above|previous|earlier|same|"
    r"former|latter|again|another|more)\b|^(and|also|but|so|then|what about|how about)\b",
    re.IGNORECASE,
)

_NEEDS_CONTEXT_RE = re.compile(
    r"\b(still (unable|not able|confused|(do not|don'?t|cannot|can'?t) understand)|"
    r"more (descriptive|detailed|detail|elaborate)|"
    r"(explain|elaborate on|describe)\s+(it|that|this|them)\b|"
    r"(you|u) (said|mentioned|told|gave)|as (you )?(said|mentioned)|"
    r"(the|your) (above|previous|last|earlier) (answer|reply|response|steps?))\b",
    re.IGNORECASE,
)


EXPORT_TOOLS = {"export_records_to_excel", "export_joined_records_to_excel", "export_summary_to_excel"}

EXPORT_WORDS = re.compile(r"\b(export\w*|excel|xlsx|spreadsheet|sheets?|csv|download\w*)\b", re.I)

YES_WORDS = re.compile(r"^\s*(yes|yeah|yep|sure|ok|okay|please do|go ahead|do it)\b", re.I)

def _export_allowed(user_text, prev_model_text=""):
    text = user_text or ""
    if EXPORT_WORDS.search(text):
        return True
    return bool(YES_WORDS.search(text) and re.search(r"\b(export\w*|excel|file|spreadsheet|report)\b", prev_model_text or "", re.I))


def in_standalone_question(text):
    text = (text or "").strip()
    return (
        len(text.split()) >= 2
        and not _FOLLOWUP_RE.search(text)
        and not _looks_like_correction(text)
        and not _NEEDS_CONTEXT_RE.search(text)
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("lims_copilot.log"),   # persists to disk
        logging.StreamHandler(),                    # still shows in terminal
    ],
)
logger = logging.getLogger("lims.agent")


def _to_gemini_tools(openai_tools):
    declarations = []
    for t in openai_tools:
        fn = t["function"]
        declarations.append(
            types.FunctionDeclaration(
                name=fn["name"],
                description=fn.get("description", ""),
                parameters_json_schema=fn.get("parameters") or {"type": "object", "properties": {}},
            )
        )
    return [types.Tool(function_declarations=declarations)]


GEMINI_TOOLS = _to_gemini_tools(tools)
GEMINI_TOOLS_NO_EXPORT= _to_gemini_tools([t for t in tools if t['function']['name'] not in EXPORT_TOOLS])


def _user_text(text):
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


def _model_text(text):
    return types.Content(role="model", parts=[types.Part.from_text(text=text)])


def _response_content(response):
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    return candidates[0].content


def _response_parts(response):
    content = _response_content(response)
    if content is None or not content.parts:
        return []
    return content.parts


def _response_text(response):
    return "".join(
        p.text for p in _response_parts(response)
        if getattr(p, "text", None) and not getattr(p, "thought", False)
    ).strip()


def _response_function_calls(response):
    return [p.function_call for p in _response_parts(response) if getattr(p, "function_call", None)]


def _normalize_args(value):
    """JSON numbers can arrive as floats (e.g. limit=10.0); PyMongo's .limit() needs an int."""
    if isinstance(value, dict):
        return {k: _normalize_args(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_args(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _function_response_part(function_call, result):
    payload = json.loads(json.dumps(_compact_tool_result_for_context(result), default=str))
    return types.Part(
        function_response=types.FunctionResponse(
            id=getattr(function_call, "id", None),
            name=function_call.name,
            response={"result": payload},
        )
    )


def _log_usage(response, session_id="default", turn_id="", label=""):
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return 0, 0
    prompt_tokens = getattr(usage, "prompt_token_count", None) or 0
    thought_tokens = getattr(usage, "thoughts_token_count", None) or 0
    output_tokens = (getattr(usage, "candidates_token_count", None) or 0) + thought_tokens
    total_tokens = getattr(usage, "total_token_count", None) or (prompt_tokens + output_tokens)
    logger.info(
        f"[{session_id}|{turn_id}] [tokens]{' ' + label if label else ''} "
        f"input={prompt_tokens} output={output_tokens} "
        f"(thinking={thought_tokens}) total={total_tokens}"
    )
    return prompt_tokens, output_tokens


def _is_user_turn_start(m):
    """True only for a real user text message. Function-response turns also use
    role 'user' in the native API, so they must not count as turn boundaries."""
    if isinstance(m, dict):
        return m.get("role") == "user"
    if getattr(m, "role", None) != "user":
        return False
    parts = getattr(m, "parts", None) or []
    has_text = any(getattr(p, "text", None) for p in parts)
    has_fn_response = any(getattr(p, "function_response", None) for p in parts)
    return has_text and not has_fn_response


def _trim_messages(msgs, keep_last_turns=3):
    system_msg = msgs[0]
    turn_starts = [i for i, m in enumerate(msgs) if _is_user_turn_start(m)]
    if len(turn_starts) <= keep_last_turns:
        return msgs
    cutoff = turn_starts[-keep_last_turns]
    return [system_msg] + msgs[cutoff:]


DOC_TOOLS = {"search_documents", "multi_hop_search"}
_STUB = {"note": "Earlier document-search results were removed. If the new question "
                 "needs details from the documents, search again."}


def _stub_old_doc_results(msgs):
    """Replace document-search results from PREVIOUS turns with a stub.
    The stored history is not changed, only what is sent to the model."""
    last_turn_start = max((i for i, m in enumerate(msgs) if _is_user_turn_start(m)), default=0)
    out = []
    for i, m in enumerate(msgs):
        if i < last_turn_start and not isinstance(m, dict) and getattr(m, "role", None) == "user":
            parts = getattr(m, "parts", None) or []
            if any(getattr(p, "function_response", None) and p.function_response.name in DOC_TOOLS
                   for p in parts):
                new_parts = []
                for p in parts:
                    fr = getattr(p, "function_response", None)
                    if fr and fr.name in DOC_TOOLS:
                        new_parts.append(types.Part(function_response=types.FunctionResponse(
                            id=getattr(fr, "id", None), name=fr.name, response={"result": _STUB})))
                    else:
                        new_parts.append(p)
                m = types.Content(role="user", parts=new_parts)
        out.append(m)
    return out


def _extract_retry_seconds(err_str, default=1.0):
    match = re.search(r"(?:try again|retry) in (\d+(?:\.\d+)?)\s*ms", err_str, re.IGNORECASE)
    if match:
        return float(match.group(1)) / 1000.0
    match = re.search(r"(?:try again|retry) in (\d+(?:\.\d+)?)\s*s", err_str, re.IGNORECASE)
    if match:
        return float(match.group(1))
    match = re.search(r"retryDelay['\"]?\s*:\s*['\"](\d+(?:\.\d+)?)s", err_str)
    if match:
        return float(match.group(1))
    return default


def _is_rate_limited(e):
    status = getattr(e, "code", None) or getattr(e, "status_code", None) \
        or getattr(getattr(e, "response", None), "status_code", None)
    text = str(e).lower()
    # NOTE: don't match the bare substring "rate": it appears inside "generate"
    # (e.g. "generateContent") and would flag unrelated errors as rate limits.
    return status == 429 or "429" in text or "resource_exhausted" in text or "rate limit" in text


def _is_transient_server_error(e):
    status = getattr(e, "code", None) or getattr(e, "status_code", None)
    text = str(e).lower()
    return status in (500, 502, 503, 504) or "unavailable" in text or "overloaded" in text


def _validate_completion(response):
    candidates = getattr(response, "candidates", None)
    if not candidates:
        feedback = getattr(response, "prompt_feedback", None)
        raise RuntimeError(
            f"Model returned no candidates (no choices; upstream failure or blocked prompt): {feedback or response}"
        )
    finish = getattr(candidates[0], "finish_reason", None)
    finish_name = getattr(finish, "name", str(finish)) if finish else None
    if finish_name and finish_name not in ("STOP", "MAX_TOKENS"):
        logger.warning(f"[llm] unusual finish_reason={finish_name}")
    return response


def _call_llm(msgs, trim=True, max_retries=3, allow_tools=True, model=None):
    payload = _trim_messages(msgs) if trim else msgs
    payload = _stub_old_doc_results(payload)
    system_text = payload[0]["content"]   # msgs[0] is always the system prompt dict
    contents = payload[1:]

    last_user = next((m for m in reversed(msgs) if _is_user_turn_start(m)), None)
    user_said = "".join(p.text for p in (getattr(last_user, "parts", None) or []) if getattr(p, "text", None))
    config_kwargs = dict(
        system_instruction=system_text,
        tools=GEMINI_TOOLS if _export_allowed(user_said) else GEMINI_TOOLS_NO_EXPORT,
        max_output_tokens=4096,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    if not allow_tools:
        config_kwargs["tool_config"] = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="NONE")
        )
    config = types.GenerateContentConfig(**config_kwargs)

    attempt = 0
    while True:
        try:
            response = genai_client.models.generate_content(
                model=model or MODEL,
                contents=contents,
                config=config,
            )
            return _validate_completion(response)
        except Exception as e:
            err_str = str(e)
            retryable = (
                _is_rate_limited(e)
                or _is_transient_server_error(e)
                or "no choices" in err_str.lower()
            )
            if retryable and attempt < max_retries:
                wait_s = _extract_retry_seconds(err_str) if _is_rate_limited(e) else 1.5
                time.sleep(wait_s)
                attempt += 1
                continue

            raise


def transcribe_with_model(audio_bytes, mime_type="audio/wav", translate=False):
    if not MODEL:
        raise RuntimeError("MODEL is not set")
    if translate:
        instruction = (
            "Transcribe this audio. If the speech is not in English, translate it into English. "
            "Return only the final English text, with no commentary. "
            "If there is no intelligible speech, return exactly: NO_SPEECH"
        )
    else:
        instruction = (
            "Transcribe this audio exactly, in the language spoken. Return only the transcript, "
            "with no commentary. If there is no intelligible speech, return exactly: NO_SPEECH"
        )
    response = genai_client.models.generate_content(
        model=MODEL,
        contents=[instruction, types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)],
        config=types.GenerateContentConfig(
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    text = (response.text or "").strip()
    return "" if text.upper().startswith("NO_SPEECH") else text


def transcribe_local(path, translate=False):
    import whisper
    import numpy as np
    from math import gcd
    from scipy.io import wavfile
    from scipy.signal import resample_poly
    
    sr, data= wavfile.read(path)
    audio= data.astype(np.float32)/32768
    if audio.ndim > 1:
        audio= audio.mean(axis=1)
    if sr != 16000:
        g= gcd(16000, sr)
        audio= resample_poly(audio, 16000//g, sr//g).astype(np.float32)
        
    model= whisper.load_model('small')
    response= model.transcribe(audio, fp16= False, task= "translate" if translate else "transcribe")
    return response['text']


def transcribe_bytes(audio_bytes, retries=3):
    for attempt in range(retries):
        try:
            return transcribe_with_model(audio_bytes, "audio/wav", translate=True)
        except Exception as e:
            logger.warning(f"[transcribe] attempt { attempt+1} failed: {e}")
            if attempt < retries-1:
                time.sleep(2*(attempt+1))
    
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path= tmp.name

    try:
        return transcribe_local(tmp_path, translate=True)
    finally:
        os.remove(tmp_path)


def _fetch_records(args):
    result = run_query_collection(
        SCHEMA_CATALOG,
        args["collection_name"],
        filters=args.get("filters"),
        date_field=args.get("date_field"),
        date_from=args.get("date_from"),
        date_to=args.get("date_to"),
        fields=args.get("fields") or args.get("columns"),
        limit=args.get("limit"),
        bypass_cap=True,
    )
    return result.get("records", []), result.get("error")


def _fetch_join_side(side):
    fields = side.get("fields")
    join_key = side.get("join_key")
    fetch_args = side
    if fields and join_key and join_key not in fields:
        fetch_args = dict(side)
        fetch_args["fields"] = fields + [join_key]
    return _fetch_records(fetch_args)

'''converts the values in the dict provided into ("name"."subname"), which is easier to implement in excel instead of dict.'''
def _get_nested(rec, dotted_key):
    return _get_nested_parts(rec, dotted_key.split("."))


def _get_nested_parts(value, parts):
    if not parts:
        if isinstance(value, list):
            return ", ".join(str(v) for v in value if v not in (None, ""))
        return value

    if isinstance(value, list):
        extracted = [_get_nested_parts(item, parts) for item in value]
        extracted = [str(v) for v in extracted if v not in (None, "")]
        return ", ".join(extracted)

    if isinstance(value, dict):
        return _get_nested_parts(value.get(parts[0], ""), parts[1:])

    return ""

'''converts the dict to a list, of the record, if it is not a string value.'''
def _extract_fields(rec, fields):
    if not fields:
        return dict(rec)
    return {f: _get_nested(rec, f) for f in fields}


'''converts the list or dict to string, to be able to write in the excel columns as names'''
def _excel_safe(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value


def _apply_columns(records_list, columns=None, dedupe=False):
    if columns:
        records_list = [
            {col: _excel_safe(_get_nested(rec, col)) for col in columns} for rec in records_list
        ]
    else:
        records_list = [
            {k: _excel_safe(v) for k, v in rec.items()} for rec in records_list
        ]
    if dedupe:
        seen = set()
        unique = []
        for rec in records_list:
            key = json.dumps(rec, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                unique.append(rec)
        records_list = unique
    return records_list



def _flatten_record(doc, prefix=""):
    flat = {}
    for key, value in doc.items():
        if key == "_id" and prefix == "":
            flat["_id"] = value
            continue
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten_record(value, path))
        elif isinstance(value, list):
            if value and isinstance(value[0], dict):
                continue  # handled by _extract_array_object_fields instead
            flat[path] = ", ".join(str(v) for v in value) if value else ""
        else:
            flat[path] = value
    return flat


def _extract_array_object_fields(doc):
    return {
        key: value for key, value in doc.items()
        if isinstance(value, list) and value and isinstance(value[0], dict)
    }


def _dedupe_rows(rows):
    seen = set()
    unique = []
    for rec in rows:
        key = json.dumps(rec, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(rec)
    return unique


def _build_normalized_export(records):

    main_rows = []
    child_rows = {}

    for rec in records:
        rec_id = str(rec.get("_id", ""))
        arrays = _extract_array_object_fields(rec)
        flat = _flatten_record(rec)
        main_rows.append({k: _excel_safe(v) for k, v in flat.items()})

        for field_name, items in arrays.items():
            rows = child_rows.setdefault(field_name, [])
            for item in items:
                row = {"record_id": rec_id}
                item_flat = _flatten_record(item) if isinstance(item, dict) else {"value": item}
                row.update({k: _excel_safe(v) for k, v in item_flat.items()})
                rows.append(row)

    sheets = {"Main": main_rows}
    for field_name, rows in child_rows.items():
        sheets[field_name[:31]] = rows  # Excel sheet-name length limit
    return sheets


def new_conversation():
    return [{"role": "system", "content": build_system_prompt()}]


def _compact_tool_result_for_context(result, max_items=10):
    if not isinstance(result, dict):
        return result

    compacted = dict(result)
    
    def _value(g):
        sums= g.get("sums") if isinstance(g, dict) else None
        return next(iter(sums.values()), 0) if isinstance(sums, dict) else 0
    
    if isinstance(compacted.get("groups"), list) and not compacted.get("date_bucket_used"):   # new
        compacted["groups"] = sorted(compacted["groups"], key=_value, reverse=True)

    for list_key in ("records", "counts", "groups", "results"):
        items = compacted.get(list_key)
        limit = 30 if list_key == "groups" else max_items
        if isinstance(items, list) and len(items) > limit:
            total = compacted.get("distinct_groups") or compacted.get("total_matching_count") or len(items)
            compacted[list_key] = items[:limit]
            compacted["note"] = (f"Showing {limit} of {total} '{list_key}' entries to save context space. "
                                 f"Ask for a narrower filter or an export if you need the full set.")
            
    if isinstance(compacted.get("groups"), list):
        trimmed_groups = []
        for g in compacted["groups"]:
            if isinstance(g, dict) and isinstance(g.get("duration_seconds"), dict):
                dur = dict(g["duration_seconds"])
                all_vals = dur.get("all_values")
                if isinstance(all_vals, list) and len(all_vals) > max_items:
                    dur["all_values"] = all_vals[:max_items]
                    dur["all_values_note"] = f"showing {max_items} of {len(all_vals)} values"
                g = dict(g)
                g["duration_seconds"] = dur
            trimmed_groups.append(g)
        compacted["groups"] = trimmed_groups

    return compacted

def run_tool_call(function_call, session_id="default", turn_id="", user= None, user_text=""):
    name = function_call.name
    artifact = None
    tag = f"[{session_id}|{turn_id}]"
    
    try:
        # Native SDK gives args as a dict already, no json.loads needed.
        args = _normalize_args(dict(function_call.args or {}))
        
        print(f"[tool_call] {name} args={json.dumps(args, indent=2, default=str)}")
        logger.info(f"{tag}[tool_call] {name} args={json.dumps(args, indent=2, default=str)}")
        
        if name in EXPORT_TOOLS and not _export_allowed(user_text):
            result = {"error": "The user did not ask for a file. Do not export. Show the data in chat, or ask whether they want it exported and wait for their answer."}

        elif name == "list_collections":
            result = get_shallow_summary(SCHEMA_CATALOG)

        elif name == "describe_field":
            result = describe_field_lookup(
                SCHEMA_CATALOG, args["collection_name"], args["field_path"]
            )

        elif name == "distinct_values":
            result = run_distinct_values(
                SCHEMA_CATALOG, args["collection_name"], args["field_path"]
            )

        elif name == "query_collection":
            result = run_query_collection(
                SCHEMA_CATALOG,
                args["collection_name"],
                filters=args.get("filters"),
                date_field=args.get("date_field"),
                date_from=args.get("date_from"),
                date_to=args.get("date_to"),
                fields=args.get("fields"),
                limit=args.get("limit"),
            )

        elif name == "export_records_to_excel":
            sheets_arg = args.get("sheets")
            label = args.get("label")
            columns = args.get("columns")
            dedupe = args.get("dedupe", False)

            def _fetch_export_records(fetch_args, default_dedupe=False):
                cols = fetch_args.get("columns")
                ded = fetch_args.get("dedupe", default_dedupe)
                if ded and cols and len(cols) == 1:
                    dv = run_distinct_values(
                        SCHEMA_CATALOG, fetch_args["collection_name"], cols[0], limit=5000
                    )
                    if "error" in dv:
                        return [], dv["error"]
                    return [{cols[0]: v} for v in dv.get("values", [])], None
                return _fetch_records(fetch_args)

            if sheets_arg:
                sheets_data = {}
                total = 0
                array_field_notes = {}

                for sheet_def in sheets_arg:
                    sheet_name = sheet_def.get("sheet_name", "Sheet")[:31]
                    sheet_columns = sheet_def.get("columns")
                    sheet_dedupe = sheet_def.get("dedupe", False)

                    records, err = _fetch_export_records(sheet_def)
                    if err:
                        sheets_data[sheet_name] = []
                        continue

                    if sheet_columns:
                        already_distinct = sheet_dedupe and len(sheet_columns) == 1
                        if not already_distinct:
                            records = _apply_columns(records, columns=sheet_columns, dedupe=sheet_dedupe)
                        sheets_data[sheet_name] = records
                        total += len(records)
                    else:
                        main_rows = []
                        for rec in records:
                            rec_id = str(rec.get("_id", ""))
                            arrays = _extract_array_object_fields(rec)
                            flat = _flatten_record(rec)
                            main_rows.append({k: _excel_safe(v) for k, v in flat.items()})

                            for field_name, items in arrays.items():
                                rows = sheets_data.setdefault(field_name[:31], [])
                                for item in items:
                                    row = {"record_id": rec_id}
                                    item_flat = _flatten_record(item) if isinstance(item, dict) else {"value": item}
                                    row.update({k: _excel_safe(v) for k, v in item_flat.items()})
                                    rows.append(row)
                                array_field_notes.setdefault(field_name[:31], set()).add(sheet_name)

                        if sheet_dedupe:
                            main_rows = _dedupe_rows(main_rows)

                        sheets_data[sheet_name] = main_rows
                        total += len(main_rows)

                filepath = export_excel_file_multi_sheet(sheets_data, label=label)

                if array_field_notes:
                    note = (
                        f"nested detail fields ({', '.join(array_field_notes)}) were pulled into "
                        f"their own shared sheets across all groups, linked back via 'record_id'"
                    )
                    result = {
                        "message": (
                            f"Exported {total} records across {len(sheets_data)} sheets to {filepath} "
                            f"({note})."
                        )
                    }
                else:
                    result = {"message": f"Exported {total} records across {len(sheets_data)} sheets to {filepath}"}
                artifact = {"type": "excel", "path": filepath}
            else:
                records, err = _fetch_export_records(args, default_dedupe=dedupe)
                if err:
                    result = {"error": err}
                else:
                    already_distinct = dedupe and columns and len(columns) == 1
                    if columns:
                        if not already_distinct:
                            records = _apply_columns(records, columns=columns, dedupe=dedupe)
                        filepath = export_excel_file(records, label=label)
                        result = {"message": f"Exported {len(records)} records to {filepath}"}
                        artifact = {"type": "excel", "path": filepath}
                    else:
                        sheets_data = _build_normalized_export(records)
                        if dedupe:
                            sheets_data["Main"] = _dedupe_rows(sheets_data["Main"])

                        array_sheets = [s for s in sheets_data if s != "Main"]
                        filepath = export_excel_file_multi_sheet(sheets_data, label=label)

                        if array_sheets:
                            note = (
                                f"split into separate linked sheets ({', '.join(array_sheets)}) "
                                f"since those fields hold multiple sub-records per order — each "
                                f"row references the main sheet via 'record_id'"
                            )
                        else:
                            note = "all fields fit cleanly on one sheet"

                        result = {
                            "message": (
                                f"Exported {len(sheets_data['Main'])} records to {filepath} "
                                f"across {len(sheets_data)} sheet(s) ({note})."
                            )
                        }
                        artifact = {"type": "excel", "path": filepath}

        elif name == "export_joined_records_to_excel":
            left = args.get("left", {})
            right = args.get("right", {})
            dedupe = args.get("dedupe", False)
            label = args.get("label")

            left_records, left_err = _fetch_join_side(left)
            if left_err:
                result = {"error": f"Left side error: {left_err}"}
            else:
                right_records, right_err = _fetch_join_side(right)
                if right_err:
                    result = {"error": f"Right side error: {right_err}"}
                else:
                    left_key = left.get("join_key", "_id")
                    right_key = right.get("join_key")

                    right_index = {}
                    for r in right_records:
                        k = str(_get_nested(r, right_key))
                        right_index.setdefault(k, []).append(r)

                    joined = []
                    for l in left_records:
                        lk = str(_get_nested(l, left_key))
                        left_row = _extract_fields(l, left.get("fields"))
                        matches = right_index.get(lk, [])
                        if not matches:
                            joined.append(dict(left_row))
                        for m in matches:
                            row = dict(left_row)
                            row.update(_extract_fields(m, right.get("fields")))
                            joined.append(row)

                    if dedupe:
                        seen = set()
                        unique = []
                        for rec in joined:
                            key = json.dumps(rec, sort_keys=True, default=str)
                            if key not in seen:
                                seen.add(key)
                                unique.append(rec)
                        joined = unique

                    joined = [{k: _excel_safe(v) for k, v in rec.items()} for rec in joined]

                    filepath = export_excel_file(joined, label=label)
                    result = {"message": f"Exported {len(joined)} joined records to {filepath}"}
                    artifact = {"type": "excel", "path": filepath}

        elif name == "analyze_parameter_durations":
            result = run_analyze_parameter_durations(
                SCHEMA_CATALOG,
                args["collection_name"],
                filters=args.get("filters"),
                date_field=args.get("date_field"),
                date_from=args.get("date_from"),
                date_to=args.get("date_to"),
                group_by=args.get("group_by", "parameter.name"),
                result_field=args.get("result_field", "test_result"),
                start_field=args.get("start_field", "analysisStartDate"),
                end_field=args.get("end_field", "analysisEndDate"),
                limit=args.get("limit"),
            )

        elif name == "count_by_field":
            result = run_count_by_field(
                SCHEMA_CATALOG,
                args["collection_name"],
                args["group_by"],
                filters=args.get("filters"),
                date_field=args.get("date_field"),
                date_from=args.get("date_from"),
                date_to=args.get("date_to"),
                date_bucket=args.get("date_bucket", "auto"),
            )

        elif name == "sum_by_field":
            result = run_sum_by_field(
                SCHEMA_CATALOG,
                args["collection_name"],
                args["group_by"],
                args["sum_fields"],
                filters=args.get("filters"),
                date_field=args.get("date_field"),
                date_from=args.get("date_from"),
                date_to=args.get("date_to"),
                date_bucket=args.get("date_bucket", "auto"),
            )

        elif name == "export_summary_to_excel":
            agg = run_sum_by_field(
                SCHEMA_CATALOG,
                args["collection_name"],
                args["group_by"],
                args["sum_fields"],
                filters=args.get("filters"),
                date_field=args.get("date_field"),
                date_from=args.get("date_from"),
                date_to=args.get("date_to"),
                date_bucket=args.get("date_bucket", "month"),
            )
            if "error" in agg:
                result = agg
            else:
                fields = args["sum_fields"]
                rows = []
                for g in agg.get("groups", []):
                    row = {args["group_by"]: g["group"], "records": g["count"]}
                    for f in fields:
                        row[f] = g["sums"].get(f, 0)
                    rows.append(row)
                total = {args["group_by"]: "TOTAL", "records": agg.get("total_matching_count", 0)}
                for f in fields:
                    total[f] = agg.get("overall_totals", {}).get(f, 0)
                rows.append(total)
                filepath = export_excel_file(rows, label=args.get("label"))
                result = {
                    "message": f"Exported {len(rows) - 1} rows plus a total to {filepath}",
                    "overall_totals": agg.get("overall_totals"),
                }
                artifact = {"type": "excel", "path": filepath}


        elif name == "generate_chart":
            chart_result = generate_chart(
                labels=args.get("labels", []),
                series=args["series"],
                chart_type=args.get("chart_type", "bar"),
                title=args.get("title"),
                x_label=args.get("x_label"),
                y_label=args.get("y_label"),
                label=args.get("label"),
                mode=args.get("mode", "offline"),
                width=args.get("width"),
                height=args.get("height"),
                font_size=args.get("font_size"),
            )
            filepath = chart_result["filepath"]
            mode_used = chart_result["mode"]
            note = (
                "self-contained, works with no internet connection" if mode_used == "offline"
                else "small file size, easy to share/email — needs internet to render"
            )
            result = {"message": f"Chart saved to {filepath} ({mode_used} mode: {note})"}
            artifact = {
                "type": "chart_png" if mode_used == "offline" else "chart_html",
                "path": filepath,
            }

        elif name == "search_documents":
            result = run_search_documents(
                args["query"],
                top_k=args.get("top_k", 5),
                min_relevance=args.get("min_relevance", 0.3),
            )
        
        elif name == "multi_hop_search":
            result = run_multi_hop_search(args["question"])

        elif name == "cache_stats":
            result = get_cache_stats()

        elif name == "first_or_last_by_field":
            result = run_first_or_last_by_field(
                SCHEMA_CATALOG,
                args["collection_name"],
                args["group_by"],
                args["target_field"],
                mode=args.get("mode", "first"),
                filters=args.get("filters"),
                date_field=args.get("date_field"),
                date_from=args.get("date_from"),
                date_to=args.get("date_to"),
            )

        elif name == "prepare_report_email":
            result= prepare_report_email(
                session_id, user=user,
                recipient= args.get("recipient"),
                contact_id= args.get("contact_id"),
                file_path= args.get("file_path"),
                file_paths= args.get("file_paths"),
                subject= args.get("subject", "LIMS report"),
                body= args.get("body", "PFA Report.")
            )
        
        else:
            result = {"error": f"Unknown tool: {name}"}

    except Exception as e:
        logger.error(f"{tag} [tool_call error] {name}: {e}")
        result = {"error": str(e)}

    print(f"[tool_result]: {name} ->\n{json.dumps(result, indent=2, default=str)}")
    logger.info(f"[tool_result]: {name} ->\n{json.dumps(result, indent=2, default=str)}")
    return result, artifact



def handle_turn(messages, content, session_id="default", user= None):
    
    turn_id = uuid.uuid4().hex[:8]
    logger.info(f"[{session_id}|{turn_id}] New turn: {content!r}")
    note_user_message(session_id, content)

    # If this turn fails outright, drop everything it appended so the stored
    # history never ends on a dangling tool call / tool result.
    turn_start_len = len(messages)

    def _rollback():
        del messages[turn_start_len:]

    is_fresh_conversation = (len(messages) == 1)


    if is_fresh_conversation and _NEEDS_CONTEXT_RE.search(content):
        reply = ("I don't have an earlier answer to build on in this chat. "
                 "Which topic or screen would you like me to explain?")
        messages.append(_user_text(content))
        messages.append(_model_text(reply))
        learn_if_correction(session_id, content, reply)
        logger.info(f"[{session_id}|{turn_id}] [needs_context] asked user to clarify")
        return reply, messages, []
    
    prev = _pending_turns.get(session_id)
    is_repeat = bool(prev) and _normalize_query(prev["question"]) == _normalize_query(content)
    use_cache = (not is_repeat) and in_standalone_question(content) and "@" not in content

    if not use_cache:
        reason = "repeat of the previous question" if is_repeat else "not a standalone question"
        logger.info(f"[{session_id}|{turn_id}] [answer_cache] SKIPPED ({reason})")

    if use_cache:
        try:
            if is_fresh_conversation:
                cached_reply = lookup_cached_answer(content)
            else:
                cached_reply = lookup_cached_answer(content, threshold=MIDCHAT_CACHE_THRESHOLD)
        except Exception as e:
            logger.warning(f"[{session_id}|{turn_id}] [answer_cache] lookup failed: {e}")
            cached_reply = None

        if cached_reply:
            logger.info(f"[{session_id}|{turn_id}] [answer_cache] HIT")
            note_assistant_message(session_id, cached_reply)
            messages.append(_user_text(content))
            messages.append(_model_text(cached_reply))
            learn_if_correction(session_id, content, cached_reply)
            return cached_reply, messages, []

    messages.append(_user_text(content))
    artifacts = []
    tools_used = set()
    used_fallback = False

    total_input_tokens = 0
    total_output_tokens = 0

    try:
        chat_completion = _call_llm(messages)
    except Exception as e:
        _rollback()
        return f"Error talking to model API: {e}", messages, artifacts

    inp, out = _log_usage(chat_completion, session_id=session_id, turn_id=turn_id, label="initial call")
    total_input_tokens += inp
    total_output_tokens += out

    function_calls = _response_function_calls(chat_completion)
    rounds = 0

    while function_calls and rounds < MAX_ROUND_TOOLS:
        rounds += 1
        # Append the model's turn exactly as returned (it may carry metadata the
        # API expects back alongside the function calls).
        messages.append(_response_content(chat_completion))

        response_parts = []
        email_ready= False
        
        for fc in function_calls:
            tools_used.add(fc.name)
            result, artifact = run_tool_call(fc, session_id=session_id, turn_id=turn_id, user=user, user_text=content)
            if artifact:
                artifacts.append(artifact)
                note_artifact(session_id, artifact["path"])
            response_parts.append(_function_response_part(fc, result))
            if isinstance(result, dict) and result.get("status") == "awaiting_user_confirmation":
                email_ready= True
        messages.append(types.Content(role="user", parts=response_parts))

        if email_ready:
            reply= "The files are ready, Please confirm below to send."
            note_assistant_message(session_id, reply)
            messages.append(_model_text(reply))
            learn_if_correction(session_id, content, reply)
            return reply, messages, artifacts
        
        try:
            chat_completion = _call_llm(messages)
        except Exception as e:
            _rollback()
            return f"Error talking to model API: {e}", messages, artifacts

        inp, out = _log_usage(chat_completion, session_id=session_id, turn_id=turn_id, label=f"round {rounds}")
        total_input_tokens += inp
        total_output_tokens += out

        function_calls = _response_function_calls(chat_completion)

    if function_calls and rounds >= MAX_ROUND_TOOLS:
        messages.append(_response_content(chat_completion))
        pending = [
            types.Part(
                function_response=types.FunctionResponse(
                    id=getattr(fc, "id", None),
                    name=fc.name,
                    response={"result": {"error": "Skipped: tool-call budget reached."}},
                )
            )
            for fc in function_calls
        ]
        pending.append(types.Part.from_text(text=(
            "You've made several tool calls without reaching an answer. "
            "Stop calling tools now — either answer directly with what you "
            "already know, or tell the user plainly what's unclear about "
            "their request."
        )))
        messages.append(types.Content(role="user", parts=pending))
        try:
            chat_completion = _call_llm(messages, allow_tools=False)
            inp, out = _log_usage(chat_completion, session_id=session_id, turn_id=turn_id, label="budget-exhausted call")
            total_input_tokens += inp
            total_output_tokens += out
            reply = _response_text(chat_completion) or (
                "I wasn't able to finish this within the current tool-call budget. "
                "Could you narrow the request a bit (e.g. fewer collections/fields at "
                "once), or ask me to try again?"
            )
        except Exception as e:
            _rollback()
            return f"Error talking to model API: {e}", messages, artifacts

        print(f"[tokens] TURN TOTAL: input={total_input_tokens} output={total_output_tokens} "   
              f"total={total_input_tokens + total_output_tokens}")
        logger.info(f"[tokens] TURN TOTAL: input={total_input_tokens} output={total_output_tokens} "
                      f"total={total_input_tokens + total_output_tokens}")

        note_assistant_message(session_id, reply)
        messages.append(_model_text(reply))
        learn_result = learn_if_correction(session_id, content, reply)
        print(f"[learn_if_correction] {learn_result}")
        logger.info(f"[learn_if_correction] {learn_result}")
        return reply, messages, artifacts

    reply = _response_text(chat_completion)

    empty_retries = 0
    while not reply and empty_retries < 2:
        empty_retries += 1
        # An empty model turn isn't valid to send back, so use a placeholder
        # when there's nothing real to keep.
        last_content = _response_content(chat_completion)
        if last_content is not None and last_content.parts:
            messages.append(last_content)
        else:
            messages.append(_model_text("(empty response)"))
        messages.append(_user_text(
            "Your last reply came back empty. Using the tool results already "
            "gathered above, answer the original question directly now — "
            "don't call any more tools unless something is genuinely still "
            "missing."
        ))
        try:
            chat_completion = _call_llm(messages)
            inp, out = _log_usage(chat_completion, session_id=session_id, turn_id=turn_id, label=f"empty-retry {empty_retries}")
            total_input_tokens += inp
            total_output_tokens += out
            if _response_function_calls(chat_completion):
                reply = None
                break
            reply = _response_text(chat_completion)
        except Exception:
            reply = None
            break

    if not reply:
        used_fallback = True
        reply = (
            "I wasn't able to put together a full answer for this one — it's a "
            "fairly involved request. Could you try breaking it into smaller "
            "steps?"
        )

    print(f"[tokens] TURN TOTAL: input={total_input_tokens} output={total_output_tokens} "
          f"total={total_input_tokens + total_output_tokens}")
    logger.info(f"[tokens] TURN TOTAL: input={total_input_tokens} output={total_output_tokens} "
              f"total={total_input_tokens + total_output_tokens}")

    if (
        is_fresh_conversation
        and in_standalone_question(content)
        and not used_fallback
        and not artifacts
        and tools_used & {"search_documents", "multi_hop_search"}
        and tools_used <= ANSWER_CACHEABLE_TOOLS
    ):
        try:
            store_cached_answer(content, reply)
            logger.info(f"[{session_id}|{turn_id}] [answer_cache] STORED")
        except Exception as e:
            logger.warning(f"[{session_id}|{turn_id}] [answer_cache] store failed: {e}")

    note_assistant_message(session_id, reply)
    messages.append(_model_text(reply))
    learn_result = learn_if_correction(session_id, content, reply)
    print(f"[learn_if_correction] {learn_result} \n")
    return reply, messages, artifacts
import os
import re
import json
import threading
import time
import yaml
from google.genai import types

from mongo_discovery import build_schema_catalog
from mongo_query import distinct_values as run_distinct_values, BULKY_FIELD_PATTERNS
from rag_pipeline import DOCUMENTS_DIR, SCHEMA_DIR, ingest_documents
from agent import ALLOWED_COLLECTIONS, genai_client, MODEL

SCHEMA_DOC_PREFIX = "schema_"

MAX_DISTINCT_VALUES = 30

SKIP_DISTINCT_TYPES = {"array_of_objects", "NoneType"}

CANDIDATE_TYPES = {"str", "bool", "int", "float", "array"}

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
FIELD_DESC_CACHE_PATH = os.path.join(CACHE_DIR, "schema_field_descriptions.json")

DESC_BATCH_SIZE = 40

_STRUCTURAL_LEAVES = {"allSelected", "select"}

LINK_OVERRIDES= {}

def _norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())

def _reference_stem(path):
    parts=[p.split("[]")[0] for p in path.split(".")]
    leaf= parts[-1]
    if leaf == "_id":
        return parts[-2] if len(parts) > 1 else None
    m= re.match(f"^(.+?)(?:_id|Id)$", leaf)
    return m.group(1) if m else None

def _resolve_collection(stem, all_names, self_name):
    cand= _norm(stem)
    if not cand:
        return None
    norms= {n: _norm(n) for n in all_names if n != self_name}
    forms= (cand, cand+"s", cand+"es")
    for form in forms:
        hits= [n for n, nn in norms.items() if nn == form]
        if len(hits) == 1:
            return hits[0]
    for form in forms:
        hits = [n for n, nn in norms.items() if nn.endswith(form)]
        if len(hits)==1:
            return hits[0]
    return None

def _find_related(collection_name, fields, all_names):
    related, unresolved = {},[]
    overrides= LINK_OVERRIDES.get(collection_name, {})
    for path in sorted(fields):
        if path == "_id":
            continue
        target= overrides.get(path)
        if target is None:
            stem= _reference_stem(path)
            if stem is None:
                continue
            target= _resolve_collection(stem, all_names, collection_name)
            if target is None:
                unresolved.append(path)
                continue
        related.setdefault(target, []).append(_mongo_path(path))
    return related, unresolved

def _build_frontmatter(collection_name, n_fields):
    meta={
        "type": "collection",
        "title": collection_name,
        "description": f"Schema reference for the MongoDB collection '{collection_name}'.",
        "tags": ["schema" "mongodb", collection_name],
        "field_count": n_fields,
    }
    return "---\n"+ yaml.safe_dump(meta, sort_keys=False, allow_unicode= True) + "---\n"



def _schema_filename(collection_name):
    return f"{SCHEMA_DOC_PREFIX}{collection_name}.md"


def _leaf(path):
    return path.split(".")[-1].split("[]")[0]


def _is_infra_field(path, type_name):

    leaf = _leaf(path)
    if leaf == "_id" or leaf.endswith("_id"):
        return True
    if leaf in _STRUCTURAL_LEAVES or leaf == "version":
        return True
    if type_name in SKIP_DISTINCT_TYPES:
        return True
    if _is_bulky(path):
        return True
    return False


def _infra_description(path, type_name):
    leaf = _leaf(path)
    if leaf == "_id" or leaf.endswith("_id"):
        return (
            "Internal database identifier for the linked record. Use the "
            "sibling `.name` or `.code` field for human-readable lookups "
            "rather than filtering on this id directly."
        )
    if leaf in _STRUCTURAL_LEAVES:
        return "Internal UI selection-state flag on this linked record; not meaningful for reporting or filtering."
    if leaf == "version":
        return "Internal optimistic-locking version counter on this linked record; not meaningful for reporting or filtering."
    if _is_bulky(path):
        return "Internal file/storage reference (S3 path, signature image, barcode, QR code, etc.); not human-readable, not useful as a filter."
    if type_name in SKIP_DISTINCT_TYPES:
        return "Nested structure — see its sub-fields below for the actual queryable data."
    return "Internal/system field."


def _is_bulky(field_path):
    leaf = field_path.split(".")[-1].split("[]")[0].lower()
    return any(pattern in leaf for pattern in BULKY_FIELD_PATTERNS)


def _mongo_path(flattened_path):
    return flattened_path.replace("[]", "")


def _load_desc_cache():
    if not os.path.exists(FIELD_DESC_CACHE_PATH):
        return {}
    try:
        with open(FIELD_DESC_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[schema_docs] could not read description cache, starting fresh: {e}")
        return {}


def _save_desc_cache(cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp_path = FIELD_DESC_CACHE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
    os.replace(tmp_path, FIELD_DESC_CACHE_PATH)


def _draft_descriptions_via_llm(collection_name, field_entries):
    if not field_entries:
        return {}

    lines = []
    for fe in field_entries:
        sample_note = ""
        if fe.get("sample_values"):
            shown = ", ".join(str(v) for v in fe["sample_values"][:6])
            sample_note = f" Example stored values: {shown}."
        lines.append(f'- "{fe["path"]}" (type: {fe["type"]}).{sample_note}')

    prompt = (
        "You are documenting the schema of a MongoDB collection used by a "
        f'testing/QA laboratory management system. The collection is "{collection_name}".\n'
        "For EACH field below, write ONE short sentence (max ~20 words) on what "
        "that field most likely represents in this business domain, based on its "
        "exact name, type, and any example values. Be specific, not generic — "
        "don't just restate the field name or type.\n\n"
        "Fields:\n" + "\n".join(lines) + "\n\n"
        "Respond with ONLY a JSON object mapping each field path EXACTLY as given "
        "(including every dot and bracket) to its one-sentence description. No "
        "markdown, no code fences, no extra keys, no preamble, no trailing text."
    )

    try:
        response = genai_client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(max_output_tokens=4096),
        )
        text = (response.text or "{}").strip()
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return {}
        return {str(k): str(v).strip() for k, v in parsed.items()}
    except Exception as e:
        print(f"[schema_docs] LLM description drafting failed for '{collection_name}': {e}")
        return {}


def _build_collection_doc(catalog, collection_name, desc_cache, max_distinct=MAX_DISTINCT_VALUES):
    fields = catalog.get(collection_name, {})
    coll_cache = desc_cache.setdefault(collection_name, {})

    field_meta = {}   # path -> dict(type, bucket, values, note)
    to_describe = []  # fields needing a fresh LLM description this run

    for path, type_name in sorted(fields.items()):
        if path == "_id":
            continue

        if _is_infra_field(path, type_name):
            field_meta[path] = {
                "type": type_name, "bucket": "infra",
                "values": None, "note": None,
                "description": _infra_description(path, type_name),
            }
            continue

        values, note, bucket = None, None, "other"
        if type_name in CANDIDATE_TYPES:
            try:
                result = run_distinct_values(
                    catalog, collection_name, _mongo_path(path), limit=max_distinct
                )
            except Exception as e:
                result = {"error": str(e)}
            if "error" not in result:
                count, values = result["count"], result["values"]
                if count == 0:
                    note = "no values found in sample"
                elif count <= max_distinct:
                    bucket = "categorical"
                else:
                    note = (
                        f"{count} distinct values, too many to list here; "
                        f"use the distinct_values tool if you need the exact set"
                    )

        cached = coll_cache.get(path)
        if cached is None or cached.get("type") != type_name:
            to_describe.append({
                "path": path,
                "type": type_name,
                "sample_values": values[:6] if values else None,
            })

        field_meta[path] = {
            "type": type_name, "bucket": bucket,
            "values": values, "note": note, "description": None,
        }

    for i in range(0, len(to_describe), DESC_BATCH_SIZE):
        batch = to_describe[i:i + DESC_BATCH_SIZE]
        drafted = _draft_descriptions_via_llm(collection_name, batch)
        for fe in batch:
            desc = drafted.get(fe["path"], "").strip()
            if not desc:
                desc = "(description unavailable — LLM drafting failed for this field; re-run to retry)"
            coll_cache[fe["path"]] = {"type": fe["type"], "description": desc}

    for path, meta in field_meta.items():
        if meta["description"] is None:
            meta["description"] = coll_cache.get(path, {}).get(
                "description", "(no description available)"
            )

    categorical_lines, other_lines, infra_lines = [], [], []
    for path, meta in field_meta.items():
        desc = meta["description"]
        type_name = meta["type"]
        if meta["bucket"] == "categorical":
            values_str = ", ".join(str(v) for v in meta["values"])
            categorical_lines.append(f"- `{path}` ({type_name}) — {desc} Known values: {values_str}")
        elif meta["bucket"] == "infra":
            infra_lines.append(f"- `{path}` ({type_name}) — {desc}")
        else:
            suffix = f" ({meta['note']})" if meta["note"] else ""
            other_lines.append(f"- `{path}` ({type_name}) — {desc}{suffix}")

    related, unresolved = _find_related(collection_name, fields, ALLOWED_COLLECTIONS)
    if unresolved:
        print(f"[schema_docs] {collection_name}: no collection matched for {unresolved[:8]}"
          f"{'...' if len(unresolved) > 8 else ''} (add to LINK_OVERRIDES if these are real references)")

    parts = [
        f"# Schema reference: {collection_name}",
        "",
        (
            "Auto-generated from the live database schema, a sample of stored "
            "values, and model-drafted field descriptions. Regenerate with "
            "schema_docs.refresh_schema_documents() whenever the schema or its "
            "value sets change. Use the exact backticked field name shown below "
            "when querying — descriptions are for context only."
        ),
        "",
    ]
    
    if related:
        parts += ["## Related collections", ""]
        parts += [
            f"- [{target}]({_schema_filename(target)}) — linked via "
            + ", ".join(f"`{v}`" for v in sorted(set(via)))
            for target, via in sorted(related.items())
        ]
        parts += [""]

    if categorical_lines:
        parts += ["## Categorical fields (known values)", ""]
        parts += categorical_lines
        parts += [""]

    if other_lines:
        parts += ["## Other queryable fields", ""]
        parts += other_lines
        parts += [""]

    if infra_lines:
        parts += ["## Internal / system fields (reference only, not for filtering)", ""]
        parts += infra_lines
        parts += [""]

    return _build_frontmatter(collection_name, len(field_meta)) + "\n".join(parts)

def generate_schema_documents(catalog=None, collection_names=None, max_distinct=MAX_DISTINCT_VALUES):
    allowed = set(ALLOWED_COLLECTIONS)

    if collection_names is None:
        collection_names = list(ALLOWED_COLLECTIONS)
    else:
        dropped = [c for c in collection_names if c not in allowed]
        if dropped:
            print(f"[schema_docs] ignoring non-allowed collections: {dropped}")
        collection_names = [c for c in collection_names if c in allowed]

    if catalog is None:
        catalog = build_schema_catalog(collection_names=collection_names)
    else:
        catalog = {name: fields for name, fields in catalog.items() if name in allowed}

    os.makedirs(SCHEMA_DIR, exist_ok=True)

    desc_cache = _load_desc_cache()

    written = []
    for collection_name in catalog:
        content = _build_collection_doc(catalog, collection_name, desc_cache, max_distinct=max_distinct)
        filepath = os.path.join(SCHEMA_DIR, _schema_filename(collection_name))
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(f"schema/{_schema_filename(collection_name)}")

    _save_desc_cache(desc_cache)

    return written


def clear_description_cache(collection_name=None, field_path=None):
    cache = _load_desc_cache()
    if collection_name is None:
        cache = {}
    elif field_path is None:
        cache.pop(collection_name, None)
    else:
        cache.get(collection_name, {}).pop(field_path, None)
    _save_desc_cache(cache)


def refresh_schema_documents(collection_names=None, max_distinct=MAX_DISTINCT_VALUES):
    written = generate_schema_documents(collection_names=collection_names, max_distinct=max_distinct)
    ingest_result = ingest_documents()
    return {"written_files": written, "ingest_result": ingest_result}


_auto_refresh_started = False
_auto_refresh_lock = threading.Lock()


def _auto_refresh_loop(interval_seconds, collection_names, max_distinct):
    while True:
        time.sleep(interval_seconds)
        try:
            result = refresh_schema_documents(collection_names=collection_names, max_distinct=max_distinct)
            print(f"[schema_docs] auto-refresh: {result['written_files']} -> "
                  f"{result['ingest_result'].get('message', result['ingest_result'])}")
        except Exception as e:
            print(f"[schema_docs] auto-refresh failed: {e}")


def start_auto_refresh(interval_seconds=1800, collection_names=None, max_distinct=MAX_DISTINCT_VALUES,
                        run_immediately=True):
    
    global _auto_refresh_started
    with _auto_refresh_lock:
        if _auto_refresh_started:
            return
        _auto_refresh_started = True

    if run_immediately:
        try:
            result = refresh_schema_documents(collection_names=collection_names, max_distinct=max_distinct)
            print(f"[schema_docs] initial refresh: {result['written_files']} -> "
                  f"{result['ingest_result'].get('message', result['ingest_result'])}")
        except Exception as e:
            print(f"[schema_docs] initial refresh failed: {e}")

    thread = threading.Thread(
        target=_auto_refresh_loop,
        args=(interval_seconds, collection_names, max_distinct),
        daemon=True,
    )
    thread.start()


if __name__ == "__main__":
    print(refresh_schema_documents())
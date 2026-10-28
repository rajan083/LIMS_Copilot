import os
import json
import logging

from dotenv import load_dotenv
from google import genai
from google.genai import types

from rag_pipeline import search_documents

load_dotenv()

_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
MODEL = "gemini-3.1-flash-lite"

MAX_SUBQUERIES = 3
MAX_FOLLOWUPS = 2
MAX_CHUNKS = 8
PER_QUERY_TOP_K = 3
MIN_RELEVANCE = 0.3
GAP_CHECK_CHARS = 700   # per-chunk excerpt shown to the LLM during the gap check

logger = logging.getLogger("lims.multihop")


def _ask_json_list(prompt):
    """Ask the model for a JSON list of strings. Returns [] on any problem."""
    try:
        resp = _client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=list[str],
                max_output_tokens=512,
                temperature=0,
            ),
        )
        u = resp.usage_metadata
        logger.info(f"[multi_hop] tokens input={u.prompt_token_count} output={u.candidates_token_count}")
        data = json.loads(resp.text)
        if isinstance(data, list):
            return [q.strip() for q in data if isinstance(q, str) and q.strip()]
    except Exception as e:
        logger.warning(f"[multi_hop] LLM step failed: {e}")
    return []


def _decompose(question):
    prompt = (
        "You help search a laboratory information system's manuals and SOPs.\n"
        f"Split the question below into at most {MAX_SUBQUERIES} short, standalone "
        "search queries, each answerable from a single passage of a manual. "
        "Resolve pronouns so each query makes sense on its own. "
        "If the question is already simple, return just one query.\n\n"
        f"Question: {question}"
    )
    subs = _ask_json_list(prompt)[:MAX_SUBQUERIES]
    return subs or [question]


def _find_gaps(question, chunks):
    excerpts = "\n\n".join(
        f"[{i + 1}] ({c['source']}) {c['text'][:GAP_CHECK_CHARS]}"
        for i, c in enumerate(chunks)
    )
    prompt = (
        "You are checking whether retrieved passages are enough to answer a question.\n"
        f"Question: {question}\n\nPassages:\n{excerpts}\n\n"
        "If the passages fully cover the question, return an empty list. "
        "Otherwise return up to "
        f"{MAX_FOLLOWUPS} short search queries for the missing information "
        "(for example a form, step, role or term the passages mention but do not "
        "explain). Do not repeat information already present."
    )
    return _ask_json_list(prompt)[:MAX_FOLLOWUPS]


def _retrieve(queries, seen, found):
    """Run each query, merging new chunks into `found` (keyed by source+chunk)."""
    for q in queries:
        result = search_documents(q, top_k=PER_QUERY_TOP_K, min_relevance=MIN_RELEVANCE)
        for m in result.get("matches", []):
            key = (m.get("source"), m.get("chunk_index"))
            if key in seen:
                # keep the higher score if the same chunk is hit again
                if m["relevance_score"] > found[key]["relevance_score"]:
                    found[key]["relevance_score"] = m["relevance_score"]
                continue
            seen.add(key)
            found[key] = dict(m, found_for=q)


def multi_hop_search(question, max_chunks=MAX_CHUNKS):
    question = (question or "").strip()
    if not question:
        return {"error": "question is required"}

    seen, found = set(), {}
    sub_queries = _decompose(question)
    _retrieve(sub_queries, seen, found)

    follow_ups = []
    if found:
        current = sorted(found.values(), key=lambda c: c["relevance_score"], reverse=True)
        follow_ups = [q for q in _find_gaps(question, current[:max_chunks]) if q not in sub_queries]
        if follow_ups:
            _retrieve(follow_ups, seen, found)

    logger.info(f"[multi_hop] sub_queries={sub_queries} follow_ups={follow_ups} chunks={len(found)}")

    if not found:
        return {"message": "No sufficiently relevant documents found for this question.",
                "sub_queries": sub_queries}

    matches = sorted(found.values(), key=lambda c: c["relevance_score"], reverse=True)[:max_chunks]
    matches.sort(key=lambda c: (str(c.get("source")), c.get("chunk_index") or 0))
    
    return {
        "sub_queries": sub_queries,
        "follow_up_queries": follow_ups,
        "hops": 2 if follow_ups else 1,
        "matches": matches,
    }
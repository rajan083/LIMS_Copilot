import os
import re
import json
import hashlib
import posixpath
import threading
import time
from datetime import datetime, timezone

import numpy as np
import yaml
import chromadb
import logging
from sentence_transformers import SentenceTransformer

#remove this line below, when changing the embedding model for transformer.
os.environ.setdefault("HF_HUB_OFFLINE", "1")        #suppresses the H_F warning in the tool_call, and tells the model, that the chunking is offline.

VECTOR_STORE_DIR = os.path.join(os.path.dirname(__file__), "vector_store")
DOCUMENTS_DIR = os.path.join(os.path.dirname(__file__), "documents")
MANIFEST_PATH = os.path.join(VECTOR_STORE_DIR, "manifest.json")
CACHE_JSON_PATH = os.path.join(VECTOR_STORE_DIR, "search_cache.json")
COLLECTION_NAME = "lims_documents"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"       #before changing the embedding model remove the os.environ.setdefault(), so it can download the embeddings accordingly.

# Bump this whenever the ingest format changes (chunking, metadata, frontmatter
# handling). It is folded into every file hash, so all files are re-ingested
# automatically on the next run, with no need to delete manifest.json by hand.
INGEST_VERSION = "okf-5"        #chaging the version here will reingest the documents again, without the need to delete the previous files, so with every change in pipeline embeddings, update the version, so the hash changes.

# Semantic chunking tuning
BREAKPOINT_PERCENTILE = 90   # higher = fewer, larger chunks; lower = more, smaller chunks
MAX_CHUNK_WORDS = 180        # hard fallback so no chunk exceeds the embedder's token limit
MIN_CHUNK_SENTENCES = 2      # avoid pathologically tiny 1-sentence chunks
MIN_CHUNK_WORDS = 15         # chunks shorter than this are merged into a neighbour

# Cache tuning
CACHE_MAX_ENTRIES = 2000     # rows beyond this get trimmed, oldest-used first
CACHE_TTL_SECONDS = None     # e.g. 86400 for a 1-day TTL; None = never expire on its own

# OKF link expansion tuning
MAX_RELATED_DOCS = 3         # max linked documents added to a search result
RELATED_MAX_CHARS = 900      # cap on the text returned per linked document

SCHEMA_SUBDIR = "schema"
LEARNED_SUBDIR = "learned"
SCHEMA_DIR = os.path.join(DOCUMENTS_DIR, SCHEMA_SUBDIR)
LEARNED_DIR = os.path.join(DOCUMENTS_DIR, LEARNED_SUBDIR)

LEARNED_PREFIX = "learned_"

_model = None
_client = None
_collection = None
_cache_lock = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("lims_copilot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger('lims.rag')

def _get_model():                           #stops the model to ask if the files are changedm and tells the embedding model exists locally saves 20 rounds and 6-7 seconds.
    global _model
    if _model is None:
        try:
            _model = SentenceTransformer(EMBEDDING_MODEL, local_files_only=True)
        except:
            _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model


def _get_collection():
    global _client, _collection
    if _collection is None:
        _client = chromadb.PersistentClient(path=VECTOR_STORE_DIR)
        _collection = _client.get_or_create_collection(
            COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def warm_up():                      #load the server with dummy search, so the model is loaded, when the real question arrives. takes around 8 seconds when the server starts.
    model= _get_model()
    emb= model.encode(["warm up"], normalize_embeddings=True).tolist()
    collection= _get_collection()
    if collection.count():
        collection.query(query_embeddings=emb, n_results=1)

def _split_sentences(text):
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text)
    return [s.strip() for s in sentences if s.strip()]


def _cosine_distances(embeddings):
    dists = []
    for i in range(len(embeddings) - 1):
        sim = float(np.dot(embeddings[i], embeddings[i + 1]))
        dists.append(1 - sim)
    return dists


def _split_long_chunk(sentences, max_words):
    words = " ".join(sentences).split()
    if len(words) <= max_words:
        return [" ".join(sentences)]
    sub_chunks = []
    start = 0
    while start < len(words):
        end = start + max_words
        sub_chunks.append(" ".join(words[start:end]))
        start = end
    return sub_chunks


def _semantic_chunk_text(text, model, breakpoint_percentile=BREAKPOINT_PERCENTILE,
                          max_chunk_words=MAX_CHUNK_WORDS, min_chunk_sentences=MIN_CHUNK_SENTENCES):
    sentences = _split_sentences(text)
    if len(sentences) <= min_chunk_sentences:
        return [text.strip()] if text.strip() else []

    sentence_embeddings = model.encode(sentences, normalize_embeddings=True)
    distances = _cosine_distances(sentence_embeddings)

    if not distances:
        return [text.strip()]

    threshold = np.percentile(distances, breakpoint_percentile)

    chunks = []
    current = [sentences[0]]
    for i, dist in enumerate(distances):
        if dist > threshold and len(current) >= min_chunk_sentences:
            chunks.append(" ".join(current))
            current = [sentences[i + 1]]
        else:
            current.append(sentences[i + 1])
    if current:
        chunks.append(" ".join(current))

    # Enforce the hard word-count ceiling on any chunk that ended up too long
    final_chunks = []
    for chunk in chunks:
        final_chunks.extend(_split_long_chunk(chunk.split(" "), max_chunk_words)
                             if len(chunk.split()) > max_chunk_words else [chunk])
    return final_chunks


# ---------------------------------------------------------------------------
# OKF (Open Knowledge Format) parsing: YAML frontmatter + Markdown links
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")


def _parse_okf(text, relpath):
    """Split a document into (frontmatter dict, body text, resolved link paths).

    Files without frontmatter still work: meta is {} and the whole text is the body.
    Link paths are resolved relative to the file and returned in the same
    forward-slash relpath form used as the `source` metadata.
    """
    meta, body = {}, text
    m = _FM_RE.match(text)
    if m:
        try:
            parsed = yaml.safe_load(m.group(1))
            # Only treat it as frontmatter if it is a YAML mapping. A file that
            # merely starts with a Markdown horizontal rule (---) must keep its
            # full text as the body.
            if isinstance(parsed, dict):
                meta = parsed
                body = m.group(2)
        except yaml.YAMLError:
            logger.warning(f"Bad frontmatter in {relpath}; indexing as plain text")

    # Links come from Markdown links in the body and from an optional
    # `related:` list in the frontmatter (handy for generated files, since it
    # keeps link lines out of the embedded text).
    raw_links = list(_LINK_RE.findall(body))
    related = meta.get("related") or []
    if isinstance(related, str):
        related = [related]
    raw_links += [str(r) for r in related if str(r).lower().endswith(".md")]

    base = posixpath.dirname(relpath)
    links = sorted({
        posixpath.normpath(posixpath.join(base, l))
        for l in raw_links if "://" not in l
    })
    return meta, body, links


def _chunk_metadata(relpath, i, meta, links):
    md = {
        "source": relpath,
        "chunk_index": i,
        "type": str(meta.get("type", "unknown")),
        "title": str(meta.get("title", relpath)),
        "links": "|".join(links),          # Chroma metadata values must be scalar
    }
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    for t in tags:                         # tags become boolean keys so you can filter
        md[f"tag_{t}"] = True
    return md


def _merge_tiny_chunks(chunks, min_words=MIN_CHUNK_WORDS):
    """Merge fragments such as "1", "E-Labs" or "the order." into a neighbour.
    Tiny chunks embed as near-noise and otherwise show up as top matches."""
    merged = []
    for c in chunks:
        if merged and len(c.split()) < min_words:
            merged[-1] = merged[-1] + " " + c
        else:
            merged.append(c)
    if len(merged) > 1 and len(merged[0].split()) < min_words:
        merged[1] = merged[0] + " " + merged[1]
        merged = merged[1:]
    return merged


def _load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_manifest(manifest):
    os.makedirs(VECTOR_STORE_DIR, exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def _file_hash(filepath):
    h = hashlib.sha256()
    h.update(INGEST_VERSION.encode("utf-8"))
    with open(filepath, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _relpath(documents_dir, filepath):
    # Always use forward slashes so keys/ids are stable across OSes
    # (Windows os.path.relpath uses backslashes).
    return os.path.relpath(filepath, documents_dir).replace(os.sep, "/")


def _iter_document_files(documents_dir):
    found = {}
    for root, _dirs, files in os.walk(documents_dir):
        for f in files:
            if f.lower().endswith((".txt", ".md")):
                abspath = os.path.join(root, f)
                found[_relpath(documents_dir, abspath)] = abspath
    return found


def ingest_documents(documents_dir=DOCUMENTS_DIR):
    model = _get_model()
    collection = _get_collection()

    if not os.path.isdir(documents_dir):
        return {"error": f"Documents directory not found: {documents_dir}"}

    manifest = _load_manifest()
    current_files = _iter_document_files(documents_dir)  # {relpath: abspath}

    removed_files = set(manifest.keys()) - set(current_files.keys())
    for relpath in removed_files:
        collection.delete(where={"source": relpath})
        del manifest[relpath]

    ingested_files = 0
    skipped_files = 0
    ingested_chunks = 0

    for relpath, filepath in current_files.items():
        current_hash = _file_hash(filepath)

        if manifest.get(relpath) == current_hash:
            skipped_files += 1
            continue

        collection.delete(where={"source": relpath})

        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()

        meta, body, links = _parse_okf(text, relpath)
        title = str(meta.get("title", relpath))

        chunks = _merge_tiny_chunks(_semantic_chunk_text(body, model))
        if chunks:
            # Schema files: prefix the collection name when embedding so a chunk like
            # "joins on order_id" keeps its context. Manual pages already carry
            # "<manual>, page N" in their own text; prefixing every chunk with it made
            # tiny chunks match any query that merely mentioned the manual.
            # The stored document text stays the raw chunk either way.
            if meta.get("type") == "manual_page":
                embed_texts = chunks
            else:
                embed_texts = [f"{title}: {c}" for c in chunks]
            embeddings = model.encode(embed_texts, normalize_embeddings=True).tolist()
            # relpath may contain "/" (e.g. "schema/schema_el_orders.md") which
            # is fine for a Chroma id, but swap it for "__" to keep ids simple.
            id_prefix = relpath.replace("/", "__")
            ids = [f"{id_prefix}::chunk{i}" for i in range(len(chunks))]
            metadatas = [_chunk_metadata(relpath, i, meta, links) for i in range(len(chunks))]
            collection.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=chunks,
                metadatas=metadatas,
            )
            ingested_chunks += len(chunks)

        manifest[relpath] = current_hash
        ingested_files += 1

    _save_manifest(manifest)

    if ingested_files or removed_files:
        clear_search_cache()

    return {
        "message": (
            f"Ingested {ingested_files} new/changed file(s) "
            f"({ingested_chunks} semantic chunks), skipped {skipped_files} unchanged, "
            f"removed {len(removed_files)} deleted file(s)."
        )
    }


def _learned_filename(question):
    normalized = re.sub(r"\s+", " ", question.strip().lower())
    q_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{LEARNED_PREFIX}{q_hash}.md"


def save_learned_answer(question, answer, note="user-confirmed"):
    question = (question or "").strip()
    answer = (answer or "").strip()

    if not question or not answer:
        return {"error": "Both question and answer are required and cannot be empty."}

    os.makedirs(LEARNED_DIR, exist_ok=True)

    filename = _learned_filename(question)
    filepath = os.path.join(LEARNED_DIR, filename)

    frontmatter = yaml.safe_dump(
        {
            "type": "learned_answer",
            "title": question[:80],
            "note": note,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        sort_keys=False,
        allow_unicode=True,
    )

    content = (
        f"---\n{frontmatter}---\n"
        f"# Learned answer ({note})\n\n"
        f"**Question:** {question}\n\n"
        f"**Answer:** {answer}\n"
    )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    ingest_result = ingest_documents()

    return {
        "saved_as": f"{LEARNED_SUBDIR}/{filename}",
        "ingest_result": ingest_result,
    }

_pending_turns = {}

_CORRECTION_PATTERNS = [
    r"\bno[,.]?\s", r"\bactually\b", r"\bi meant\b",
    r"\bthat'?s (not|wrong|incorrect)\b", r"\byou'?re wrong\b",
    r"\bcorrect(ion)? is\b", r"\bi (said|asked for)\b",
    r"\bwrong\b", r"\bincorrect\b", r"\bnot\s+.{1,25}\bbut\b",
]
_CORRECTION_RE = re.compile("|".join(_CORRECTION_PATTERNS), re.IGNORECASE)


def _looks_like_correction(user_message):
    return bool(_CORRECTION_RE.search(user_message or ""))


def record_turn(session_id, question, answer):
    _pending_turns[session_id] = {"question": question, "answer": answer}


def learn_if_correction(session_id, user_message, new_answer):

    pending = _pending_turns.get(session_id)

    if pending and _looks_like_correction(user_message):
        original_question = pending["question"]
        result = save_learned_answer(original_question, new_answer, note="user-corrected")
        record_turn(session_id, original_question, new_answer)
        return {"learned": True, "question": original_question, **result}

    record_turn(session_id, user_message, new_answer)
    return {"learned": False}


def list_learned_answers():
    if not os.path.isdir(LEARNED_DIR):
        return []
    return sorted(
        f for f in os.listdir(LEARNED_DIR)
        if f.startswith(LEARNED_PREFIX) and f.lower().endswith(".md")
    )


def delete_learned_answer(filename):
    if not filename.startswith(LEARNED_PREFIX):
        return {"error": "Refusing to delete a file outside the learned-answers namespace."}

    filepath = os.path.join(LEARNED_DIR, filename)
    if not os.path.exists(filepath):
        return {"error": f"No such learned answer: {filename}"}

    os.remove(filepath)
    ingest_result = ingest_documents()
    return {"deleted": filename, "ingest_result": ingest_result}


def _load_cache_file():
    if os.path.exists(CACHE_JSON_PATH):
        with open(CACHE_JSON_PATH, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def _save_cache_file(cache):
    os.makedirs(VECTOR_STORE_DIR, exist_ok=True)
    tmp_path = CACHE_JSON_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp_path, CACHE_JSON_PATH)


def _normalize_query(query):
    return re.sub(r"\s+", " ", query.strip().lower())


def _cache_key(query, top_k, min_relevance, doc_type=None, expand=True):
    normalized = _normalize_query(query)
    raw = f"{normalized}|{top_k}|{min_relevance}|{doc_type}|{expand}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get_cached(key):
    with _cache_lock:
        cache = _load_cache_file()
        entry = cache.get(key)

        if entry is None:
            return None

        if CACHE_TTL_SECONDS is not None and (time.time() - entry["created_at"]) > CACHE_TTL_SECONDS:
            del cache[key]
            _save_cache_file(cache)
            return None

        entry["last_accessed"] = time.time()
        _save_cache_file(cache)
        return entry["result"]


def _set_cached(key, value):
    now = time.time()
    with _cache_lock:
        cache = _load_cache_file()
        cache[key] = {"entry": key,"result": value, "created_at": now, "last_accessed": now}

        if len(cache) > CACHE_MAX_ENTRIES:
            overflow = len(cache) - CACHE_MAX_ENTRIES
            oldest_keys = sorted(cache, key=lambda k: cache[k]["last_accessed"])[:overflow]
            for k in oldest_keys:
                del cache[k]

        _save_cache_file(cache)


def clear_search_cache():
    with _cache_lock:
        _save_cache_file({})
    from answer_cache import clear_answer_cache
    clear_answer_cache()


def _fetch_related(collection, linked_sources, limit=MAX_RELATED_DOCS):
    """Return the text of each linked document (all its chunks, in order), capped at
    RELATED_MAX_CHARS. Near-empty documents are skipped; they only waste tokens."""
    linked_sources = list(linked_sources)
    if not linked_sources:
        return []

    extra = collection.get(
        where={"source": {"$in": linked_sources}},
        include=["documents", "metadatas"],
    )

    by_source = {}
    for doc, meta in zip(extra["documents"], extra["metadatas"]):
        entry = by_source.setdefault(meta.get("source"), {"meta": meta, "chunks": []})
        entry["chunks"].append((meta.get("chunk_index", 0), doc))

    related = []
    for source in linked_sources:                      # keep a stable order
        entry = by_source.get(source)
        if not entry:
            continue
        text = " ".join(d for _, d in sorted(entry["chunks"]))[:RELATED_MAX_CHARS]
        if len(text.split()) < MIN_CHUNK_WORDS:
            continue
        related.append({
            "text": text,
            "source": source,
            "title": entry["meta"].get("title"),
            "type": entry["meta"].get("type"),
        })
    return related[:limit]


def _search_documents_uncached(query, top_k, min_relevance, doc_type=None, expand=True):
    collection = _get_collection()

    if collection.count() == 0:
        return {"message": "No documents have been ingested yet."}

    model = _get_model()
    query_embedding = model.encode([query], normalize_embeddings=True).tolist()

    query_kwargs = {"query_embeddings": query_embedding, "n_results": top_k}
    if doc_type:
        query_kwargs["where"] = {"type": doc_type}

    results = collection.query(**query_kwargs)

    matches = []
    linked = []
    for doc, meta, dist in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        score = round(1 - dist, 4)
        if score >= min_relevance:
            matches.append({
                "text": doc,
                "source": meta.get("source"),
                "chunk_index": meta.get("chunk_index"),
                "relevance_score": score,
            })
            for l in (meta.get("links") or "").split("!"):
                if l and l not in linked:
                    linked.append(l)

    if not matches:
        return {"message": "No sufficiently relevant documents found for this query."}

    result = {"matches": matches}

    if expand:
        # Don't repeat documents that were already matched directly
        matched= {m["source"] for m in matches}
        linked = {l for l in linked if l not in matched}
        related = _fetch_related(collection, linked)
        if related:
            result["related"] = related

    return result


def search_documents(query, top_k=3, min_relevance=0.3, doc_type=None, expand=True):
    key = _cache_key(query, top_k, min_relevance, doc_type, expand)

    cached = _get_cached(key)
    if cached is not None:
        logger.info(f"[search_documents] cache HIT  (top_k={top_k}, min_relevance={min_relevance}, doc_type={doc_type}, expand={expand}) query={query!r}")
        return cached

    logger.info(f"[search_documents] cache MISS (top_k={top_k}, min_relevance={min_relevance}, doc_type={doc_type}, expand={expand}) query={query!r} — querying vector store")
    result = _search_documents_uncached(query, top_k, min_relevance, doc_type, expand)
    _set_cached(key, result)
    return result


def get_cache_stats():
    with _cache_lock:
        cache = _load_cache_file()

    if not cache:
        return {"entries": 0}

    last_accessed_values = [entry["last_accessed"] for entry in cache.values()]
    return {
        "entries": len(cache),
        "max_entries": CACHE_MAX_ENTRIES,
        "oldest_last_accessed": min(last_accessed_values),
        "newest_last_accessed": max(last_accessed_values),
        "cache_file": CACHE_JSON_PATH,
    }


if __name__ == "__main__":
    print(ingest_documents())
import re, time, hashlib, logging, difflib
import rag_pipeline as rp   # reuses its Chroma client and embedding model


ANSWER_CACHE_NAME = "answer_cache"
DEFAULT_THRESHOLD = 0.70        # cosine similarity needed for a hit; keep high
DEFAULT_TTL_SECONDS = 86400     # 1 day
GRAY_ZONE = 0.50      # embedding similarity floor for the typo check
TYPO_RATIO = 0.93     # character similarity needed inside that zone

_collection = None

_FILLER_WORDS = {
    "what", "whats", "is", "are", "the", "a", "an", "meaning", "mean", "means",
    "of", "does", "define", "definition", "explain", "tell", "me", "about",
}

logger = logging.getLogger("lims.answer_cache")


def _clean(question):
    q = re.sub(r"[_\-/]+", " ", question.lower())      # customer_po -> customer po
    words = re.findall(r"[a-z0-9]+", q)
    kept = [w for w in words if w not in _FILLER_WORDS]
    return " ".join(kept) or " ".join(words) 


def _get_cache_collection():
    global _collection
    if _collection is None:
        rp._get_collection()   # makes sure rp._client has been created
        _collection = rp._client.get_or_create_collection(
            ANSWER_CACHE_NAME, metadata={"hnsw:space": "cosine"}
        )
    return _collection


def _question_id(question):
    normalized = re.sub(r"\s+", " ", question.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def lookup_cached_answer(question, threshold=DEFAULT_THRESHOLD, ttl=DEFAULT_TTL_SECONDS):
    cache = _get_cache_collection()
    if cache.count() == 0:
        logger.info('[answer_cache] MISS (cache is empty)')
        return None

    clean_q = _clean(question)                                   # same cleaning as store_cached_answer
    emb = rp._get_model().encode([clean_q], normalize_embeddings=True).tolist()
    res = cache.query(query_embeddings=emb, n_results=min(3, cache.count()))
    if not res["ids"] or not res["ids"][0]:
        return None

    best = None
    for i in range(len(res["ids"][0])):
        meta = res["metadatas"][0][i]
        if time.time() - meta["created_at"] >= ttl:
            continue
        score = round(1 - res["distances"][0][i], 4)
        ratio = difflib.SequenceMatcher(None, clean_q, _clean(meta.get("question", ""))).ratio()
        if score >= threshold or (score >= GRAY_ZONE and ratio >= TYPO_RATIO):
            if best is None or score > best[0]:
                best = (score, ratio, meta.get("question"), res["documents"][0][i])

    if best:
        logger.info(f"[answer_cache] HIT sim={best[0]} text_ratio={best[1]:.2f} asked={question!r} matched={best[2]!r}")
        return best[3]

    top_score = round(1 - res["distances"][0][0], 4)
    logger.info(f"[answer_cache] MISS nearest_sim={top_score} threshold={threshold} "
                f"asked={question!r} nearest={res['metadatas'][0][0].get('question')!r}")
    return None

def store_cached_answer(question, answer):
    cache = _get_cache_collection()
    emb = rp._get_model().encode([_clean(question)], normalize_embeddings=True).tolist()
    cache.upsert(
        ids=[_question_id(question)],
        embeddings=emb,
        documents=[answer],
        metadatas=[{"question": question, "created_at": time.time()}],
    )


def clear_answer_cache():
    global _collection
    rp._get_collection()
    try:
        rp._client.delete_collection(ANSWER_CACHE_NAME)
    except Exception:
        pass
    _collection = None
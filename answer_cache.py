import re
import time
import hashlib
import logging
import rag_pipeline as rp   # reuses its Chroma client and embedding model


ANSWER_CACHE_NAME = "answer_cache"
DEFAULT_THRESHOLD = 0.70        # cosine similarity needed for a hit; keep high
DEFAULT_TTL_SECONDS = 86400     # 1 day

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
        logger.info(f'[answer_cache] MISS (cache is empty)')
        return None

    emb = rp._get_model().encode([question], normalize_embeddings=True).tolist()
    res = cache.query(query_embeddings=emb, n_results=1)
    if not res["ids"] or not res["ids"][0]:
        return None

    score = round(1 - res["distances"][0][0], 4)
    meta = res["metadatas"][0][0]
    age = time.time() - meta["created_at"]
    matched = meta.get("question")

    if score >= threshold and age < ttl:
        logger.info(f"[answer_cache] HIT sim={score} asked={question!r} matched={matched!r}")
        return res["documents"][0][0]

    logger.info(f"[answer_cache] MISS nearest_sim={score} threshold={threshold} "
                f"expired={age >= ttl} asked={question!r} nearest={matched!r}")
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
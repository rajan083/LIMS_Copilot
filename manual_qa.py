import os
import io
import re
import json
import time
import hashlib
import logging
import yaml

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pypdf import PdfReader, PdfWriter
from rag_pipeline import ingest_documents


load_dotenv()
logger = logging.getLogger("lims.manual_docs")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.join(BASE_DIR, "manual_sources")       # put the PDFs here (NOT under documents/)
# IMPORTANT: make DOCUMENTS_DIR the same root that schema_docs.py / ingest_documents() use.
DOCUMENTS_DIR = os.path.join(BASE_DIR, "documents")
OUTPUT_DIR = os.path.join(DOCUMENTS_DIR, "manuals")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
CACHE_PATH = os.path.join(CACHE_DIR, "manual_pages_cache.json")

VISION_MODEL = "gemini-3.1-flash-lite"   # same model agent.py uses; any Gemini model that reads PDFs works
PAGES_PER_CALL = 4                       # keeps each vision response well under the output limit
PROMPT_VERSION = "v3"                    # bump to force re-transcription after editing the prompt

TRANSCRIBE_PROMPT = """You are preparing a software user manual for a search index.
The attached PDF contains {count} consecutive pages of the document "{title}".

For EACH page output exactly this format, numbering pages 1 to {count} in the
order they appear in THIS attached file (page 1 is the first page you see here,
regardless of where it falls in the original document):

## Page <number>
<content>

Rules:
- This file is a short excerpt, not the whole manual. Do not ask for missing
  pages or comment on the excerpt being incomplete — transcribe exactly what
  is attached.
- Transcribe all text faithfully: headings, notes, warnings and step-by-step instructions.
- Write every numbered step as "Step N: <text>" and end each step, note and warning with a period. Do not use "1." style numbering.
- Do not use markdown tables. Write each table row as one line: "<Column>: <value>; <Column>: <value>."
- For every screenshot, diagram or figure, write a line starting with "Figure:" that describes it: which screen or dialog it is, the visible menu items, buttons and field labels (read exactly as shown), and what it illustrates in the context of the surrounding steps.
- If a figure shows checkboxes, radio buttons, toggles, or a labeled group of choices, list every option as its own line inside the figure description, in the format "Option: <exact label as shown>." List sub-options the same way, indented one level under their parent option. Keep the exact wording shown, even if it looks unusual or contradictory — do not reword or "correct" it.
- Do not add commentary, summaries, or anything that is not on the page."""

PAGE_HEADER = re.compile(r"^##\s*Page\s+(\d+)\s*$", re.MULTILINE)


_REFUSAL_MARKERS = (
    "please provide", "only contains pages", "i don't have access",
    "cannot see", "no pages were", "the pdf provided",
)


def _looks_like_refusal(markdown, expected_pages):
    if not markdown.strip():
        return True
    headers = len(PAGE_HEADER.findall(markdown))
    if headers < expected_pages:
        return True
    low = markdown.lower()
    return any(m in low for m in _REFUSAL_MARKERS)


def _split_pages_relative(markdown, first, last):
    """Model numbers pages 1..N within the slice; remap to real page numbers."""
    matches = list(PAGE_HEADER.finditer(markdown))
    pages = {}
    for i, m in enumerate(matches):
        rel_num = int(m.group(1))
        real_num = first + rel_num - 1
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        if first <= real_num <= last:
            pages[real_num] = markdown[m.end():end].strip()
    return pages



# ---------- cache ----------

def _load_cache():
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"could not read manual cache ({e}); starting fresh")
        return {}


def _save_cache(cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE_PATH)


# ---------- vision call ----------

def _is_transient(e):
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    text = str(e).lower()
    return code in (429, 500, 502, 503, 504) or any(
        s in text for s in ("resource_exhausted", "unavailable", "overloaded", "rate limit")
    )


def _call_vision(client, pdf_bytes, prompt, retries=3):
    for attempt in range(retries + 1):
        try:
            response = client.models.generate_content(
                model=VISION_MODEL,
                contents=[types.Content(role="user", parts=[
                    types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                    types.Part.from_text(text=prompt),
                ])],
                config=types.GenerateContentConfig(max_output_tokens=8192),
            )
            return (response.text or "").strip()
        except Exception as e:
            if _is_transient(e) and attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise


def _slice_pdf(reader, start, end):
    """Bytes of a small PDF holding pages [start, end) (0-based)."""
    writer = PdfWriter()
    for i in range(start, end):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _page_filename(slug, num):
    return f"manual_{slug}_p{num:03d}.md"


def _frontmatter(slug, title, pdf_name, num, neighbours):
    meta = {
        "type": "manual_page",
        "title": f"{title}, page {num}",
        "manual": title,
        "page": num,
        "source_pdf": pdf_name,
        "tags": ["manual", slug],
    }
    if neighbours:
        meta["related"] = neighbours
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n"



# ---------- markdown files ----------

def _desired_files(slug, title, pdf_name, pages):
    nums = sorted(n for n in pages if pages[n].strip())
    have = set(nums)                      # only link to pages that actually exist
    files = {}
    for num in nums:
        neighbours = [_page_filename(slug, n) for n in (num - 1, num + 1) if n in have]
        files[_page_filename(slug, num)] = (
            _frontmatter(slug, title, pdf_name, num, neighbours)
            + f"{title}, page {num}. Source file: {pdf_name}.\n\n{pages[num].strip()}\n"
        )
    return files


def _sync_files(slug, desired):
    """Write new/changed files, delete stale ones from an older version of this PDF.
    Unchanged files are left untouched so change-detection in ingest stays quiet."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    prefix = f"manual_{slug}_p"
    for existing in os.listdir(OUTPUT_DIR):
        if existing.startswith(prefix) and existing.endswith(".md") and existing not in desired:
            os.remove(os.path.join(OUTPUT_DIR, existing))
    changed = 0
    for name, content in desired.items():
        path = os.path.join(OUTPUT_DIR, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                if f.read() == content:
                    continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        changed += 1
    return changed


# ---------- main entry points ----------

def generate_manual_documents(pdf_names=None):
    """Transcribe every PDF in manual_sources/ (skipping pages already cached) and write the
    markdown chunks. Resumable: the cache is saved after every vision call."""
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    os.makedirs(SOURCE_DIR, exist_ok=True)
    cache = _load_cache()
    summary = []

    for pdf_name in sorted(os.listdir(SOURCE_DIR)):
        if not pdf_name.lower().endswith(".pdf"):
            continue
        if pdf_names and pdf_name not in pdf_names:
            continue

        with open(os.path.join(SOURCE_DIR, pdf_name), "rb") as f:
            raw = f.read()
        key = f"{hashlib.sha256(raw).hexdigest()}:{PROMPT_VERSION}"
        entry = cache.setdefault(key, {"pdf": pdf_name, "pages": {}})

        reader = PdfReader(io.BytesIO(raw))
        total = len(reader.pages)
        title = os.path.splitext(pdf_name)[0]
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "manual"

        for start in range(0, total, PAGES_PER_CALL):
            end = min(start + PAGES_PER_CALL, total)
            first, last = start + 1, end
            count = last - first + 1
            if all(str(p) in entry["pages"] for p in range(first, last + 1)):
                continue

            prompt = TRANSCRIBE_PROMPT.format(count=count, title=title)
            pdf_bytes = _slice_pdf(reader, start, end)

            markdown = _call_vision(client, pdf_bytes, prompt)
            if _looks_like_refusal(markdown, count):
                logger.warning(f"[manual_docs] {pdf_name}: refusal-like response for "
                                f"pages {first}-{last}, retrying once")
                retry_prompt = prompt + (
                    "\n\nReminder: you were given exactly the pages attached, nothing "
                    "more. Transcribe them now without asking for anything else."
                )
                markdown = _call_vision(client, pdf_bytes, retry_prompt)

            if _looks_like_refusal(markdown, count):
                logger.error(f"[manual_docs] {pdf_name}: still refusing for pages "
                             f"{first}-{last} after retry — leaving ungenerated, will "
                             f"retry on next run")
                continue  # don't cache, don't write — next run retries this batch

            pages = _split_pages_relative(markdown, first, last)
            for p in range(first, last + 1):
                if p not in pages:
                    logger.warning(f"[manual_docs] {pdf_name}: no text returned for page {p}")
                entry["pages"][str(p)] = pages.get(p, "")
            _save_cache(cache)
            logger.info(f"[manual_docs] {pdf_name}: pages {first}-{last} of {total} done")

        page_map = {int(k): v for k, v in entry["pages"].items()}
        desired = _desired_files(slug, title, pdf_name, page_map)
        changed = _sync_files(slug, desired)
        summary.append({"pdf": pdf_name, "pages": total, "chunk_files": len(desired), "files_written": changed})

    return summary


def refresh_manual_documents():
    """Generate the markdown, then embed it into the vector store."""
    summary = generate_manual_documents()
    # Use the same import line schema_docs.py uses for ingest_documents.
    ingest = ingest_documents()
    return {"manuals": summary, "ingest": ingest}


if __name__ == "__main__":
    print(json.dumps(refresh_manual_documents(), indent=2, default=str))
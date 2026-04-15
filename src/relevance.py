"""
Fox relevance engine — TF-IDF scoring and entity extraction using stdlib only
(math, collections, re). No external dependencies.
"""

import math
import re
from collections import Counter
# ── Stopwords ─────────────────────────────────────────────────────────────────

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to",
    "for", "of", "and", "or", "it", "this", "that", "be", "has", "have",
    "had", "do", "does", "did", "with", "from", "by", "as", "not", "but",
    "if", "we", "you", "i", "he", "she", "they", "its", "will", "can",
    "would", "could", "should", "may", "might", "then", "than", "so",
})


# ── Tokenizer ─────────────────────────────────────────────────────────────────

def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric, remove stopwords."""
    tokens = re.split(r'[^a-z0-9_]+', text.lower())
    return [t for t in tokens if t and t not in _STOPWORDS and len(t) > 1]


# ── TF-IDF index ──────────────────────────────────────────────────────────────

class TFIDFIndex:
    """
    Lightweight in-memory TF-IDF index for small corpora (<1000 docs).
    No matrix math — uses plain dicts throughout.
    """

    def __init__(self):
        # doc_id -> {term: tf}
        self._tf: dict[str, dict[str, float]] = {}
        # term -> set of doc_ids containing it
        self._df: dict[str, set[str]] = {}

    def add_document(self, doc_id: str, text: str):
        tokens = tokenize(text)
        if not tokens:
            return
        counts = Counter(tokens)
        total = len(tokens)
        tf = {term: count / total for term, count in counts.items()}
        self._tf[doc_id] = tf
        for term in tf:
            self._df.setdefault(term, set()).add(doc_id)

    def remove_document(self, doc_id: str):
        if doc_id not in self._tf:
            return
        for term in self._tf[doc_id]:
            self._df[term].discard(doc_id)
        del self._tf[doc_id]

    def score(self, query: str) -> list[tuple[str, float]]:
        """Return [(doc_id, score), ...] sorted descending by TF-IDF cosine similarity."""
        if not self._tf:
            return []

        q_tokens = tokenize(query)
        if not q_tokens:
            return [(doc_id, 0.0) for doc_id in self._tf]

        n_docs = len(self._tf)
        q_counts = Counter(q_tokens)
        q_total = len(q_tokens)

        # Build query TF-IDF vector
        q_vec: dict[str, float] = {}
        for term, count in q_counts.items():
            tf = count / q_total
            df = len(self._df.get(term, set()))
            idf = math.log((n_docs + 1) / (df + 1))  # +1 smoothing
            q_vec[term] = tf * idf

        # Score each document
        results: list[tuple[str, float]] = []
        for doc_id, doc_tf in self._tf.items():
            dot = 0.0
            doc_norm = 0.0
            for term, doc_tf_val in doc_tf.items():
                df = len(self._df.get(term, set()))
                idf = math.log((n_docs + 1) / (df + 1))
                doc_tfidf = doc_tf_val * idf
                doc_norm += doc_tfidf ** 2
                if term in q_vec:
                    dot += q_vec[term] * doc_tfidf

            q_norm = sum(v ** 2 for v in q_vec.values())
            denom = math.sqrt(doc_norm * q_norm)
            sim = dot / denom if denom > 0 else 0.0
            results.append((doc_id, sim))

        results.sort(key=lambda x: x[1], reverse=True)
        return results


# ── Entity extraction ─────────────────────────────────────────────────────────

# Compiled patterns for speed
_FILE_PATH_RE = re.compile(r'(?:/[\w.\-]+)+\.[\w]+')
_FUNCTION_RE  = re.compile(r'(?:def|function|func)\s+(\w+)')
_EXIT_CODE_RE = re.compile(r'\[exit code:\s*(\d+)\]')
_ERROR_RE     = re.compile(r'Error:\s*(.{1,80})')
_GREP_PAT_RE  = re.compile(r"grep.*?[\"'](.+?)[\"']")


def extract_entities(text: str) -> list[tuple[str, str]]:
    """
    Extract typed entities from text. Returns deduplicated [(entity_type, value), ...].

    Types: file_path, function, error_code, pattern
    """
    found: set[tuple[str, str]] = set()

    for m in _FILE_PATH_RE.finditer(text):
        found.add(("file_path", m.group(0)))

    for m in _FUNCTION_RE.finditer(text):
        found.add(("function", m.group(1)))

    for m in _EXIT_CODE_RE.finditer(text):
        code = m.group(1)
        if code != "0":  # don't bother indexing successful exits
            found.add(("error_code", code))

    for m in _ERROR_RE.finditer(text):
        # Only store short error labels, not full sentences
        label = m.group(1).strip()
        if len(label) <= 40:
            found.add(("error_code", label))

    for m in _GREP_PAT_RE.finditer(text):
        found.add(("pattern", m.group(1)))

    return list(found)


# ── Ranking helpers ───────────────────────────────────────────────────────────

def rank_results_for_query(
    query: str,
    results: list[dict],
    top_k: int = 3,
) -> list[dict]:
    """
    Score a list of result dicts against a query using TF-IDF.

    Each dict must have "id" (str) and "text" (str).
    Returns up to top_k dicts sorted by relevance, with a "_score" key added.
    """
    if not results:
        return []

    idx = TFIDFIndex()
    for r in results:
        idx.add_document(str(r["id"]), r["text"])

    scores = dict(idx.score(query))
    ranked = sorted(results, key=lambda r: scores.get(str(r["id"]), 0.0), reverse=True)
    for r in ranked:
        r["_score"] = scores.get(str(r["id"]), 0.0)

    return ranked[:top_k]


def select_relevant_tool_results(
    query: str,
    tool_messages: list[dict],
    keep: int = 2,
) -> list[int]:
    """
    Given a query and a list of tool-role messages, return the indices of the
    `keep` most relevant ones by TF-IDF score.

    Falls back to most-recent indices if scores are too similar (spread < 0.05).
    """
    if len(tool_messages) <= keep:
        return list(range(len(tool_messages)))

    idx = TFIDFIndex()
    for i, msg in enumerate(tool_messages):
        idx.add_document(str(i), msg.get("content", ""))

    scores = idx.score(query)

    # Check score spread
    if scores:
        top_score = scores[0][1]
        bottom_score = scores[-1][1]
        if top_score - bottom_score < 0.05:
            # Scores too similar — fall back to most recent
            return list(range(max(0, len(tool_messages) - keep), len(tool_messages)))

    top_indices = [int(doc_id) for doc_id, _ in scores[:keep]]
    return top_indices

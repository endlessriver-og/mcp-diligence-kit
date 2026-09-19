"""Corpus parser shared by the MCP server, the agent's citation check, and the evals."""
import re
from pathlib import Path

PAGE_RE = re.compile(r"^<!-- page (\d+) -->$", re.M)


def load_corpus(corpus: Path) -> dict[str, dict]:
    docs = {}
    for p in sorted(corpus.glob("*.md")):
        text = p.read_text()
        _, front, body = text.split("---\n", 2)
        meta = dict(line.split(": ", 1) for line in front.strip().splitlines())
        parts = PAGE_RE.split(body)
        pages = {int(parts[i]): parts[i + 1].strip() for i in range(1, len(parts), 2)}
        docs[p.stem] = {"id": p.stem, "title": meta["title"], "doc_type": meta["doc_type"], "pages": pages}
    return docs

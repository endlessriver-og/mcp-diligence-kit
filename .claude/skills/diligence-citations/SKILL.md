---
name: diligence-citations
description: House rules for any answer drawn from the diligence data room — how to search, how to cite page-level sources, and when to flag for human review. Use for every lease, NDA, redline, or term-sheet question.
---

# Diligence citation rules

1. **Find, then read.** `search_documents` finds candidate pages; `read_page` gets the text you quote. Never quote from a search snippet.
2. **Every finding carries a citation**: `doc_id`, `page`, and a `quote` copied exactly, character for character, from `read_page` output. Keep quotes short (one clause, under 30 words). A claim you cannot quote is not a finding. Say so in `gaps`.
3. **Redlines**: use `compare_documents(base_id, revised_id)`, then confirm each change you report with `read_page` on the revised doc. Report the old and new value and which party the change favors.
4. **NDA form selection**: read the "WHEN TO USE THIS FORM" paragraph of each form. Pick by who discloses: both sides means Form A, only the company means Form B. Quote the sentence that decides it.
5. **Numbers**: copy dollar amounts, percentages and durations exactly as written. Do not compute totals unless asked. If you do compute one, show the inputs.
6. **Human review**: set `needs_human_review: true` if any requested fact is missing, two documents conflict, or the answer would be used to sign, send, pay or share.
7. **Writes**: `file_document` is gated. Propose filing in `proposed_actions`. A human approves it outside this session.

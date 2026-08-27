# -*- encoding: utf-8 -*-
"""Heading-aware markdown chunking.

A chunk carries the heading path it came from ("Developer guide > Scheduled
jobs (cron)") because that is what makes a citation readable and what gives the
embedding enough context to be retrievable on its own.

Token counts are estimated, not tokenized: the exact tokenizer depends on the
generation model, and every budget in this service is a safety margin rather
than a hard limit. Words * 1.35 is close enough for English and pt-BR prose and
costs nothing.
"""
import re
from dataclasses import dataclass, field

FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
BADGE_RE = re.compile(r"^\s*\[!\[.*?\]\(.*?\)\]\(.*?\)\s*$")


@dataclass
class Chunk:
    text: str
    title_path: str
    ord: int = 0
    meta: dict = field(default_factory=dict)


def estimate_tokens(text):
    """Rough token count. Deliberately an over-estimate rather than an under."""
    if not text:
        return 0
    return int(len(text.split()) * 1.35) + 1


def normalize_markdown(text):
    """Strip the parts of a markdown document that are noise for retrieval."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n")
    text = FRONTMATTER_RE.sub("", text)
    text = HTML_COMMENT_RE.sub("", text)
    lines = [ln for ln in text.split("\n") if not BADGE_RE.match(ln)]
    # collapse runs of blank lines
    out, blank = [], False
    for ln in lines:
        if ln.strip():
            out.append(ln.rstrip())
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def split_sections(text, title=""):
    """Split markdown into (heading_path, body) sections.

    Content before the first heading belongs to the document title. Fenced code
    blocks are passed through untouched -- a '#' inside a shell snippet is a
    comment, not a heading.
    """
    sections = []
    stack = []  # (level, heading text)
    body = []
    in_fence = False

    def flush():
        content = "\n".join(body).strip()
        if content:
            parts = ([title] if title else []) + [h for _lvl, h in stack]
            # a document whose H1 repeats its title would otherwise read
            # "FAQ > FAQ > How do I start a lab?"
            deduped = [p for i, p in enumerate(parts) if i == 0 or p != parts[i - 1]]
            sections.append((" > ".join(deduped) or title, content))
        body.clear()

    for line in text.split("\n"):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            body.append(line)
            continue
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
        else:
            body.append(line)
    flush()
    return sections


def _paragraphs(body):
    """Split a section body into blocks, keeping fenced code blocks whole."""
    blocks, current, in_fence = [], [], False
    for line in body.split("\n"):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            current.append(line)
            continue
        if not line.strip() and not in_fence:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


def _split_long_block(block, max_tokens):
    """Hard-split a single block that is bigger than a whole chunk."""
    words = block.split()
    per_chunk = max(1, int(max_tokens / 1.35))
    return [" ".join(words[i:i + per_chunk]) for i in range(0, len(words), per_chunk)]


def chunk_document(text, title="", chunk_tokens=400, overlap_tokens=60):
    """Chunk a markdown document into retrievable pieces.

    Sections are packed up to ``chunk_tokens``; when a section spills over, the
    tail of the previous chunk (~``overlap_tokens``) is prepended to the next so
    a sentence split across the boundary stays retrievable from either side.
    """
    text = normalize_markdown(text)
    if not text:
        return []

    chunks = []
    for path, body in split_sections(text, title=title):
        blocks = _paragraphs(body)
        current, current_tokens, overlap = [], 0, ""

        def emit():
            nonlocal current, current_tokens, overlap
            if not current:
                return
            content = "\n\n".join(current).strip()
            if content:
                chunks.append(Chunk(text=content, title_path=path, ord=len(chunks)))
                words = content.split()
                keep = max(0, int(overlap_tokens / 1.35))
                overlap = " ".join(words[-keep:]) if keep else ""
            current, current_tokens = [], 0

        for block in blocks:
            btokens = estimate_tokens(block)
            if btokens > chunk_tokens:
                emit()
                for piece in _split_long_block(block, chunk_tokens):
                    chunks.append(Chunk(text=piece, title_path=path, ord=len(chunks)))
                overlap = ""
                continue
            if current_tokens + btokens > chunk_tokens:
                emit()
                if overlap:
                    current.append(overlap)
                    current_tokens += estimate_tokens(overlap)
            current.append(block)
            current_tokens += btokens
        emit()

    for i, chunk in enumerate(chunks):
        chunk.ord = i
    return chunks

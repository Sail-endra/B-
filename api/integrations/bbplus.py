"""Serialize BB Plus's structured document IR for Course Copilot ingestion."""

from __future__ import annotations

import hashlib
import re
from typing import Any


MAX_DOCUMENT_CHARS = 2_000_000


def safe_material_filename(item_id: str, title: str = "") -> str:
    """Stable per Blackboard item. The display title stays in the file content."""
    item_hash = hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:16]
    return f"bbplus_{item_hash}.md"


def serialize_document(title: str, blocks: list[dict[str, Any]]) -> str:
    """Keep headings, lists, tables, equations, code, and page/slide locations."""
    lines = [f"# {title.strip() or 'Blackboard material'}", "", "Source: BB Plus / Blackboard", ""]
    readable = False

    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type", "")).lower()
        location = block.get("page")
        location_label = "page"
        if location is None:
            location = block.get("slide")
            location_label = "slide"
        if location is not None:
            lines.extend([f"[{location_label.title()} {str(location)[:30]}]", ""])

        text = block.get("text")
        if kind == "heading":
            content = str(text or "").strip()
            if content:
                level = max(1, min(6, int(block.get("level", 2) or 2)))
                lines.extend([f"{'#' * level} {content}", ""])
                readable = True
        elif kind == "paragraph":
            content = str(text or "").strip()
            if content:
                lines.extend([content, ""])
                readable = True
        elif kind == "list":
            items = block.get("items")
            if isinstance(items, list):
                for index, item in enumerate(items, 1):
                    content = str(item or "").strip()
                    if content:
                        prefix = f"{index}." if block.get("ordered") else "-"
                        lines.append(f"{prefix} {content}")
                        readable = True
                lines.append("")
        elif kind == "table":
            rows = block.get("rows")
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, list):
                        cells = [re.sub(r"\s+", " ", str(cell or "")).strip() for cell in row]
                        if any(cells):
                            lines.append(" | ".join(cells))
                            readable = True
                lines.append("")
        elif kind == "math":
            latex = str(block.get("latex") or "").strip()
            if latex:
                lines.extend([f"$${latex}$$", ""])
                readable = True
        elif kind == "code":
            code = str(text or "").strip()
            if code:
                language = re.sub(r"[^A-Za-z0-9_+-]", "", str(block.get("language") or ""))[:20]
                lines.extend([f"```{language}", code, "```", ""])
                readable = True
        elif kind == "image":
            caption = str(block.get("caption") or block.get("alt") or "").strip()
            if caption:
                lines.extend([f"[Figure: {caption}]", ""])
                readable = True
        # Unparsed blocks and image binaries are intentionally not claimed as
        # searchable text; the source remains available in BB Plus's local IR.

    content = "\n".join(lines).strip() + "\n"
    if len(content) > MAX_DOCUMENT_CHARS:
        raise ValueError("A Blackboard document exceeds the 2 MB text limit.")
    if not readable:
        raise ValueError("This Blackboard document contains no readable text, table, equation, or caption.")
    return content

"""Conservative text-source parsing helpers for Phase 6b3."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
LIST_PATTERN = re.compile(r"^\s*(?:[-+*]|\d+\.)\s+")
TABLE_SEPARATOR_PATTERN = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
RAW_HTML_PATTERN = re.compile(r"^\s*<[^>]+>\s*$")
FRONT_MATTER_DELIMITER = "---"
LINK_PATTERN = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
TEX_SECTION_PATTERN = re.compile(r"\\(?:sub)*section\{([^}]+)\}")
TEX_TITLE_PATTERN = re.compile(r"\\title\{(.+)\}")
YAML_KEY_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+):(?:\s+.+)?$")
YAML_SCALAR_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+):\s*(.+?)\s*$")
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z]+|[\u4e00-\u9fff]+")
MARKDOWN_HEADING_PREFIX_PATTERN = re.compile(r"^#{1,6}[ \t]+")
LATEX_WRAPPER_PATTERN = re.compile(r"\\[A-Za-z]+\*?\{([^{}]+)\}")
LATEX_COMMAND_PATTERN = re.compile(r"\\[A-Za-z]+\*?(?:\[[^\]]*\])?")
GENERIC_SECTION_TITLE_PATTERN = re.compile(r"^(?:section|sheet)\s+\d+$", re.IGNORECASE)
SKIPPED_TEXT_LINES = {"---", "...", "```", "~~~"}
YAML_TITLE_KEYS = {"title", "name", "subject", "topic", "heading"}


@dataclass(frozen=True)
class ParsedUnit:
    """One parsed evidence unit for a text-like source."""

    unit_id: str
    unit_type: str
    ordinal: int
    title: str
    text: str
    structure_data: dict[str, Any]
    embedded_media: list[dict[str, Any]]
    extraction_confidence: str
    warnings: list[str]


@dataclass(frozen=True)
class ParsedTextSource:
    """Parsed text-source output consumed by knowledge builders."""

    document_type: str
    source_title: str | None
    source_language: str
    units: list[ParsedUnit]
    document_media: list[dict[str, Any]]
    warnings: list[str]
    failures: list[str]


def _detect_language(texts: list[str]) -> str:
    joined = " ".join(texts)
    if not joined.strip():
        return "unknown"
    ascii_ratio = sum(1 for character in joined if ord(character) < 128) / max(len(joined), 1)
    if ascii_ratio > 0.95:
        return "en"
    return "mixed-or-non-en"


def _read_source_text(path: Path) -> tuple[str, list[str], list[str]]:
    warnings: list[str] = []
    failures: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
        if text.startswith("\ufeff"):
            text = text.removeprefix("\ufeff")
        return text, warnings, failures
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="utf-8-sig")
            warnings.append("Decoded source text with utf-8-sig fallback.")
            return text, warnings, failures
        except UnicodeDecodeError:
            try:
                text = path.read_bytes().decode("utf-8", errors="replace")
            except OSError as exc:
                failures.append(f"Could not read source text: {exc.strerror or str(exc)}")
                return "", warnings, failures
            warnings.append(
                "Source text required replacement decoding; some characters may be degraded."
            )
            return text, warnings, failures
    except OSError as exc:
        failures.append(f"Could not read source text: {exc.strerror or str(exc)}")
        return "", warnings, failures


def _tokenize_title_text(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_PATTERN.findall(text)]


def _similarity_text(text: str) -> str:
    return "".join(_tokenize_title_text(text))


def _strip_markdown_heading_prefix(text: str) -> str:
    return MARKDOWN_HEADING_PREFIX_PATTERN.sub("", text, count=1).strip()


def _clean_tex_inline(text: str) -> str:
    cleaned = text.strip()
    previous = None
    while cleaned != previous:
        previous = cleaned
        cleaned = LATEX_WRAPPER_PATTERN.sub(r"\1", cleaned)
    cleaned = cleaned.replace("\\\\", " ")
    cleaned = LATEX_COMMAND_PATTERN.sub(" ", cleaned)
    cleaned = cleaned.replace("{", " ").replace("}", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _normalize_headingish_line(document_type: str, raw_line: str) -> str | None:
    compact = " ".join(raw_line.strip().split())
    if not compact:
        return None
    if compact in SKIPPED_TEXT_LINES:
        return None
    if compact.startswith("%"):
        return None
    if document_type == "yaml":
        if compact.startswith("#"):
            return None
        scalar_match = YAML_SCALAR_PATTERN.match(compact)
        if scalar_match is not None:
            key = scalar_match.group(1).strip()
            value = scalar_match.group(2).strip().strip("'\"")
            if key.lower() in YAML_TITLE_KEYS and value not in {"|", "|-", ">", ">-"}:
                return value or None
        key_match = YAML_KEY_PATTERN.match(compact)
        if key_match is not None:
            return key_match.group(1).strip() or None
    compact = _strip_markdown_heading_prefix(compact)
    if not compact or compact in SKIPPED_TEXT_LINES:
        return None
    if document_type == "tex":
        if compact.startswith("\\"):
            if title_match := TEX_TITLE_PATTERN.match(compact):
                tex_title = _clean_tex_inline(title_match.group(1))
                return tex_title or None
            if section_match := TEX_SECTION_PATTERN.match(compact):
                tex_section = _clean_tex_inline(section_match.group(1))
                return tex_section or None
            return None
        cleaned_tex = _clean_tex_inline(compact)
        if cleaned_tex:
            compact = cleaned_tex
    if compact.startswith("\\") and document_type != "yaml":
        return None
    return compact or None


def _source_title_score(candidate: str, *, source_name: str | None) -> float:
    compact = " ".join(candidate.split()).strip()
    if not compact or compact in SKIPPED_TEXT_LINES:
        return -1.0
    if GENERIC_SECTION_TITLE_PATTERN.match(compact):
        return -1.0
    score = 0.0
    if len(compact) >= 12:
        score += 0.5
    if len(_tokenize_title_text(compact)) >= 2:
        score += 0.5
    if compact.startswith("\\"):
        score -= 2.0
    if source_name:
        candidate_similarity = _similarity_text(compact)
        source_similarity = _similarity_text(source_name)
        if candidate_similarity and source_similarity:
            score += 4.0 * SequenceMatcher(
                None,
                candidate_similarity,
                source_similarity,
            ).ratio()
    return score


def _pick_source_title(
    parsed_units: list[ParsedUnit],
    *,
    source_name: str | None,
) -> str | None:
    best_title: str | None = None
    best_score = -1.0
    for unit in parsed_units:
        candidate = " ".join(str(unit.title).split()).strip()
        score = _source_title_score(candidate, source_name=source_name)
        if score > best_score:
            best_title = candidate
            best_score = score
    if best_title and best_score >= 1.5:
        return best_title
    if source_name:
        fallback = " ".join(source_name.split()).strip()
        if fallback:
            return _truncate_title(fallback)
    return best_title


def _slugify_heading(text: str) -> str:
    compact = text.strip().lower()
    compact = re.sub(r"[^\w\s-]", "", compact)
    compact = compact.replace("_", "-")
    compact = re.sub(r"\s+", "-", compact)
    compact = re.sub(r"-{2,}", "-", compact)
    return compact.strip("-")


def _extract_front_matter(lines: list[str]) -> tuple[dict[str, Any], list[str], int]:
    if len(lines) < 3 or lines[0].strip() != FRONT_MATTER_DELIMITER:
        return {}, [], 0
    closing_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == FRONT_MATTER_DELIMITER:
            closing_index = index
            break
    if closing_index is None:
        return {}, [], 0
    metadata: dict[str, Any] = {}
    raw_lines = lines[1:closing_index]
    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        scalar = value.strip().strip("'\"")
        if key and scalar:
            metadata[key] = scalar
    return metadata, raw_lines, closing_index + 1


def _normalize_target(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if " " in target and not target.startswith(("http://", "https://")):
        target = target.split(" ", 1)[0].strip()
    return target


def _extract_inline_refs(
    text: str,
    *,
    line_start: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    links: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    for alt, raw_target in IMAGE_PATTERN.findall(text):
        images.append(
            {
                "alt_text": alt.strip(),
                "target": _normalize_target(raw_target),
                "line_start": line_start,
            }
        )
    for label, raw_target in LINK_PATTERN.findall(text):
        target = _normalize_target(raw_target)
        if target.startswith("!"):
            continue
        links.append(
            {
                "label": label.strip(),
                "target": target,
                "line_start": line_start,
            }
        )
    return links, images


def _truncate_title(text: str, *, limit: int = 120) -> str:
    compact = " ".join(text.split()).strip()
    if len(compact) <= limit:
        return compact or "Untitled Section"
    return compact[: limit - 3].rstrip() + "..."


def _markdown_block(
    *,
    kind: str,
    line_start: int,
    line_end: int,
    text: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    links, images = _extract_inline_refs(text, line_start=line_start)
    payload = {
        "kind": kind,
        "line_start": line_start,
        "line_end": line_end,
        "text": text,
        "links": links,
        "images": images,
    }
    if extra:
        payload.update(extra)
    return payload


def _parse_markdown_blocks(
    content_lines: list[tuple[int, str]],
    *,
    front_matter_metadata: dict[str, Any] | None = None,
    include_front_matter: bool = False,
    front_matter_lines: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], str]:
    blocks: list[dict[str, Any]] = []
    embedded_media: list[dict[str, Any]] = []
    warnings: list[str] = []
    if include_front_matter and front_matter_lines:
        front_text = "\n".join(front_matter_lines).strip()
        blocks.append(
            {
                "kind": "front_matter",
                "line_start": 1,
                "line_end": len(front_matter_lines),
                "text": front_text,
                "metadata": dict(front_matter_metadata or {}),
                "links": [],
                "images": [],
            }
        )

    index = 0
    while index < len(content_lines):
        line_number, line = content_lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            info = stripped[3:].strip()
            block_lines = [line]
            start_line = line_number
            end_line = start_line
            index += 1
            while index < len(content_lines):
                current_line_number, current_line = content_lines[index]
                block_lines.append(current_line)
                if current_line.strip().startswith(fence):
                    break
                end_line = current_line_number
                index += 1
            else:
                warnings.append(f"Unclosed fenced block starting at line {start_line}.")
            end_line = content_lines[min(index, len(content_lines) - 1)][0]
            block_text = "\n".join(block_lines).strip()
            block_kind = "mermaid" if info.lower() == "mermaid" else "code_fence"
            blocks.append(
                _markdown_block(
                    kind=block_kind,
                    line_start=start_line,
                    line_end=end_line,
                    text=block_text,
                    extra={"fence_info": info},
                )
            )
            index += 1
            continue

        if (
            index + 1 < len(content_lines)
            and "|" in line
            and TABLE_SEPARATOR_PATTERN.match(content_lines[index + 1][1].strip())
        ):
            start_line = line_number
            end_line = start_line
            block_lines = [line, content_lines[index + 1][1]]
            index += 2
            while index < len(content_lines):
                current_line_number, current_line = content_lines[index]
                if not current_line.strip() or "|" not in current_line:
                    break
                block_lines.append(current_line)
                index += 1
                end_line = current_line_number
            else:
                end_line = content_lines[-1][0]
            block_text = "\n".join(block_lines).strip()
            block = _markdown_block(
                kind="table",
                line_start=start_line,
                line_end=end_line,
                text=block_text,
            )
            blocks.append(block)
            embedded_media.extend(block["images"])
            continue

        if LIST_PATTERN.match(line):
            start_line = line_number
            end_line = start_line
            block_lines = [line]
            index += 1
            while index < len(content_lines):
                current_line_number, current_line = content_lines[index]
                if not current_line.strip():
                    break
                if not LIST_PATTERN.match(current_line) and not current_line.startswith(
                    (" ", "\t")
                ):
                    break
                block_lines.append(current_line)
                end_line = current_line_number
                index += 1
            else:
                end_line = content_lines[-1][0]
            block_text = "\n".join(block_lines).strip()
            block = _markdown_block(
                kind="list",
                line_start=start_line,
                line_end=end_line,
                text=block_text,
            )
            blocks.append(block)
            embedded_media.extend(block["images"])
            continue

        if RAW_HTML_PATTERN.match(stripped):
            start_line = line_number
            end_line = start_line
            block_lines = [line]
            index += 1
            while index < len(content_lines):
                current_line_number, current_line = content_lines[index]
                if not current_line.strip() or not RAW_HTML_PATTERN.match(current_line.strip()):
                    break
                block_lines.append(current_line)
                end_line = current_line_number
                index += 1
            else:
                end_line = content_lines[-1][0]
            block_text = "\n".join(block_lines).strip()
            block = _markdown_block(
                kind="raw_html_or_unsupported",
                line_start=start_line,
                line_end=end_line,
                text=block_text,
            )
            warnings.append(
                f"Preserved raw HTML or unsupported block at lines {start_line}-{end_line}."
            )
            blocks.append(block)
            embedded_media.extend(block["images"])
            continue

        start_line = line_number
        end_line = start_line
        block_lines = [line]
        index += 1
        while index < len(content_lines):
            current_line_number, current_line = content_lines[index]
            current_stripped = current_line.strip()
            if not current_stripped:
                break
            if current_stripped.startswith(("```", "~~~")):
                break
            if LIST_PATTERN.match(current_line):
                break
            if (
                index + 1 < len(content_lines)
                and "|" in current_line
                and TABLE_SEPARATOR_PATTERN.match(content_lines[index + 1][1].strip())
            ):
                break
            if RAW_HTML_PATTERN.match(current_stripped):
                break
            block_lines.append(current_line)
            end_line = current_line_number
            index += 1
        else:
            end_line = content_lines[-1][0]
        block_text = "\n".join(block_lines).strip()
        block = _markdown_block(
            kind="paragraph",
            line_start=start_line,
            line_end=end_line,
            text=block_text,
        )
        blocks.append(block)
        embedded_media.extend(block["images"])

    normalized_embedded_media: list[dict[str, Any]] = []
    seen_media: set[tuple[str, int]] = set()
    for image in embedded_media:
        key = (str(image.get("target") or ""), int(image.get("line_start") or 0))
        if key in seen_media:
            continue
        seen_media.add(key)
        normalized_embedded_media.append(image)

    confidence = "high"
    if warnings:
        confidence = "medium"
    if not blocks:
        confidence = "low"
    return blocks, normalized_embedded_media, warnings, confidence


def _parse_markdown_like_source(text: str, *, document_type: str) -> ParsedTextSource:
    lines = text.splitlines()
    front_matter_metadata, front_matter_lines, content_start_index = _extract_front_matter(lines)
    content_lines = lines[content_start_index:]
    sections: list[dict[str, Any]] = []
    current_content: list[tuple[int, str]] = []
    current_heading: dict[str, Any] | None = None
    in_fence = False
    fence_marker = ""
    for offset, raw_line in enumerate(content_lines, start=content_start_index + 1):
        stripped = raw_line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if in_fence and marker == fence_marker:
                in_fence = False
                fence_marker = ""
            elif not in_fence:
                in_fence = True
                fence_marker = marker
        heading_match = HEADING_PATTERN.match(raw_line)
        if heading_match and not in_fence:
            if current_heading is not None or current_content:
                sections.append(
                    {
                        "heading": dict(current_heading) if current_heading is not None else None,
                        "content": list(current_content),
                    }
                )
            current_heading = {
                "level": len(heading_match.group(1)),
                "text": heading_match.group(2).strip(),
                "line_number": offset,
            }
            current_content = []
            continue
        current_content.append((offset, raw_line))
    if current_heading is not None or current_content:
        sections.append(
            {
                "heading": dict(current_heading) if current_heading is not None else None,
                "content": list(current_content),
            }
        )

    parsed_units: list[ParsedUnit] = []
    document_media: list[dict[str, Any]] = []
    warnings: list[str] = []
    source_title = None
    if isinstance(front_matter_metadata.get("title"), str):
        source_title = str(front_matter_metadata["title"]).strip() or None
    for index, section in enumerate(sections, start=1):
        heading = section.get("heading")
        content = section.get("content", [])
        include_front_matter = bool(index == 1 and front_matter_lines)
        blocks, media, unit_warnings, confidence = _parse_markdown_blocks(
            content,
            front_matter_metadata=front_matter_metadata,
            include_front_matter=include_front_matter,
            front_matter_lines=front_matter_lines,
        )
        heading_text = str(heading.get("text")) if isinstance(heading, dict) else ""
        heading_level = (
            int(heading["level"])
            if isinstance(heading, dict) and isinstance(heading.get("level"), int)
            else None
        )
        heading_line_number = (
            int(heading["line_number"])
            if isinstance(heading, dict) and isinstance(heading.get("line_number"), int)
            else None
        )
        if not source_title and heading_text:
            source_title = heading_text
        title = heading_text or source_title or "Introduction"
        line_start = (
            1
            if include_front_matter
            else (
                heading_line_number
                if heading_line_number is not None
                else (content[0][0] if content else 1)
            )
        )
        last_line = line_start
        if content:
            last_line = int(content[-1][0])
        if heading_line_number is not None and content:
            last_line = max(last_line, int(content[-1][0]))
        elif heading_line_number is not None:
            last_line = heading_line_number
        unit_lines: list[str] = []
        if include_front_matter and front_matter_lines:
            unit_lines.extend(
                [FRONT_MATTER_DELIMITER, *front_matter_lines, FRONT_MATTER_DELIMITER, ""]
            )
        if heading_text and heading_level is not None:
            unit_lines.append("#" * heading_level + " " + heading_text)
        unit_lines.extend(raw_line for _line_number, raw_line in content)
        unit_text = "\n".join(unit_lines).strip()
        slug_anchor = _slugify_heading(heading_text) if heading_text else None
        structure_data = {
            "heading": heading_text or None,
            "heading_level": heading_level,
            "slug_anchor": slug_anchor,
            "line_start": line_start,
            "line_end": last_line,
            "front_matter": dict(front_matter_metadata) if include_front_matter else {},
            "blocks": blocks,
        }
        parsed_units.append(
            ParsedUnit(
                unit_id=f"section-{index:03d}",
                unit_type="section",
                ordinal=index,
                title=_truncate_title(title),
                text=unit_text,
                structure_data=structure_data,
                embedded_media=media,
                extraction_confidence=confidence,
                warnings=unit_warnings,
            )
        )
        document_media.extend(media)
        warnings.extend(unit_warnings)

    if not parsed_units and front_matter_lines:
        structure_data = {
            "heading": None,
            "heading_level": None,
            "slug_anchor": None,
            "line_start": 1,
            "line_end": len(lines),
            "front_matter": dict(front_matter_metadata),
            "blocks": [
                {
                    "kind": "front_matter",
                    "line_start": 1,
                    "line_end": len(front_matter_lines),
                    "text": "\n".join(front_matter_lines).strip(),
                    "metadata": dict(front_matter_metadata),
                    "links": [],
                    "images": [],
                }
            ],
        }
        parsed_units.append(
            ParsedUnit(
                unit_id="section-001",
                unit_type="section",
                ordinal=1,
                title=_truncate_title(source_title or "Introduction"),
                text=text.strip(),
                structure_data=structure_data,
                embedded_media=[],
                extraction_confidence="medium",
                warnings=["Document contains front matter but no body content."],
            )
        )
        warnings.append("Document contains front matter but no body content.")

    return ParsedTextSource(
        document_type=document_type,
        source_title=source_title,
        source_language=_detect_language([unit.text for unit in parsed_units]),
        units=parsed_units,
        document_media=document_media,
        warnings=list(dict.fromkeys(warnings)),
        failures=[],
    )


def _infer_headingish_aliases(document_type: str, block_text: str) -> list[str]:
    aliases: list[str] = []
    for raw_line in block_text.splitlines():
        normalized_line = _normalize_headingish_line(document_type, raw_line)
        if normalized_line is None:
            continue
        compact = _truncate_title(normalized_line, limit=90)
        if len(compact.split()) <= 12:
            aliases.append(compact)
        break
    if document_type == "tex":
        if title_match := TEX_TITLE_PATTERN.search(block_text):
            tex_title = _clean_tex_inline(title_match.group(1))
            if tex_title:
                aliases.insert(0, _truncate_title(tex_title, limit=90))
        for match in TEX_SECTION_PATTERN.finditer(block_text):
            section_title = _clean_tex_inline(match.group(1))
            if section_title:
                aliases.append(_truncate_title(section_title, limit=90))
    if document_type == "yaml":
        for line in block_text.splitlines():
            stripped = line.strip()
            scalar_match = YAML_SCALAR_PATTERN.match(stripped)
            if scalar_match is not None:
                key = scalar_match.group(1).strip()
                value = scalar_match.group(2).strip().strip("'\"")
                if key.lower() in YAML_TITLE_KEYS and value not in {"|", "|-", ">", ">-"}:
                    aliases.insert(0, _truncate_title(value, limit=90))
                    continue
            yaml_match = YAML_KEY_PATTERN.match(stripped)
            if yaml_match is not None:
                aliases.append(yaml_match.group(1))
    deduplicated: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        if alias and alias not in seen:
            seen.add(alias)
            deduplicated.append(alias)
    return deduplicated


def _parse_paragraph_source(
    text: str,
    *,
    document_type: str,
    source_name: str | None = None,
) -> ParsedTextSource:
    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []
    current_lines: list[tuple[int, str]] = []
    for line_number, raw_line in enumerate(lines, start=1):
        if raw_line.strip():
            current_lines.append((line_number, raw_line))
            continue
        if current_lines:
            blocks.append({"lines": list(current_lines)})
            current_lines = []
    if current_lines:
        blocks.append({"lines": list(current_lines)})

    parsed_units: list[ParsedUnit] = []
    warnings: list[str] = []
    for index, block in enumerate(blocks, start=1):
        block_lines = list(block["lines"])
        block_text = "\n".join(raw_line for _line_number, raw_line in block_lines).strip()
        if not block_text:
            continue
        line_start = int(block_lines[0][0])
        line_end = int(block_lines[-1][0])
        headingish_aliases = _infer_headingish_aliases(document_type, block_text)
        title = headingish_aliases[0] if headingish_aliases else f"Section {index}"
        structure_data = {
            "line_start": line_start,
            "line_end": line_end,
            "headingish_aliases": headingish_aliases,
            "blocks": [
                {
                    "kind": "paragraph",
                    "line_start": line_start,
                    "line_end": line_end,
                    "text": block_text,
                }
            ],
        }
        parsed_units.append(
            ParsedUnit(
                unit_id=f"section-{index:03d}",
                unit_type="section",
                ordinal=index,
                title=_truncate_title(title),
                text=block_text,
                structure_data=structure_data,
                embedded_media=[],
                extraction_confidence="high",
                warnings=[],
            )
        )
    if not parsed_units and text.strip():
        warnings.append(
            "Text source did not split into paragraph blocks cleanly; preserved whole file."
        )
        parsed_units.append(
            ParsedUnit(
                unit_id="section-001",
                unit_type="section",
                ordinal=1,
                title="Section 1",
                text=text.strip(),
                structure_data={
                    "line_start": 1,
                    "line_end": max(len(lines), 1),
                    "headingish_aliases": [],
                    "blocks": [
                        {
                            "kind": "paragraph",
                            "line_start": 1,
                            "line_end": max(len(lines), 1),
                            "text": text.strip(),
                        }
                    ],
                },
                embedded_media=[],
                extraction_confidence="medium",
                warnings=list(warnings),
            )
        )
    source_title = _pick_source_title(parsed_units, source_name=source_name)
    return ParsedTextSource(
        document_type=document_type,
        source_title=source_title,
        source_language=_detect_language([unit.text for unit in parsed_units]),
        units=parsed_units,
        document_media=[],
        warnings=warnings,
        failures=[],
    )


def _parse_delimited_source(
    text: str,
    *,
    document_type: str,
    source_name: str,
) -> ParsedTextSource:
    delimiter = "\t" if document_type == "tsv" else ","
    lines = text.splitlines()
    reader = csv.reader(lines, delimiter=delimiter)
    rows = [row for row in reader]
    warnings: list[str] = []
    header_names = [value.strip() for value in rows[0]] if rows else []
    sample_rows = rows[1:21] if len(rows) > 1 else rows[:20]
    row_lengths = {len(row) for row in rows if row}
    if len(row_lengths) > 1:
        warnings.append("Delimited rows have inconsistent column counts.")
    structure_data = {
        "sheet_name": "Sheet 1",
        "delimiter": delimiter,
        "header_names": header_names,
        "line_start": 1,
        "line_end": max(len(lines), 1),
        "row_count": max(len(rows) - 1, 0) if header_names else len(rows),
        "sample_rows": sample_rows,
        "blocks": [
            {
                "kind": "table",
                "line_start": 1,
                "line_end": max(len(lines), 1),
                "text": text.strip(),
            }
        ],
    }
    unit = ParsedUnit(
        unit_id="sheet-001",
        unit_type="sheet",
        ordinal=1,
        title=_truncate_title(source_name or "Sheet 1"),
        text=text.strip(),
        structure_data=structure_data,
        embedded_media=[],
        extraction_confidence="medium" if warnings else "high",
        warnings=warnings,
    )
    return ParsedTextSource(
        document_type=document_type,
        source_title=unit.title,
        source_language=_detect_language([text]),
        units=[unit],
        document_media=[],
        warnings=warnings,
        failures=[],
    )


def parse_text_source(source_path: Path, *, document_type: str) -> ParsedTextSource:
    """Parse one supported text-like source into conservative evidence units."""
    text, warnings, failures = _read_source_text(source_path)
    if failures:
        return ParsedTextSource(
            document_type=document_type,
            source_title=None,
            source_language="unknown",
            units=[],
            document_media=[],
            warnings=warnings,
            failures=failures,
        )
    if document_type in {"markdown", "mdx"}:
        parsed = _parse_markdown_like_source(text, document_type=document_type)
    elif document_type in {"csv", "tsv"}:
        parsed = _parse_delimited_source(
            text,
            document_type=document_type,
            source_name=source_path.stem,
        )
    else:
        parsed = _parse_paragraph_source(
            text,
            document_type=document_type,
            source_name=source_path.stem,
        )
    return ParsedTextSource(
        document_type=parsed.document_type,
        source_title=parsed.source_title,
        source_language=parsed.source_language,
        units=parsed.units,
        document_media=parsed.document_media,
        warnings=list(dict.fromkeys([*warnings, *parsed.warnings])),
        failures=list(dict.fromkeys([*failures, *parsed.failures])),
    )

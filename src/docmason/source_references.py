"""Shared user-native source reference normalization and resolution helpers."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .project import WorkspacePaths, read_json
from .routing import normalize_source_scope_intent, question_has_compare_scope_hint
from .truth_boundary import format_user_visible_source_ref

TOKEN_PATTERN = re.compile(r"[0-9A-Za-z]+|[\u4e00-\u9fff]+")
PAGE_PATTERN = re.compile(r"\bpage\s*(?:no\.?|number|#)?\s*(\d{1,4})\b", re.IGNORECASE)
SLIDE_PATTERN = re.compile(r"\bslide\s*(?:no\.?|number|#)?\s*(\d{1,4})\b", re.IGNORECASE)
SHEET_NUMBER_PATTERN = re.compile(r"\bsheet\s*(?:no\.?|number|#)?\s*(\d{1,4})\b", re.IGNORECASE)
SHEET_NAME_PATTERN = re.compile(r"\bsheet\s+([A-Za-z0-9][A-Za-z0-9 _-]{0,79})", re.IGNORECASE)
CELL_HINT_PATTERN = re.compile(r"\b([A-Z]{1,3}\d{1,7}(?::[A-Z]{1,3}\d{1,7})?)\b")
LINE_PATTERN = re.compile(
    r"\blines?\s*(\d{1,6})(?:\s*(?:-|to|through)\s*(\d{1,6}))?\b",
    re.IGNORECASE,
)
ROW_PATTERN = re.compile(
    r"\brows?\s*(\d{1,6})(?:\s*(?:-|to|through)\s*(\d{1,6}))?\b",
    re.IGNORECASE,
)
HEADER_PATTERN = re.compile(
    r"\b(?:header|column)\s+(?:named\s+)?[\"']?([A-Za-z0-9][A-Za-z0-9 _.-]{0,79})[\"']?",
    re.IGNORECASE,
)
ANCHOR_PATTERN = re.compile(
    r"(?:^|\s)#([A-Za-z0-9][A-Za-z0-9_-]{0,119})\b|\banchor\s+([A-Za-z0-9][A-Za-z0-9_-]{0,119})\b",
    re.IGNORECASE,
)

GENERIC_UNIT_TITLE_PATTERN = re.compile(
    r"^(?:page|slide|section|sheet|block)\s+\d+$",
    re.IGNORECASE,
)
LONG_HEXISH_PATTERN = re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE)
DOCUMENT_HINT_PATTERN = re.compile(
    (
        r"\b(deck|document|proposal|ppt|pptx|pdf|xlsx|docx|sheet|slide|page|file|doc|"
        r"xls|markdown|md|markdown|txt|text|csv|tsv|yaml|yml|mdx|tex|table|email|mail|message|"
        r"eml|attachment)\b"
    ),
    re.IGNORECASE,
)
ARTIFACT_HINT_PATTERN = re.compile(
    r"\b(diagram|figure|chart|table|kpi|metric|dashboard|architecture|flow|screenshot|ui|image|picture|photo|caption|legend)\b",
    re.IGNORECASE,
)
EXPLICIT_SOURCE_REQUEST_PATTERN = re.compile(
    (
        r"\b(?:using|use)\s+only\s+the\s+(?:document|deck|file|proposal|pdf|pptx?|docx?|xlsx?|"
        r"spreadsheet|sheet|markdown|email|message|eml)\s+[\"']([^\"']+)[\"']"
        r"|\bonly\s+the\s+(?:document|deck|file|proposal|pdf|pptx?|docx?|xlsx?|spreadsheet|"
        r"sheet|markdown|email|message|eml)\s+[\"']([^\"']+)[\"']"
        r"|\b(?:document|deck|file|proposal|pdf|pptx?|docx?|xlsx?|spreadsheet|sheet|"
        r"markdown|email|message|eml)\s+[\"']([^\"']+)[\"']"
    ),
    re.IGNORECASE,
)
QUOTED_SOURCE_PATH_PATTERN = re.compile(r"(?P<quote>[\"'`])(original_doc/.+?)(?P=quote)")
QUOTED_TEXT_PATTERN = re.compile(r"(?P<quote>[\"'`])(?P<text>.+?)(?P=quote)")
COMPARE_CLAUSE_PATTERN = re.compile(
    r"\bcompare\s+(?P<left>.+?)\s+(?:versus|vs\.?|and)\s+(?P<right>.+)",
    re.IGNORECASE,
)
COMPARE_BETWEEN_PATTERN = re.compile(
    r"\b(?:difference\s+between|between)\s+(?P<left>.+?)\s+and\s+(?P<right>.+)",
    re.IGNORECASE,
)
COMPARE_TRAILING_CONTEXT_PATTERN = re.compile(
    r"\s+(?:on|about|regarding|with\s+respect\s+to|in\s+terms\s+of|across)\s+.+$",
    re.IGNORECASE,
)
LEADING_SOURCE_LABEL_PATTERN = re.compile(
    r"^(?:the\s+)?(?:document|deck|file|proposal|spreadsheet|sheet|email|message|eml|attachment)\s+",
    re.IGNORECASE,
)
SINGLE_SOURCE_REQUEST_PATTERN = re.compile(
    (
        r"\b(using only|use only|only the document|only the deck|only the file|"
        r"do not use any other source|don't use any other source|no other source|"
        r"single document|single source)\b"
    ),
    re.IGNORECASE,
)
GENERIC_SOURCE_TEXT_TOKENS = frozenset(
    {
        "page",
        "slide",
        "sheet",
        "section",
        "row",
        "line",
        "use",
        "uses",
        "cases",
        "case",
        "architecture",
        "strategy",
        "detail",
        "details",
        "summary",
        "roadmap",
        "purpose",
        "components",
        "main",
        "benefits",
        "scenarios",
    }
)


def _nonempty_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _deduplicate_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = _nonempty_string(value)
        if normalized is None:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def tokenize_text(text: str) -> list[str]:
    """Return normalized lexical tokens for source reference matching."""
    return [token.lower() for token in TOKEN_PATTERN.findall(text)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _mapping_copy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return dict(value)


def normalized_text(text: str) -> str:
    """Return a whitespace-normalized token string for phrase matching."""
    return " ".join(tokenize_text(text))


def _contains_normalized_phrase(query_text: str, alias: str) -> bool:
    normalized_query = normalized_text(query_text)
    normalized_alias = normalized_text(alias)
    if not normalized_query or not normalized_alias:
        return False
    return normalized_alias in normalized_query


def _token_overlap_score(query_text: str, alias: str) -> float:
    query_tokens = set(tokenize_text(query_text))
    alias_tokens = tokenize_text(alias)
    if not query_tokens or not alias_tokens:
        return 0.0
    overlap = query_tokens & set(alias_tokens)
    if not overlap:
        return 0.0
    return len(overlap) / max(len(set(alias_tokens)), 1)


def _alias_can_be_exact(alias: str) -> bool:
    tokens = tokenize_text(alias)
    if not tokens:
        return False
    if len(tokens) == 1:
        token = tokens[0]
        if len(token) <= 1:
            return False
        if len(token) == 2 and token.isalpha() and "/" not in alias and "." not in alias:
            return False
    return True


def _filename_stem_aliases(stem: str) -> list[str]:
    cleaned = stem.replace("_", " ").replace("-", " ")
    aliases = [cleaned]
    tokens = [token for token in tokenize_text(cleaned) if token]
    if tokens:
        collapsed = " ".join(tokens)
        if _alias_can_be_exact(collapsed):
            aliases.append(collapsed)
        simplified = [
            token
            for token in tokens
            if not token.isdigit() and not LONG_HEXISH_PATTERN.match(token)
        ]
        if len(simplified) >= 2:
            aliases.append(" ".join(simplified))
    return _deduplicate_strings(aliases)


def build_source_reference_fields(
    source_manifest: dict[str, Any],
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Return normalized source alias fields for a source-like record."""
    current_path = _nonempty_string(source_manifest.get("current_path")) or ""
    path_history = [
        value
        for value in source_manifest.get("path_history", [])
        if isinstance(value, str) and value
    ]
    prior_paths = [
        value
        for value in source_manifest.get("prior_paths", [])
        if isinstance(value, str) and value
    ]
    path_aliases: list[str] = []
    for path_value in [current_path, *prior_paths, *path_history]:
        normalized = _nonempty_string(path_value)
        if normalized is None:
            continue
        path_aliases.append(normalized)
        path_obj = Path(normalized)
        path_aliases.append(path_obj.name)
        if normalized.startswith("original_doc/"):
            path_aliases.append(normalized.removeprefix("original_doc/"))
        path_aliases.extend(_filename_stem_aliases(path_obj.stem))
    raw_title = title or str(source_manifest.get("title") or "")
    title_aliases = _filename_stem_aliases(raw_title) if raw_title else []
    return {
        "path_aliases": _deduplicate_strings(path_aliases),
        "title_aliases": title_aliases,
        "source_aliases": _deduplicate_strings(path_aliases + title_aliases),
    }


def enrich_source_manifest_reference_fields(
    source_manifest: dict[str, Any],
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Return a source manifest enriched with normalized source alias fields."""
    enriched = dict(source_manifest)
    enriched.update(build_source_reference_fields(source_manifest, title=title))
    return enriched


def _render_ordinal_from_reference(reference: str | None) -> int | None:
    if not isinstance(reference, str) or not reference:
        return None
    stem = Path(reference).stem
    tokens = tokenize_text(stem)
    if not tokens:
        return None
    final = tokens[-1]
    if final.isdigit():
        return int(final)
    return None


def _render_ordinal_from_unit(unit: dict[str, Any]) -> int | None:
    references: list[str] = []
    rendered_asset = unit.get("rendered_asset")
    if isinstance(rendered_asset, str) and rendered_asset:
        references.append(rendered_asset)
    for key in ("render_references", "render_reference_ids"):
        values = unit.get(key, [])
        if isinstance(values, list):
            references.extend(value for value in values if isinstance(value, str) and value)
    for reference in references:
        ordinal = _render_ordinal_from_reference(reference)
        if isinstance(ordinal, int):
            return ordinal
    return None


def _heading_like_aliases_from_docx(structure_data: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    blocks = structure_data.get("blocks", [])
    if not isinstance(blocks, list):
        return aliases
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = _nonempty_string(block.get("text"))
        if text is None:
            continue
        collapsed = " ".join(text.split())
        if len(collapsed) > 120:
            continue
        token_count = len(tokenize_text(collapsed))
        if token_count == 0 or token_count > 14:
            continue
        aliases.append(collapsed)
        if len(aliases) >= 12:
            break
    return _deduplicate_strings(aliases)


def _heading_like_aliases_from_text_structure(structure_data: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    heading = _nonempty_string(structure_data.get("heading"))
    if heading is not None:
        aliases.append(heading)
    headingish_aliases = structure_data.get("headingish_aliases", [])
    if isinstance(headingish_aliases, list):
        aliases.extend(alias for alias in headingish_aliases if isinstance(alias, str))
    slug_anchor = _nonempty_string(structure_data.get("slug_anchor"))
    if slug_anchor is not None:
        aliases.extend([slug_anchor, f"#{slug_anchor}", f"anchor {slug_anchor}"])
    return _deduplicate_strings(aliases)


def _title_like_aliases(
    *,
    source_manifest: dict[str, Any],
    unit: dict[str, Any],
    structure_data: dict[str, Any],
    text_content: str,
) -> list[str]:
    aliases: list[str] = []
    title = _nonempty_string(unit.get("title"))
    if title is not None and not GENERIC_UNIT_TITLE_PATTERN.match(title):
        aliases.append(title)

    document_type = str(source_manifest.get("document_type") or "")
    if document_type == "pptx":
        for value in structure_data.get("visible_text", []):
            if not isinstance(value, str):
                continue
            collapsed = " ".join(value.split())
            if not collapsed or collapsed.isdigit() or len(collapsed) > 100:
                continue
            if len(tokenize_text(collapsed)) > 14:
                continue
            aliases.append(collapsed)
            if len(aliases) >= 8:
                break
    elif document_type == "xlsx":
        sheet_name = _nonempty_string(structure_data.get("sheet_name"))
        if sheet_name is not None:
            aliases.append(sheet_name)
    elif document_type == "docx":
        aliases.extend(_heading_like_aliases_from_docx(structure_data))
    elif document_type in {"markdown", "mdx", "plaintext", "yaml", "tex", "csv", "tsv"}:
        aliases.extend(_heading_like_aliases_from_text_structure(structure_data))
    else:
        for line in text_content.splitlines():
            collapsed = " ".join(line.split())
            if not collapsed or collapsed.isdigit() or len(collapsed) > 100:
                continue
            token_count = len(tokenize_text(collapsed))
            if token_count == 0 or token_count > 14:
                continue
            aliases.append(collapsed)
            if len(aliases) >= 8:
                break
    return _deduplicate_strings(aliases)


def build_unit_reference_fields(
    source_manifest: dict[str, Any],
    unit: dict[str, Any],
    *,
    structure_data: dict[str, Any] | None = None,
    text_content: str = "",
) -> dict[str, Any]:
    """Return normalized locator and alias fields for one evidence unit."""
    structure = structure_data if isinstance(structure_data, dict) else {}
    unit_type = str(unit.get("unit_type") or "")
    logical_ordinal = unit.get("logical_ordinal")
    if not isinstance(logical_ordinal, int):
        ordinal = unit.get("ordinal")
        logical_ordinal = ordinal if isinstance(ordinal, int) else None
    render_ordinal = unit.get("render_ordinal")
    if not isinstance(render_ordinal, int):
        render_ordinal = _render_ordinal_from_unit(unit)
    sheet_name = _nonempty_string(unit.get("sheet_name"))
    if sheet_name is None:
        sheet_name = _nonempty_string(structure.get("sheet_name"))
    line_start = unit.get("line_start")
    if not isinstance(line_start, int):
        structure_line_start = structure.get("line_start")
        line_start = structure_line_start if isinstance(structure_line_start, int) else None
    line_end = unit.get("line_end")
    if not isinstance(line_end, int):
        structure_line_end = structure.get("line_end")
        line_end = structure_line_end if isinstance(structure_line_end, int) else None
    slug_anchor = _nonempty_string(unit.get("slug_anchor"))
    if slug_anchor is None:
        slug_anchor = _nonempty_string(structure.get("slug_anchor"))
    header_names = [
        name
        for name in structure.get("header_names", [])
        if isinstance(name, str) and _nonempty_string(name) is not None
    ]
    row_count = structure.get("row_count")
    if not isinstance(row_count, int):
        row_count = None
    heading_aliases = [
        alias
        for alias in (
            _heading_like_aliases_from_docx(structure)
            if str(source_manifest.get("document_type") or "") == "docx"
            else _heading_like_aliases_from_text_structure(structure)
        )
        if alias
    ]
    semantic_page_aliases = _title_like_aliases(
        source_manifest=source_manifest,
        unit=unit,
        structure_data=structure,
        text_content=text_content,
    )
    normalized_unit_type = "section" if unit_type == "email-section" else unit_type
    locator_aliases: list[str] = []
    if normalized_unit_type in {"page", "slide", "sheet", "section"} and isinstance(
        logical_ordinal, int
    ):
        locator_aliases.extend(
            [
                f"{normalized_unit_type} {logical_ordinal}",
                f"{normalized_unit_type} #{logical_ordinal}",
                f"{normalized_unit_type}-{logical_ordinal:03d}",
            ]
        )
    if unit_type == "sheet" and sheet_name:
        locator_aliases.append(sheet_name)
        locator_aliases.append(f"sheet {sheet_name}")
        locator_aliases.append(f"{sheet_name} sheet")
    if unit_type == "email-attachment":
        filename = _nonempty_string(structure.get("attachment_filename"))
        if filename is not None:
            locator_aliases.extend([filename, f"attachment {filename}"])
        if isinstance(logical_ordinal, int):
            locator_aliases.extend(
                [f"attachment {logical_ordinal}", f"attachment #{logical_ordinal}"]
            )
    if unit_type == "slide" and isinstance(render_ordinal, int):
        locator_aliases.extend(
            [
                f"render page {render_ordinal}",
                f"page {render_ordinal}",
            ]
        )
    if isinstance(line_start, int):
        locator_aliases.extend([f"line {line_start}", f"lines {line_start}"])
        if isinstance(line_end, int) and line_end != line_start:
            locator_aliases.append(f"lines {line_start}-{line_end}")
    if slug_anchor:
        locator_aliases.extend([slug_anchor, f"#{slug_anchor}", f"anchor {slug_anchor}"])
    locator_aliases.extend(heading_aliases)
    locator_aliases.extend(semantic_page_aliases)
    cell_hint_supported = bool(unit_type == "sheet")
    return {
        "logical_ordinal": logical_ordinal,
        "render_ordinal": render_ordinal,
        "sheet_name": sheet_name,
        "line_start": line_start,
        "line_end": line_end,
        "slug_anchor": slug_anchor,
        "header_names": _deduplicate_strings(header_names),
        "row_count": row_count,
        "heading_aliases": _deduplicate_strings(heading_aliases),
        "semantic_page_aliases": _deduplicate_strings(semantic_page_aliases),
        "locator_aliases": _deduplicate_strings(locator_aliases),
        "cell_hint_supported": cell_hint_supported,
    }


def enrich_evidence_manifest_reference_fields(
    source_manifest: dict[str, Any],
    evidence_manifest: dict[str, Any],
    *,
    source_dir: Path | None = None,
) -> dict[str, Any]:
    """Return an evidence manifest enriched with normalized locator fields."""
    enriched = dict(evidence_manifest)
    enriched_units: list[dict[str, Any]] = []
    for unit in evidence_manifest.get("units", []):
        if not isinstance(unit, dict):
            continue
        structure_data: dict[str, Any] = {}
        text_content = ""
        if source_dir is not None:
            structure_asset = unit.get("structure_asset")
            if isinstance(structure_asset, str) and structure_asset:
                structure_data = read_json(source_dir / structure_asset)
            text_asset = unit.get("text_asset")
            if isinstance(text_asset, str) and text_asset:
                try:
                    text_content = (source_dir / text_asset).read_text(encoding="utf-8")
                except FileNotFoundError:
                    text_content = ""
        if not structure_data and isinstance(unit.get("structure_summary"), str):
            try:
                structure_data = json.loads(str(unit["structure_summary"]))
            except json.JSONDecodeError:
                structure_data = {}
        if not text_content:
            text_content = str(unit.get("text", ""))
        enriched_unit = dict(unit)
        enriched_unit.update(
            build_unit_reference_fields(
                source_manifest,
                unit,
                structure_data=structure_data,
                text_content=text_content,
            )
        )
        enriched_units.append(enriched_unit)
    enriched["units"] = enriched_units
    return enriched


def build_reference_resolution_summary(reference_resolution: dict[str, Any] | None) -> str | None:
    """Return the compact review-facing resolution summary label."""
    if not isinstance(reference_resolution, dict):
        return None
    status = reference_resolution.get("status")
    if status == "exact":
        return "exact-reference"
    if status == "approximate":
        return "approximate-reference"
    if status == "unresolved":
        return "unresolved-reference"
    return None


def normalize_source_record_reference(record: dict[str, Any]) -> dict[str, Any]:
    """Return a source-like record with normalized alias fields."""
    normalized = dict(record)
    title = _nonempty_string(record.get("title"))
    normalized.update(
        build_source_reference_fields(
            {
                "current_path": record.get("current_path"),
                "prior_paths": record.get("prior_paths", []),
                "path_history": record.get("path_history", []),
                "title": title,
            },
            title=title,
        )
    )
    return normalized


def normalize_unit_record_reference(record: dict[str, Any]) -> dict[str, Any]:
    """Return a unit-like record with normalized locator fields."""
    normalized = dict(record)
    structure_data: dict[str, Any] = {}
    structure_summary = record.get("structure_summary")
    if isinstance(structure_summary, str) and structure_summary.strip():
        try:
            loaded = json.loads(structure_summary)
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            structure_data = loaded
    normalized.update(
        build_unit_reference_fields(
            {
                "document_type": record.get("document_type"),
            },
            record,
            structure_data=structure_data,
            text_content=str(record.get("text", "")),
        )
    )
    return normalized


def _document_ref_detected(query: str, source_candidates: list[dict[str, Any]]) -> bool:
    if DOCUMENT_HINT_PATTERN.search(query):
        return True
    return any(bool(candidate.get("exact_source_match")) for candidate in source_candidates)


def extract_requested_source_text(query: str) -> str | None:
    """Extract the strongest user-requested source text from a question when present."""
    if not isinstance(query, str) or not query.strip():
        return None
    quoted_path_match = QUOTED_SOURCE_PATH_PATTERN.search(query)
    if quoted_path_match is not None:
        candidate = _nonempty_string(quoted_path_match.group(2))
        if candidate is not None:
            return candidate
    start = query.find("original_doc/")
    if start >= 0:
        candidate_chars: list[str] = []
        for char in query[start:]:
            if char in "\n\r\t,;!?\"'`()[]{}<>":
                break
            candidate_chars.append(char)
        candidate = _nonempty_string("".join(candidate_chars).rstrip(" .:"))
        if candidate is not None:
            return candidate
    match = EXPLICIT_SOURCE_REQUEST_PATTERN.search(query)
    if match is None:
        return None
    for group in match.groups():
        candidate = _nonempty_string(group)
        if candidate is not None:
            return candidate
    return None


def extract_declared_compare_source_texts(query: str) -> list[str]:
    """Extract deterministically declared compare-source texts when the query provides them."""
    if not isinstance(query, str) or not query.strip():
        return []
    if not question_has_compare_scope_hint(query):
        return []

    quoted_candidates = _deduplicate_strings([
        normalized
        for match in QUOTED_TEXT_PATTERN.finditer(query)
        if (
            normalized := _nonempty_string(match.group("text"))
        ) is not None
    ])
    if len(quoted_candidates) >= 2:
        return quoted_candidates[:2]

    def _normalize_compare_clause_text(
        value: str,
        *,
        trim_trailing_context: bool,
        allow_single_token: bool,
    ) -> str | None:
        candidate = _nonempty_string(value)
        if candidate is None:
            return None
        if trim_trailing_context:
            candidate = COMPARE_TRAILING_CONTEXT_PATTERN.sub("", candidate)
        candidate = LEADING_SOURCE_LABEL_PATTERN.sub("", candidate)
        candidate = candidate.strip(" \"'`.,:;?!")
        normalized = _nonempty_string(candidate)
        if normalized is None:
            return None
        if allow_single_token:
            return normalized
        tokens = tokenize_text(normalized)
        if normalized.startswith("original_doc/") or "/" in normalized or "." in normalized:
            return normalized
        if len(tokens) >= 2:
            return normalized
        return None

    def _extract_compare_clause_side(
        value: str,
        *,
        trim_trailing_context: bool,
    ) -> str | None:
        quoted_candidates = [
            normalized
            for match in QUOTED_TEXT_PATTERN.finditer(value)
            if (
                normalized := _normalize_compare_clause_text(
                    match.group("text"),
                    trim_trailing_context=False,
                    allow_single_token=True,
                )
            )
            is not None
        ]
        if quoted_candidates:
            return quoted_candidates[0]
        return _normalize_compare_clause_text(
            value,
            trim_trailing_context=trim_trailing_context,
            allow_single_token=False,
        )

    clause_match = COMPARE_BETWEEN_PATTERN.search(query) or COMPARE_CLAUSE_PATTERN.search(query)
    if clause_match is None:
        return []
    left = _extract_compare_clause_side(clause_match.group("left"), trim_trailing_context=False)
    right = _extract_compare_clause_side(clause_match.group("right"), trim_trailing_context=True)
    return _deduplicate_strings([
        candidate
        for candidate in [left, right]
        if candidate is not None
    ])


def question_has_single_source_constraint(query: str) -> bool:
    """Return whether the question explicitly requires a single source."""
    if not isinstance(query, str) or not query.strip():
        return False
    return bool(SINGLE_SOURCE_REQUEST_PATTERN.search(query))


def _requested_source_text_is_generic(requested_source_text: str | None) -> bool:
    requested = _nonempty_string(requested_source_text)
    if requested is None:
        return False
    tokens = [token for token in tokenize_text(requested) if token]
    if not tokens:
        return True
    return all(token in GENERIC_SOURCE_TEXT_TOKENS for token in tokens)


def _alias_query_coverage(query: str, alias: str | None) -> float:
    alias_text = _nonempty_string(alias)
    if alias_text is None:
        return 0.0
    query_tokens = set(tokenize_text(query))
    alias_tokens = set(tokenize_text(alias_text))
    if not query_tokens or not alias_tokens:
        return 0.0
    return len(alias_tokens & query_tokens) / len(query_tokens)


def _source_narrowing_allowed(
    query: str,
    *,
    chosen_source: dict[str, Any] | None,
    chosen_source_status: str | None,
    parsed_locator: dict[str, Any],
) -> bool:
    if not isinstance(chosen_source, dict):
        return False
    if chosen_source_status != "exact":
        return False
    if question_has_compare_scope_hint(query):
        return False
    if DOCUMENT_HINT_PATTERN.search(query):
        return True
    locator_type = _nonempty_string(parsed_locator.get("locator_type"))
    if locator_type in {"page", "slide", "sheet", "line", "row", "section"}:
        return True
    if _alias_query_coverage(query, chosen_source.get("matched_alias")) >= 0.5:
        return True
    if ARTIFACT_HINT_PATTERN.search(query):
        return False
    return True


def _source_alias_match(
    query: str, alias: str, *, weight: float, basis: str
) -> tuple[float, str] | None:
    normalized_query = normalized_text(query)
    normalized_alias = normalized_text(alias)
    if normalized_alias and _alias_can_be_exact(alias) and normalized_alias in normalized_query:
        return weight, f"exact-{basis}"
    overlap = _token_overlap_score(query, alias)
    alias_token_count = len(set(tokenize_text(alias)))
    if alias_token_count >= 2 and overlap >= 0.5:
        return round(weight * overlap, 3), f"approx-{basis}"
    return None


def _parse_locator_hints(query: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {
        "raw_text": None,
        "locator_type": None,
        "logical_ordinal": None,
        "render_ordinal": None,
        "sheet_name": None,
        "cell_hint": None,
        "semantic_alias_text": None,
        "line_start": None,
        "line_end": None,
        "row_start": None,
        "row_end": None,
        "header_name": None,
        "anchor": None,
    }
    if slide_match := SLIDE_PATTERN.search(query):
        parsed.update(
            {
                "raw_text": slide_match.group(0),
                "locator_type": "slide",
                "logical_ordinal": int(slide_match.group(1)),
            }
        )
    elif page_match := PAGE_PATTERN.search(query):
        parsed.update(
            {
                "raw_text": page_match.group(0),
                "locator_type": "page",
                "logical_ordinal": int(page_match.group(1)),
            }
        )
    elif sheet_number_match := SHEET_NUMBER_PATTERN.search(query):
        parsed.update(
            {
                "raw_text": sheet_number_match.group(0),
                "locator_type": "sheet",
                "logical_ordinal": int(sheet_number_match.group(1)),
            }
        )
    elif sheet_name_match := SHEET_NAME_PATTERN.search(query):
        raw_sheet_name = sheet_name_match.group(1).strip()
        raw_sheet_name = CELL_HINT_PATTERN.sub("", raw_sheet_name).strip(" -:_")
        if raw_sheet_name:
            parsed.update(
                {
                    "raw_text": sheet_name_match.group(0),
                    "locator_type": "sheet",
                    "sheet_name": raw_sheet_name,
                }
            )
    if cell_match := CELL_HINT_PATTERN.search(query):
        parsed["cell_hint"] = cell_match.group(1)
        if parsed["locator_type"] is None:
            parsed["locator_type"] = "sheet"
            parsed["raw_text"] = cell_match.group(1)
    if line_match := LINE_PATTERN.search(query):
        parsed["locator_type"] = parsed["locator_type"] or "line"
        parsed["line_start"] = int(line_match.group(1))
        parsed["line_end"] = (
            int(line_match.group(2)) if line_match.group(2) else int(line_match.group(1))
        )
        parsed["raw_text"] = line_match.group(0)
    if row_match := ROW_PATTERN.search(query):
        parsed["locator_type"] = parsed["locator_type"] or "row"
        parsed["row_start"] = int(row_match.group(1))
        parsed["row_end"] = (
            int(row_match.group(2)) if row_match.group(2) else int(row_match.group(1))
        )
        parsed["raw_text"] = row_match.group(0)
    if header_match := HEADER_PATTERN.search(query):
        header_name = header_match.group(1).strip(" \"'")
        if header_name:
            parsed["header_name"] = header_name
            parsed["locator_type"] = parsed["locator_type"] or "sheet"
            parsed["raw_text"] = parsed["raw_text"] or header_match.group(0)
    if anchor_match := ANCHOR_PATTERN.search(query):
        anchor_value = anchor_match.group(1) or anchor_match.group(2)
        if anchor_value:
            parsed["anchor"] = anchor_value
            parsed["locator_type"] = parsed["locator_type"] or "section"
            parsed["raw_text"] = parsed["raw_text"] or anchor_match.group(0).strip()
    return parsed


def _unit_locator_score(
    query: str,
    unit: dict[str, Any],
    *,
    parsed_locator: dict[str, Any],
) -> dict[str, Any] | None:
    score = 0.0
    match_basis: list[str] = []
    exact = False
    locator_type = parsed_locator.get("locator_type")
    locator_hint_matched = False
    logical_ordinal = parsed_locator.get("logical_ordinal")
    if isinstance(logical_ordinal, int):
        if locator_type == unit.get("unit_type") and unit.get("logical_ordinal") == logical_ordinal:
            score += 60.0
            exact = True
            locator_hint_matched = True
            match_basis.append("exact-logical-ordinal")
        elif unit.get("render_ordinal") == logical_ordinal and (
            locator_type == unit.get("unit_type")
            or (locator_type == "page" and unit.get("unit_type") == "slide")
        ):
            score += 55.0
            exact = True
            locator_hint_matched = True
            match_basis.append("exact-render-ordinal-alias")
    sheet_name = _nonempty_string(parsed_locator.get("sheet_name"))
    if sheet_name is not None and sheet_name == unit.get("sheet_name"):
        score += 60.0
        exact = True
        locator_hint_matched = True
        match_basis.append("exact-sheet-name")
    anchor = _nonempty_string(parsed_locator.get("anchor"))
    if anchor is not None and anchor == unit.get("slug_anchor"):
        score += 60.0
        exact = True
        locator_hint_matched = True
        match_basis.append("exact-slug-anchor")
    line_start = parsed_locator.get("line_start")
    line_end = parsed_locator.get("line_end")
    unit_line_start = unit.get("line_start")
    unit_line_end = unit.get("line_end")
    if (
        isinstance(line_start, int)
        and isinstance(line_end, int)
        and isinstance(unit_line_start, int)
        and isinstance(unit_line_end, int)
        and line_start >= unit_line_start
        and line_end <= unit_line_end
    ):
        score += 58.0
        exact = True
        locator_hint_matched = True
        match_basis.append("exact-line-span")
    row_start = parsed_locator.get("row_start")
    row_end = parsed_locator.get("row_end")
    row_count = unit.get("row_count")
    if (
        isinstance(row_start, int)
        and isinstance(row_end, int)
        and isinstance(row_count, int)
        and unit.get("unit_type") == "sheet"
        and row_start >= 1
        and row_start <= row_end
        and row_end <= row_count
    ):
        score += 18.0
        locator_hint_matched = True
        match_basis.append("row-hint")
    header_name = _nonempty_string(parsed_locator.get("header_name"))
    header_name_normalized = normalized_text(header_name) if header_name is not None else None
    header_names = {
        normalized_text(str(name).strip())
        for name in unit.get("header_names", [])
        if isinstance(name, str) and str(name).strip()
    }
    if header_name_normalized is not None and header_name_normalized in header_names:
        score += 22.0
        locator_hint_matched = True
        match_basis.append("header-hint")
    if parsed_locator.get("cell_hint") and unit.get("cell_hint_supported"):
        score += 8.0
        locator_hint_matched = True
        match_basis.append("sheet-cell-hint")
    if locator_type is not None and not locator_hint_matched:
        return None
    heading_aliases = {alias for alias in unit.get("heading_aliases", []) if isinstance(alias, str)}
    semantic_aliases = {
        alias for alias in unit.get("semantic_page_aliases", []) if isinstance(alias, str)
    }
    best_alias_score = 0.0
    best_alias_basis: str | None = None
    best_alias_text: str | None = None
    for alias in unit.get("locator_aliases", []):
        if not isinstance(alias, str):
            continue
        match = _source_alias_match(query, alias, weight=38.0, basis="unit-alias")
        if match is None:
            continue
        alias_score, alias_basis = match
        if alias in heading_aliases or alias in semantic_aliases:
            alias_score = min(alias_score, 31.0)
            alias_basis = alias_basis.replace("exact-", "approx-")
        if alias_score > best_alias_score or (
            alias_score == best_alias_score
            and alias_basis.startswith("exact-")
            and not str(best_alias_basis or "").startswith("exact-")
        ):
            best_alias_score = alias_score
            best_alias_basis = alias_basis
            best_alias_text = alias
    if best_alias_basis is not None:
        score += best_alias_score
        match_basis.append(best_alias_basis)
        if best_alias_basis.startswith("exact-"):
            exact = True
    if score <= 0:
        return None
    return {
        "unit_id": unit.get("unit_id"),
        "score": score,
        "exact": exact,
        "match_basis": match_basis,
        "matched_alias": best_alias_text,
        "logical_ordinal": unit.get("logical_ordinal"),
        "render_ordinal": unit.get("render_ordinal"),
        "unit_type": unit.get("unit_type"),
    }


def _source_candidate_sort_key(item: dict[str, Any]) -> tuple[float, float, str]:
    exact_rank = 1.0 if bool(item.get("exact_source_match")) else 0.0
    return (
        -exact_rank,
        -float(item.get("source_score", 0.0)),
        str(item.get("source_id") or ""),
    )


def _combined_candidate_sort_key(item: dict[str, Any]) -> tuple[float, float, float, str]:
    return (
        -float(item.get("score", 0.0)),
        -(1.0 if bool(item.get("exact_source_match")) else 0.0),
        -float(item.get("source_score", 0.0)),
        str(item.get("source_id") or ""),
    )


def _clear_source_winner(
    top: dict[str, Any],
    second: dict[str, Any] | None,
) -> bool:
    top_score = float(top.get("score", 0.0))
    if second is None:
        return top_score > 0.0
    second_score = float(second.get("score", 0.0))
    if top_score <= second_score:
        return False
    if bool(top.get("exact_source_match")) and not bool(second.get("exact_source_match")):
        return True
    return (top_score - second_score) >= 8.0 or top_score >= (second_score * 1.25)


def _compare_source_status(candidate: dict[str, Any]) -> str:
    return "exact" if bool(candidate.get("exact_source_match")) else "approximate"


def _build_source_candidate(
    source: dict[str, Any],
    *,
    source_query: str,
    query: str,
    units_by_source: dict[str, list[dict[str, Any]]],
    parsed_locator: dict[str, Any],
) -> dict[str, Any] | None:
    source_id = source.get("source_id")
    if not isinstance(source_id, str):
        return None
    if str(source.get("source_family") or "corpus") != "corpus":
        return None
    best_score = 0.0
    best_basis: str | None = None
    matched_alias: str | None = None
    exact_match = False
    for alias in source.get("path_aliases", []):
        if not isinstance(alias, str):
            continue
        match = _source_alias_match(source_query, alias, weight=90.0, basis="path-alias")
        if match is None:
            continue
        alias_score, alias_basis = match
        if alias_score > best_score:
            best_score = alias_score
            best_basis = alias_basis
            matched_alias = alias
            exact_match = alias_basis.startswith("exact-")
    for alias in source.get("title_aliases", []):
        if not isinstance(alias, str):
            continue
        match = _source_alias_match(source_query, alias, weight=72.0, basis="title-alias")
        if match is None:
            continue
        alias_score, alias_basis = match
        if alias_score > best_score:
            best_score = alias_score
            best_basis = alias_basis
            matched_alias = alias
            exact_match = alias_basis.startswith("exact-")
    best_unit, unit_candidates = _pick_best_unit_candidate(
        query,
        units_by_source.get(source_id, []),
        parsed_locator=parsed_locator,
    )
    if best_score <= 0 and best_unit is None:
        return None
    return {
        "source_id": source_id,
        "source_score": best_score,
        "score": best_score + (float(best_unit["score"]) if best_unit is not None else 0.0),
        "exact_source_match": exact_match,
        "match_basis": [best_basis] if best_basis else [],
        "matched_alias": matched_alias,
        "best_unit": best_unit,
        "candidate_unit_ids": [
            item["unit_id"]
            for item in unit_candidates[:3]
            if isinstance(item.get("unit_id"), str)
        ],
        "current_path": source.get("current_path"),
        "title": source.get("title"),
    }


def _resolve_declared_compare_source(
    requested_source_text: str,
    *,
    normalized_source_records: list[dict[str, Any]],
    units_by_source: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    parsed_locator = _parse_locator_hints(requested_source_text)
    candidates = [
        candidate
        for source in normalized_source_records
        if (
            candidate := _build_source_candidate(
                source,
                source_query=requested_source_text,
                query=requested_source_text,
                units_by_source=units_by_source,
                parsed_locator=parsed_locator,
            )
        )
        is not None
    ]
    candidates.sort(key=_combined_candidate_sort_key)
    exact_candidates = [item for item in candidates if item.get("exact_source_match")]
    chosen: dict[str, Any] | None = None
    status = "unresolved"
    if exact_candidates:
        top_exact = exact_candidates[0]
        second_exact = exact_candidates[1] if len(exact_candidates) > 1 else None
        if _clear_source_winner(top_exact, second_exact):
            chosen = top_exact
            status = "exact"
    if chosen is None and candidates:
        approximate_candidates = [
            item for item in candidates if float(item.get("source_score", 0.0)) > 0.0
        ]
        if approximate_candidates:
            top_approx = approximate_candidates[0]
            second_approx = (
                approximate_candidates[1] if len(approximate_candidates) > 1 else None
            )
            if _clear_source_winner(top_approx, second_approx):
                chosen = top_approx
                status = "approximate"
    return {
        "requested_source_text": requested_source_text,
        "source_match_status": status,
        "resolved_source_id": (
            _nonempty_string(chosen.get("source_id")) if isinstance(chosen, dict) else None
        ),
        "match_basis": (
            _string_list(chosen.get("match_basis")) if isinstance(chosen, dict) else []
        ),
        "candidate_source_ids": [
            candidate["source_id"]
            for candidate in candidates[:3]
            if isinstance(candidate.get("source_id"), str)
        ],
        "target_source_ref": (
            format_user_visible_source_ref(
                title=(chosen.get("title") if isinstance(chosen.get("title"), str) else None),
                current_path=(
                    chosen.get("current_path")
                    if isinstance(chosen.get("current_path"), str)
                    else None
                ),
            )
            if isinstance(chosen, dict)
            else None
        ),
    }


def _declared_compare_sources(
    query: str,
    *,
    normalized_source_records: list[dict[str, Any]],
    units_by_source: dict[str, list[dict[str, Any]]],
    source_candidates: list[dict[str, Any]],
    source_scope_intent: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    compare_intent = normalize_source_scope_intent(source_scope_intent, question=query)
    compare_requested = compare_intent["mode"] == "compare" or bool(
        question_has_compare_scope_hint(query)
    )
    if not compare_requested:
        return []

    declared_source_texts = list(compare_intent["explicit_source_texts"]) or (
        extract_declared_compare_source_texts(query)
    )
    if declared_source_texts:
        return [
            _resolve_declared_compare_source(
                requested_source_text,
                normalized_source_records=normalized_source_records,
                units_by_source=units_by_source,
            )
            for requested_source_text in declared_source_texts
        ]

    ranked_candidates = [
        item
        for item in source_candidates
        if bool(item.get("exact_source_match")) or float(item.get("source_score", 0.0)) > 0.0
    ]
    ranked_candidates.sort(key=_combined_candidate_sort_key)
    declared: list[dict[str, Any]] = []
    seen_source_ids: set[str] = set()
    for candidate in ranked_candidates:
        source_id = _nonempty_string(candidate.get("source_id"))
        if source_id is None or source_id in seen_source_ids:
            continue
        seen_source_ids.add(source_id)
        declared.append(
            {
                "source_id": source_id,
                "source_match_status": _compare_source_status(candidate),
                "match_basis": _string_list(candidate.get("match_basis")),
                "target_source_ref": format_user_visible_source_ref(
                    title=(
                        candidate.get("title")
                        if isinstance(candidate.get("title"), str)
                        else None
                    ),
                    current_path=(
                        candidate.get("current_path")
                        if isinstance(candidate.get("current_path"), str)
                        else None
                    ),
                )
                or _nonempty_string(candidate.get("current_path")),
                "requested_source_text": _nonempty_string(candidate.get("matched_alias")),
            }
        )
        if len(declared) >= 2:
            break
    return declared


def _apply_compare_resolution(
    result: dict[str, Any],
    *,
    query: str,
    normalized_source_records: list[dict[str, Any]],
    units_by_source: dict[str, list[dict[str, Any]]],
    source_candidates: list[dict[str, Any]],
    source_scope_intent: dict[str, Any] | None,
) -> None:
    compare_intent = normalize_source_scope_intent(source_scope_intent, question=query)
    compare_requested = compare_intent["mode"] == "compare" or bool(
        question_has_compare_scope_hint(query)
    )
    explicit_compare_scope = bool(
        compare_intent["explicit_source_texts"] or extract_declared_compare_source_texts(query)
    )
    declared_sources = _declared_compare_sources(
        query,
        normalized_source_records=normalized_source_records,
        units_by_source=units_by_source,
        source_candidates=source_candidates,
        source_scope_intent=compare_intent,
    )
    expected_count = (
        len(declared_sources)
        if declared_sources
        else (int(compare_intent["expected_source_count"]) if compare_requested else 0)
    )
    compare_source_ids = _deduplicate_strings([
        item["resolved_source_id"]
        for item in declared_sources
        if isinstance(item.get("resolved_source_id"), str)
    ])
    compare_source_refs = _deduplicate_strings([
        item["target_source_ref"]
        for item in declared_sources
        if isinstance(item.get("target_source_ref"), str)
    ])
    if expected_count <= 0:
        result["declared_compare_sources"] = []
        result["declared_compare_source_ids"] = []
        result["declared_compare_source_refs"] = []
        result["declared_compare_expected_count"] = 0
        result["declared_compare_missing_count"] = 0
        result["compare_resolution_status"] = None
        return
    missing_count = max(expected_count - len(compare_source_ids), 0)
    if len(compare_source_ids) >= expected_count and all(
        item.get("source_match_status") == "exact" for item in declared_sources[:expected_count]
    ):
        compare_status = "exact"
    elif len(compare_source_ids) >= expected_count:
        compare_status = "approximate"
    else:
        compare_status = "unresolved"

    hard_boundary_on_missing_sources = bool(
        compare_intent["hard_boundary_on_missing_sources"] or explicit_compare_scope
    )

    if compare_requested and hard_boundary_on_missing_sources and compare_status != "exact":
        compare_status = "unresolved"

    result["declared_compare_sources"] = declared_sources
    result["declared_compare_source_ids"] = compare_source_ids
    result["declared_compare_source_refs"] = compare_source_refs
    result["declared_compare_expected_count"] = expected_count
    result["declared_compare_missing_count"] = missing_count
    result["compare_resolution_status"] = compare_status
    result["status"] = compare_status
    if compare_status == "exact":
        result["continued_with_best_effort"] = False
        result["unresolved_reason"] = None
        result["notice_text"] = None
        result["hard_boundary"] = False
        if (
            result.get("resolved_source_id") is None
            and result.get("source_match_status") == "unresolved"
        ):
            result["source_match_status"] = "none"
        if (
            result.get("resolved_unit_id") is None
            and result.get("unit_match_status") == "unresolved"
        ):
            result["unit_match_status"] = "none"
    elif compare_status == "approximate":
        result["continued_with_best_effort"] = True
        result["unresolved_reason"] = "compare-soft-approximation"
        result["notice_text"] = (
            "I isolated the comparison scope, but at least one requested source was only an "
            "approximate published match. I am continuing with explicit approximation truth."
        )
    elif compare_status == "unresolved":
        result["continued_with_best_effort"] = not hard_boundary_on_missing_sources
        result["unresolved_reason"] = "compare-source-unresolved"
        result["notice_text"] = (
            "I could not isolate every requested comparison source exactly, so I am stopping "
            "at that comparison boundary."
            if hard_boundary_on_missing_sources
            else (
                "I could not isolate every requested comparison source clearly. I am "
                "continuing with explicit unresolved comparison truth instead of treating "
                "the scope as exact."
            )
        )
        result["hard_boundary"] = hard_boundary_on_missing_sources


def _pick_best_unit_candidate(
    query: str,
    units: list[dict[str, Any]],
    *,
    parsed_locator: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    unit_candidates = [
        candidate
        for unit in units
        if (candidate := _unit_locator_score(query, unit, parsed_locator=parsed_locator))
        is not None
    ]
    unit_candidates.sort(key=lambda item: (-float(item["score"]), str(item.get("unit_id") or "")))
    best_unit = unit_candidates[0] if unit_candidates else None
    if len(unit_candidates) >= 2 and best_unit is not None:
        second_unit = unit_candidates[1]
        if float(best_unit.get("score") or 0.0) == float(second_unit.get("score") or 0.0) and bool(
            best_unit.get("exact")
        ) == bool(second_unit.get("exact")):
            best_unit = {**best_unit, "ambiguous": True}
    return best_unit, unit_candidates


def resolve_reference_query(
    query: str,
    *,
    source_records: list[dict[str, Any]],
    unit_records: list[dict[str, Any]],
    source_scope_intent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one user query onto source and unit references when possible."""
    normalized_source_records = [
        normalize_source_record_reference(record)
        for record in source_records
        if isinstance(record, dict)
    ]
    normalized_unit_records = [
        normalize_unit_record_reference(record)
        for record in unit_records
        if isinstance(record, dict)
    ]
    units_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in normalized_unit_records:
        source_id = unit.get("source_id")
        source_family = str(unit.get("source_family") or "corpus")
        if isinstance(source_id, str) and source_family == "corpus":
            units_by_source[source_id].append(unit)

    requested_source_text = extract_requested_source_text(query)
    compare_intent = normalize_source_scope_intent(source_scope_intent, question=query)
    compare_scope_requested = compare_intent["mode"] == "compare" or bool(
        question_has_compare_scope_hint(query)
    )
    declared_compare_source_texts = (
        list(compare_intent["explicit_source_texts"])
        or extract_declared_compare_source_texts(query)
    )
    source_query = (
        requested_source_text
        if requested_source_text is not None
        and not _requested_source_text_is_generic(requested_source_text)
        else query
    )
    parsed_locator = _parse_locator_hints(query)
    source_candidates = [
        candidate
        for source in normalized_source_records
        if (
            candidate := _build_source_candidate(
                source,
                source_query=source_query,
                query=query,
                units_by_source=units_by_source,
                parsed_locator=parsed_locator,
            )
        )
        is not None
    ]

    source_candidates.sort(key=_combined_candidate_sort_key)
    document_ref_detected = bool(requested_source_text) or _document_ref_detected(
        query, source_candidates
    )
    detected = (
        document_ref_detected
        or parsed_locator.get("locator_type") is not None
        or bool(compare_scope_requested and declared_compare_source_texts)
    )
    result = {
        "detected": detected,
        "parsed_document_ref": (
            {"raw_text": requested_source_text, "match_basis": []}
            if requested_source_text is not None
            else None
        ),
        "parsed_locator_ref": parsed_locator
        if any(value is not None for value in parsed_locator.values())
        else None,
        "status": "none" if not detected else "unresolved",
        "source_match_status": "none" if not detected else "unresolved",
        "unit_match_status": "none"
        if not detected
        else ("unresolved" if parsed_locator.get("locator_type") is not None else "none"),
        "match_basis": [],
        "resolved_source_id": None,
        "resolved_unit_id": None,
        "candidate_source_ids": [
            item["source_id"]
            for item in source_candidates[:3]
            if isinstance(item.get("source_id"), str)
        ],
        "candidate_unit_ids": [],
        "source_narrowing_allowed": False,
        "continued_with_best_effort": False,
        "notice_text": None,
        "requested_source_text": requested_source_text,
        "scope_mode": "global",
        "unresolved_reason": None,
        "hard_boundary": False,
        "target_source_ref": None,
        "analysis_guard_applied": False,
        "declared_compare_sources": [],
        "declared_compare_source_ids": [],
        "declared_compare_source_refs": [],
        "declared_compare_expected_count": 0,
        "declared_compare_missing_count": 0,
        "compare_resolution_status": None,
    }
    if not detected:
        return result
    exact_source_candidates = [item for item in source_candidates if item.get("exact_source_match")]
    chosen_source: dict[str, Any] | None = None
    chosen_source_status: str | None = None
    if exact_source_candidates:
        exact_source_candidates.sort(key=_combined_candidate_sort_key)
        top_exact = exact_source_candidates[0]
        second_exact = exact_source_candidates[1] if len(exact_source_candidates) > 1 else None
        if _clear_source_winner(top_exact, second_exact):
            chosen_source = top_exact
            chosen_source_status = "exact"
        elif requested_source_text is not None:
            chosen_source = None
            chosen_source_status = None
    if chosen_source is None and source_candidates:
        approximate_sources = [
            item for item in source_candidates if float(item.get("source_score", 0.0)) > 0.0
        ]
        if approximate_sources and not (
            requested_source_text is not None and len(exact_source_candidates) >= 2
        ):
            approximate_sources.sort(key=_combined_candidate_sort_key)
            top_approx = approximate_sources[0]
            second_approx = approximate_sources[1] if len(approximate_sources) > 1 else None
            if _clear_source_winner(top_approx, second_approx):
                chosen_source = top_approx
                chosen_source_status = "approximate"
        elif any(item.get("best_unit") is not None for item in source_candidates):
            source_candidates.sort(key=_combined_candidate_sort_key)
            chosen_source = source_candidates[0]
            chosen_source_status = "approximate"

    if chosen_source is not None:
        top = chosen_source
        match_basis = _string_list(result.get("match_basis"))
        result["match_basis"] = match_basis
        result["source_narrowing_allowed"] = _source_narrowing_allowed(
            query,
            chosen_source=top,
            chosen_source_status=chosen_source_status,
            parsed_locator=parsed_locator,
        )
        result["parsed_document_ref"] = {
            "raw_text": requested_source_text or top.get("matched_alias"),
            "match_basis": _string_list(top.get("match_basis")),
        }
        result["status"] = chosen_source_status or "approximate"
        result["source_match_status"] = chosen_source_status or "approximate"
        result["resolved_source_id"] = top["source_id"]
        result["target_source_ref"] = format_user_visible_source_ref(
            title=top.get("title") if isinstance(top.get("title"), str) else None,
            current_path=(
                top.get("current_path")
                if isinstance(top.get("current_path"), str)
                else None
            ),
        ) or top.get("current_path")
        match_basis.extend(_string_list(top.get("match_basis")))
        best_unit = top.get("best_unit")
        if isinstance(best_unit, dict) and (
            parsed_locator.get("locator_type") is not None or requested_source_text is None
        ):
            result["candidate_unit_ids"] = _string_list(top.get("candidate_unit_ids"))
            parsed_locator_ref = _mapping_copy(result.get("parsed_locator_ref"))
            parsed_locator_ref["matched_alias"] = best_unit.get("matched_alias")
            if isinstance(best_unit.get("logical_ordinal"), int):
                parsed_locator_ref["resolved_logical_ordinal"] = best_unit.get("logical_ordinal")
            if isinstance(best_unit.get("render_ordinal"), int):
                parsed_locator_ref["resolved_render_ordinal"] = best_unit.get("render_ordinal")
            if (
                best_unit.get("unit_type") == "slide"
                and isinstance(best_unit.get("logical_ordinal"), int)
                and isinstance(best_unit.get("render_ordinal"), int)
                and best_unit.get("logical_ordinal") != best_unit.get("render_ordinal")
            ):
                parsed_locator_ref["ordinal_difference"] = {
                    "logical": best_unit.get("logical_ordinal"),
                    "render": best_unit.get("render_ordinal"),
                }
            result["parsed_locator_ref"] = parsed_locator_ref
            if bool(best_unit.get("ambiguous")):
                result["status"] = "approximate"
                result["continued_with_best_effort"] = True
                result["unit_match_status"] = "unresolved"
                match_basis.extend(_string_list(best_unit.get("match_basis")))
            elif bool(best_unit.get("exact")):
                result["resolved_unit_id"] = best_unit.get("unit_id")
                result["unit_match_status"] = "exact"
                match_basis.extend(_string_list(best_unit.get("match_basis")))
            elif float(best_unit.get("score") or 0.0) >= 22.0:
                result["resolved_unit_id"] = best_unit.get("unit_id")
                result["status"] = "approximate"
                result["continued_with_best_effort"] = True
                result["unit_match_status"] = "approximate"
                match_basis.extend(_string_list(best_unit.get("match_basis")))
        if (
            result["status"] == "exact"
            and result["resolved_unit_id"] is None
            and parsed_locator.get("locator_type")
        ):
            result["status"] = "approximate"
            result["continued_with_best_effort"] = True
            result["unit_match_status"] = "unresolved"
        if result["status"] == "approximate":
            result["continued_with_best_effort"] = True
            result["unresolved_reason"] = "soft-approximation"
    single_source_constraint = question_has_single_source_constraint(query)
    hard_missing_source = bool(
        single_source_constraint
        and requested_source_text
        and result["source_match_status"] != "exact"
    )
    if hard_missing_source:
        unresolved_reason = (
            "ambiguous-source" if len(exact_source_candidates) >= 2 else "missing-source"
        )
        result["status"] = "unresolved"
        result["source_match_status"] = "unresolved"
        result["unit_match_status"] = (
            "unresolved" if parsed_locator.get("locator_type") is not None else "none"
        )
        result["continued_with_best_effort"] = False
        result["resolved_source_id"] = None
        result["resolved_unit_id"] = None
        result["target_source_ref"] = None
        result["notice_text"] = (
            "I could not isolate the requested published source exactly, so I am stopping at "
            "that boundary."
            if unresolved_reason == "ambiguous-source"
            else (
                "I could not find the requested published source, so I am stopping at a "
                "missing-source boundary."
            )
        )
        result["unresolved_reason"] = unresolved_reason
        result["hard_boundary"] = True
    elif result["status"] == "unresolved":
        result["unresolved_reason"] = (
            "unresolved-locator"
            if result["resolved_source_id"] is not None and parsed_locator.get("locator_type")
            else "ambiguous-source"
        )
    if result["status"] == "approximate":
        if result["resolved_source_id"] and result["resolved_unit_id"]:
            result["notice_text"] = (
                "I did not find an exact document-and-locator match. "
                "I am continuing with the closest published source and unit."
            )
        elif result["resolved_source_id"]:
            result["notice_text"] = (
                "I did not find an exact locator match for the referenced document. "
                "I am continuing with the closest published match in that document."
            )
        else:
            result["notice_text"] = (
                "I did not find a single exact source reference. "
                "I am continuing with the closest published match."
            )
    elif result["status"] == "unresolved":
        result["continued_with_best_effort"] = not result["hard_boundary"]
        if not result["hard_boundary"]:
            result["notice_text"] = (
                "I did not find a clear document or locator match. "
                "I am continuing with the closest published evidence."
            )
    if compare_scope_requested:
        _apply_compare_resolution(
            result,
            query=query,
            normalized_source_records=normalized_source_records,
            units_by_source=units_by_source,
            source_candidates=source_candidates,
            source_scope_intent=compare_intent,
        )
    result["scope_mode"] = (
        "compare"
        if compare_scope_requested
        else (
            "source-scoped-hard"
            if single_source_constraint
            else (
                "source-scoped-soft"
                if result["resolved_source_id"] and result["source_narrowing_allowed"]
                else "global"
            )
        )
    )
    result["match_basis"] = _deduplicate_strings(_string_list(result.get("match_basis")))
    return result


def resolve_workspace_reference(
    paths: WorkspacePaths,
    *,
    query: str,
    target: str = "current",
    source_scope_intent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a query against published retrieval artifacts when they exist."""
    source_records_path = paths.retrieval_source_records_path(target)
    unit_records_path = paths.retrieval_unit_records_path(target)
    if not source_records_path.exists() or not unit_records_path.exists():
        return {
            "detected": False,
            "parsed_document_ref": None,
            "parsed_locator_ref": None,
            "status": "none",
            "source_match_status": "none",
            "unit_match_status": "none",
            "match_basis": [],
            "resolved_source_id": None,
            "resolved_unit_id": None,
            "candidate_source_ids": [],
            "candidate_unit_ids": [],
            "continued_with_best_effort": False,
            "notice_text": None,
            "requested_source_text": None,
            "scope_mode": "global",
            "unresolved_reason": None,
            "hard_boundary": False,
            "target_source_ref": None,
            "analysis_guard_applied": False,
        }
    source_records = read_json(source_records_path).get("records", [])
    unit_records = read_json(unit_records_path).get("records", [])
    if not isinstance(source_records, list) or not isinstance(unit_records, list):
        return {
            "detected": False,
            "parsed_document_ref": None,
            "parsed_locator_ref": None,
            "status": "none",
            "source_match_status": "none",
            "unit_match_status": "none",
            "match_basis": [],
            "resolved_source_id": None,
            "resolved_unit_id": None,
            "candidate_source_ids": [],
            "candidate_unit_ids": [],
            "continued_with_best_effort": False,
            "notice_text": None,
        }
    return resolve_reference_query(
        query,
        source_records=source_records,
        unit_records=unit_records,
        source_scope_intent=source_scope_intent,
    )

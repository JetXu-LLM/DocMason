"""Conservative `.eml` parsing helpers for first-class email sources."""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Any, cast

from .project import source_type_definition
from .text_sources import ParsedUnit

CID_PATTERN = re.compile(r"cid:([^\"'> )]+)", re.IGNORECASE)
HTML_BREAK_PATTERN = re.compile(r"(?i)<br\s*/?>")
HTML_BLOCK_END_PATTERN = re.compile(r"(?i)</(?:p|div|li|tr|h[1-6]|table|section|article)>")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
SCRIPT_STYLE_PATTERN = re.compile(r"(?is)<(script|style)\b.*?>.*?</\1>")
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z]+|[\u4e00-\u9fff]+")


@dataclass(frozen=True)
class ParsedEmailAttachment:
    """One parsed attachment or inline asset preserved from an email message."""

    unit_id: str
    ordinal: int
    lineage_slot: str
    lineage_segments: tuple[str, ...]
    filename: str
    content_type: str
    disposition: str
    content_id: str | None
    inline: bool
    size: int
    payload_bytes: bytes
    source_extension: str
    document_type: str | None
    support_tier: str | None
    warnings: list[str]


@dataclass(frozen=True)
class ParsedEmailSource:
    """Parsed email output consumed by the source-building layer."""

    document_type: str
    source_title: str | None
    source_language: str
    units: list[ParsedUnit]
    document_media: list[dict[str, Any]]
    warnings: list[str]
    failures: list[str]
    email_metadata: dict[str, Any]
    html_body: str
    mime_structure: dict[str, Any]
    attachments: list[ParsedEmailAttachment]


def _detect_language(texts: list[str]) -> str:
    joined = " ".join(texts)
    if not joined.strip():
        return "unknown"
    ascii_ratio = sum(1 for character in joined if ord(character) < 128) / max(len(joined), 1)
    if ascii_ratio > 0.95:
        return "en"
    return "mixed-or-non-en"


def _truncate_title(text: str, *, limit: int = 120) -> str:
    compact = " ".join(text.split()).strip()
    if not compact:
        return "Untitled Email Section"
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _safe_filename(part: EmailMessage, ordinal: int) -> str:
    filename = part.get_filename()
    if isinstance(filename, str) and filename.strip():
        return Path(filename).name
    content_type = part.get_content_type().lower()
    if content_type == "message/rfc822":
        return f"attachment-{ordinal:03d}.eml"
    maintype, _separator, subtype = content_type.partition("/")
    extension = subtype.strip() or maintype.strip() or "bin"
    return f"attachment-{ordinal:03d}.{extension}"


def _normalize_content_id(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized.startswith("<") and normalized.endswith(">"):
        normalized = normalized[1:-1].strip()
    return normalized or None


def _decode_bytes(part: EmailMessage) -> bytes:
    if part.get_content_type().lower() == "message/rfc822":
        payload = part.get_payload()
        if isinstance(payload, list) and payload and isinstance(payload[0], EmailMessage):
            nested = payload[0]
            try:
                nested_content = nested.get_content()
            except Exception:
                nested_content = None
            if isinstance(nested_content, str):
                try:
                    decoded = base64.b64decode(nested_content)
                    if decoded:
                        return decoded
                except Exception:
                    pass
                return nested_content.encode("utf-8", errors="replace")
            nested_bytes = nested.get_payload(decode=True)
            if isinstance(nested_bytes, bytes):
                return nested_bytes
            try:
                return nested.as_bytes(policy=policy.default)
            except Exception:
                return b""
        if isinstance(payload, EmailMessage):
            try:
                return payload.as_bytes(policy=policy.default)
            except Exception:
                return b""
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    try:
        return part.as_bytes(policy=policy.default)
    except Exception:
        return b""


def _decode_text_payload(part: EmailMessage) -> tuple[str, list[str]]:
    warnings: list[str] = []
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content, warnings
    except Exception:
        pass
    payload = _decode_bytes(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset), warnings
    except (LookupError, UnicodeDecodeError):
        warnings.append(
            f"Decoded `{part.get_content_type()}` with replacement fallback; some characters may be degraded."
        )
        return payload.decode("utf-8", errors="replace"), warnings


def _html_to_text(value: str) -> str:
    collapsed = SCRIPT_STYLE_PATTERN.sub(" ", value)
    collapsed = HTML_BREAK_PATTERN.sub("\n", collapsed)
    collapsed = HTML_BLOCK_END_PATTERN.sub("\n", collapsed)
    collapsed = HTML_TAG_PATTERN.sub(" ", collapsed)
    collapsed = html.unescape(collapsed)
    collapsed = collapsed.replace("\r\n", "\n").replace("\r", "\n")
    collapsed = re.sub(r"[ \t]+", " ", collapsed)
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed)
    return "\n".join(line.strip() for line in collapsed.splitlines()).strip()


def _build_mime_structure(
    message: EmailMessage,
    *,
    part_path: str = "1",
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "part_path": part_path,
        "content_type": message.get_content_type(),
        "content_disposition": message.get_content_disposition(),
        "filename": message.get_filename(),
        "content_id": _normalize_content_id(message.get("Content-ID")),
        "is_multipart": message.is_multipart(),
    }
    if message.is_multipart():
        node["children"] = [
            _build_mime_structure(cast(EmailMessage, child), part_path=f"{part_path}.{index}")
            for index, child in enumerate(message.iter_parts(), start=1)
        ]
    return node


def _build_email_metadata(message: EmailMessage) -> dict[str, Any]:
    return {
        "subject": str(message.get("Subject") or "").strip(),
        "from": str(message.get("From") or "").strip(),
        "to": str(message.get("To") or "").strip(),
        "cc": str(message.get("Cc") or "").strip(),
        "bcc": str(message.get("Bcc") or "").strip(),
        "reply_to": str(message.get("Reply-To") or "").strip(),
        "date": str(message.get("Date") or "").strip(),
        "message_id": str(message.get("Message-ID") or "").strip(),
    }


def _attachment_definition(filename: str, content_type: str) -> tuple[str, str | None, str | None]:
    extension = Path(filename).suffix.lower().lstrip(".")
    if content_type.lower() == "message/rfc822" and not extension:
        extension = "eml"
    definition = source_type_definition(extension)
    if definition is None:
        return extension, None, None
    return extension, definition.document_type, definition.support_tier


def _parse_body_sections(body_text: str) -> list[ParsedUnit]:
    lines = body_text.splitlines()
    sections: list[ParsedUnit] = []
    block_lines: list[str] = []
    start_line: int | None = None

    def flush() -> None:
        nonlocal block_lines, start_line
        if start_line is None:
            return
        block_text = "\n".join(block_lines).strip()
        if not block_text:
            block_lines = []
            start_line = None
            return
        ordinal = len(sections) + 1
        headingish_aliases: list[str] = []
        first_line = next((line.strip() for line in block_lines if line.strip()), "")
        if first_line:
            token_count = len(TOKEN_PATTERN.findall(first_line))
            if 1 <= token_count <= 14 and len(first_line) <= 120:
                headingish_aliases.append(first_line)
        sections.append(
            ParsedUnit(
                unit_id=f"section-{ordinal:03d}",
                unit_type="email-section",
                ordinal=ordinal,
                title=_truncate_title(first_line or f"Email Section {ordinal}"),
                text=block_text,
                structure_data={
                    "line_start": start_line,
                    "line_end": start_line + len(block_lines) - 1,
                    "headingish_aliases": headingish_aliases,
                },
                embedded_media=[],
                extraction_confidence="high" if block_text else "low",
                warnings=[],
            )
        )
        block_lines = []
        start_line = None

    for line_number, line in enumerate(lines, start=1):
        if line.strip():
            if start_line is None:
                start_line = line_number
            block_lines.append(line)
            continue
        flush()
    flush()
    return sections


def parse_email_source(source_path: Path) -> ParsedEmailSource:
    """Parse one `.eml` file into conservative email evidence units and attachments."""
    warnings: list[str] = []
    failures: list[str] = []
    try:
        message = BytesParser(policy=policy.default).parse(source_path.open("rb"))
    except OSError as exc:
        failures.append(f"Could not read email source: {exc.strerror or str(exc)}")
        return ParsedEmailSource(
            document_type="email",
            source_title=source_path.stem,
            source_language="unknown",
            units=[],
            document_media=[],
            warnings=warnings,
            failures=failures,
            email_metadata={},
            html_body="",
            mime_structure={},
            attachments=[],
        )
    except Exception as exc:
        failures.append(f"Could not parse email source: {exc}")
        return ParsedEmailSource(
            document_type="email",
            source_title=source_path.stem,
            source_language="unknown",
            units=[],
            document_media=[],
            warnings=warnings,
            failures=failures,
            email_metadata={},
            html_body="",
            mime_structure={},
            attachments=[],
        )

    email_metadata = _build_email_metadata(message)
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[ParsedEmailAttachment] = []

    for ordinal, part in enumerate(message.walk(), start=1):
        content_type = part.get_content_type().lower()
        disposition = (part.get_content_disposition() or "").lower()
        if part.is_multipart() and content_type != "message/rfc822":
            continue
        filename = _safe_filename(part, len(attachments) + 1)
        inline = disposition == "inline"
        content_id = _normalize_content_id(part.get("Content-ID"))
        looks_like_attachment = bool(
            disposition == "attachment"
            or part.get_filename()
            or content_type == "message/rfc822"
            or (inline and not content_type.startswith("text/"))
        )
        if looks_like_attachment:
            payload_bytes = _decode_bytes(part)
            attachment_warnings: list[str] = []
            if not payload_bytes:
                attachment_warnings.append(
                    f"Attachment `{filename}` preserved no decoded payload bytes."
                )
            source_extension, document_type, support_tier = _attachment_definition(
                filename,
                content_type,
            )
            attachments.append(
                ParsedEmailAttachment(
                    unit_id=f"attachment-{len(attachments) + 1:03d}",
                    ordinal=len(attachments) + 1,
                    lineage_slot=f"{len(attachments) + 1:03d}",
                    lineage_segments=(f"{len(attachments) + 1:03d}-{Path(filename).name}",),
                    filename=filename,
                    content_type=content_type,
                    disposition=disposition or "attachment",
                    content_id=content_id,
                    inline=inline,
                    size=len(payload_bytes),
                    payload_bytes=payload_bytes,
                    source_extension=source_extension,
                    document_type=document_type,
                    support_tier=support_tier,
                    warnings=attachment_warnings,
                )
            )
            warnings.extend(attachment_warnings)
            continue
        if content_type == "text/plain":
            text, text_warnings = _decode_text_payload(part)
            plain_parts.append(text)
            warnings.extend(text_warnings)
        elif content_type == "text/html":
            text, text_warnings = _decode_text_payload(part)
            html_parts.append(text)
            warnings.extend(text_warnings)

    html_body = "\n\n".join(part for part in html_parts if part.strip()).strip()
    plain_body = "\n\n".join(part for part in plain_parts if part.strip()).strip()
    if not plain_body and html_body:
        plain_body = _html_to_text(html_body)
        warnings.append("Email body fell back to HTML-to-text normalization.")
    cid_references = sorted(
        {
            cid.strip()
            for cid in CID_PATTERN.findall(html_body)
            if isinstance(cid, str) and cid.strip()
        }
    )
    mime_structure = _build_mime_structure(message)
    if cid_references:
        mime_structure["cid_references"] = cid_references

    units: list[ParsedUnit] = []
    header_lines = [
        f"Subject: {email_metadata['subject']}".strip(),
        f"From: {email_metadata['from']}".strip(),
        f"To: {email_metadata['to']}".strip(),
        f"Cc: {email_metadata['cc']}".strip(),
        f"Reply-To: {email_metadata['reply_to']}".strip(),
        f"Date: {email_metadata['date']}".strip(),
        f"Message-ID: {email_metadata['message_id']}".strip(),
    ]
    header_text = "\n".join(line for line in header_lines if line and not line.endswith(":")).strip()
    units.append(
        ParsedUnit(
            unit_id="header-001",
            unit_type="email-header",
            ordinal=1,
            title="Email Headers",
            text=header_text,
            structure_data={"email_metadata": email_metadata},
            embedded_media=[],
            extraction_confidence="high" if header_text else "medium",
            warnings=[],
        )
    )
    units.extend(_parse_body_sections(plain_body))
    for attachment in attachments:
        summary_bits = [attachment.filename, attachment.content_type]
        if attachment.inline:
            summary_bits.append("inline")
        if attachment.size:
            summary_bits.append(f"{attachment.size} bytes")
        units.append(
            ParsedUnit(
                unit_id=attachment.unit_id,
                unit_type="email-attachment",
                ordinal=attachment.ordinal,
                title=attachment.filename,
                text=" | ".join(summary_bits),
                structure_data={
                    "attachment_filename": attachment.filename,
                    "mime_type": attachment.content_type,
                    "disposition": attachment.disposition,
                    "size": attachment.size,
                    "inline": attachment.inline,
                    "content_id": attachment.content_id,
                    "lineage_slot": attachment.lineage_slot,
                    "lineage_segments": list(attachment.lineage_segments),
                    "headingish_aliases": [attachment.filename],
                },
                embedded_media=[],
                extraction_confidence="high",
                warnings=list(attachment.warnings),
            )
        )

    source_title = email_metadata["subject"] or source_path.stem
    source_language = _detect_language(
        [
            source_title,
            plain_body,
            email_metadata["from"],
            email_metadata["to"],
        ]
    )
    return ParsedEmailSource(
        document_type="email",
        source_title=source_title,
        source_language=source_language,
        units=units,
        document_media=[],
        warnings=list(dict.fromkeys(warnings)),
        failures=failures,
        email_metadata=email_metadata,
        html_body=html_body,
        mime_structure=mime_structure,
        attachments=attachments,
    )

"""Input validation, CSV parsing, template personalization, and SMS segment accounting.

Deliberately independent of Tkinter and ADB so validation rules can be tested
and reused across CLI, worker, storage, and GUI layers.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import math
import re
from dataclasses import dataclass, field

MAX_SMS_CHARS = 160
MAX_MULTIPART_CHARS = 1000
MAX_RECIPIENTS = 10_000
DEFAULT_MAX_CSV_BYTES = 20 * 1024 * 1024

PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()-]{5,24}$")
IP_PORT_RE = re.compile(r"^(.+):([1-9][0-9]{0,4})$")
PAIRING_CODE_RE = re.compile(r"^[0-9]{6}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}|\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

CONSENT_TRUE = frozenset({"yes", "true", "1", "opted-in", "opted in"})

# GSM 03.38 default alphabet and extension table. Extension characters consume
# two septets; messages outside these tables use UCS-2 and have a 70-unit limit
# (or 67 units per segment in concatenated multipart mode).
GSM_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ "
    "!\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿"
    "abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM_EXTENSION = frozenset("^{}\\[~]|€\f")


class ValidationError(ValueError):
    """An actionable user-input validation error."""


@dataclass(frozen=True)
class Recipient:
    phone: str
    source_row: int
    name: str = ""
    consent_source: str = ""
    consented_at: str = ""
    variables: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RejectedRow:
    row_number: int
    raw_phone: str
    reason_code: str
    message: str


@dataclass(frozen=True)
class CsvResult:
    recipients: tuple[Recipient, ...]
    rejected: tuple[str, ...]
    rejected_rows: tuple[RejectedRow, ...] = ()
    headers: tuple[str, ...] = ()
    total_rows: int = 0
    duplicate_count: int = 0
    missing_consent_count: int = 0
    invalid_phone_count: int = 0


@dataclass(frozen=True)
class SegmentMetrics:
    encoding: str
    units: int
    single_capacity: int
    per_segment_capacity: int
    segments: int
    chars: int
    extension_chars: int
    exceeds_single: bool


def normalize_phone(value: str, *, default_country_code: str = "") -> str:
    """Remove presentation separators only; preserve an optional leading +.

    Never fabricates country codes or strips leading zeros unless an explicit
    default_country_code prefix is configured and the number does not start with +.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValidationError("Phone number cannot be empty.")
    if not PHONE_RE.fullmatch(raw):
        raise ValidationError(
            "Phone must be a valid number; only digits, spaces, (), - and an optional leading + are accepted."
        )
    normalized = re.sub(r"[ ()-]", "", raw)
    if default_country_code and not normalized.startswith("+"):
        cc = default_country_code.strip()
        if not re.fullmatch(r"^\+[1-9][0-9]{0,3}$", cc):
            raise ValidationError("Default country code must start with '+' followed by 1–4 digits (e.g. +1 or +91).")
        normalized = f"{cc}{normalized}"
    digits = normalized[1:] if normalized.startswith("+") else normalized
    if not 7 <= len(digits) <= 15:
        raise ValidationError("Phone number must contain 7–15 digits.")
    return normalized


def analyze_sms_segments(message: str) -> SegmentMetrics:
    """Compute detailed GSM 03.38 / UCS-2 encoding and segment metrics."""
    chars = len(message)
    if all(char in GSM_BASIC or char in GSM_EXTENSION for char in message):
        ext_count = sum(1 for char in message if char in GSM_EXTENSION)
        units = chars + ext_count
        single_cap = 160
        multi_cap = 153
        if units == 0:
            segments = 0
        elif units <= single_cap:
            segments = 1
        else:
            segments = math.ceil(units / multi_cap)
        return SegmentMetrics(
            encoding="GSM",
            units=units,
            single_capacity=single_cap,
            per_segment_capacity=single_cap if segments <= 1 else multi_cap,
            segments=segments,
            chars=chars,
            extension_chars=ext_count,
            exceeds_single=(units > single_cap or chars > MAX_SMS_CHARS),
        )
    try:
        units = len(message.encode("utf-16-be")) // 2
    except UnicodeEncodeError:
        units = 0
    single_cap = 70
    multi_cap = 67
    if units == 0:
        segments = 0
    elif units <= single_cap:
        segments = 1
    else:
        segments = math.ceil(units / multi_cap)
    return SegmentMetrics(
        encoding="UCS-2",
        units=units,
        single_capacity=single_cap,
        per_segment_capacity=single_cap if segments <= 1 else multi_cap,
        segments=segments,
        chars=chars,
        extension_chars=0,
        exceeds_single=(units > single_cap or chars > MAX_SMS_CHARS),
    )


def validate_message(message: str, *, allow_multipart: bool = False) -> str:
    """Validate SMS text for null bytes, surrogates, blank content, and segment limits."""
    if not message or not message.strip():
        raise ValidationError("Message cannot be blank.")
    if "\x00" in message:
        raise ValidationError("Message contains an unsupported null character.")
    try:
        message.encode("utf-16-be")
    except UnicodeEncodeError as exc:
        raise ValidationError("Message contains an invalid Unicode surrogate.") from exc

    if not allow_multipart and len(message) > MAX_SMS_CHARS:
        raise ValidationError(f"Message is {len(message)} characters; a single SMS is limited to {MAX_SMS_CHARS}.")
    if allow_multipart and len(message) > MAX_MULTIPART_CHARS:
        raise ValidationError(f"Message is {len(message)} characters; maximum allowed length is {MAX_MULTIPART_CHARS}.")

    if all(char in GSM_BASIC or char in GSM_EXTENSION for char in message):
        septets = sum(2 if char in GSM_EXTENSION else 1 for char in message)
        if not allow_multipart and septets > 160:
            raise ValidationError(f"Message uses {septets} GSM septets; one SMS is limited to 160.")
    else:
        utf16_units = len(message.encode("utf-16-be")) // 2
        if not allow_multipart and utf16_units > 70:
            raise ValidationError(
                f"Message requires UCS-2 and uses {utf16_units} UTF-16 units; one SMS is limited to 70."
            )
    return message


def sms_encoding_usage(message: str) -> tuple[str, int, int]:
    """Return encoding, used units and one-segment capacity for live feedback."""
    metrics = analyze_sms_segments(message)
    return metrics.encoding, metrics.units, metrics.single_capacity


def safe_truncate_message(message: str) -> str:
    """Truncate visibly to one SMS segment without splitting an encoding unit."""
    if all(char in GSM_BASIC or char in GSM_EXTENSION for char in message):
        limit, usage = 160, 0
        accepted = []
        for char in message:
            units = 2 if char in GSM_EXTENSION else 1
            if usage + units > limit or len(accepted) >= MAX_SMS_CHARS:
                break
            accepted.append(char)
            usage += units
        return "".join(accepted)
    accepted = []
    usage = 0
    for char in message:
        try:
            units = len(char.encode("utf-16-be")) // 2
        except UnicodeEncodeError:
            return "".join(accepted)
        if usage + units > 70 or len(accepted) >= MAX_SMS_CHARS:
            break
        accepted.append(char)
        usage += units
    return "".join(accepted)


def extract_template_variables(template: str) -> list[str]:
    """Return unique variable names referenced as {var} or {{var}} in order of appearance."""
    seen: list[str] = []
    for match in PLACEHOLDER_RE.finditer(template or ""):
        name = (match.group(1) or match.group(2) or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def render_template(template: str, variables: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Render a template with recipient variables and return (rendered_text, missing_variables)."""
    vars_map = {str(k).strip().lower(): str(v) for k, v in (variables or {}).items() if v is not None and str(v).strip() != ""}
    missing: list[str] = []

    def _replace(match: re.Match) -> str:
        raw_name = (match.group(1) or match.group(2) or "").strip()
        key = raw_name.lower()
        if key in vars_map:
            return vars_map[key]
        if raw_name not in missing:
            missing.append(raw_name)
        return match.group(0)

    rendered = PLACEHOLDER_RE.sub(_replace, template)
    return rendered, missing


def validate_personalized_batch(
    template: str,
    recipients_vars: list[tuple[str, dict[str, str]]],
    *,
    allow_multipart: bool = False,
) -> dict:
    """Validate rendered messages for every recipient individually."""
    if not template or not template.strip():
        raise ValidationError("Message template cannot be blank.")
    valid_messages: dict[str, str] = {}
    missing_vars_by_phone: dict[str, list[str]] = {}
    overflow_by_phone: dict[str, str] = {}
    previews: list[tuple[str, str, SegmentMetrics]] = []

    for phone, vars_dict in recipients_vars:
        merged = dict(vars_dict or {})
        merged.setdefault("phone", phone)
        rendered, missing = render_template(template, merged)
        metrics = analyze_sms_segments(rendered)
        if len(previews) < 10:
            previews.append((phone, rendered, metrics))
        if missing:
            missing_vars_by_phone[phone] = missing
        try:
            validate_message(rendered, allow_multipart=allow_multipart)
            if not missing:
                valid_messages[phone] = rendered
        except ValidationError as exc:
            overflow_by_phone[phone] = str(exc)

    return {
        "valid_messages": valid_messages,
        "missing_vars_by_phone": missing_vars_by_phone,
        "overflow_by_phone": overflow_by_phone,
        "previews": previews,
    }


def bounded_number(value: str | float, *, label: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be a number.") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValidationError(f"{label} must be between {minimum:g} and {maximum:g}.")
    return result


def validate_ip_port(value: str) -> str:
    candidate = (value or "").strip()
    match = IP_PORT_RE.fullmatch(candidate)
    if not match:
        raise ValidationError(
            "Enter a Wi-Fi ADB endpoint as IP:PORT (IPv6 must be bracketed), for example 192.168.1.20:5555."
        )
    host, raw_port = match.groups()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        if ":" not in host:
            raise ValidationError("Bracketed endpoints must contain an IPv6 address.")
    elif ":" in host:
        raise ValidationError("IPv6 addresses must be bracketed, for example [2001:db8::1]:5555.")
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValidationError("Wi-Fi ADB endpoint must use an IPv4 or bracketed IPv6 address.") from exc
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise ValidationError("Port must be between 1 and 65535.")
    return candidate


def validate_pairing_code(value: str) -> str:
    code = (value or "").strip()
    if not PAIRING_CODE_RE.fullmatch(code):
        raise ValidationError("Wireless debugging pairing code must be a 6-digit number.")
    return code


def validate_port(value: str | int, *, label: str = "Port") -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be an integer between 1 and 65535.") from exc
    if not 1 <= port <= 65535:
        raise ValidationError(f"{label} must be between 1 and 65535.")
    return port


def _strip_bom(text: str) -> str:
    return text.lstrip("\ufeff") if text else ""


def preview_csv_headers(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Return CSV headers and up to 5 sample rows for column mapping preview."""
    try:
        clean_text = _strip_bom(text)
        reader = csv.DictReader(io.StringIO(clean_text, newline=""))
        headers = [str(h).strip() for h in (reader.fieldnames or []) if h and str(h).strip()]
        sample_rows: list[dict[str, str]] = []
        for idx, row in enumerate(reader):
            if idx >= 5:
                break
            sample_rows.append({str(k).strip(): (v or "").strip() for k, v in row.items() if k})
        return headers, sample_rows
    except csv.Error as exc:
        raise ValidationError(f"Could not parse CSV headers: {exc}") from exc


def parse_contacts_csv(
    text: str,
    *,
    limit: int = MAX_RECIPIENTS,
    column_map: dict[str, str] | None = None,
    default_country_code: str = "",
) -> CsvResult:
    """Require a per-row phone and explicit consent/opt_in column.

    Non-consenting/malformed rows are rejected with row-specific messages and
    are never returned as recipients. A malformed file/header is a hard error.
    Supports UTF-8 BOM, optional name, consent_source, consented_at, and custom personalization columns.
    """
    try:
        clean_text = _strip_bom(text)
        reader = csv.DictReader(io.StringIO(clean_text, newline=""))
        raw_fieldnames = [str(h).strip() for h in (reader.fieldnames or []) if h and str(h).strip()]
        headers = {h.lower(): h for h in raw_fieldnames}
        cmap = {k.lower(): headers.get(v.strip().lower(), v.strip()) for k, v in (column_map or {}).items() if v}

        phone_header = cmap.get("phone") or headers.get("phone")
        consent_header = cmap.get("consent") or headers.get("consent") or headers.get("opt_in")
        name_header = cmap.get("name") or headers.get("name") or headers.get("full_name")
        source_header = cmap.get("consent_source") or headers.get("consent_source") or headers.get("source")
        timestamp_header = cmap.get("consented_at") or headers.get("consented_at") or headers.get("consent_timestamp")

        if not phone_header or phone_header not in raw_fieldnames:
            raise ValidationError("CSV must contain a 'phone' column.")
        if not consent_header or consent_header not in raw_fieldnames:
            raise ValidationError(
                "CSV must contain a per-recipient 'consent' or 'opt_in' column. Accepted opt-in values: yes, true, 1, opted-in."
            )

        recipients: list[Recipient] = []
        rejected: list[str] = []
        rejected_rows: list[RejectedRow] = []
        seen: set[str] = set()
        total_rows = 0
        duplicate_count = 0
        missing_consent_count = 0
        invalid_phone_count = 0

        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValidationError(f"Row {row_number}: malformed CSV row has extra unmapped columns.")
            total_rows += 1
            norm_row = {str(k).strip(): (v or "").strip() for k, v in row.items() if k is not None}
            raw_phone = norm_row.get(phone_header, "")
            if len(recipients) >= limit:
                msg = f"Row {row_number}: recipient limit ({limit}) reached; remaining rows were not imported."
                rejected.append(msg)
                rejected_rows.append(RejectedRow(row_number, raw_phone, "limit_reached", msg))
                break
            consent = norm_row.get(consent_header, "").lower()
            if consent not in CONSENT_TRUE:
                missing_consent_count += 1
                msg = f"Row {row_number}: missing/negative consent; excluded."
                rejected.append(msg)
                rejected_rows.append(RejectedRow(row_number, raw_phone, "missing_consent", msg))
                continue
            try:
                phone = normalize_phone(raw_phone, default_country_code=default_country_code)
            except ValidationError as exc:
                invalid_phone_count += 1
                msg = f"Row {row_number}: {exc}"
                rejected.append(msg)
                rejected_rows.append(RejectedRow(row_number, raw_phone, "invalid_phone", msg))
                continue
            if phone in seen:
                duplicate_count += 1
                msg = f"Row {row_number}: duplicate phone in this file; excluded."
                rejected.append(msg)
                rejected_rows.append(RejectedRow(row_number, raw_phone, "duplicate_in_file", msg))
                continue
            seen.add(phone)
            name_val = norm_row.get(name_header, "") if name_header else ""
            src_val = norm_row.get(source_header, "") if source_header else ""
            ts_val = norm_row.get(timestamp_header, "") if timestamp_header else ""
            variables: dict[str, str] = {}
            for col in raw_fieldnames:
                variables[col.lower()] = norm_row.get(col, "")
            if name_val:
                variables.setdefault("name", name_val)
            variables["phone"] = phone
            recipients.append(
                Recipient(
                    phone=phone,
                    source_row=row_number,
                    name=name_val,
                    consent_source=src_val,
                    consented_at=ts_val,
                    variables=variables,
                )
            )
    except csv.Error as exc:
        raise ValidationError(f"Could not parse CSV: {exc}") from exc
    if not recipients and not rejected:
        raise ValidationError("CSV contains no recipient rows.")
    return CsvResult(
        recipients=tuple(recipients),
        rejected=tuple(rejected),
        rejected_rows=tuple(rejected_rows),
        headers=tuple(raw_fieldnames),
        total_rows=total_rows,
        duplicate_count=duplicate_count,
        missing_consent_count=missing_consent_count,
        invalid_phone_count=invalid_phone_count,
    )


def format_rejected_rows_csv(rejected_rows: tuple[RejectedRow, ...] | list[RejectedRow]) -> str:
    """Serialize rejected import rows with reasons to a CSV string."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["row_number", "raw_phone", "reason_code", "detail"])
    for item in rejected_rows:
        writer.writerow([item.row_number, item.raw_phone, item.reason_code, item.message])
    return buf.getvalue()

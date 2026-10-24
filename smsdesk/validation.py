"""Input validation and CSV parsing; deliberately independent of Tk and ADB."""

from __future__ import annotations

import csv
import ipaddress
import io
import math
import re
from dataclasses import dataclass

MAX_SMS_CHARS = 160
MAX_RECIPIENTS = 10_000
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()-]{5,24}$")
IP_PORT_RE = re.compile(r"^(.+):([1-9][0-9]{0,4})$")
CONSENT_TRUE = frozenset({"yes", "true", "1", "opted-in", "opted in"})
# GSM 03.38 default alphabet and extension table. Extension characters consume
# two septets; messages outside these tables use UCS-2 and have a 70-unit limit.
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


@dataclass(frozen=True)
class CsvResult:
    recipients: tuple[Recipient, ...]
    rejected: tuple[str, ...]


def normalize_phone(value: str) -> str:
    """Remove presentation separators only; preserve an optional leading +."""
    raw = (value or "").strip()
    if not PHONE_RE.fullmatch(raw):
        raise ValidationError("Phone must be a valid number; only digits, spaces, (), - and an optional leading + are accepted.")
    normalized = re.sub(r"[ ()-]", "", raw)
    digits = normalized[1:] if normalized.startswith("+") else normalized
    if not 7 <= len(digits) <= 15:
        raise ValidationError("Phone number must contain 7–15 digits.")
    return normalized


def validate_message(message: str) -> str:
    if len(message) > MAX_SMS_CHARS:
        raise ValidationError(f"Message is {len(message)} characters; a single SMS is limited to {MAX_SMS_CHARS}.")
    if not message.strip():
        raise ValidationError("Message cannot be blank.")
    if "\x00" in message:
        raise ValidationError("Message contains an unsupported null character.")
    if all(char in GSM_BASIC or char in GSM_EXTENSION for char in message):
        septets = sum(2 if char in GSM_EXTENSION else 1 for char in message)
        if septets > 160:
            raise ValidationError(f"Message uses {septets} GSM septets; one SMS is limited to 160.")
    else:
        try:
            utf16_units = len(message.encode("utf-16-be")) // 2
        except UnicodeEncodeError as exc:
            raise ValidationError("Message contains an invalid Unicode surrogate.") from exc
        if utf16_units > 70:
            raise ValidationError(f"Message requires UCS-2 and uses {utf16_units} UTF-16 units; one SMS is limited to 70.")
    return message


def sms_encoding_usage(message: str) -> tuple[str, int, int]:
    """Return encoding, used units and one-segment capacity for live feedback."""
    if all(char in GSM_BASIC or char in GSM_EXTENSION for char in message):
        return "GSM", sum(2 if char in GSM_EXTENSION else 1 for char in message), 160
    try:
        units = len(message.encode("utf-16-be")) // 2
    except UnicodeEncodeError:
        units = 0
    return "UCS-2", units, 70


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


def bounded_number(value: str | float, *, label: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be a number.") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValidationError(f"{label} must be between {minimum:g} and {maximum:g}.")
    return result


def validate_ip_port(value: str) -> str:
    candidate = value.strip()
    match = IP_PORT_RE.fullmatch(candidate)
    if not match:
        raise ValidationError("Enter a Wi-Fi ADB endpoint as IP:PORT (IPv6 must be bracketed), for example 192.168.1.20:5555.")
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
    if port > 65535:
        raise ValidationError("Port must be between 1 and 65535.")
    return candidate


def parse_contacts_csv(text: str, *, limit: int = MAX_RECIPIENTS) -> CsvResult:
    """Require a per-row phone and explicit consent/opt_in column.

    Non-consenting/malformed rows are rejected with row-specific messages and
    are never returned as recipients. A malformed file/header is a hard error.
    """
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = {str(h).strip().lower(): h for h in (reader.fieldnames or []) if h}
        phone_header = headers.get("phone")
        consent_header = headers.get("consent") or headers.get("opt_in")
        if not phone_header:
            raise ValidationError("CSV must contain a 'phone' column.")
        if not consent_header:
            raise ValidationError("CSV must contain a per-recipient 'consent' or 'opt_in' column. Accepted opt-in values: yes, true, 1, opted-in.")
        recipients: list[Recipient] = []
        rejected: list[str] = []
        seen: set[str] = set()
        for row_number, row in enumerate(reader, start=2):
            if len(recipients) >= limit:
                rejected.append(f"Row {row_number}: recipient limit ({limit}) reached; remaining rows were not imported.")
                break
            raw_phone = (row.get(phone_header) or "").strip()
            consent = (row.get(consent_header) or "").strip().lower()
            if consent not in CONSENT_TRUE:
                rejected.append(f"Row {row_number}: missing/negative consent; excluded.")
                continue
            try:
                phone = normalize_phone(raw_phone)
            except ValidationError as exc:
                rejected.append(f"Row {row_number}: {exc}")
                continue
            if phone in seen:
                rejected.append(f"Row {row_number}: duplicate phone in this file; excluded.")
                continue
            seen.add(phone)
            recipients.append(Recipient(phone, row_number))
    except csv.Error as exc:
        raise ValidationError(f"Could not parse CSV: {exc}") from exc
    if not recipients and not rejected:
        raise ValidationError("CSV contains no recipient rows.")
    return CsvResult(tuple(recipients), tuple(rejected))

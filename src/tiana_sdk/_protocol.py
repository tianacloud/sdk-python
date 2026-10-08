"""Frozen v1 Endpoint, credential, and response rules."""

import re

from .errors import ConnectError, GatewayError

ENDPOINT_SUFFIX = ".db.service.internal.tiana.com"
ERROR_CODES = {
    "MALFORMED_CONNECT", "EARLY_TUNNEL_DATA", "ENDPOINT_MISMATCH",
    "AUTH_REQUIRED", "ACCESS_DENIED", "AUTHORIZATION_EXPIRED", "CALLER_DEADLINE",
    "CONNECTION_LIMIT", "POLICY_UNAVAILABLE", "INSTANCE_UNAVAILABLE",
    "ACTIVATION_TIMEOUT",
}


def validate_protocol(value: str) -> None:
    """Validate a generic routing identifier; the Gateway owns profile support."""
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is None:
        raise ConnectError("INVALID_PROTOCOL", "configuration")


def endpoint_host(value: str) -> str:
    if not isinstance(value, str) or not value.isascii():
        raise ConnectError("INVALID_ENDPOINT", "configuration")
    value = value.lower()
    endpoint_id = value.removesuffix(ENDPOINT_SUFFIX)
    if re.fullmatch(r"ep-[0-7][0-9a-hjkmnp-tv-z]{25}", endpoint_id) is None:
        raise ConnectError("INVALID_ENDPOINT", "configuration")
    return endpoint_id + ENDPOINT_SUFFIX


class SecretToken:
    """Opaque bounded credential with redacted display and owned mutable storage."""

    def __init__(self, value: str):
        if (not isinstance(value, str) or not 1 <= len(value) <= 4096
                or re.fullmatch(r"[\x21-\x7e]+", value) is None):
            raise ConnectError("INVALID_TOKEN", "configuration")
        self._value = bytearray(value, "ascii")

    def __repr__(self) -> str:
        return "SecretToken([REDACTED])"

    def clear(self) -> None:
        self._value[:] = b"\0" * len(self._value)

    def _header(self) -> bytes:
        return b"Bearer " + self._value


def response_metadata(headers: list[tuple[bytes, bytes]], request_id: str) -> str:
    observed_200 = (b":status", b"200") in headers
    if len(headers) > 64 or len(dict(headers)) != len(headers):
        raise ConnectError("INVALID_RESPONSE", "response", outcome_unknown=observed_200)
    fields = dict(headers)
    status = fields.get(b":status", b"")
    if not re.fullmatch(rb"[1-5][0-9]{2}", status):
        raise ConnectError("INVALID_RESPONSE", "response", outcome_unknown=observed_200)
    if status == b"200":
        if (
            set(fields) != {b":status", b"tiana-tunnel-version", b"tiana-auth-mode", b"tiana-request-id"}
            or fields[b"tiana-tunnel-version"] != b"1"
            or fields[b"tiana-request-id"] != request_id.encode("ascii")
            or fields[b"tiana-auth-mode"] not in (b"TOKEN_REQUIRED", b"DISABLED")
        ):
            raise ConnectError("INVALID_RESPONSE", "response", outcome_unknown=True)
        return fields[b"tiana-auth-mode"].decode("ascii")
    code = fields.get(b"tiana-error-code", b"").decode("ascii", errors="replace")
    if code not in ERROR_CODES:
        code = "GATEWAY_REJECTED"
    hint = fields.get(b"tiana-retry-after-ms", b"")
    retry_after = None
    if re.fullmatch(rb"[0-9]{1,5}", hint) and 1 <= int(hint) <= 60_000:
        retry_after = int(hint)
    raise GatewayError(int(status), code, retry_after)

"""Bounded, payload-free failure metadata."""


class ConnectError(Exception):
    """A connection failure; an accepted session may have executed application work."""

    def __init__(self, code: str, phase: str, *, outcome_unknown: bool = False):
        self.code = code
        self.phase = phase
        self.outcome_unknown = outcome_unknown
        self.retryable = False
        message = f"Tiana {phase} failed ({code})"
        if outcome_unknown:
            message += "; application outcome unknown"
        super().__init__(message)


class GatewayError(ConnectError):
    """A Gateway rejection before acceptance. Retry hints never trigger replay."""

    def __init__(self, status: int, code: str, retry_after_ms: int | None = None):
        super().__init__(code, "gateway")
        self.status = status
        self.retry_after_ms = retry_after_ms
        self.retryable = retry_after_ms is not None and (status, code) in {
            (429, "CONNECTION_LIMIT"),
            (503, "POLICY_UNAVAILABLE"),
            (503, "INSTANCE_UNAVAILABLE"),
            (504, "ACTIVATION_TIMEOUT"),
        }
        self.args = (f"Gateway rejected CONNECT ({status}, {code})",)


class TunnelError(ConnectError):
    """An accepted tunnel failed. No reconnection or replay has occurred."""

    def __init__(self, code: str):
        super().__init__(code, "tunnel", outcome_unknown=True)

"""Shared lifecycle errors for retry-suppressing managed starts."""

from __future__ import annotations


class SessionLifecycleError(ValueError):
    """Raised when lifecycle attestation or state transition fails closed."""


class ManagedStartResponseLost(SessionLifecycleError):
    """The native start may have succeeded, but completion is ambiguous."""

    def __init__(self, message: str, *, native_session_id: str | None = None) -> None:
        super().__init__(message)
        self.native_session_id = native_session_id


class ManagedStartOrphaned(SessionLifecycleError):
    """A native start returned an identity but could not be safely bound."""

    def __init__(self, message: str, *, native_session_id: str) -> None:
        if (
            not isinstance(native_session_id, str)
            or not native_session_id
            or len(native_session_id.encode("utf-8")) > 256
            or "\x00" in native_session_id
            or any(0xD800 <= ord(char) <= 0xDFFF for char in native_session_id)
            or native_session_id.casefold()
            in {"*", "current", "frontmost", "latest", "newest"}
        ):
            raise SessionLifecycleError("orphan native_session_id must be exact text")
        super().__init__(message)
        self.native_session_id = native_session_id

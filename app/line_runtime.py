"""Process-local ownership of the explicitly enabled LINE reply worker."""

from __future__ import annotations

from threading import Lock

import httpx

from app.line_replies import LineReplyBuffer, LiveLineReplySender, process_line_replies


class LineRuntime:
    """Share one bounded reply buffer between the webhook and live worker.

    Construction performs no dispatch. Fake and disabled configurations never
    create a live adapter, and a closed runtime cannot be enabled again.
    """

    def __init__(self, settings, factory, *, sender: LiveLineReplySender | None = None,
                 transport: httpx.BaseTransport | None = None):
        if sender is not None and not isinstance(sender, LiveLineReplySender):
            raise ValueError("LINE runtime requires a live reply sender")
        if sender is not None and transport is not None:
            raise ValueError("Configure either a LINE sender or a transport")
        from app.line_handoff import CaseHandoffRegistry

        self.settings = settings
        self.factory = factory
        self.buffer = LineReplyBuffer()
        self.case_handoffs = CaseHandoffRegistry()
        self.sender: LiveLineReplySender | None = None
        self._closed = False
        self._lifecycle_lock = Lock()
        if self._configured_live():
            self.sender = sender if sender is not None else LiveLineReplySender(
                getattr(settings, "line_channel_access_token", ""), transport=transport,
            )

    def _configured_live(self) -> bool:
        return (getattr(self.settings, "line_bot_enabled", False) is True
                and getattr(self.settings, "line_reply_mode", "disabled") == "live")

    @property
    def enabled(self) -> bool:
        return (not self._closed and self._configured_live()
                and isinstance(self.sender, LiveLineReplySender))

    def tick(self) -> dict[str, int]:
        """Run the dedicated reply worker once with an explicit live adapter."""
        with self._lifecycle_lock:
            if not self.enabled:
                return {key: 0 for key in (
                    "processed", "fake_sent", "api_accepted", "expired", "unknown", "failed",
                )}
            return process_line_replies(
                self.factory, self.settings, buffer=self.buffer, sender=self.sender, limit=1,
            )

    def close(self):
        """Finish any current tick, then wipe transient replies and handoffs."""
        with self._lifecycle_lock:
            self._closed = True
            self.sender = None
            try:
                self.buffer.clear()
            finally:
                self.case_handoffs.clear()

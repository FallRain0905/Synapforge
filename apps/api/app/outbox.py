"""Provider-neutral transactional outbox dispatcher.

The dispatcher owns claiming and delivery state, while a publisher owns the
transport (NATS, an in-process test publisher, or another event bus). Events
remain the source of truth; publishing is at-least-once and consumers must be
idempotent by event id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .contracts import Event
from .repository import PlatformRepository


class EventPublisher(Protocol):
    def publish(self, event: Event) -> None: ...


@dataclass(frozen=True)
class DispatchStats:
    claimed: int = 0
    delivered: int = 0
    failed: int = 0


class EventOutboxDispatcher:
    def __init__(self, repository: PlatformRepository, publisher: EventPublisher, *, lock_seconds: int = 60, retry_base_seconds: int = 5, retry_max_seconds: int = 300) -> None:
        if lock_seconds < 1:
            raise ValueError("event_outbox_lock_seconds_invalid")
        if retry_base_seconds < 1 or retry_max_seconds < retry_base_seconds:
            raise ValueError("event_outbox_retry_policy_invalid")
        self.repository = repository
        self.publisher = publisher
        self.lock_seconds = lock_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds

    def dispatch_once(self, limit: int = 100) -> DispatchStats:
        claimed = self.repository.claim_event_outbox(limit=limit, lock_seconds=self.lock_seconds)
        delivered = 0
        failed = 0
        for outbox in claimed:
            try:
                event = self.repository.get_event(outbox.event_id)
                self.publisher.publish(event)
            except Exception as error:
                delay = min(self.retry_max_seconds, self.retry_base_seconds * (2 ** min(outbox.attempts, 6)))
                self.repository.mark_event_outbox_failed(
                    outbox.id,
                    str(error) or error.__class__.__name__,
                    retry_at=datetime.now(UTC) + timedelta(seconds=delay),
                )
                failed += 1
            else:
                self.repository.mark_event_outbox_delivered(outbox.id)
                delivered += 1
        return DispatchStats(claimed=len(claimed), delivered=delivered, failed=failed)

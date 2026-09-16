from __future__ import annotations

from app.domain import EventRecord
from app.events import EventBroker


async def test_slow_subscriber_does_not_break_event_publisher() -> None:
    broker = EventBroker()
    async with broker.channel("session") as queue:
        for event_id in range(250):
            await broker.publish(
                EventRecord(
                    id=event_id,
                    session_id="session",
                    run_id="run",
                    kind="progress",
                    payload={"index": event_id},
                    created_at="2026-09-16T00:00:00+00:00",
                )
            )
        assert queue.qsize() == 200
        first_retained = queue.get_nowait()
        assert first_retained.id == 50

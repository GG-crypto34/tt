import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


def next_broadcast(now: datetime, timezone: str = "Europe/Moscow") -> datetime:
    local = now.astimezone(ZoneInfo(timezone))
    anchor = local.replace(hour=(local.hour // 3) * 3, minute=0, second=0, microsecond=0)
    return anchor + timedelta(hours=3)


def due_slot(now: datetime, timezone: str = "Europe/Moscow") -> str | None:
    local = now.astimezone(ZoneInfo(timezone))
    # One minute grace; outages do not trigger catch-up broadcasts.
    if local.hour % 3 == 0 and local.minute == 0:
        return local.replace(second=0, microsecond=0).isoformat()
    return None


class Scheduler:
    def __init__(self, service):
        self.service = service
        self.tasks: dict[str, asyncio.Task] = {}
        self.next_cleanup = 0.0

    def launch(self, name: str, coroutine) -> None:
        if name in self.tasks and not self.tasks[name].done():
            coroutine.close()
            return
        self.tasks[name] = asyncio.create_task(self._guard(name, coroutine))

    async def _guard(self, name: str, coroutine) -> None:
        try:
            await coroutine
        except Exception:
            log.exception("scheduled_job_failed job=%s", name)

    async def tick(self, now: datetime) -> None:
        timestamp = now.timestamp()
        if timestamp >= self.next_cleanup:
            self.service.cleanup(timestamp)
            self.next_cleanup = timestamp + 60
        db = self.service.db
        if not db.enabled:
            return
        last_attempt = float(db.state("last_scan_attempt", "0"))
        if timestamp - last_attempt >= self.service.settings.scan_interval_minutes * 60:
            self.launch("scan", self.service.scan())
        slot = due_slot(now, self.service.settings.timezone)
        if slot and not ("broadcast" in self.tasks and not self.tasks["broadcast"].done()):
            if db.claim_slot(slot, timestamp):
                self.launch("broadcast", self.service.broadcast())

    async def run(self) -> None:
        try:
            while True:
                await self.tick(datetime.now(UTC))
                await asyncio.sleep(5)
        finally:
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

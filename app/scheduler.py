"""Persistent schedule slots and a single lightweight polling loop."""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def schedule_from_row(row):
    value = dict(row)
    for key in ("days", "categories"):
        value[key] = json.loads(value[key])
    value["enabled"] = bool(value["enabled"])
    value.pop("created_at", None)
    return value


def slots_between(schedule, start, end, zone):
    """UTC slots in (start,end], respecting local weekday and DST round trips."""
    if not schedule["enabled"] or end <= start:
        return []
    tz = ZoneInfo(zone)
    day = start.astimezone(tz).date()
    end_day = end.astimezone(tz).date()
    hour, minute = map(int, schedule["time"].split(":"))
    slots = []
    while day <= end_day:
        if day.weekday() in schedule["days"]:
            local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            slot = local.astimezone(timezone.utc)
            # A nonexistent local time at the spring clock change has no slot.
            roundtrip = slot.astimezone(tz)
            if roundtrip.hour == hour and roundtrip.minute == minute and start < slot <= end:
                slots.append(slot)
        day += timedelta(days=1)
    return slots


def next_run(schedule, now, zone):
    if not schedule["enabled"]:
        return None
    candidates = slots_between(schedule, now, now + timedelta(days=9), zone)
    return candidates[0].isoformat() if candidates else None


def due_slots(db, since, now, startup=False):
    settings = db.settings()
    if startup and not settings["catch_up"]:
        return []
    floor = now - timedelta(hours=settings["catch_up_hours"])
    start = floor if startup else max(since, floor)
    due = []
    with db.connection() as conn:
        for row in conn.execute("""SELECT * FROM schedules WHERE enabled=1 AND
                (reader_id='owner' OR EXISTS(SELECT 1 FROM guest_sessions g
                 WHERE g.token_hash=schedules.reader_id AND g.expires_at>?))""", (now.isoformat(),)):
            schedule = schedule_from_row(row)
            created_at = datetime.fromisoformat(row["created_at"])
            candidates = slots_between(schedule, max(start, created_at), now, row["timezone"] or settings["timezone"])
            # Restart/long pause coalesces to the most recent configured slot.
            if candidates:
                slot = candidates[-1].isoformat()
                if not conn.execute("SELECT 1 FROM schedule_slots WHERE schedule_id=? AND scheduled_at=?", (row["id"], slot)).fetchone():
                    due.append({"id": row["id"], "scheduled_at": slot, "categories": schedule["categories"]})
    return due


class Scheduler:
    def __init__(self, db, collector):
        self.db = db
        self.collector = collector
        self.wake = asyncio.Event()
        self.task = None
        self.event_loop = None

    def start(self):
        self.event_loop = asyncio.get_running_loop()
        self.task = asyncio.create_task(self.loop())

    def notify(self):
        if self.event_loop and self.event_loop.is_running():
            self.event_loop.call_soon_threadsafe(self.wake.set)

    async def loop(self):
        previous = datetime.now(timezone.utc)
        startup = True
        while True:
            now = datetime.now(timezone.utc)
            try:
                if not self.collector.fetching:
                    due = due_slots(self.db, previous, now, startup)
                    if due:
                        await self.collector.start([], "scheduled", due)
                    previous = now
                    startup = False
            except Exception:
                logger.exception("Schedule poll failed")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=15)
                self.wake.clear()
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

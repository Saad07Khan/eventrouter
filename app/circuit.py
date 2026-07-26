from collections import defaultdict
from datetime import UTC, datetime, timedelta


class CircuitBreaker:
    """Stops hammering a destination that is clearly down.

    After `threshold` consecutive failures, the circuit "opens" for `cooldown`
    seconds — we stop delivering to that destination and let its work wait,
    instead of burning worker slots on something that isn't working right now.
    Any success closes it again. State is in-process (a dict), which is why this
    needs no Redis.
    """

    def __init__(self, threshold: int, cooldown: float):
        self.threshold = threshold
        self.cooldown = cooldown
        self._fails: dict[str, int] = defaultdict(int)
        self._open_until: dict[str, datetime] = {}

    def is_open(self, dest_id: str) -> bool:
        until = self._open_until.get(dest_id)
        if until is None:
            return False
        if until <= datetime.now(UTC):  # cooldown elapsed -> half-open
            del self._open_until[dest_id]
            self._fails[dest_id] = 0
            return False
        return True

    def retry_at(self, dest_id: str) -> datetime:
        """When the destination may be tried again (now, if the circuit is closed)."""
        return self._open_until.get(dest_id, datetime.now(UTC))

    def record(self, dest_id: str, ok: bool) -> bool:
        """Record one delivery outcome. Returns True if this just OPENED the circuit."""
        if ok:
            self._fails[dest_id] = 0
            self._open_until.pop(dest_id, None)
            return False
        self._fails[dest_id] += 1
        if self._fails[dest_id] >= self.threshold and dest_id not in self._open_until:
            self._open_until[dest_id] = datetime.now(UTC) + timedelta(seconds=self.cooldown)
            return True
        return False

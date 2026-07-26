import random

from app.config import settings


def compute_delay(attempts: int, retry_after: int | None = None) -> float:
    """Seconds to wait before the next attempt, after `attempts` failures.

    - If the destination told us exactly when to come back (429 Retry-After),
      honor that instead of guessing.
    - Otherwise exponential backoff: base, 2x, 4x, ... capped, so we give a
      struggling destination room to recover instead of hammering it.
    - Jitter (a random 0.5x-1.5x multiplier) spreads out retries that failed
      together, so N deliveries don't all retry in the same instant and knock
      the destination over again the moment it recovers.
    """
    if retry_after is not None and retry_after > 0:
        return float(retry_after)
    delay = min(settings.retry_base_seconds * (2 ** (attempts - 1)), settings.retry_cap_seconds)
    return delay * random.uniform(0.5, 1.5)

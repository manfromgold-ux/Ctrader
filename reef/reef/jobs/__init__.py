from dataclasses import dataclass


@dataclass
class Retry:
    """Returned by a job that could not do its work yet; the scheduler runs it again after `hours`
    instead of waiting for the job's full interval."""

    hours: float
    reason: str

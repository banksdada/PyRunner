"""
Automatic retries for failed runs.

A failed or timed-out run is retried up to ``script.max_retries`` times. Each
retry is a NEW Run (so it has its own logs) linked to the one it retries, and is
queued to start after a delay (fixed, or doubling each time).
"""

import logging
from datetime import timedelta

from django.utils import timezone
from django_q.models import Schedule as QSchedule
from django_q.tasks import schedule as q_schedule

from core.models import Run, Script

logger = logging.getLogger(__name__)

MAX_RETRIES_LIMIT = 10
MAX_DELAY_SECONDS = 24 * 3600


class RetryService:
    @classmethod
    def delay_for(cls, script: Script, attempt: int) -> int:
        """Seconds to wait before retrying after failed ``attempt`` (1-based)."""
        base = max(script.retry_delay_seconds, 1)
        if script.retry_backoff == Script.RetryBackoff.EXPONENTIAL:
            base = base * (2 ** (attempt - 1))
        return min(base, MAX_DELAY_SECONDS)

    @classmethod
    def should_retry(cls, run: Run) -> bool:
        script = run.script
        if run.status not in (Run.Status.FAILED, Run.Status.TIMEOUT):
            return False  # success and cancelled never retry
        if run.exit_code == Script.NO_RETRY_EXIT_CODE:
            return False  # script said "don't bother retrying"
        if not script.can_run:
            return False
        return run.attempt <= min(script.max_retries, MAX_RETRIES_LIMIT)

    @classmethod
    def schedule_retry(cls, run: Run, webhook_data: dict | None = None) -> Run | None:
        """Create and schedule the next attempt. Returns it, or None if no retry."""
        if not cls.should_retry(run):
            return None

        delay = cls.delay_for(run.script, run.attempt)
        new_run = Run.objects.create(
            script=run.script,
            workspace_id=run.workspace_id,
            status=Run.Status.PENDING,
            triggered_by=run.triggered_by,
            trigger_type=run.trigger_type,  # keep original trigger for filtering
            code_snapshot=run.code_snapshot,
            attempt=run.attempt + 1,
            retry_of=run,
            idempotency_key=run.idempotency_key or run.id.hex,
            workflow_step_run=run.workflow_step_run,
        )
        q_schedule(
            "core.tasks.execute_run_task",
            str(new_run.id),
            webhook_data,
            name=f"retry-{new_run.id}",
            schedule_type=QSchedule.ONCE,
            next_run=timezone.now() + timedelta(seconds=delay),
        )
        logger.info(
            f"Run {run.id} failed; retry {new_run.attempt - 1}/{run.script.max_retries} "
            f"scheduled as {new_run.id} in {delay}s"
        )
        return new_run

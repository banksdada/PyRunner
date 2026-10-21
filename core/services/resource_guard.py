"""
Resource guard: decides whether a queued run may start right now.

It lets you raise the worker count (more scripts in flight) while protecting the
server: a run is held back, and re-checked 30 seconds later, while the host is
short of memory, busy on CPU, or already running its quota of "heavy" scripts.

Safety rails so work never gets stuck:
  - if nothing else is running, a run always starts;
  - after MAX_WAIT_SECONDS of waiting, a run starts anyway.
Memory/CPU are read for the whole host (psutil), so other apps on the same
server (the rest of your Coolify stack) count towards the load.
"""

import logging
from dataclasses import dataclass

import psutil

from core.models import GlobalSettings, Run

logger = logging.getLogger(__name__)

RECHECK_SECONDS = 30
MAX_WAIT_SECONDS = 30 * 60
MAX_DEFERRALS = MAX_WAIT_SECONDS // RECHECK_SECONDS


@dataclass
class GuardDecision:
    allowed: bool
    reason: str = ""


class ResourceGuard:
    @classmethod
    def check(cls, run: Run, defer_count: int = 0) -> GuardDecision:
        gs = GlobalSettings.get_settings()
        if not gs.resource_guard_enabled:
            return GuardDecision(True)
        if defer_count >= MAX_DEFERRALS:
            return GuardDecision(True, "waited the maximum time")

        running = Run.objects.filter(status=Run.Status.RUNNING).exclude(pk=run.pk)
        if not running.exists():
            return GuardDecision(True)  # never block the only run

        if run.script.is_heavy:
            heavy = running.filter(script__is_heavy=True).count()
            if heavy >= gs.max_heavy_runs:
                return GuardDecision(False, f"{heavy} heavy script(s) already running (limit {gs.max_heavy_runs})")

        try:
            mem = psutil.virtual_memory()
            free_pct = mem.available / mem.total * 100
            if free_pct < gs.guard_min_free_memory_pct:
                return GuardDecision(False, f"only {free_pct:.0f}% memory free (needs {gs.guard_min_free_memory_pct}%)")
            cpu = psutil.cpu_percent(interval=0.3)
            if cpu > gs.guard_max_cpu_pct:
                return GuardDecision(False, f"CPU at {cpu:.0f}% (limit {gs.guard_max_cpu_pct}%)")
        except Exception as e:  # if we can't measure, don't block work
            logger.warning(f"Resource guard could not read system load: {e}")
        return GuardDecision(True)

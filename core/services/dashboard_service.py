"""
Dashboard service for gathering statistics and widgets data.
"""

from datetime import timedelta
from typing import Optional

from django.db.models import QuerySet
from django.utils import timezone


class DashboardService:
    """Service for gathering dashboard statistics and widget data."""

    @classmethod
    def get_statistics(cls, workspace=None) -> dict:
        """
        Get all dashboard statistics.

        Args:
            workspace: when given, script/run counts are scoped to it (tenancy
                Stage 3). ``None`` keeps the legacy instance-wide behavior.

        Returns dict with:
        - total_scripts: Total number of scripts
        - active_scripts: Scripts with is_enabled=True
        - runs_today: Runs created today
        - runs_this_week: Runs created in last 7 days
        - success_rate: Percentage of successful runs (or None if no runs)
        - queue_size: Number of tasks in queue
        """
        from django_q.models import OrmQ

        from core.models import Run, Script

        now = timezone.now()
        today = now.date()
        week_ago = now - timedelta(days=7)

        scripts = Script.objects
        runs = Run.objects
        if workspace is not None:
            scripts = scripts.for_workspace(workspace)
            runs = runs.for_workspace(workspace)

        # Script counts
        total_scripts = scripts.count()
        active_scripts = scripts.filter(is_enabled=True).count()

        # Run counts
        total_runs = runs.count()
        runs_today = runs.filter(created_at__date=today).count()
        runs_this_week = runs.filter(created_at__gte=week_ago).count()

        # Success rate
        success_rate = None
        if total_runs > 0:
            success_count = runs.filter(status=Run.Status.SUCCESS).count()
            success_rate = round((success_count / total_runs) * 100, 1)

        # Queue size
        try:
            queue_size = OrmQ.objects.count()
        except Exception:
            queue_size = 0

        return {
            "total_scripts": total_scripts,
            "active_scripts": active_scripts,
            "runs_today": runs_today,
            "runs_this_week": runs_this_week,
            "success_rate": success_rate,
            "queue_size": queue_size,
        }

    @classmethod
    def get_recent_failures(cls, limit: int = 5, workspace=None) -> QuerySet:
        """
        Get recent failed and timeout runs.

        Args:
            limit: Maximum number of runs to return
            workspace: when given, scope to the active workspace (tenancy Stage 3)

        Returns:
            QuerySet of Run objects ordered by most recent
        """
        from core.models import Run

        runs = Run.objects
        if workspace is not None:
            runs = runs.for_workspace(workspace)
        return (
            runs.filter(status__in=[Run.Status.FAILED, Run.Status.TIMEOUT])
            .select_related("script")
            .order_by("-created_at")[:limit]
        )

    @classmethod
    def get_upcoming_scheduled_runs(cls, limit: int = 5, workspace=None) -> QuerySet:
        """
        Get upcoming scheduled script runs.

        Args:
            limit: Maximum number of schedules to return
            workspace: when given, scope to the active workspace (tenancy Stage 3).
                Schedules are scoped by ``script__workspace`` — the authoritative
                source — since a schedule's own ``workspace`` FK may predate
                stamping.

        Returns:
            QuerySet of ScriptSchedule objects with upcoming runs
        """
        from core.models import ScriptSchedule

        now = timezone.now()

        schedules = ScriptSchedule.objects.filter(
            next_run__isnull=False,
            next_run__gt=now,
            is_active=True,
            run_mode__in=[ScriptSchedule.RunMode.INTERVAL, ScriptSchedule.RunMode.DAILY],
        )
        if workspace is not None:
            schedules = schedules.filter(script__workspace=workspace)
        return schedules.select_related("script").order_by("next_run")[:limit]

    @classmethod
    def get_system_health(cls) -> dict:
        """
        Get system health status.

        Returns dict with:
        - worker_status: str ("running", "stopped", "unknown")
        - worker_status_text: str (human-readable)
        - schedules_paused: bool
        - queue_size: int
        """
        from core.models import GlobalSettings
        from core.services.system_info_service import SystemInfoService

        worker_info = SystemInfoService.get_worker_status()
        global_settings = GlobalSettings.get_settings()

        return {
            "worker_status": worker_info["status"],
            "worker_status_text": worker_info["status_text"],
            "schedules_paused": global_settings.schedules_paused,
            "queue_size": worker_info["queued_tasks"],
        }

    @classmethod
    def get_script_health(cls, workspace=None, days: int = 30, limit: int = 15) -> list[dict]:
        """
        Per-script health over the last ``days`` days: runs, success rate, average
        duration, last status. Worst scripts (lowest success rate) come first.
        Retry attempts count as runs, so a script that only passes on retry shows
        a rate below 100%.
        """
        from django.db.models import Avg, Count, ExpressionWrapper, F, DurationField, Q

        from core.models import Run

        since = timezone.now() - timedelta(days=days)
        runs = Run.objects.filter(created_at__gte=since, status__in=[
            Run.Status.SUCCESS, Run.Status.FAILED, Run.Status.TIMEOUT])
        if workspace is not None:
            runs = runs.for_workspace(workspace)
        rows = (
            runs.values("script_id", "script__name")
            .annotate(
                total=Count("id"),
                ok=Count("id", filter=Q(status=Run.Status.SUCCESS)),
                avg_duration=Avg(ExpressionWrapper(F("ended_at") - F("started_at"), output_field=DurationField())),
            )
        )
        result = []
        for r in rows:
            last = (
                Run.objects.filter(script_id=r["script_id"]).order_by("-created_at").values_list("status", flat=True).first()
            )
            avg = r["avg_duration"]
            result.append({
                "script_id": r["script_id"],
                "name": r["script__name"],
                "total": r["total"],
                "failed": r["total"] - r["ok"],
                "success_rate": round(r["ok"] / r["total"] * 100, 1),
                "avg_seconds": round(avg.total_seconds(), 1) if avg else None,
                "last_status": last,
            })
        result.sort(key=lambda x: (x["success_rate"], -x["total"]))
        return result[:limit]

    @classmethod
    def get_runs_per_day(cls, workspace=None, days: int = 14) -> list[dict]:
        """Successful vs failed runs per day (oldest first) for the trend chart."""
        from django.db.models import Count, Q
        from django.db.models.functions import TruncDate

        from core.models import Run

        today = timezone.localdate()
        start = today - timedelta(days=days - 1)
        runs = Run.objects.filter(created_at__date__gte=start)
        if workspace is not None:
            runs = runs.for_workspace(workspace)
        counts = {
            row["day"]: row
            for row in runs.annotate(day=TruncDate("created_at")).values("day").annotate(
                ok=Count("id", filter=Q(status=Run.Status.SUCCESS)),
                bad=Count("id", filter=Q(status__in=[Run.Status.FAILED, Run.Status.TIMEOUT])),
            )
        }
        series = []
        for i in range(days):
            d = start + timedelta(days=i)
            row = counts.get(d, {"ok": 0, "bad": 0})
            series.append({"day": d, "ok": row["ok"], "bad": row["bad"]})
        peak = max([x["ok"] + x["bad"] for x in series] + [1])
        for x in series:
            x["ok_pct"] = round(x["ok"] / peak * 100)
            x["bad_pct"] = round(x["bad"] / peak * 100)
        return series

    @classmethod
    def get_workflow_summary(cls, workspace=None) -> dict:
        """Counts of workflow runs that need attention."""
        from core.models import WorkflowRun

        runs = WorkflowRun.objects
        if workspace is not None:
            runs = runs.for_workspace(workspace)
        waiting = list(runs.filter(status=WorkflowRun.Status.WAITING_APPROVAL).select_related("workflow")[:5])
        failed_today = runs.filter(status=WorkflowRun.Status.FAILED, created_at__date=timezone.localdate()).count()
        running = runs.filter(status=WorkflowRun.Status.RUNNING).count()
        return {"waiting": waiting, "failed_today": failed_today, "running": running}


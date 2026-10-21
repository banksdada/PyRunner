"""
Workflow engine: runs a workflow's steps one after another.

Flow:
  start() -> creates a WorkflowRun and starts the first step.
  A script step creates a normal Run and queues it. When that Run finishes for
  good (after any retries), core.tasks calls on_run_finished(), which decides the
  next step from the step's success/failure rules.
  An approval step pauses the workflow until someone calls approve() or reject().
"""

import json
import logging

from django.db import transaction
from django.utils import timezone

from core.models import (
    Run,
    Workflow,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
)

logger = logging.getLogger(__name__)

MAX_STEPS_PER_RUN = 100  # stops a "jump to step" loop running forever
MAX_OUTPUT_CHARS = 60_000


def extract_output(stdout: str) -> str:
    """
    What a step hands to the next one. If the last non-empty line of stdout is
    JSON, that line is the output (easy for scripts to parse). Otherwise the whole
    stdout (truncated).
    """
    text = (stdout or "").strip()
    if not text:
        return ""
    last = text.splitlines()[-1].strip()
    try:
        json.loads(last)
        return last
    except ValueError:
        return text[-MAX_OUTPUT_CHARS:]


class WorkflowService:
    # ---- starting ---------------------------------------------------------
    @classmethod
    def start(cls, workflow: Workflow, user=None, trigger_type="manual") -> WorkflowRun:
        steps = list(workflow.steps.all())
        if not workflow.is_enabled:
            raise ValueError("Workflow is disabled")
        if not steps:
            raise ValueError("Workflow has no steps")
        wr = WorkflowRun.objects.create(
            workflow=workflow,
            workspace_id=workflow.workspace_id,
            trigger_type=trigger_type,
            triggered_by=user,
        )
        cls._start_step(wr, steps[0], user)
        return wr

    @classmethod
    def _start_step(cls, wr: WorkflowRun, step: WorkflowStep, user=None) -> None:
        from core.tasks import queue_script_run

        if wr.steps_executed >= MAX_STEPS_PER_RUN:
            cls._finish(wr, WorkflowRun.Status.FAILED, "Too many steps executed (possible loop)")
            return
        wr.steps_executed += 1
        wr.save(update_fields=["steps_executed"])

        sr = WorkflowStepRun.objects.create(
            workflow_run=wr, step=step, step_name=step.name
        )

        if step.step_type == WorkflowStep.StepType.APPROVAL:
            sr.status = WorkflowStepRun.Status.WAITING_APPROVAL
            sr.save(update_fields=["status"])
            wr.status = WorkflowRun.Status.WAITING_APPROVAL
            wr.save(update_fields=["status"])
            cls._alert(
                wr,
                f"[PyRunner] Approval needed: {wr.workflow.name}",
                f"Step '{step.name}' is waiting for approval.\n\n{step.approval_message}",
            )
            return

        script = step.script
        if script is None or not script.can_run:
            sr.status = WorkflowStepRun.Status.FAILED
            sr.ended_at = timezone.now()
            sr.output = "Script is missing, disabled or archived."
            sr.save()
            cls._after_step(wr, sr, success=False)
            return

        sr.status = WorkflowStepRun.Status.RUNNING
        sr.save(update_fields=["status"])
        run = Run.objects.create(
            script=script,
            workspace_id=script.workspace_id,
            status=Run.Status.PENDING,
            triggered_by=user or wr.triggered_by,
            trigger_type=Run.TriggerType.WORKFLOW,
            code_snapshot=script.code,
            workflow_step_run=sr,
        )
        run.idempotency_key = run.id.hex
        run.save(update_fields=["idempotency_key"])
        queue_script_run(run)

    # ---- reacting to results ---------------------------------------------
    @classmethod
    def on_run_finished(cls, run: Run) -> None:
        """Called once a workflow step's run is final (no retry pending)."""
        sr = run.workflow_step_run
        if sr is None:
            return
        with transaction.atomic():
            sr = WorkflowStepRun.objects.select_for_update().get(pk=sr.pk)
            if sr.status != WorkflowStepRun.Status.RUNNING:
                return  # already handled
            sr.ended_at = timezone.now()
            sr.output = extract_output(run.stdout) if run.status == Run.Status.SUCCESS else (run.stderr or "")[-MAX_OUTPUT_CHARS:]
            if run.status == Run.Status.CANCELLED:
                sr.status = WorkflowStepRun.Status.CANCELLED
                sr.save()
                cls._finish(sr.workflow_run, WorkflowRun.Status.CANCELLED)
                return
            ok = run.status == Run.Status.SUCCESS
            sr.status = WorkflowStepRun.Status.SUCCESS if ok else WorkflowStepRun.Status.FAILED
            sr.save()
        cls._after_step(sr.workflow_run, sr, success=ok)

    @classmethod
    def _after_step(cls, wr: WorkflowRun, sr: WorkflowStepRun, success: bool) -> None:
        step = sr.step
        steps = list(wr.workflow.steps.all())
        if step is None:
            cls._finish(wr, WorkflowRun.Status.FAILED, "Step was deleted while running")
            return
        nxt = None
        if success or step.on_failure == WorkflowStep.OnFailure.CONTINUE:
            later = [s for s in steps if s.pk != step.pk and (s.order, s.name) > (step.order, step.name)]
            nxt = later[0] if later else None
            if nxt is None:
                # last step: success overall (a tolerated failure still counts as done)
                cls._finish(wr, WorkflowRun.Status.SUCCESS)
                return
        elif step.on_failure == WorkflowStep.OnFailure.GOTO and step.failure_goto_id:
            nxt = next((s for s in steps if s.pk == step.failure_goto_id), None)
        if nxt is None:
            cls._finish(wr, WorkflowRun.Status.FAILED, f"Step '{sr.step_name}' failed")
            return
        wr.status = WorkflowRun.Status.RUNNING
        wr.save(update_fields=["status"])
        cls._start_step(wr, nxt)

    # ---- approvals -------------------------------------------------------
    @classmethod
    def decide(cls, step_run: WorkflowStepRun, user, approve: bool, note: str = "") -> None:
        with transaction.atomic():
            sr = WorkflowStepRun.objects.select_for_update().get(pk=step_run.pk)
            if sr.status != WorkflowStepRun.Status.WAITING_APPROVAL:
                raise ValueError("This step is not waiting for approval")
            sr.decided_by = user
            sr.note = note
            sr.ended_at = timezone.now()
            sr.status = WorkflowStepRun.Status.SUCCESS if approve else WorkflowStepRun.Status.REJECTED
            # Pass the previous step's output straight through an approval.
            prev = (
                sr.workflow_run.step_runs.exclude(pk=sr.pk)
                .filter(status=WorkflowStepRun.Status.SUCCESS)
                .order_by("-started_at")
                .first()
            )
            sr.output = prev.output if prev else ""
            sr.save()
        if approve:
            cls._after_step(sr.workflow_run, sr, success=True)
        else:
            cls._finish(sr.workflow_run, WorkflowRun.Status.FAILED, f"Rejected at '{sr.step_name}'")

    @classmethod
    def cancel(cls, wr: WorkflowRun) -> None:
        if wr.is_finished:
            return
        wr.step_runs.filter(
            status__in=[WorkflowStepRun.Status.WAITING_APPROVAL, WorkflowStepRun.Status.PENDING]
        ).update(status=WorkflowStepRun.Status.CANCELLED, ended_at=timezone.now())
        cls._finish(wr, WorkflowRun.Status.CANCELLED)

    # ---- helpers ---------------------------------------------------------
    @classmethod
    def _finish(cls, wr: WorkflowRun, status: str, reason: str = "") -> None:
        wr.status = status
        wr.ended_at = timezone.now()
        wr.save(update_fields=["status", "ended_at"])
        if status == WorkflowRun.Status.FAILED:
            cls._alert(wr, f"[PyRunner] Workflow failed: {wr.workflow.name}", reason or "A step failed.")

    @classmethod
    def _alert(cls, wr: WorkflowRun, subject: str, body: str) -> None:
        from core.services.notification_service import send_plain_alert

        send_plain_alert(subject, body, wr.workflow.notify_email)

    @classmethod
    def build_env(cls, run: Run) -> dict:
        """Environment variables describing the workflow context for a step's script."""
        sr = run.workflow_step_run
        if sr is None:
            return {}
        wr = sr.workflow_run
        prev = (
            wr.step_runs.exclude(pk=sr.pk)
            .filter(status=WorkflowStepRun.Status.SUCCESS, started_at__lte=sr.started_at)
            .order_by("-started_at")
            .first()
        )
        outputs = {
            s.step_name: s.output
            for s in wr.step_runs.filter(status=WorkflowStepRun.Status.SUCCESS)
        }
        env = {
            "WORKFLOW_RUN_ID": str(wr.id),
            "WORKFLOW_NAME": wr.workflow.name,
            "WORKFLOW_STEP_NAME": sr.step_name,
            "WORKFLOW_INPUT": prev.output if prev else "",
            "WORKFLOW_OUTPUTS_JSON": json.dumps(outputs)[:MAX_OUTPUT_CHARS],
        }
        return env

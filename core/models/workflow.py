"""
Workflow models: chain scripts together ("run B after A succeeds").

Workflow      - the definition (a named, ordered list of steps)
WorkflowStep  - one step: run a script, or pause for human approval
WorkflowRun   - one execution of a workflow
WorkflowStepRun - one execution of one step (links to the Run(s) behind it)
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from .workspace import WorkspaceScopedManager


class Workflow(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="workflows",
    )
    objects = WorkspaceScopedManager()

    is_enabled = models.BooleanField(default=True)
    notify_email = models.EmailField(
        blank=True,
        help_text="Emailed when the workflow fails or waits for approval "
        "(uses the global default if empty)",
    )
    # Optional schedule, as a standard 5-field cron expression (server time zone).
    schedule_cron = models.CharField(
        max_length=100,
        blank=True,
        help_text="Optional, e.g. '30 8 * * 1-5' = 08:30 on weekdays. Leave empty for manual only.",
    )
    q_schedule_id = models.IntegerField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflows",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workflows"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def last_run(self):
        return self.runs.order_by("-created_at").first()


class WorkflowStep(models.Model):
    class StepType(models.TextChoices):
        SCRIPT = "script", "Run a script"
        APPROVAL = "approval", "Wait for approval"

    class OnFailure(models.TextChoices):
        STOP = "stop", "Stop the workflow"
        CONTINUE = "continue", "Continue to the next step"
        GOTO = "goto", "Jump to another step"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="steps")
    order = models.PositiveIntegerField(default=1, help_text="Steps run in this order, lowest first")
    name = models.CharField(max_length=200)
    step_type = models.CharField(max_length=20, choices=StepType.choices, default=StepType.SCRIPT)
    script = models.ForeignKey(
        "core.Script",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="workflow_steps",
        help_text="Script to run (script steps only)",
    )
    on_failure = models.CharField(max_length=20, choices=OnFailure.choices, default=OnFailure.STOP)
    failure_goto = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Step to jump to when this one fails (only with 'Jump to another step')",
    )
    approval_message = models.TextField(
        blank=True, help_text="What the approver should check (approval steps only)"
    )

    class Meta:
        db_table = "workflow_steps"
        ordering = ["order", "name"]

    def __str__(self):
        return f"{self.workflow.name}: {self.name}"


class WorkflowRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        WAITING_APPROVAL = "waiting_approval", "Waiting for approval"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="runs")
    workspace = models.ForeignKey(
        "core.Workspace", on_delete=models.SET_NULL, null=True, blank=True, related_name="workflow_runs"
    )
    objects = WorkspaceScopedManager()

    status = models.CharField(max_length=30, choices=Status.choices, default=Status.RUNNING, db_index=True)
    trigger_type = models.CharField(max_length=20, default="manual")
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    steps_executed = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "workflow_runs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.workflow.name} ({self.status})"

    @property
    def is_finished(self):
        return self.status in (self.Status.SUCCESS, self.Status.FAILED, self.Status.CANCELLED)

    @property
    def duration_display(self):
        end = self.ended_at or timezone.now()
        secs = int((end - self.started_at).total_seconds())
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"


class WorkflowStepRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        WAITING_APPROVAL = "waiting_approval", "Waiting for approval"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow_run = models.ForeignKey(WorkflowRun, on_delete=models.CASCADE, related_name="step_runs")
    step = models.ForeignKey(WorkflowStep, on_delete=models.SET_NULL, null=True, related_name="step_runs")
    step_name = models.CharField(max_length=200)  # snapshot, survives step edits
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)
    output = models.TextField(blank=True, help_text="Passed to the next step as WORKFLOW_INPUT")
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    note = models.TextField(blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "workflow_step_runs"
        ordering = ["started_at"]

    def __str__(self):
        return f"{self.step_name} ({self.status})"

    @property
    def latest_run(self):
        return self.runs.order_by("-created_at").first()

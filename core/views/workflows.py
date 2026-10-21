"""
Workflow views for the control panel.
"""
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_q.models import Schedule as QSchedule

from core.forms import WorkflowForm, WorkflowStepFormSet
from core.models import GlobalSettings, Workflow, WorkflowRun, WorkflowStepRun
from core.services.workflow_service import WorkflowService

logger = logging.getLogger(__name__)


def sync_workflow_schedule(workflow: Workflow) -> None:
    """Create/replace/remove the django-q2 cron schedule for a workflow."""
    if workflow.q_schedule_id:
        QSchedule.objects.filter(pk=workflow.q_schedule_id).delete()
        workflow.q_schedule_id = None
    if (
        workflow.is_enabled
        and workflow.schedule_cron
        and not GlobalSettings.get_settings().schedules_paused
    ):
        q = QSchedule.objects.create(
            name=f"pyrunner-workflow-{workflow.id}",
            func="core.tasks.execute_scheduled_workflow",
            args=f"'{workflow.id}'",
            schedule_type=QSchedule.CRON,
            cron=workflow.schedule_cron,
            repeats=-1,
            next_run=timezone.now(),
        )
        workflow.q_schedule_id = q.pk
    workflow.save(update_fields=["q_schedule_id"])


def _get_workflow(request, pk):
    return get_object_or_404(Workflow, pk=pk, workspace=request.workspace)


@login_required
def workflow_list_view(request: HttpRequest) -> HttpResponse:
    workflows = Workflow.objects.for_workspace(request.workspace).prefetch_related("steps")
    rows = [
        {"workflow": w, "last_run": w.last_run, "step_count": w.steps.count()} for w in workflows
    ]
    return render(request, "cpanel/workflows/list.html", {"rows": rows})


@login_required
def workflow_form_view(request: HttpRequest, pk=None) -> HttpResponse:
    workflow = _get_workflow(request, pk) if pk else None
    if request.method == "POST":
        form = WorkflowForm(request.POST, instance=workflow)
        formset = WorkflowStepFormSet(
            request.POST, instance=workflow or Workflow(), form_kwargs={"workspace": request.workspace}
        )
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                obj = form.save(commit=False)
                if workflow is None:
                    obj.created_by = request.user
                    obj.workspace = request.workspace
                obj.save()
                formset.instance = obj
                formset.save()
            sync_workflow_schedule(obj)
            messages.success(request, f'Workflow "{obj.name}" saved.')
            return redirect("cpanel:workflow_detail", pk=obj.pk)
    else:
        form = WorkflowForm(instance=workflow)
        formset = WorkflowStepFormSet(
            instance=workflow or Workflow(), form_kwargs={"workspace": request.workspace}
        )
    return render(
        request,
        "cpanel/workflows/form.html",
        {"form": form, "formset": formset, "workflow": workflow},
    )


@login_required
def workflow_detail_view(request: HttpRequest, pk) -> HttpResponse:
    workflow = _get_workflow(request, pk)
    return render(
        request,
        "cpanel/workflows/detail.html",
        {
            "workflow": workflow,
            "steps": workflow.steps.select_related("script", "failure_goto"),
            "runs": workflow.runs.all()[:20],
        },
    )


@login_required
@require_POST
def workflow_start_view(request: HttpRequest, pk) -> HttpResponse:
    workflow = _get_workflow(request, pk)
    try:
        wr = WorkflowService.start(workflow, user=request.user)
    except ValueError as e:
        messages.error(request, str(e))
        return redirect("cpanel:workflow_detail", pk=pk)
    return redirect("cpanel:workflow_run_detail", pk=wr.pk)


@login_required
@require_POST
def workflow_delete_view(request: HttpRequest, pk) -> HttpResponse:
    workflow = _get_workflow(request, pk)
    if workflow.q_schedule_id:
        QSchedule.objects.filter(pk=workflow.q_schedule_id).delete()
    name = workflow.name
    workflow.delete()
    messages.success(request, f'Workflow "{name}" deleted.')
    return redirect("cpanel:workflow_list")


@login_required
def workflow_run_detail_view(request: HttpRequest, pk) -> HttpResponse:
    wr = get_object_or_404(
        WorkflowRun.objects.select_related("workflow"), pk=pk, workspace=request.workspace
    )
    step_runs = []
    for sr in wr.step_runs.select_related("decided_by"):
        step_runs.append({"sr": sr, "runs": list(sr.runs.order_by("created_at"))})
    return render(
        request, "cpanel/workflows/run_detail.html", {"wr": wr, "step_runs": step_runs}
    )


@login_required
@require_POST
def workflow_step_decide_view(request: HttpRequest, pk) -> HttpResponse:
    sr = get_object_or_404(WorkflowStepRun, pk=pk, workflow_run__workspace=request.workspace)
    approve = request.POST.get("decision") == "approve"
    try:
        WorkflowService.decide(sr, request.user, approve, request.POST.get("note", "")[:1000])
        messages.success(request, "Approved." if approve else "Rejected.")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("cpanel:workflow_run_detail", pk=sr.workflow_run_id)


@login_required
@require_POST
def workflow_run_cancel_view(request: HttpRequest, pk) -> HttpResponse:
    wr = get_object_or_404(WorkflowRun, pk=pk, workspace=request.workspace)
    WorkflowService.cancel(wr)
    messages.info(request, "Workflow cancelled.")
    return redirect("cpanel:workflow_run_detail", pk=wr.pk)

"""
Tests for automatic retries, workflows (incl. approval steps) and dashboard data.
Scripts are never really executed: execute_run is replaced with a fake that
sets each run's final status.
"""

from unittest import mock

from django.test import TestCase
from django_q.models import Schedule as QSchedule

from core.models import (
    Environment, Run, Script, User, Workflow, WorkflowRun, WorkflowStep, WorkflowStepRun,
    Workspace, WorkspaceMembership,
)
from core.services.retry_service import RetryService
from core.services.dashboard_service import DashboardService
from core.services.workflow_service import WorkflowService, extract_output
from core import tasks


def _setup_mocks(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


class Base(TestCase):
    def setUp(self):
        self.env = Environment.objects.create(name="e", path="rwenv")
        self.script = Script.objects.create(
            name="s", code="print(1)", environment=self.env,
            max_retries=2, retry_delay_seconds=10,
        )

    def finish(self, run, status, exit_code=None, stdout="", stderr=""):
        """Fake execute_run: mark the run finished, then run the task's post-processing."""
        def fake(r, webhook_data=None):
            r.status = status
            r.exit_code = exit_code if exit_code is not None else (0 if status == Run.Status.SUCCESS else 1)
            r.stdout, r.stderr = stdout, stderr
            r.save()
        with mock.patch("core.executor.execute_run", side_effect=fake), \
             mock.patch("core.services.notification_service.NotificationService.send_notification") as notif:
            result = tasks.execute_run_task(str(run.id))
        return result, notif

    def new_run(self, **kw):
        r = Run.objects.create(script=self.script, **kw)
        r.idempotency_key = r.id.hex
        r.save()
        return r


class RetryTests(Base):
    def test_failure_schedules_retry_and_skips_alert(self):
        run = self.new_run()
        result, notif = self.finish(run, Run.Status.FAILED)
        retry = Run.objects.get(retry_of=run)
        self.assertEqual(retry.attempt, 2)
        self.assertEqual(retry.status, Run.Status.PENDING)
        self.assertEqual(retry.idempotency_key, run.idempotency_key)
        self.assertEqual(result["retry_run_id"], str(retry.id))
        notif.assert_not_called()
        self.assertTrue(QSchedule.objects.filter(name=f"retry-{retry.id}").exists())

    def test_alert_only_after_final_attempt(self):
        run = self.new_run(attempt=3)  # max_retries=2 -> attempt 3 is the last
        run.retry_of = self.new_run(attempt=2)
        run.save()
        _, notif = self.finish(run, Run.Status.FAILED)
        self.assertFalse(Run.objects.filter(retry_of=run).exists())
        notif.assert_called_once()

    def test_success_does_not_retry(self):
        run = self.new_run()
        self.finish(run, Run.Status.SUCCESS)
        self.assertFalse(Run.objects.filter(retry_of=run).exists())

    def test_no_retry_exit_code(self):
        run = self.new_run()
        self.finish(run, Run.Status.FAILED, exit_code=Script.NO_RETRY_EXIT_CODE)
        self.assertFalse(Run.objects.filter(retry_of=run).exists())

    def test_zero_max_retries_never_retries(self):
        self.script.max_retries = 0
        self.script.save()
        run = self.new_run()
        self.finish(run, Run.Status.FAILED)
        self.assertFalse(Run.objects.filter(retry_of=run).exists())

    def test_cancelled_never_retries(self):
        run = self.new_run()
        self.finish(run, Run.Status.CANCELLED)
        self.assertFalse(Run.objects.filter(retry_of=run).exists())

    def test_backoff(self):
        self.assertEqual(RetryService.delay_for(self.script, 1), 10)
        self.assertEqual(RetryService.delay_for(self.script, 2), 20)
        self.assertEqual(RetryService.delay_for(self.script, 3), 40)
        self.script.retry_backoff = Script.RetryBackoff.FIXED
        self.assertEqual(RetryService.delay_for(self.script, 3), 10)

    def test_attempt_chain(self):
        a = self.new_run()
        b = Run.objects.create(script=self.script, attempt=2, retry_of=a)
        c = Run.objects.create(script=self.script, attempt=3, retry_of=b)
        self.assertEqual([r.pk for r in c.attempt_chain], [a.pk, b.pk, c.pk])
        self.assertEqual([r.pk for r in a.attempt_chain], [a.pk, b.pk, c.pk])


class WorkflowTests(Base):
    def setUp(self):
        super().setUp()
        self.script.max_retries = 0
        self.script.save()
        self.s2 = Script.objects.create(name="s2", code="x", environment=self.env)
        self.s_alt = Script.objects.create(name="alt", code="x", environment=self.env)
        self.wf = Workflow.objects.create(name="wf")

    def step(self, order, name, script=None, **kw):
        t = WorkflowStep.StepType.SCRIPT if script else WorkflowStep.StepType.APPROVAL
        return WorkflowStep.objects.create(
            workflow=self.wf, order=order, name=name, step_type=t, script=script, **kw
        )

    def current_run(self, wr):
        return Run.objects.filter(workflow_step_run__workflow_run=wr, status=Run.Status.PENDING).get()

    def test_b_runs_after_a_and_gets_output(self):
        self.step(1, "A", self.script)
        self.step(2, "B", self.s2)
        wr = WorkflowService.start(self.wf)
        a = self.current_run(wr)
        self.finish(a, Run.Status.SUCCESS, stdout='log line\n{"rows": 5}')
        b = self.current_run(wr)
        self.assertEqual(b.script, self.s2)
        from core.services.workflow_service import WorkflowService as WS
        env = WS.build_env(b)
        self.assertEqual(env["WORKFLOW_INPUT"], '{"rows": 5}')
        self.finish(b, Run.Status.SUCCESS)
        wr.refresh_from_db()
        self.assertEqual(wr.status, WorkflowRun.Status.SUCCESS)

    def test_failure_stops_workflow(self):
        self.step(1, "A", self.script)
        self.step(2, "B", self.s2)
        wr = WorkflowService.start(self.wf)
        self.finish(self.current_run(wr), Run.Status.FAILED)
        wr.refresh_from_db()
        self.assertEqual(wr.status, WorkflowRun.Status.FAILED)
        self.assertFalse(Run.objects.filter(script=self.s2).exists())

    def test_failure_continue(self):
        self.step(1, "A", self.script, on_failure=WorkflowStep.OnFailure.CONTINUE)
        self.step(2, "B", self.s2)
        wr = WorkflowService.start(self.wf)
        self.finish(self.current_run(wr), Run.Status.FAILED)
        self.assertEqual(self.current_run(wr).script, self.s2)

    def test_failure_goto_alternate_step(self):
        alt = self.step(3, "Alt", self.s_alt)
        self.step(1, "A", self.script, on_failure=WorkflowStep.OnFailure.GOTO, failure_goto=alt)
        self.step(2, "B", self.s2)
        wr = WorkflowService.start(self.wf)
        self.finish(self.current_run(wr), Run.Status.FAILED)
        self.assertEqual(self.current_run(wr).script, self.s_alt)

    def test_retry_inside_workflow_waits_for_final_attempt(self):
        self.script.max_retries = 1
        self.script.save()
        self.step(1, "A", self.script)
        self.step(2, "B", self.s2)
        wr = WorkflowService.start(self.wf)
        a1 = self.current_run(wr)
        self.finish(a1, Run.Status.FAILED)
        wr.refresh_from_db()
        self.assertEqual(wr.status, WorkflowRun.Status.RUNNING)  # not failed yet
        a2 = Run.objects.get(retry_of=a1)
        self.finish(a2, Run.Status.SUCCESS)
        self.assertEqual(self.current_run(wr).script, self.s2)  # moved on after retry passed

    def test_approval_pauses_then_approve_continues(self):
        self.step(1, "A", self.script)
        self.step(2, "Check", None, approval_message="Looks right?")
        self.step(3, "C", self.s2)
        user = User.objects.create(email="a@example.com")
        wr = WorkflowService.start(self.wf)
        self.finish(self.current_run(wr), Run.Status.SUCCESS, stdout="hello")
        wr.refresh_from_db()
        self.assertEqual(wr.status, WorkflowRun.Status.WAITING_APPROVAL)
        self.assertFalse(Run.objects.filter(script=self.s2).exists())
        sr = wr.step_runs.get(status=WorkflowStepRun.Status.WAITING_APPROVAL)
        WorkflowService.decide(sr, user, approve=True)
        c = self.current_run(wr)
        self.assertEqual(c.script, self.s2)
        self.assertEqual(WorkflowService.build_env(c)["WORKFLOW_INPUT"], "hello")  # passed through approval
        with self.assertRaises(ValueError):
            WorkflowService.decide(sr, user, approve=True)  # can't decide twice

    def test_reject_fails_workflow(self):
        self.step(1, "Check", None)
        self.step(2, "C", self.s2)
        wr = WorkflowService.start(self.wf)
        sr = wr.step_runs.get()
        WorkflowService.decide(sr, None, approve=False)
        wr.refresh_from_db()
        self.assertEqual(wr.status, WorkflowRun.Status.FAILED)
        self.assertFalse(Run.objects.exists())

    def test_cancel(self):
        self.step(1, "Check", None)
        wr = WorkflowService.start(self.wf)
        WorkflowService.cancel(wr)
        wr.refresh_from_db()
        self.assertEqual(wr.status, WorkflowRun.Status.CANCELLED)

    def test_start_requires_steps_and_enabled(self):
        with self.assertRaises(ValueError):
            WorkflowService.start(self.wf)
        self.step(1, "A", self.script)
        self.wf.is_enabled = False
        with self.assertRaises(ValueError):
            WorkflowService.start(self.wf)

    def test_extract_output(self):
        self.assertEqual(extract_output('x\n{"a": 1}\n'), '{"a": 1}')
        self.assertEqual(extract_output("plain\ntext"), "plain\ntext")
        self.assertEqual(extract_output(""), "")


class DashboardAndViewTests(Base):
    def test_script_health_and_chart(self):
        for st in (Run.Status.SUCCESS, Run.Status.SUCCESS, Run.Status.FAILED):
            Run.objects.create(script=self.script, status=st)
        health = DashboardService.get_script_health()
        self.assertEqual(health[0]["total"], 3)
        self.assertEqual(health[0]["success_rate"], 66.7)
        series = DashboardService.get_runs_per_day()
        self.assertEqual(len(series), 14)
        self.assertEqual(series[-1]["ok"], 2)
        self.assertEqual(series[-1]["bad"], 1)

    def test_pages_render(self):
        _setup_mocks(self)
        user = User.objects.create(email="v@example.com")
        ws = Workspace.get_default()
        self.script.workspace = ws
        self.script.save()
        self.client.force_login(user)
        wf = Workflow.objects.create(name="wf", workspace=ws)
        WorkflowStep.objects.create(workflow=wf, order=1, name="A", script=self.script)
        WorkflowStep.objects.create(workflow=wf, order=2, name="OK?", step_type="approval")
        from django.urls import reverse

        for name in ("dashboard", "workflow_list", "workflow_create"):
            self.assertEqual(self.client.get(reverse(f"cpanel:{name}")).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("cpanel:workflow_detail", args=[wf.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("cpanel:workflow_edit", args=[wf.pk])).status_code, 200)
        resp = self.client.post(reverse("cpanel:workflow_start", args=[wf.pk]))
        self.assertEqual(resp.status_code, 302)
        wr = WorkflowRun.objects.get()
        self.assertEqual(self.client.get(reverse("cpanel:workflow_run_detail", args=[wr.pk])).status_code, 200)
        run = Run.objects.get(workflow_step_run__workflow_run=wr)
        self.assertEqual(self.client.get(reverse("cpanel:run_detail", args=[run.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("cpanel:script_edit", args=[self.script.pk])).status_code, 200)

    def test_workflow_form_saves_steps_and_schedule(self):
        _setup_mocks(self)
        from django.urls import reverse

        user = User.objects.create(email="w@example.com")
        self.script.workspace = Workspace.get_default()
        self.script.save()
        self.client.force_login(user)
        data = {
            "name": "Nightly", "description": "", "is_enabled": "on", "notify_email": "",
            "schedule_cron": "30 8 * * 1-5",
            "steps-TOTAL_FORMS": "2", "steps-INITIAL_FORMS": "0",
            "steps-MIN_NUM_FORMS": "0", "steps-MAX_NUM_FORMS": "1000",
            "steps-0-order": "1", "steps-0-name": "Fetch", "steps-0-step_type": "script",
            "steps-0-script": str(self.script.pk), "steps-0-on_failure": "stop",
            "steps-1-order": "2", "steps-1-name": "Approve", "steps-1-step_type": "approval",
            "steps-1-on_failure": "stop", "steps-1-approval_message": "ok?",
        }
        resp = self.client.post(reverse("cpanel:workflow_create"), data)
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context["formset"].errors)
        wf = Workflow.objects.get(name="Nightly")
        self.assertEqual(wf.steps.count(), 2)
        self.assertTrue(QSchedule.objects.filter(pk=wf.q_schedule_id, cron="30 8 * * 1-5").exists())
        # invalid cron is rejected
        data["name"] = "Bad"
        data["schedule_cron"] = "nonsense"
        self.assertEqual(self.client.post(reverse("cpanel:workflow_create"), data).status_code, 200)


class ResourceGuardTests(Base):
    def _running(self, heavy=False):
        s = Script.objects.create(name=f"r{Script.objects.count()}", code="x", environment=self.env, is_heavy=heavy)
        return Run.objects.create(script=s, status=Run.Status.RUNNING)

    def check(self, run, defer=0, mem_free=50, cpu=10):
        from core.services.resource_guard import ResourceGuard
        vm = mock.Mock(available=mem_free, total=100)
        with mock.patch("psutil.virtual_memory", return_value=vm), \
             mock.patch("psutil.cpu_percent", return_value=cpu):
            return ResourceGuard.check(run, defer)

    def test_only_run_always_starts(self):
        run = self.new_run()
        self.assertTrue(self.check(run, mem_free=1, cpu=99).allowed)

    def test_low_memory_holds_back(self):
        self._running()
        self.assertFalse(self.check(self.new_run(), mem_free=10).allowed)

    def test_high_cpu_holds_back(self):
        self._running()
        self.assertFalse(self.check(self.new_run(), cpu=95).allowed)

    def test_healthy_server_allows(self):
        self._running()
        self.assertTrue(self.check(self.new_run()).allowed)

    def test_heavy_cap(self):
        self._running(heavy=True)
        self.script.is_heavy = True
        self.script.save()
        self.assertFalse(self.check(self.new_run()).allowed)
        # a light script is not limited by the heavy cap
        light = Script.objects.create(name="light", code="x", environment=self.env)
        self.assertTrue(self.check(Run.objects.create(script=light)).allowed)

    def test_gives_up_waiting(self):
        self._running()
        self.assertTrue(self.check(self.new_run(), defer=60, mem_free=1).allowed)

    def test_disabled_guard(self):
        from core.models import GlobalSettings
        gs = GlobalSettings.get_settings()
        gs.resource_guard_enabled = False
        gs.save()
        self._running()
        self.assertTrue(self.check(self.new_run(), mem_free=1).allowed)

    def test_task_defers_instead_of_running(self):
        self._running()
        run = self.new_run()
        vm = mock.Mock(available=5, total=100)
        with mock.patch("psutil.virtual_memory", return_value=vm), \
             mock.patch("psutil.cpu_percent", return_value=5), \
             mock.patch("core.executor.execute_run") as ex:
            result = tasks.execute_run_task(str(run.id))
        ex.assert_not_called()
        self.assertIn("deferred", result)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.PENDING)
        self.assertTrue(QSchedule.objects.filter(name=f"defer-{run.id}-1").exists())

from __future__ import annotations

import dataclasses
import unittest

from automation.identity import execution_key


RECIPE_ID = "restarea-hwpx-to-xls"
TASK_ID = "alpha-13452"
INPUT_HASH = "a" * 64


class ExecutionRequestTest(unittest.TestCase):
    def model(self):
        try:
            from automation.execution import ExecutionRequest
        except ModuleNotFoundError as exc:
            self.fail(f"automation.execution should exist: {exc}")
        return ExecutionRequest

    def machine(self):
        try:
            from automation.execution import ExecutionStateMachine
        except ModuleNotFoundError as exc:
            self.fail(f"automation.execution should exist: {exc}")
        return ExecutionStateMachine

    def request(self):
        return self.model().create(TASK_ID, RECIPE_ID, INPUT_HASH)

    def test_request_derives_key_and_starts_requested(self):
        request = self.request()

        self.assertEqual(request.state, "requested")
        self.assertEqual(
            request.execution_key,
            execution_key(TASK_ID, RECIPE_ID, INPUT_HASH),
        )

    def test_request_model_is_frozen(self):
        request = self.request()

        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.state = "approved"

    def test_recipe_identity_is_validated_without_global_registry_lookup(self):
        with self.assertRaises(ValueError):
            self.model().create(TASK_ID, "not allowlisted", INPUT_HASH)

    def test_runtime_scoped_recipe_id_is_accepted_by_lifecycle_dto(self):
        request = self.model().create(TASK_ID, "code-analysis-alpha", INPUT_HASH)
        self.assertEqual(request.recipe_id, "code-analysis-alpha")

    def test_requested_requires_non_blank_approval(self):
        request = self.request()

        for approval in (None, "", "   "):
            with self.subTest(approval=approval):
                with self.assertRaisesRegex(ValueError, "approval"):
                    request.transition("approved", approval=approval)

    def test_approved_execution_requires_approval_then_can_start(self):
        approved = self.request().transition(
            "approved", approval="decision:2026-09-02:jsmith"
        )

        self.assertEqual(approved.state, "approved")
        self.assertEqual(approved.approval, "decision:2026-09-02:jsmith")
        self.assertEqual(approved.transition("executing").state, "executing")

    def test_executing_can_finish_succeeded_or_failed(self):
        executing = self.request().transition("approved", approval="decision:run").transition(
            "executing"
        )

        self.assertEqual(executing.transition("succeeded").state, "succeeded")
        self.assertEqual(executing.transition("failed").state, "failed")

    def test_failed_and_succeeded_are_terminal(self):
        executing = self.request().transition("approved", approval="decision:run").transition(
            "executing"
        )

        for terminal in ("succeeded", "failed"):
            result = executing.transition(terminal)
            with self.subTest(terminal=terminal):
                for target in ("requested", "approved", "executing", "succeeded", "failed"):
                    with self.subTest(target=target):
                        with self.assertRaisesRegex(ValueError, "illegal transition"):
                            result.transition(target)

    def test_duplicate_key_is_refused_by_state_machine(self):
        machine = self.machine()()
        first = machine.request(TASK_ID, RECIPE_ID, INPUT_HASH)

        with self.assertRaisesRegex(ValueError, "duplicate"):
            machine.request(TASK_ID, RECIPE_ID, INPUT_HASH)

        self.assertEqual(machine.get(first.execution_key), first)

    def test_state_machine_records_transitions(self):
        machine = self.machine()()
        requested = machine.request(TASK_ID, RECIPE_ID, INPUT_HASH)
        approved = machine.transition(
            requested.execution_key,
            "approved",
            approval="decision:run",
        )
        executing = machine.transition(requested.execution_key, "executing")
        succeeded = machine.transition(requested.execution_key, "succeeded")

        self.assertEqual(approved.state, "approved")
        self.assertEqual(executing.state, "executing")
        self.assertEqual(succeeded.state, "succeeded")
        self.assertEqual(machine.get(requested.execution_key).state, "succeeded")

    def test_failed_execution_cannot_be_rewritten_as_succeeded(self):
        machine = self.machine()()
        request = machine.request(TASK_ID, RECIPE_ID, INPUT_HASH)
        machine.transition(request.execution_key, "approved", approval="decision:run")
        machine.transition(request.execution_key, "executing")
        machine.transition(request.execution_key, "failed")

        with self.assertRaisesRegex(ValueError, "illegal transition"):
            machine.transition(request.execution_key, "succeeded")


if __name__ == "__main__":
    unittest.main()


class ExecutionAttemptTest(unittest.TestCase):
    """The execution key is the identity of the WORK; the attempt separates
    the runs of it, so a failure is retryable without weakening idempotency."""

    def request(self, **kwargs):
        from automation.execution import ExecutionRequest

        return ExecutionRequest("alpha-13452", "restarea-hwpx-to-xls", "a" * 64, **kwargs)

    def test_an_attempt_defaults_to_one(self):
        self.assertEqual(self.request().attempt, 1)

    def test_the_key_is_stable_across_attempts(self):
        # Approvals and confirmations bind to the key, so a retry must not
        # move the work out from under them.
        self.assertEqual(self.request().execution_key, self.request(attempt=4).execution_key)

    def test_the_attempt_round_trips(self):
        from automation.execution import ExecutionRequest

        payload = self.request(attempt=3).to_dict()

        self.assertEqual(payload["attempt"], 3)
        self.assertEqual(ExecutionRequest.from_dict(payload).attempt, 3)

    def test_a_record_written_before_attempts_existed_reads_as_attempt_one(self):
        from automation.execution import ExecutionRequest

        payload = self.request().to_dict()
        payload.pop("attempt")

        self.assertEqual(ExecutionRequest.from_dict(payload).attempt, 1)

    def test_an_invalid_attempt_is_rejected(self):
        for bad in (0, -1, True, 1.0, "2"):
            with self.subTest(attempt=bad):
                with self.assertRaisesRegex(ValueError, "attempt"):
                    self.request(attempt=bad)

    def test_a_retry_is_not_refused_as_a_duplicate_key(self):
        from automation.execution import ExecutionRequest

        key = self.request().execution_key

        with self.assertRaises(Exception):
            ExecutionRequest.create(
                "alpha-13452", "restarea-hwpx-to-xls", "a" * 64, existing_execution_keys=(key,)
            )
        retry = ExecutionRequest.create(
            "alpha-13452",
            "restarea-hwpx-to-xls",
            "a" * 64,
            existing_execution_keys=(key,),
            attempt=2,
        )
        self.assertEqual(retry.attempt, 2)

    def test_rebuild_identity_is_distinct_stable_and_safe(self):
        from automation.identity import rebuild_execution_key

        first = rebuild_execution_key(TASK_ID, RECIPE_ID, INPUT_HASH, "r1")
        self.assertEqual(first, rebuild_execution_key(TASK_ID, RECIPE_ID, INPUT_HASH, "r1"))
        self.assertNotEqual(first, rebuild_execution_key(TASK_ID, RECIPE_ID, INPUT_HASH, "r2"))
        with self.assertRaises(ValueError):
            rebuild_execution_key(TASK_ID, RECIPE_ID, INPUT_HASH, "../r1")

    def test_rebuild_request_requires_later_version_and_round_trips(self):
        from automation.execution import ExecutionRequest

        with self.assertRaises(ValueError):
            ExecutionRequest(TASK_ID, RECIPE_ID, INPUT_HASH, result_version=1, rebuild_request_id="r1")
        request = ExecutionRequest(TASK_ID, RECIPE_ID, INPUT_HASH, result_version=2, rebuild_request_id="r1")
        self.assertEqual(ExecutionRequest.from_dict(request.to_dict()), request)

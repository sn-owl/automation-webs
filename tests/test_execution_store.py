import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from automation.execution import ExecutionRequest


class ExecutionStoreTest(unittest.TestCase):
    def store(self):
        from automation import execution_store

        return execution_store

    def request(self, input_hash="a" * 64):
        return ExecutionRequest.create("alpha-13452", "restarea-hwpx-to-xls", input_hash)

    # -- R04: a failed attempt is retryable, a succeeded one is not ----

    def failed(self, store, directory, request=None):
        """Reserve a request and drive it to failed, as a real run would."""
        request = request or self.request()
        store.reserve_execution(request, root=directory)
        for state, approval in (("approved", "event:d1"), ("executing", None), ("failed", None)):
            request = request.transition(state, approval=approval)
            store.put_execution(request, root=directory)
        return request

    def test_a_fresh_key_starts_at_attempt_one(self):
        store = self.store()
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(store.next_attempt("f" * 64, root=directory), 1)

    def test_a_failed_attempt_opens_the_next_one(self):
        store = self.store()
        with tempfile.TemporaryDirectory() as directory:
            failed = self.failed(store, directory)

            self.assertEqual(store.next_attempt(failed.execution_key, root=directory), 2)

    def test_a_succeeded_execution_is_not_retryable(self):
        # Repeating work that already succeeded is exactly what the execution
        # key exists to refuse.
        store = self.store()
        request = self.request()
        with tempfile.TemporaryDirectory() as directory:
            store.reserve_execution(request, root=directory)
            for state, approval in (("approved", "event:d1"), ("executing", None), ("succeeded", None)):
                request = request.transition(state, approval=approval)
                store.put_execution(request, root=directory)

            with self.assertRaises(store.ExecutionAlreadyReservedError):
                store.next_attempt(request.execution_key, root=directory)

    def test_an_in_flight_execution_is_not_retryable(self):
        store = self.store()
        request = self.request()
        with tempfile.TemporaryDirectory() as directory:
            store.reserve_execution(request, root=directory)
            for state, approval in (("approved", "event:d1"), ("executing", None)):
                request = request.transition(state, approval=approval)
                store.put_execution(request, root=directory)

            with self.assertRaises(store.ExecutionAlreadyReservedError):
                store.next_attempt(request.execution_key, root=directory)

    def test_reserving_the_next_attempt_supersedes_the_failed_record(self):
        store = self.store()
        with tempfile.TemporaryDirectory() as directory:
            failed = self.failed(store, directory)
            retry = ExecutionRequest.create(
                "alpha-13452", "restarea-hwpx-to-xls", "a" * 64, attempt=2
            )

            store.reserve_execution(retry, root=directory)

            stored = store.get_execution(failed.execution_key, root=directory)
            self.assertEqual(stored.attempt, 2)
            self.assertEqual(stored.state, "requested")
            # one record per key: the key is the identity of the work
            self.assertEqual(store.list_execution_keys(root=directory), (failed.execution_key,))

    def test_an_attempt_may_not_skip_ahead(self):
        store = self.store()
        with tempfile.TemporaryDirectory() as directory:
            self.failed(store, directory)
            leap = ExecutionRequest.create(
                "alpha-13452", "restarea-hwpx-to-xls", "a" * 64, attempt=5
            )

            with self.assertRaises(store.ExecutionAlreadyReservedError):
                store.reserve_execution(leap, root=directory)

    def test_a_retry_cannot_supersede_a_succeeded_attempt(self):
        store = self.store()
        request = self.request()
        with tempfile.TemporaryDirectory() as directory:
            store.reserve_execution(request, root=directory)
            for state, approval in (("approved", "event:d1"), ("executing", None), ("succeeded", None)):
                request = request.transition(state, approval=approval)
                store.put_execution(request, root=directory)
            retry = ExecutionRequest.create(
                "alpha-13452", "restarea-hwpx-to-xls", "a" * 64, attempt=2
            )

            with self.assertRaises(store.ExecutionAlreadyReservedError):
                store.reserve_execution(retry, root=directory)

    def test_failed_retry_write_preserves_previous_attempt(self):
        store = self.store()
        with tempfile.TemporaryDirectory() as directory:
            failed = self.failed(store, directory)
            retry = ExecutionRequest.create(
                "alpha-13452", "restarea-hwpx-to-xls", "a" * 64, attempt=2
            )
            with mock.patch.object(store.os, "open", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    store.reserve_execution(retry, root=directory)
            self.assertEqual(store.get_execution(failed.execution_key, root=directory), failed)

    def test_put_then_get_round_trips(self):
        store = self.store()
        request = self.request()
        with tempfile.TemporaryDirectory() as directory:
            store.put_execution(request, root=directory)
            loaded = store.get_execution(request.execution_key, root=directory)
        self.assertEqual(loaded.to_dict(), request.to_dict())

    def test_missing_key_raises_not_found(self):
        store = self.store()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(store.ExecutionNotStoredError):
                store.get_execution("f" * 64, root=directory)

    def test_keys_are_reserved_across_processes(self):
        # The whole point of persistence: a second run must see the first
        # run's key and refuse to treat it as a fresh request.
        store = self.store()
        request = self.request()
        with tempfile.TemporaryDirectory() as directory:
            store.put_execution(request, root=directory)
            self.assertEqual(store.list_execution_keys(root=directory), (request.execution_key,))

    def test_empty_root_lists_nothing(self):
        store = self.store()
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(store.list_execution_keys(root=Path(directory) / "absent"), ())

    def test_state_updates_replace_the_stored_record(self):
        store = self.store()
        request = self.request()
        with tempfile.TemporaryDirectory() as directory:
            store.put_execution(request, root=directory)
            approved = request.transition("approved", approval="event:abc")
            store.put_execution(approved, root=directory)
            loaded = store.get_execution(request.execution_key, root=directory)
        self.assertEqual(loaded.state, "approved")
        self.assertEqual(loaded.approval, "event:abc")

    def test_stored_file_is_named_by_execution_key(self):
        store = self.store()
        request = self.request()
        with tempfile.TemporaryDirectory() as directory:
            store.put_execution(request, root=directory)
            path = Path(directory) / "state" / "executions" / f"{request.execution_key}.json"
            self.assertTrue(path.is_file())
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["execution_key"], request.execution_key)

    def test_unsafe_execution_key_is_refused(self):
        store = self.store()
        with tempfile.TemporaryDirectory() as directory:
            for evil in ("../escape", "a/b", "", ".", "..", "a\x00b"):
                with self.subTest(key=evil):
                    with self.assertRaises(ValueError):
                        store.get_execution(evil, root=directory)

    def test_corrupt_record_is_reported_not_returned(self):
        store = self.store()
        request = self.request()
        with tempfile.TemporaryDirectory() as directory:
            store.put_execution(request, root=directory)
            path = Path(directory) / "state" / "executions" / f"{request.execution_key}.json"
            path.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(store.CorruptExecutionError):
                store.get_execution(request.execution_key, root=directory)


    def test_result_versions_are_bound_and_never_reused(self):
        store = self.store()
        with tempfile.TemporaryDirectory() as directory:
            initial_key = self.request().execution_key
            first, duplicate = store.reserve_result_version(
                "alpha-13452", execution_key=initial_key, root=directory
            )
            self.assertEqual((first, duplicate), (1, False))
            key = ExecutionRequest.create(
                "alpha-13452", "restarea-hwpx-to-xls", "a" * 64,
                result_version=2, rebuild_request_id="r1",
            ).execution_key
            second, duplicate = store.reserve_result_version(
                "alpha-13452", rebuild_request_id="r1", execution_key=key, root=directory
            )
            self.assertEqual((second, duplicate), (2, False))
            self.assertEqual(
                store.reserve_result_version(
                    "alpha-13452", rebuild_request_id="r1", execution_key=key, root=directory
                ),
                (2, True),
            )
            with self.assertRaises(store.ExecutionAlreadyReservedError):
                store.reserve_result_version(
                    "alpha-13452", rebuild_request_id="r1", execution_key="different", root=directory
                )
            third, duplicate = store.reserve_result_version(
                "alpha-13452", rebuild_request_id="r2", execution_key="other", root=directory
            )
            self.assertEqual((third, duplicate), (3, False))
    def test_result_registry_rejects_invalid_next_and_duplicate_versions(self):
        store = self.store()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "result_versions" / "alpha-13452.json"
            path.parent.mkdir(parents=True)
            key = self.request().execution_key
            path.write_text(json.dumps({
                "next_version": True,
                "requests": {"initial": {"version": 1, "execution_key": key}},
            }), encoding="utf-8")
            with self.assertRaises(store.CorruptExecutionError):
                store.reserve_result_version("alpha-13452", execution_key=key, root=directory)

            path.write_text(json.dumps({
                "next_version": 3,
                "requests": {
                    "initial": {"version": 1, "execution_key": key},
                    "rebuild:r1": {"version": 1, "execution_key": "other"},
                },
            }), encoding="utf-8")
            with self.assertRaises(store.CorruptExecutionError):
                store.reserve_result_version("alpha-13452", execution_key=key, root=directory)
if __name__ == "__main__":
    unittest.main()

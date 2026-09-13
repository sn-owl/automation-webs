import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import taskctl
from automation.models import AttachmentRef, SourceRef, WorkItem
from automation.task_store import put_task
from automation.dispatch import capability_binding
from automation import execution_service
from automation.recipes import get_recipe, recipe_digest


def install_active_recipe(root: Path, recipe_id: str = "restarea-hwpx-to-xls") -> None:
    recipe = get_recipe(recipe_id)
    registry = root / "state" / "recipes" / recipe.scope / recipe_id
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"{recipe.version}.json").write_text(
        json.dumps(recipe.to_dict()),
        encoding="utf-8",
    )
    (registry / "active.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scope": recipe.scope,
                "recipe_id": recipe_id,
                "version": recipe.version,
                "definition_sha256": recipe_digest(recipe),
                "capability": capability_binding(recipe),
                "activation": {
                    "actor": "test",
                    "reason": "isolated execution contract",
                    "activated_at": "2026-09-10T00:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )


RECIPE = "restarea-hwpx-to-xls"


def synthetic_hwpx_bytes() -> bytes:
    """Build the smallest valid HWPX consumed by the restarea preflight."""
    def cell(column: int, text: str) -> str:
        return (
            f'<tc><cellAddr colAddr="{column}"/><subList>'
            f"<p><run><t>{text}</t></run></p></subList></tc>"
        )

    headers = ("연번", "해당동", "시설명", "주소", "운영요일", "운영시간", "대표전화")
    header = "".join(cell(index, value) for index, value in enumerate(headers))
    row = "".join(
        cell(index, value)
        for index, value in enumerate(
            ("1", "테스트동", "테스트 쉼터", "테스트 주소", "월~금", "09:00~18:00", "000-000-0000")
        )
    )
    section = (
        '<sec xmlns="http://www.hancom.co.kr/hwpml/2011/section">'
        f'<tbl colCnt="7"><tr>{header}</tr><tr>{row}</tr></tbl></sec>'
    ).encode("utf-8")

    info = zipfile.ZipInfo("Contents/section0.xml", date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(info, section)
    return package.getvalue()


def work_item(attachment_path):
    return WorkItem(
        task_id="yeonje-13452",
        source=SourceRef(
            type="gnuboard",
            id="yeonje",
            external_id="13452",
            url="https://example.invalid/board/13452",
        ),
        received_at="2026-08-31T00:00:00+00:00",
        title="무더위쉼터 현행화 신청서",
        body="첨부파일을 변환합니다.",
        author="PERSON_001",
        attachments=(
            AttachmentRef(
                name="restarea.hwpx",
                type="hwpx",
                raw_ref=str(attachment_path),
                extracted_ref="normalized/yeonje-13452/restarea.json",
            ),
        ),
        mask_table_ref="local://masks/yeonje-13452.json",
    )


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = taskctl.main(argv)
    return code, out.getvalue(), err.getvalue()


def events(root):
    path = Path(root) / "state" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stub_executor(calls, *, artifacts=None):
    def executor(item, artifact_dir, *, seen_execution_keys=(), **_kwargs):
        calls.append(tuple(seen_execution_keys))
        directory = Path(artifact_dir)
        directory.mkdir(parents=True, exist_ok=True)
        produced = []
        for name in artifacts or ["result.xls"]:
            path = directory / name
            path.write_bytes(b"XLS")
            produced.append({"path": str(path), "sha256": "d" * 64})
        return {
            "task_id": item.task_id,
            "recipe_id": RECIPE,
            "execution_key": "stub-key",
            "status": "succeeded",
            "duplicate": False,
            "artifacts": produced,
        }

    return executor


class ExecuteCommandTest(unittest.TestCase):

    def test_recovery_rejects_blank_actor_or_reason_at_core_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "recovery actor is required"):
                execution_service.recover_execution(
                    "missing", actor="", reason="reason", root=root
                )
            with self.assertRaisesRegex(ValueError, "recovery reason is required"):
                execution_service.recover_execution(
                    "missing", actor="operator", reason="", root=root
                )
            code, _, error = run(
                [
                    "recover",
                    "missing",
                    "--actor",
                    "",
                    "--reason",
                    "reason",
                    "--root",
                    str(root),
                ]
            )
            self.assertNotEqual(code, 0)
            self.assertIn("recovery actor is required", error)
    @contextlib.contextmanager
    def prepared(self, *, approve=True):
        from automation import dispatch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "restarea.hwpx"
            source.write_bytes(synthetic_hwpx_bytes())
            put_task(work_item(source), root=root)
            install_active_recipe(root)
            if approve:
                run(["approve", "yeonje-13452", "--actor", "r", "--reason", "ok", "--recipe", RECIPE, "--root", str(root)])
            self.calls = []
            with mock.patch.dict(
                dispatch.EXECUTOR_DISPATCH,
                {"restarea-converter": stub_executor(self.calls)},
                clear=False,
            ), mock.patch.object(
                execution_service,
                "verify_recipe_output",
                return_value={
                    "verification_version": "restarea-xls-v1",
                    "status": "passed",
                    "checks": [],
                    "handoff": {"required": True, "status": "pending"},
                },
            ):
                yield root

    def test_approved_execution_runs_the_recipe_and_writes_artifacts(self):
        with self.prepared() as root:
            code, out, err = run(
                ["execute", "yeonje-13452", "--recipe", RECIPE,                  "--root", str(root)]
            )
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["task_id"], "yeonje-13452")
            self.assertEqual(payload["recipe_id"], RECIPE)
            self.assertEqual(payload["state"], "succeeded")
            self.assertEqual(payload["result_version"], 1)
            self.assertEqual(payload["attempt"], 1)
            self.assertIn("result-1", payload["artifacts"][0]["path"])

    def test_execution_events_enqueue_profile_notifications(self):
        from automation.notification_store import NotificationProfile, NotificationStore

        with self.prepared() as root:
            NotificationStore(root).save_profile(
                NotificationProfile(profile_id="execution", channels=("local",))
            )
            code, _, err = run(
                ["execute", "yeonje-13452", "--recipe", RECIPE, "--root", str(root)]
            )
            self.assertEqual(code, 0, err)

            records = NotificationStore(root).read_deliveries()

        observed = {(record["event"], record["status"]) for record in records}
        self.assertIn(("preparation_started", "queued"), observed)
        self.assertIn(("preparation_completed", "queued"), observed)

    def test_execution_request_is_persisted_in_a_terminal_state(self):
        from automation import execution_store

        with self.prepared() as root:
            run(["execute", "yeonje-13452", "--recipe", RECIPE,                  "--root", str(root)])
            keys = execution_store.list_execution_keys(root=root)
            self.assertEqual(len(keys), 1)
            stored = execution_store.get_execution(keys[0], root=root)
        self.assertEqual(stored.state, "succeeded")
        self.assertTrue(stored.approval)

    def test_rerunning_the_same_execution_key_does_not_execute_twice(self):
        with self.prepared() as root:
            argv = ["execute", "yeonje-13452", "--recipe", RECIPE,                     "--root", str(root)]
            first, _, _ = run(argv)
            second, out, err = run(argv)
        self.assertEqual(first, 0)
        self.assertNotEqual(second, 0, out)
        self.assertEqual(len(self.calls), 1)


    def test_explicit_rebuild_is_versioned_and_replay_is_idempotent(self):
        from automation import execution_store

        with self.prepared() as root:
            first, out, err = run(["execute", "yeonje-13452", "--recipe", RECIPE, "--root", str(root)])
            self.assertEqual(first, 0, err)
            initial = json.loads(out)
            rebuild = ["rebuild", "yeonje-13452", "--recipe", RECIPE,
                       "--request-id", "r1", "--actor", "tester", "--reason", "new", "--root", str(root)]
            second, out, err = run(rebuild)
            self.assertEqual(second, 0, err)
            version_two = json.loads(out)
            self.assertEqual(version_two["result_version"], 2)
            self.assertNotEqual(version_two["execution_key"], initial["execution_key"])
            count = len(self.calls)
            replay, out, err = run(rebuild)
            self.assertEqual(replay, 0, err)
            self.assertEqual(len(self.calls), count)
            self.assertEqual(len(execution_store.list_execution_keys(root=root)), 2)

    def test_rebuild_requires_a_succeeded_initial_result(self):
        with self.prepared() as root:
            code, _, err = run(
                ["rebuild", "yeonje-13452", "--recipe", RECIPE,
                 "--request-id", "r1", "--actor", "tester", "--reason", "new",
                 "--root", str(root)]
            )
            self.assertNotEqual(code, 0)
            self.assertIn("initial execution must succeed first", err)
            self.assertEqual(self.calls, [])

    def test_failed_rebuild_is_quarantined_and_retry_keeps_version(self):
        from automation import execution_store

        with self.prepared() as root:
            run(["execute", "yeonje-13452", "--recipe", RECIPE, "--root", str(root)])
            rebuild = ["rebuild", "yeonje-13452", "--recipe", RECIPE,
                       "--request-id", "r1", "--actor", "tester", "--reason", "new", "--root", str(root)]
            with mock.patch("automation.execution_service.verify_recipe_output", return_value={"status": "failed", "checks": []}):
                failed, _, _ = run(rebuild)
            self.assertNotEqual(failed, 0)
            quarantine = root / "artifacts" / "quarantine" / "yeonje-13452" / "result-2" / "attempt-1"
            self.assertTrue(quarantine.is_dir())
            succeeded, out, err = run(rebuild)
            self.assertEqual(succeeded, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["result_version"], 2)
            self.assertEqual(payload["attempt"], 2)
            next_code, next_out, next_err = run(
                ["rebuild", "yeonje-13452", "--recipe", RECIPE, "--request-id", "r2",
                 "--actor", "tester", "--reason", "next", "--root", str(root)]
            )
            self.assertEqual(next_code, 0, next_err)
            self.assertEqual(json.loads(next_out)["result_version"], 3)
            self.assertEqual(len(execution_store.list_execution_keys(root=root)), 3)
    def test_input_hash_that_disagrees_with_the_attachment_is_rejected(self):
        # --input-hash is an optional assertion; a value that does not match the
        # real attachment bytes blocks execution before the executor is reached,
        # so it cannot be used to forge a fresh execution key and replay.
        with self.prepared() as root:
            code, _, err = run(
                ["execute", "yeonje-13452", "--recipe", RECIPE,
                 "--input-hash", "b" * 64, "--root", str(root)]
            )
        self.assertNotEqual(code, 0)
        self.assertIn("does not match", err)
        self.assertEqual(self.calls, [])

    def test_approval_bound_to_old_input_never_reaches_executor(self):
        with self.prepared() as root:
            source = root / "restarea.hwpx"
            source.write_bytes(source.read_bytes() + b"changed-after-approval")
            code, _, err = run(
                ["execute", "yeonje-13452", "--recipe", RECIPE, "--root", str(root)]
            )
        self.assertNotEqual(code, 0)
        self.assertIn("approval does not match", err)
        self.assertEqual(self.calls, [])
        self.assertFalse((root / "state" / "executions").exists())

    def test_stale_revision_approval_never_reaches_executor(self):
        # A09: an approval bound to revision 1 must not execute against a later
        # revision that changed title/body while reusing the same attachment
        # (so the attachment input_hash is unchanged).
        from automation import dispatch

        def v2(src, *, title, body, revision):
            return WorkItem.from_dict({
                "task_id": "yeonje-13452",
                "source": {"type": "gnuboard", "id": "yeonje", "external_id": "13452",
                           "url": "https://example.invalid/board/13452"},
                "received_at": "2026-08-31T00:00:00+00:00", "title": title, "body": body,
                "author": "PERSON_001",
                "attachments": [{"name": "restarea.hwpx", "type": "hwpx", "raw_ref": str(src),
                                 "extracted_ref": "normalized/yeonje-13452/restarea.json"}],
                "mask_table_ref": "local://masks/yeonje-13452.json", "contract_version": 2,
                "scope": "public", "connector_id": "c1", "capture_id": "gnuboard-13452-abc",
                "revision": revision, "completeness": "complete",
                "provenance": {"policy_version": "1"}, "source_completion_observed": False,
            })

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "restarea.hwpx"
            source.write_bytes(synthetic_hwpx_bytes())
            put_task(v2(source, title="원본 제목", body="첨부파일을 변환합니다.", revision=1), root=root)
            install_active_recipe(root)
            run(["approve", "yeonje-13452", "--actor", "r", "--reason", "ok", "--recipe", RECIPE, "--root", str(root)])
            put_task(v2(source, title="바뀐 제목", body="완전히 다른 본문", revision=2), root=root)
            calls = []
            with mock.patch.dict(
                dispatch.EXECUTOR_DISPATCH, {"restarea-converter": stub_executor(calls)}, clear=False
            ), mock.patch.object(execution_service, "verify_recipe_output", return_value={
                "verification_version": "x", "status": "passed", "checks": [],
                "handoff": {"required": True, "status": "pending"}}):
                code, _, err = run(["execute", "yeonje-13452", "--recipe", RECIPE, "--root", str(root)])
        self.assertNotEqual(code, 0)
        self.assertIn("no recorded approval", err)
        self.assertEqual(calls, [])

    def test_execution_without_approval_never_reaches_the_executor(self):
        with self.prepared(approve=False) as root:
            code, _, err = run(
                ["execute", "yeonje-13452", "--recipe", RECIPE,                  "--root", str(root)]
            )
        self.assertNotEqual(code, 0)
        self.assertIn("no recorded approval", err)
        self.assertEqual(self.calls, [])

    def test_revoked_approval_never_reaches_the_executor(self):
        with self.prepared() as root:
            run(["defer", "yeonje-13452", "--actor", "r", "--reason", "미룸", "--root", str(root)])
            code, _, err = run(
                ["execute", "yeonje-13452", "--recipe", RECIPE,                  "--root", str(root)]
            )
        self.assertNotEqual(code, 0)
        self.assertEqual(self.calls, [])

    def test_unregistered_recipe_never_reaches_the_executor(self):
        with self.prepared() as root:
            code, _, err = run(
                ["execute", "yeonje-13452", "--recipe", "not-registered",
                 "--input-hash", "a" * 64, "--root", str(root)]
            )
        self.assertNotEqual(code, 0)
        self.assertIn("unknown recipe", err)
        self.assertEqual(self.calls, [])


    def test_revoke_between_final_validation_and_dispatch_blocks_executor(self):
        with self.prepared() as root:
            def revoke_during_claim_validation(recipe):
                run(["defer", "yeonje-13452", "--actor", "r", "--reason", "revoked", "--root", str(root)])

            with mock.patch.object(
                execution_service.dispatch_core,
                "validate_ai_execution_policy",
                side_effect=revoke_during_claim_validation,
            ):
                code, _, error = run(
                    ["execute", "yeonje-13452", "--recipe", RECIPE, "--root", str(root)]
                )
            recorded = events(root)
            self.assertTrue(
                any(event.get("type") == "decision" and event["decision"]["action"] == "defer" for event in recorded)
            )
        self.assertNotEqual(code, 0)
        self.assertIn("approval was revoked or superseded", error)
        self.assertEqual(self.calls, [])

    def test_revoke_after_claim_does_not_cancel_started_dispatch(self):
        from automation.dispatch import DispatchError

        with self.prepared() as root:
            def revoke_then_fail(*args, **kwargs):
                run(["defer", "yeonje-13452", "--actor", "r", "--reason", "after claim", "--root", str(root)])
                raise DispatchError("claimed executor failure")

            with mock.patch.object(execution_service, "dispatch_recipe", side_effect=revoke_then_fail):
                code, _, error = run(
                    ["execute", "yeonje-13452", "--recipe", RECIPE, "--root", str(root)]
                )
            recorded = [event for event in events(root) if event.get("type") == "execution"]
            claim_events = [event for event in events(root) if event.get("type") == "execution_claim"]
        self.assertNotEqual(code, 0)
        self.assertIn("execute failed", error)
        self.assertEqual(len(claim_events), 1)
        self.assertEqual([event["state"] for event in recorded[-1:]], ["failed"])
        self.assertEqual(self.calls, [])
    def test_events_record_the_execution_lifecycle(self):
        with self.prepared() as root:
            run(["execute", "yeonje-13452", "--recipe", RECIPE,                  "--root", str(root)])
            recorded = [event for event in events(root) if event.get("type") == "execution"]
        self.assertTrue(recorded)
        self.assertEqual(recorded[-1]["state"], "succeeded")
        self.assertEqual(recorded[-1]["task_id"], "yeonje-13452")

    def test_executor_failure_is_recorded_as_failed_without_detail(self):
        from automation import dispatch

        def explode(item, artifact_dir, *, seen_execution_keys=()):
            raise RuntimeError("SENSITIVE_CONVERTER_TEXT")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "restarea.hwpx"
            source.write_bytes(synthetic_hwpx_bytes())
            put_task(work_item(source), root=root)
            install_active_recipe(root)
            run(["approve", "yeonje-13452", "--actor", "r", "--reason", "ok", "--recipe", RECIPE, "--root", str(root)])
            with mock.patch.dict(
                dispatch.EXECUTOR_DISPATCH, {"restarea-converter": explode}, clear=False
            ):
                code, _, err = run(
                    ["execute", "yeonje-13452", "--recipe", RECIPE,                      "--root", str(root)]
                )
            from automation import execution_store

            keys = execution_store.list_execution_keys(root=root)
            stored = execution_store.get_execution(keys[0], root=root)
            recorded = [event for event in events(root) if event.get("type") == "execution"]

        self.assertNotEqual(code, 0)
        self.assertNotIn("SENSITIVE_CONVERTER_TEXT", err)
        self.assertEqual(stored.state, "failed")
        self.assertEqual(recorded[-1]["state"], "failed")

    # -- R04: a failed run is retryable, a succeeded one is not --------

    def test_a_failed_execution_can_be_run_again_once_the_cause_is_fixed(self):
        # The execution key is the identity of the WORK, so a failure used to
        # block that input forever with "duplicate execution key".
        from automation import dispatch, execution_store

        def explode(item, artifact_dir, **kwargs):
            raise RuntimeError("transient")

        with self.prepared() as root:
            argv = ["execute", "yeonje-13452", "--recipe", RECIPE, "--root", str(root)]
            with mock.patch.dict(
                dispatch.EXECUTOR_DISPATCH, {"restarea-converter": explode}, clear=False
            ):
                failed_code, _, _ = run(argv)
            retry_code, out, err = run(argv)

            key = execution_store.list_execution_keys(root=root)[0]
            stored = execution_store.get_execution(key, root=root)

        self.assertNotEqual(failed_code, 0)
        self.assertEqual(retry_code, 0, err)
        self.assertEqual(json.loads(out)["state"], "succeeded")
        self.assertEqual(stored.attempt, 2)
        self.assertEqual(stored.state, "succeeded")

    def test_a_succeeded_execution_still_refuses_to_run_again(self):
        from automation import execution_store

        with self.prepared() as root:
            argv = ["execute", "yeonje-13452", "--recipe", RECIPE, "--root", str(root)]
            run(argv)
            second, _, err = run(argv)
            stored = execution_store.get_execution(
                execution_store.list_execution_keys(root=root)[0], root=root
            )

        self.assertNotEqual(second, 0)
        self.assertEqual(stored.attempt, 1)
        self.assertEqual(len(self.calls), 1)

    def test_each_attempt_keeps_its_artifacts_apart(self):
        # A retry must not collide with whatever a previous attempt wrote
        # before it failed, and a failed attempt's output must not be mistaken
        # for the run that actually succeeded.
        from automation import dispatch

        def half_written(item, artifact_dir, **kwargs):
            Path(artifact_dir).mkdir(parents=True, exist_ok=True)
            (Path(artifact_dir) / "partial.xls").write_bytes(b"incomplete")
            raise RuntimeError("died after writing")

        with self.prepared() as root:
            argv = ["execute", "yeonje-13452", "--recipe", RECIPE, "--root", str(root)]
            with mock.patch.dict(
                dispatch.EXECUTOR_DISPATCH, {"restarea-converter": half_written}, clear=False
            ):
                run(argv)
            code, out, err = run(argv)

            normal_root = root / "artifacts" / "yeonje-13452" / "result-1"
            attempts = sorted(path.name for path in normal_root.iterdir())
            partial_kept = (
                root / "artifacts" / "quarantine" / "yeonje-13452" / "result-1" / "attempt-1" / "partial.xls"
            ).is_file()
            good = Path(json.loads(out)["artifacts"][0]["path"])

        self.assertEqual(code, 0, err)
        self.assertEqual(attempts, ["attempt-2"])
        self.assertTrue(partial_kept)
        self.assertEqual(good.parent.name, "attempt-2")

    def test_every_attempt_stays_in_the_event_history(self):
        from automation import dispatch

        def explode(item, artifact_dir, **kwargs):
            raise RuntimeError("transient")

        with self.prepared() as root:
            argv = ["execute", "yeonje-13452", "--recipe", RECIPE, "--root", str(root)]
            with mock.patch.dict(
                dispatch.EXECUTOR_DISPATCH, {"restarea-converter": explode}, clear=False
            ):
                run(argv)
            run(argv)
            recorded = [
                (event["execution"]["attempt"], event["state"])
                for event in events(root)
                if event.get("type") == "execution"
            ]

    def test_verifier_exception_is_unverifiable_and_quarantined(self):
        with self.prepared() as root:
            with mock.patch.object(
                execution_service,
                "verify_recipe_output",
                side_effect=RuntimeError("private verifier detail"),
            ):
                code, _, error = run(
                    ["execute", "yeonje-13452", "--recipe", RECIPE, "--root", str(root)]
                )
            quarantine = (
                root
                / "artifacts"
                / "quarantine"
                / "yeonje-13452"
                / "result-1"
                / "attempt-1"
            )
            quarantine_exists = quarantine.is_dir()
            verification = [
                event["verification"]
                for event in events(root)
                if event.get("type") == "verification"
            ]
        self.assertNotEqual(code, 0)
        self.assertIn("execute verification unverifiable", error)
        self.assertTrue(quarantine_exists)
        self.assertEqual(verification[-1]["status"], "unverifiable")

    def test_verification_failure_retry_can_be_confirmed(self):
        with self.prepared() as root:
            argv = ["execute", "yeonje-13452", "--recipe", RECIPE, "--root", str(root)]
            with mock.patch.object(execution_service, "verify_recipe_output", return_value={
                "status": "failed", "checks": [],
                "handoff": {"required": True, "status": "pending"},
            }):
                first, _, _ = run(argv)
            retry, out, err = run(argv)
            self.assertNotEqual(first, 0)
            self.assertEqual(retry, 0, err)
            key = json.loads(out)["execution_key"]
            confirmed, _, err = run([
                "confirm", "yeonje-13452", "--execution-key", key, "--task-version", "1",
                "--actor", "reviewer", "--reason", "checked", "--root", str(root),
            ])
            self.assertEqual(confirmed, 0, err)
            verifications = [
                (e["execution"]["attempt"], e["verification"]["status"])
                for e in events(root) if e.get("type") == "verification"
            ]
            self.assertEqual(verifications, [(1, "failed"), (2, "passed"), (2, "passed")])

    def test_artifacts_land_under_the_state_root(self):
        with self.prepared() as root:
            code, out, _ = run(
                ["execute", "yeonje-13452", "--recipe", RECIPE,                  "--root", str(root)]
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
        for artifact in payload["artifacts"]:
            with self.subTest(path=artifact["path"]):
                self.assertTrue((root / artifact["path"]).resolve().is_relative_to(root.resolve()))


if __name__ == "__main__":
    unittest.main()

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).parents[1]


class DispatchAllowListTest(unittest.TestCase):
    def module(self):
        from automation import dispatch

        return dispatch

    def test_every_dispatch_key_is_an_allow_listed_executor(self):
        dispatch = self.module()
        from automation.recipes import EXECUTORS

        for key in dispatch.EXECUTOR_DISPATCH:
            with self.subTest(executor=key):
                self.assertIn(key, EXECUTORS)

    def test_every_registered_recipe_has_a_dispatchable_executor(self):
        dispatch = self.module()
        from automation.recipes import load_recipes

        for recipe_id, recipe in load_recipes().items():
            with self.subTest(recipe=recipe_id):
                self.assertIn(recipe.executor, dispatch.EXECUTOR_DISPATCH)

    def test_unknown_recipe_is_refused_before_any_executor_runs(self):
        dispatch = self.module()
        with TemporaryDirectory() as directory:
            with self.assertRaises(dispatch.DispatchError):
                dispatch.dispatch_recipe("not-registered", object(), directory)



class AIPolicyGateTest(unittest.TestCase):
    def recipe(self, ai_policy):
        from automation.recipes import RecipeDefinition

        return RecipeDefinition.from_dict(
            {
                "recipe_id": "synthetic-ai-policy",
                "description": "synthetic AI policy",
                "executor": "excel-table",
                "input_type": "xlsx",
                "output_type": "xlsx",
                "scope": "synthetic",
                "ai_policy": ai_policy,
            }
        )

    def test_call_limit_exhaustion_is_distinct_and_fail_closed(self):
        from automation import dispatch

        with self.assertRaisesRegex(
            dispatch.AIPolicyError,
            "declared AI call limit is below expected calls",
        ) as caught:
            dispatch.validate_ai_execution_policy(
                self.recipe(
                    {
                        "enabled": True,
                        "expected_calls": 2,
                        "max_calls": 1,
                        "model_projection": "attachment_free",
                    }
                )
            )

        self.assertEqual(caught.exception.code, "ai_call_limit_exceeded")
        self.assertEqual(caught.exception.details["expected_calls"], 2)
        self.assertEqual(caught.exception.details["max_calls"], 1)

    def test_declared_ai_without_reviewed_capability_is_not_a_fallback(self):
        from automation import dispatch

        with self.assertRaisesRegex(
            dispatch.AIPolicyError,
            "reviewed AI execution capability is unavailable",
        ) as caught:
            dispatch.validate_ai_execution_policy(
                self.recipe(
                    {
                        "enabled": True,
                        "expected_calls": 1,
                        "max_calls": 1,
                        "model_projection": "attachment_free",
                    }
                )
            )

        self.assertEqual(caught.exception.code, "ai_capability_unavailable")


class DispatchExecutionTest(unittest.TestCase):
    def module(self):
        from automation import dispatch

        return dispatch

    def work_item(self, source):
        from automation.models import AttachmentRef, SourceRef, WorkItem

        return WorkItem(
            task_id="alpha-13452",
            source=SourceRef(
                type="gnuboard",
                id="alpha",
                external_id="13452",
                url="https://example.invalid/board/13452",
            ),
            received_at="2026-08-31T00:00:00+00:00",
            title="무더위쉼터 현행화 신청서",
            body="첨부파일을 변환합니다.",
            author="PERSON_001",
            attachments=(
                AttachmentRef(
                    name=source.name,
                    type="hwpx",
                    raw_ref=str(source),
                    extracted_ref="normalized/alpha-13452/restarea.json",
                ),
            ),
            mask_table_ref="local://masks/alpha-13452.json",
        )

    @staticmethod
    def _excel_available() -> bool:
        try:
            import win32com.client
        except Exception:
            return False
        try:
            excel = win32com.client.DispatchEx("Excel.Application")
        except Exception:
            return False
        try:
            excel.Quit()
        except Exception:
            pass
        return True



    def test_executor_failure_is_reported_without_underlying_detail(self):
        from unittest import mock

        dispatch = self.module()

        def explode(item, artifact_dir, **kwargs):
            raise RuntimeError("SENSITIVE_DOCUMENT_TEXT")

        with TemporaryDirectory() as directory:
            with mock.patch.dict(
                dispatch.EXECUTOR_DISPATCH, {"restarea-converter": explode}, clear=False
            ):
                with self.assertRaises(dispatch.DispatchError) as caught:
                    dispatch.dispatch_recipe(
                        "restarea-hwpx-to-xls", "work-item", Path(directory)
                    )
        self.assertNotIn("SENSITIVE_DOCUMENT_TEXT", str(caught.exception))

    # -- F3: a failure must still say where to look --------------------

    def failing_dispatch(self, explode, recipe_id="restarea-hwpx-to-xls"):
        from unittest import mock

        dispatch = self.module()
        with TemporaryDirectory() as directory:
            with mock.patch.dict(
                dispatch.EXECUTOR_DISPATCH, {"restarea-converter": explode}, clear=False
            ):
                with self.assertRaises(dispatch.DispatchError) as caught:
                    dispatch.dispatch_recipe(recipe_id, "work-item", Path(directory))
        return caught.exception

    def test_an_executor_failure_carries_a_diagnosis(self):
        def explode(item, artifact_dir, **kwargs):
            raise RuntimeError("SENSITIVE_DOCUMENT_TEXT")

        diagnosis = self.failing_dispatch(explode).diagnosis

        self.assertEqual(diagnosis["stage"], "executor")
        self.assertEqual(diagnosis["cause_chain"], ["RuntimeError"])
        self.assertTrue(diagnosis["origin"].endswith(".py:" + diagnosis["origin"].rsplit(":", 1)[1]))

    def test_the_diagnosis_never_carries_exception_text(self):
        def explode(item, artifact_dir, **kwargs):
            raise RuntimeError("SENSITIVE_DOCUMENT_TEXT")

        self.assertNotIn(
            "SENSITIVE_DOCUMENT_TEXT", json.dumps(self.failing_dispatch(explode).diagnosis)
        )

    def test_the_diagnosis_records_the_whole_cause_chain(self):
        def explode(item, artifact_dir, **kwargs):
            try:
                raise ValueError("inner")
            except ValueError as inner:
                raise RuntimeError("outer") from inner

        self.assertEqual(
            self.failing_dispatch(explode).diagnosis["cause_chain"],
            ["RuntimeError", "ValueError"],
        )

    def test_the_stage_distinguishes_lookup_from_execution(self):
        dispatch = self.module()
        with TemporaryDirectory() as directory:
            with self.assertRaises(dispatch.DispatchError) as caught:
                dispatch.dispatch_recipe("no-such-recipe", "work-item", Path(directory))

        self.assertEqual(caught.exception.diagnosis["stage"], "recipe_lookup")

    def test_a_synthetic_frame_is_not_reported_as_repository_code(self):
        # COM proxies report a frame named "<COMObject ...>", which still
        # resolves against the working directory. The origin must point at a
        # file the reader can actually open.
        dispatch = self.module()

        def explode(item, artifact_dir, **kwargs):
            code = compile("raise RuntimeError('x')", "<COMObject <unknown>>", "exec")
            exec(code, {})

        origin = self.failing_dispatch(explode).diagnosis["origin"]

        self.assertNotIn("COMObject", origin)
        self.assertTrue(Path(ROOT / origin.rsplit(":", 1)[0]).is_file(), origin)

    @unittest.skipUnless(
        _excel_available.__func__(), "Microsoft Excel COM is unavailable"
    )
    def test_registered_recipe_produces_a_contained_artifact(self):
        from tests.test_restarea_executor import _fixture_bytes

        dispatch = self.module()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "restarea.hwpx"
            source.write_bytes(_fixture_bytes())
            artifacts = root / "artifacts"
            result = dispatch.dispatch_recipe(
                "restarea-hwpx-to-xls", self.work_item(source), artifacts
            )
            self.assertEqual(result["status"], "succeeded")
            self.assertTrue(result["artifacts"])
            for artifact in result["artifacts"]:
                produced = Path(artifact["path"])
                self.assertTrue(produced.is_file())
                self.assertTrue(produced.resolve().is_relative_to(artifacts.resolve()))

    @unittest.skipUnless(
        _excel_available.__func__(), "Microsoft Excel COM is unavailable"
    )
    def test_missing_attachment_file_fails_without_partial_artifacts(self):
        dispatch = self.module()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / "artifacts"
            with self.assertRaises(dispatch.DispatchError):
                dispatch.dispatch_recipe(
                    "restarea-hwpx-to-xls", self.work_item(root / "absent.hwpx"), artifacts
                )
            self.assertFalse(any(artifacts.glob("*.xls")) if artifacts.exists() else False)


if __name__ == "__main__":
    unittest.main()

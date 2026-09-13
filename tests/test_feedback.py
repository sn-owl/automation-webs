import tempfile
import unittest
from pathlib import Path

from automation import candidates
from automation.feedback import list_feedback, record_feedback
from automation.task_store import put_task
from tests.test_candidates import CORRECTION, OTHER_SCOPE, SCOPE, work_item


CONFIRMED = ("beta-9906", "beta-9907", "beta-9908")


class FeedbackContractTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for task_id, title in [
            ("beta-9906", "재수정 요청 _ N잡 지원센터 플랫폼 관련"),
            ("beta-9907", "재수정 요청 _ 통계 화면"),
            ("beta-9908", "재수정 요청 _ 신청서 항목"),
            ("beta-1", "대표 팝업존 추가"),
            ("beta-2", "주요뉴스 이동"),
            ("beta-3", "무관한 요청"),
        ]:
            put_task(work_item(task_id, title), root=self.root)

    def correct(self, task_id, **overrides):
        kwargs = dict(
            scope=SCOPE,
            task_revision=1,
            actor="owner",
            reason="분류 교정",
            details={"classification": CORRECTION},
            root=self.root,
        )
        kwargs.update(overrides)
        return record_feedback(task_id, "classification_correction", **kwargs)

    def test_typed_feedback_is_queryable_and_replayed_as_candidate_evidence(self):
        for task_id in CONFIRMED:
            event = self.correct(task_id)
            self.assertEqual(event["feedback"]["type"], "classification_correction")

        self.assertEqual(len(list_feedback(root=self.root)), 3)

        pattern = candidates.propose(SCOPE, ["재수정"], root=self.root)
        replay = candidates.review(SCOPE, pattern.pattern_id, root=self.root)
        self.assertEqual(replay["report"]["distinct"], 6)
        self.assertEqual(replay["report"]["match"], 3)
        self.assertTrue(replay["recommendation"]["recommend"])

    def test_feedback_records_the_scope_and_current_revision_binding(self):
        payload = self.correct("beta-9906")["feedback"]

        self.assertEqual(payload["scope"], SCOPE)
        self.assertEqual(payload["task_revision"], 1)
        self.assertEqual(len(payload["revision_digest"]), 64)

    def test_feedback_for_another_scope_is_refused(self):
        with self.assertRaisesRegex(ValueError, "scope"):
            self.correct("beta-9906", scope=OTHER_SCOPE)

    def test_feedback_for_a_stale_task_revision_is_refused(self):
        with self.assertRaisesRegex(ValueError, "revision"):
            self.correct("beta-9906", task_revision=2)

    def test_list_feedback_can_be_narrowed_to_one_scope(self):
        self.correct("beta-9906")

        self.assertEqual(len(list_feedback(scope=SCOPE, root=self.root)), 1)
        self.assertEqual(list_feedback(scope=OTHER_SCOPE, root=self.root), [])

    def test_execution_feedback_requires_linked_execution(self):
        with self.assertRaises(ValueError):
            record_feedback(
                "beta-9906",
                "execution_defect",
                scope=SCOPE,
                task_revision=1,
                actor="owner",
                reason="실행 결함",
                root=self.root,
            )


if __name__ == "__main__":
    unittest.main()

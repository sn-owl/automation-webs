import json
import shutil
import tempfile
import unittest
from pathlib import Path

from automation import candidates
from automation.candidates import CandidateError, InsufficientConfirmationsError
from automation.events import EventLog
from automation.models import WorkItem
from automation.rules import classify_with_rules, load_rules
from automation.task_store import put_task

ROOT = Path(__file__).parents[1]

SCOPE = "legacy"
OTHER_SCOPE = "owner-b"

CORRECTION = {
    "responsibility": "development",
    "task_type": "feature_bug",
    "size": "unclear",
    "ownership": "내 업무",
    "primary_role": "development",
    "responsibility_scope": "기존 개발 건 오류 수정",
    "collaboration": [],
    "risk_flags": ["코드 변경"],
    "confidence": 0.8,
    "evidence": ["재수정 요청은 기존 개발건의 오류 수정"],
    "next_action": "수정 범위를 확인하세요.",
    "pattern_match": {},
    "unmatched_aspects": [],
}


def work_item(task_id: str, title: str, *, scope: str = SCOPE) -> WorkItem:
    return WorkItem.from_dict(
        {
            "task_id": task_id,
            "scope": scope,
            "source": {
                "type": "board",
                "id": "bsbukgu",
                "external_id": task_id.rsplit("-", 1)[-1],
                "url": "https://fixture.local/task",
            },
            "received_at": "2026-09-04T09:00:00+09:00",
            "title": title,
            "body": "본문",
            "author": "요청부서A",
            "attachments": [],
            "mask_table_ref": f"local://masks/{task_id}.json",
            "contract_version": 2,
            "connector_id": "c1",
            "capture_id": f"bsbukgu-{task_id}-abc",
            "revision": 1,
            "completeness": "complete",
            "provenance": {"policy_version": "1"},
            "source_completion_observed": False,
        }
    )


def decision(task_id: str, *, details: dict | None) -> dict:
    payload = {
        "actor": "owner",
        "task_id": task_id,
        "task_version": 1,
        "action": "modify" if details else "reject",
        "reason": "개발팀 오류수정",
        "timestamp": "2026-09-04T09:10:00+09:00",
    }
    if details:
        payload["details"] = details
    return {
        "event_id": f"decision-{task_id}-{payload['action']}",
        "type": "decision",
        "task_id": task_id,
        "decision": payload,
    }


class CandidateLoopTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.root = self.work / "rt"
        self.rules = self.work / "rules.json"
        shutil.copy(ROOT / "config" / "rules.json", self.rules)

        # Three items the correction is about -- a single confirmation is no
        # longer enough to seed a pattern -- plus wider corpus for replay.
        for task_id, title in [
            ("bsbukgu-9906", "재수정 요청 _ N잡 지원센터 플랫폼 관련"),
            ("bsbukgu-9907", "재수정 요청 _ 통계 화면"),
            ("bsbukgu-9908", "재수정 요청 _ 신청서 항목"),
            ("bsbukgu-1", "대표 팝업존 추가"),
            ("bsbukgu-4", "행사 팝업존 교체"),
            ("bsbukgu-5", "공지 팝업존 삭제"),
            ("bsbukgu-2", "주요뉴스 이동"),
            ("bsbukgu-3", "무관한 요청"),
        ]:
            put_task(work_item(task_id, title), root=self.root)

    # -- helpers -------------------------------------------------------

    def record(self, task_id="bsbukgu-9906", details=None):
        if details is None:
            details = {"classification": CORRECTION}
        EventLog(self.root / "state" / "events.jsonl").append(decision(task_id, details=details))

    def confirm_all(self, details=None):
        for task_id in ("bsbukgu-9906", "bsbukgu-9907", "bsbukgu-9908"):
            self.record(task_id, details=details)

    def confirm_popups(self):
        for task_id in ("bsbukgu-1", "bsbukgu-4", "bsbukgu-5"):
            self.record(task_id)

    def propose(self, *keywords):
        self.confirm_all()
        return candidates.propose(SCOPE, list(keywords) or ["재수정"], root=self.root)

    def approve(self, pattern_id):
        return candidates.record_pattern_decision(
            SCOPE, pattern_id, "approve", actor="owner", reason="검토 완료", root=self.root
        )

    # -- propose -------------------------------------------------------

    def test_three_confirmations_become_a_replayable_candidate(self):
        pattern = self.propose("재수정")

        self.assertEqual(pattern.scope, SCOPE)
        self.assertEqual(pattern.state, "candidate")
        self.assertEqual(pattern.kind, "rule")
        self.assertIsNotNone(pattern.definition)
        rule = pattern.to_dict()["definition"]
        self.assertEqual(rule["when"], {"title_or_body_contains_any": ["재수정"]})
        self.assertEqual(rule["then"]["responsibility"], "development")
        load_rules(rules=[rule])  # the stored body is a loadable rule

    def test_the_candidate_cites_every_confirming_task_revision(self):
        pattern = self.propose("재수정")

        cited = [line.split("@")[0] for line in pattern.evidence if "@revision-" in line]
        self.assertEqual(cited, ["bsbukgu-9906", "bsbukgu-9907", "bsbukgu-9908"])

    def test_the_same_confirmations_propose_the_same_pattern_id(self):
        first = self.propose("재수정")
        second = candidates.propose(SCOPE, ["재수정"], root=self.root)

        self.assertEqual(first.pattern_id, second.pattern_id)

    def test_fewer_than_three_confirmations_are_refused(self):
        self.record("bsbukgu-9906")
        self.record("bsbukgu-9907")

        with self.assertRaises(InsufficientConfirmationsError):
            candidates.propose(SCOPE, ["재수정"], root=self.root)

    def test_confirmations_in_another_scope_do_not_count(self):
        self.confirm_all()

        with self.assertRaises(InsufficientConfirmationsError):
            candidates.propose(OTHER_SCOPE, ["재수정"], root=self.root)

    def test_prose_only_corrections_are_refused_with_guidance(self):
        self.confirm_all(details={"note": "좀더필요"})

        with self.assertRaises(InsufficientConfirmationsError):
            candidates.propose(SCOPE, ["재수정"], root=self.root)

    def test_no_decision_at_all_is_refused(self):
        with self.assertRaises(InsufficientConfirmationsError):
            candidates.propose(SCOPE, ["재수정"], root=self.root)

    def test_keywords_are_required(self):
        self.confirm_all()

        with self.assertRaises(CandidateError):
            candidates.propose(SCOPE, [], root=self.root)

    # -- review --------------------------------------------------------

    def test_review_shadows_the_candidate_and_replays_real_work(self):
        pattern = self.propose("재수정")

        outcome = candidates.review(SCOPE, pattern.pattern_id, root=self.root)

        self.assertEqual(outcome["pattern"]["state"], "shadow")
        self.assertEqual(outcome["report"]["distinct"], 8)
        self.assertEqual(outcome["report"]["match"], 3)
        self.assertEqual(outcome["report"]["disagree"], 0)
        self.assertTrue(outcome["recommendation"]["recommend"])

    def test_a_candidate_that_contradicts_an_active_rule_is_not_recommended(self):
        # "팝업" is already classified design/image_popup by the shipped rules.
        self.confirm_popups()
        pattern = candidates.propose(SCOPE, ["팝업"], root=self.root)

        outcome = candidates.review(SCOPE, pattern.pattern_id, root=self.root)

        self.assertEqual(outcome["report"]["disagree"], 3)
        self.assertFalse(outcome["recommendation"]["recommend"])

    def test_review_in_another_scope_cannot_see_the_pattern(self):
        pattern = self.propose("재수정")

        with self.assertRaises(CandidateError):
            candidates.review(OTHER_SCOPE, pattern.pattern_id, root=self.root)

    # -- decide --------------------------------------------------------

    def test_a_decision_is_bound_to_the_exact_pattern_revision(self):
        pattern = self.propose("재수정")
        candidates.review(SCOPE, pattern.pattern_id, root=self.root)

        event = self.approve(pattern.pattern_id)

        self.assertEqual(event["type"], "pattern_decision")
        self.assertEqual(event["decision"]["scope"], SCOPE)
        self.assertEqual(event["decision"]["pattern_revision"], 2)
        self.assertEqual(len(event["decision"]["definition_digest"]), 64)

    def test_recording_the_same_decision_twice_is_idempotent(self):
        pattern = self.propose("재수정")
        candidates.review(SCOPE, pattern.pattern_id, root=self.root)

        self.approve(pattern.pattern_id)
        self.approve(pattern.pattern_id)

        events = [
            e for e in EventLog(self.root / "state" / "events.jsonl").read()
            if e.get("type") == "pattern_decision"
        ]
        self.assertEqual(len(events), 1)

    def test_rejecting_an_active_pattern_suspends_it(self):
        pattern = self.promoted()

        candidates.record_pattern_decision(
            SCOPE, pattern.pattern_id, "reject", actor="owner", reason="철회", root=self.root
        )

        self.assertEqual(candidates.get(SCOPE, pattern.pattern_id, root=self.root).state, "suspended")

    # -- promote -------------------------------------------------------

    def promoted(self):
        pattern = self.propose("재수정")
        candidates.review(SCOPE, pattern.pattern_id, root=self.root)
        self.approve(pattern.pattern_id)
        return candidates.promote(SCOPE, pattern.pattern_id, root=self.root)

    def test_promotion_activates_the_pattern_and_widens_coverage(self):
        item = work_item("bsbukgu-9906", "재수정 요청 _ N잡")
        self.assertIsNone(classify_with_rules(item, root=self.root))

        pattern = self.promoted()

        self.assertEqual(pattern.state, "active")
        self.assertIsNotNone(classify_with_rules(item, root=self.root))

    def test_promotion_never_writes_the_repository_rule_file(self):
        before = self.rules.read_text(encoding="utf-8")
        shipped = (ROOT / "config" / "rules.json").read_text(encoding="utf-8")

        self.promoted()

        self.assertEqual(self.rules.read_text(encoding="utf-8"), before)
        self.assertEqual((ROOT / "config" / "rules.json").read_text(encoding="utf-8"), shipped)

    def test_an_active_pattern_does_not_leak_into_another_scope(self):
        self.promoted()

        other = work_item("bsbukgu-9906", "재수정 요청 _ N잡", scope=OTHER_SCOPE)
        self.assertIsNone(classify_with_rules(other, root=self.root))

    def test_promotion_without_an_approve_decision_is_refused(self):
        pattern = self.propose("재수정")
        candidates.review(SCOPE, pattern.pattern_id, root=self.root)

        with self.assertRaises(CandidateError):
            candidates.promote(SCOPE, pattern.pattern_id, root=self.root)

        self.assertEqual(candidates.get(SCOPE, pattern.pattern_id, root=self.root).state, "shadow")

    def test_a_rejected_decision_does_not_authorize_promotion(self):
        pattern = self.propose("재수정")
        candidates.review(SCOPE, pattern.pattern_id, root=self.root)
        candidates.record_pattern_decision(
            SCOPE, pattern.pattern_id, "reject", actor="owner", reason="근거 부족", root=self.root
        )

        with self.assertRaises(CandidateError):
            candidates.promote(SCOPE, pattern.pattern_id, root=self.root)

        self.assertEqual(candidates.get(SCOPE, pattern.pattern_id, root=self.root).state, "shadow")

    def test_an_unrecommended_candidate_is_refused(self):
        self.confirm_popups()
        pattern = candidates.propose(SCOPE, ["팝업"], root=self.root)
        candidates.review(SCOPE, pattern.pattern_id, root=self.root)
        self.approve(pattern.pattern_id)

        with self.assertRaises(CandidateError):
            candidates.promote(SCOPE, pattern.pattern_id, root=self.root)

    def test_a_second_promotion_is_refused(self):
        pattern = self.promoted()

        with self.assertRaises(CandidateError):
            candidates.promote(SCOPE, pattern.pattern_id, root=self.root)

        self.assertEqual(candidates.get(SCOPE, pattern.pattern_id, root=self.root).state, "active")

    # -- corpus --------------------------------------------------------

    def test_the_corpus_is_the_scope_s_collected_work(self):
        self.assertEqual(len(candidates.corpus(SCOPE, self.root)), 8)

    def test_the_corpus_of_an_unused_scope_is_empty(self):
        self.assertEqual(candidates.corpus(OTHER_SCOPE, self.root), [])


if __name__ == "__main__":
    unittest.main()

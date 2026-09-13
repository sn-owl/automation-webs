import unittest

from automation.models import WorkItem


def work_item(**overrides):
    base = {
        "task_id": "alpha-1",
        "source": {
            "type": "board",
            "id": "alpha",
            "external_id": "1",
            "url": "https://fixture.local/bbs/board.php?wr_id=1",
        },
        "received_at": "2026-08-28T09:44:00+09:00",
        "title": "제목",
        "body": "본문",
        "author": "요청부서",
        "attachments": [],
        "mask_table_ref": "local://masks/alpha-1.json",
    }
    attachments = overrides.pop("attachments", None)
    base.update(overrides)
    if attachments is not None:
        base["attachments"] = [
            {
                "name": name,
                "type": atype,
                "raw_ref": f"raw/{name}",
                "extracted_ref": f"normalized/{name}.json",
            }
            for name, atype in attachments
        ]
    return WorkItem.from_dict(base)


_HEAT_SHELTER_RULE = {
    "id": "heat-shelter-refresh",
    "when": {
        "title_or_body_contains_any": ["무더위쉼터"],
        "attachment_type_in": ["hwpx", "hwp", "xls", "xlsx"],
    },
    "then": {
        "responsibility": "operations",
        "task_type": "data_refresh",
        "size": "recurring",
        "confidence": 0.9,
    },
}

_POPUP_RULE = {
    "id": "popup-image-swap",
    "when": {
        "title_or_body_contains_any": ["팝업"],
        "attachment_type_in": ["png", "jpg", "jpeg", "gif"],
    },
    "then": {
        "responsibility": "design",
        "task_type": "image_popup",
        "size": "simple",
        "confidence": 0.85,
    },
}

_BASELINE_RULES = [_HEAT_SHELTER_RULE, _POPUP_RULE]


class ReplayCandidateTest(unittest.TestCase):
    def replay(self):
        try:
            from automation.replay import replay_candidate
        except ModuleNotFoundError as exc:
            self.fail(f"automation.replay should exist: {exc}")
        return replay_candidate

    # Step 1 -- exact match, abstain, misclassification fixtures.

    def test_exact_match_agrees_with_baseline(self):
        replay_candidate = self.replay()
        item = work_item(
            title="무더위쉼터 현황 현행화 요청",
            attachments=[("현황.hwpx", "hwpx")],
        )
        candidate = [dict(_HEAT_SHELTER_RULE, id="heat-shelter-refresh-v2")]

        report = replay_candidate(
            "heat-shelter-refresh-v2",
            candidate,
            [item],
            baseline_rules=_BASELINE_RULES,
        )

        self.assertEqual(report["pattern_id"], "heat-shelter-refresh-v2")
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["distinct"], 1)
        self.assertEqual(report["match"], 1)
        self.assertEqual(report["abstain"], 0)
        self.assertEqual(report["disagree"], 0)
        self.assertEqual(report["disagreements"], [])

    def test_baseline_abstain_with_candidate_classification_is_a_match(self):
        # Coverage gain: the baseline has nothing for this item, but the
        # candidate confidently classifies it -- that counts as a match,
        # not a disagreement, because there is nothing to disagree WITH.
        replay_candidate = self.replay()
        item = work_item(title="새로운 유형의 요청입니다", body="아직 baseline에 없는 패턴")
        candidate = [
            {
                "id": "new-pattern",
                "when": {"title_contains_any": ["새로운 유형"]},
                "then": {
                    "responsibility": "content",
                    "task_type": "content_edit",
                    "size": "simple",
                    "confidence": 0.7,
                },
            }
        ]

        report = replay_candidate(
            "new-pattern", candidate, [item], baseline_rules=_BASELINE_RULES
        )

        self.assertEqual(report["match"], 1)
        self.assertEqual(report["abstain"], 0)
        self.assertEqual(report["disagree"], 0)

    def test_candidate_abstain_is_reported_regardless_of_baseline(self):
        replay_candidate = self.replay()
        item = work_item(title="확인 부탁드립니다", body="지난번 건 검토 후 회신 주세요.")
        candidate: list[dict] = []  # empty candidate ruleset never matches

        report = replay_candidate(
            "empty-candidate", candidate, [item], baseline_rules=_BASELINE_RULES
        )

        self.assertEqual(report["match"], 0)
        self.assertEqual(report["abstain"], 1)
        self.assertEqual(report["disagree"], 0)

    def test_misclassification_is_recorded_as_disagreement(self):
        replay_candidate = self.replay()
        item = work_item(
            title="무더위쉼터 현황 현행화 요청",
            attachments=[("현황.hwpx", "hwpx")],
        )
        # Candidate confidently classifies this as design/image_popup work
        # -- disagreeing with the baseline's operations/data_refresh verdict.
        candidate = [
            {
                "id": "wrong-call",
                "when": {"title_contains_any": ["무더위쉼터"]},
                "then": {
                    "responsibility": "design",
                    "task_type": "image_popup",
                    "size": "simple",
                    "confidence": 0.95,
                },
            }
        ]

        report = replay_candidate(
            "wrong-call", candidate, [item], baseline_rules=_BASELINE_RULES
        )

        self.assertEqual(report["match"], 0)
        self.assertEqual(report["abstain"], 0)
        self.assertEqual(report["disagree"], 1)
        self.assertEqual(len(report["disagreements"]), 1)

        entry = report["disagreements"][0]
        self.assertEqual(entry["task_id"], item.task_id)
        self.assertEqual(entry["candidate"]["responsibility"], "design")
        self.assertEqual(entry["baseline"]["responsibility"], "operations")

    # Step 4 -- duplicate copies of the same source item must not inflate
    # distinct evidence.

    def test_duplicate_source_copies_do_not_inflate_distinct_counts(self):
        replay_candidate = self.replay()
        item = work_item(
            title="무더위쉼터 현황 현행화 요청",
            attachments=[("현황.hwpx", "hwpx")],
        )
        duplicate = work_item(
            title="무더위쉼터 현황 현행화 요청 (재수집)",  # different content, same task_id
            attachments=[("현황.hwpx", "hwpx")],
        )
        self.assertEqual(item.task_id, duplicate.task_id)
        candidate = [dict(_HEAT_SHELTER_RULE, id="heat-shelter-refresh-v2")]

        report = replay_candidate(
            "heat-shelter-refresh-v2",
            candidate,
            [item, duplicate, duplicate],
            baseline_rules=_BASELINE_RULES,
        )

        self.assertEqual(report["total"], 3)
        self.assertEqual(report["distinct"], 1)
        self.assertEqual(report["match"], 1)
        self.assertEqual(report["match"] + report["abstain"] + report["disagree"], 1)

    def test_default_baseline_is_the_shipped_config(self):
        replay_candidate = self.replay()
        item = work_item(
            title="메인 페이지 팝업 배너 이미지 교체",
            body="첨부 이미지로 팝업존 배너를 교체해 주세요.",
            attachments=[("popup.png", "png")],
        )
        # Same verdict as config/rules.json's popup-image-swap rule.
        candidate = [dict(_POPUP_RULE, id="popup-image-swap-v2")]

        report = replay_candidate("popup-image-swap-v2", candidate, [item])

        self.assertEqual(report["match"], 1)
        self.assertEqual(report["disagree"], 0)

    def test_invalid_candidate_ruleset_is_rejected_before_replay(self):
        replay_candidate = self.replay()
        item = work_item(title="아무 제목")
        bad_candidate = [
            {
                "id": "bad",
                "when": {"title_regex": ".*"},
                "then": {
                    "responsibility": "design",
                    "task_type": "image_popup",
                    "size": "simple",
                    "confidence": 0.5,
                },
            }
        ]

        with self.assertRaisesRegex(ValueError, "title_regex"):
            replay_candidate("bad", bad_candidate, [item])

    def test_report_key_set_is_exact(self):
        replay_candidate = self.replay()
        report = replay_candidate("empty", [], [], baseline_rules=_BASELINE_RULES)

        self.assertEqual(
            set(report),
            {"pattern_id", "total", "distinct", "match", "abstain", "disagree", "disagreements"},
        )


class ReplayFixtureCorpusTest(unittest.TestCase):
    # Runs the real loader against the real fixtures/sanitized/ corpus so
    # the shipped fixtures get behavioural coverage, not just an in-test
    # config copy.

    def test_loader_normalizes_the_shipped_sanitized_fixtures(self):
        try:
            from automation.replay import load_fixture_corpus
        except ModuleNotFoundError as exc:
            self.fail(f"automation.replay should exist: {exc}")

        corpus = load_fixture_corpus()

        self.assertEqual(
            {item.task_id for item in corpus},
            {"alpha-13452", "beta-2048", "egov-9001"},
        )

    def test_real_fixture_corpus_replays_deterministically(self):
        from automation.replay import load_fixture_corpus, replay_candidate

        corpus = load_fixture_corpus()
        # An empty candidate ruleset always abstains, independent of the
        # fixture content -- this proves the real corpus flows through
        # replay_candidate end to end without relying on fragile keyword
        # matches against sanitized placeholder text.
        report = replay_candidate("empty-candidate", [], corpus)

        self.assertEqual(report["total"], 3)
        self.assertEqual(report["distinct"], 3)
        self.assertEqual(report["abstain"], 3)
        self.assertEqual(report["match"], 0)
        self.assertEqual(report["disagree"], 0)

        # Determinism: same corpus + rulesets -> byte-identical report.
        self.assertEqual(report, replay_candidate("empty-candidate", [], corpus))


if __name__ == "__main__":
    unittest.main()

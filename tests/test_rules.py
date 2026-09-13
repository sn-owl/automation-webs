import unittest

from automation.models import WorkItem


def work_item(**overrides):
    base = {
        "task_id": "yeonje-1",
        "source": {
            "type": "board",
            "id": "yeonje",
            "external_id": "1",
            "url": "https://fixture.local/bbs/board.php?wr_id=1",
        },
        "received_at": "2026-08-28T09:44:00+09:00",
        "title": "제목",
        "body": "본문",
        "author": "요청부서",
        "attachments": [],
        "mask_table_ref": "local://masks/yeonje-1.json",
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


class RuleClassifierTest(unittest.TestCase):
    def classify(self):
        try:
            from automation.rules import classify_with_rules
        except ModuleNotFoundError as exc:
            self.fail(f"automation.rules should exist: {exc}")
        return classify_with_rules

    def load_rules(self):
        from automation.rules import load_rules

        return load_rules

    # Step 1 — the three fixtures named in the plan.

    def test_heat_shelter_refresh_is_classified(self):
        classify_with_rules = self.classify()
        item = work_item(
            title="2026년 무더위쉼터 현황 현행화 요청",
            body="첨부된 목록으로 현행화 부탁드립니다.",
            attachments=[("무더위쉼터 현황.hwpx", "hwpx")],
        )

        result = classify_with_rules(item)

        self.assertIsNotNone(result)
        self.assertEqual(result.responsibility, "development")
        self.assertEqual(result.task_type, "data_refresh")
        self.assertEqual(result.size, "recurring")
        self.assertTrue(result.evidence)
        self.assertTrue(any("heat-shelter" in e for e in result.evidence))

    def test_popup_image_request_is_classified(self):
        classify_with_rules = self.classify()
        item = work_item(
            title="메인 페이지 팝업 배너 이미지 교체",
            body="첨부 이미지로 팝업존 배너를 교체해 주세요.",
            attachments=[("popup.png", "png")],
        )

        result = classify_with_rules(item)

        self.assertIsNotNone(result)
        self.assertEqual(result.responsibility, "design")
        self.assertEqual(result.task_type, "image_popup")
        self.assertTrue(any("popup" in e for e in result.evidence))

    def test_popup_request_is_classified_without_an_image_attachment(self):
        # The rule used to require an image attachment. Of the four real popup
        # requests collected so far, two carry HWPX and two carry nothing at
        # all, so every one of them abstained and cost a Hermes call. Coverage
        # on real work, not on the shape the fixture happened to have.
        for attachments in ([], [("팝업존 게재 신청서.hwpx", "hwpx")]):
            with self.subTest(attachments=attachments):
                item = work_item(
                    title="대표 팝업존 추가 ( 공공데이터 찾기 행사)",
                    body="게재 기간과 링크는 첨부 확인 바랍니다.",
                    attachments=attachments,
                )

                result = self.classify()(item)

                self.assertIsNotNone(result)
                self.assertEqual(result.responsibility, "design")
                self.assertEqual(result.task_type, "image_popup")

    def test_unclear_request_is_not_forced(self):
        classify_with_rules = self.classify()
        item = work_item(
            title="확인 부탁드립니다",
            body="지난번 건 관련해서 검토 후 회신 주세요.",
        )

        self.assertIsNone(classify_with_rules(item))

    # Step 4 — ambiguity must abstain, not guess.

    def test_conflicting_rule_matches_abstain(self):
        classify_with_rules = self.classify()
        rules = [
            {
                "id": "a",
                "when": {"title_contains_any": ["현행화"]},
                "then": {
                    "responsibility": "operations",
                    "task_type": "data_refresh",
                    "size": "recurring",
                    "confidence": 0.8,
                },
            },
            {
                "id": "b",
                "when": {"title_contains_any": ["팝업"]},
                "then": {
                    "responsibility": "design",
                    "task_type": "image_popup",
                    "size": "simple",
                    "confidence": 0.8,
                },
            },
        ]
        item = work_item(title="팝업 현행화 요청")

        self.assertIsNone(classify_with_rules(item, rules=rules))

    def test_agreeing_rule_matches_take_highest_confidence(self):
        classify_with_rules = self.classify()
        rules = [
            {
                "id": "broad",
                "when": {"title_contains_any": ["보고"]},
                "then": {
                    "responsibility": "operations",
                    "task_type": "audit_report",
                    "size": "simple",
                    "confidence": 0.6,
                },
            },
            {
                "id": "specific",
                "when": {"title_contains_any": ["감사 보고"]},
                "then": {
                    "responsibility": "operations",
                    "task_type": "audit_report",
                    "size": "simple",
                    "confidence": 0.92,
                },
            },
        ]
        item = work_item(title="2026 상반기 감사 보고 자료")

        result = classify_with_rules(item, rules=rules)
        self.assertIsNotNone(result)
        self.assertEqual(result.confidence, 0.92)
        self.assertEqual(len(result.evidence), 2)

    def test_no_rule_matches_returns_none(self):
        classify_with_rules = self.classify()
        self.assertIsNone(classify_with_rules(work_item(title="x", body="y"), rules=[]))

    # config integrity

    def test_bundled_rules_are_valid_and_closed(self):
        load_rules = self.load_rules()
        from automation.classification import Classification

        rules = load_rules()
        self.assertGreater(len(rules), 0)
        allowed_conditions = {
            "title_contains_any",
            "body_contains_any",
            "title_or_body_contains_any",
            "attachment_type_in",
            "source_type_in",
        }
        for rule in rules:
            self.assertLessEqual(set(rule["when"]), allowed_conditions)
            Classification.from_rule(
                rule["then"],
                evidence=[f"rule:{rule['id']}"],
                rule_ids=[rule["id"]],
            )

    def test_unknown_condition_key_is_rejected(self):
        load_rules = self.load_rules()
        bad = [
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
            load_rules(rules=bad)


if __name__ == "__main__":
    unittest.main()

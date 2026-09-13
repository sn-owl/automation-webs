import unittest
from pathlib import Path


def pattern(**overrides):
    from automation.patterns import Pattern

    base = {
        "scope": "owner-a",
        "pattern_id": "heat-shelter-title-rule",
        "kind": "rule",
        "state": "shadow",
        "confidence": 0.82,
        "evidence": ["제목에 '무더위쉼터 현행화' 반복 관찰 (12건)"],
    }
    base.update(overrides)
    return Pattern.from_dict(base)


def report(**overrides):
    base = {
        "pattern_id": "heat-shelter-title-rule",
        "total": 5,
        "distinct": 5,
        "match": 5,
        "abstain": 0,
        "disagree": 0,
        "disagreements": [],
    }
    base.update(overrides)
    return base


_POLICY = {"min_distinct": 3, "max_disagreements": 0}


class PromotionGateTest(unittest.TestCase):
    def evaluate(self):
        try:
            from automation.promotion import evaluate_promotion
        except ModuleNotFoundError as exc:
            self.fail(f"automation.promotion should exist: {exc}")
        return evaluate_promotion

    # -- Step 1: all conditions satisfied -> recommend --------------------

    def test_all_conditions_satisfied_recommends_promotion(self):
        evaluate_promotion = self.evaluate()

        result = evaluate_promotion(pattern(), report(), policy=_POLICY)

        self.assertTrue(result["recommend"])
        self.assertEqual(result["pattern_id"], "heat-shelter-title-rule")
        self.assertTrue(result["reasons"])

    # -- Step 1: replay 부족 (insufficient replay) -------------------------

    def test_insufficient_distinct_replay_blocks_recommendation(self):
        evaluate_promotion = self.evaluate()

        result = evaluate_promotion(
            pattern(), report(distinct=2), policy=_POLICY
        )

        self.assertFalse(result["recommend"])
        self.assertTrue(any("insufficient replay" in r for r in result["reasons"]))

    def test_replay_sufficiency_reads_distinct_not_total(self):
        # A corpus with many duplicated raw copies (large `total`) but few
        # distinct source items must NOT count as sufficient evidence --
        # that's the entire point of replay.py's dedupe.
        evaluate_promotion = self.evaluate()

        result = evaluate_promotion(
            pattern(), report(total=50, distinct=2), policy=_POLICY
        )

        self.assertFalse(result["recommend"])
        self.assertTrue(any("insufficient replay" in r for r in result["reasons"]))

    # -- Step 1: disagreement 존재 ------------------------------------------

    def test_disagreement_present_blocks_recommendation(self):
        evaluate_promotion = self.evaluate()

        result = evaluate_promotion(
            pattern(), report(disagree=1, match=4), policy=_POLICY
        )

        self.assertFalse(result["recommend"])
        self.assertTrue(any("disagreement" in r for r in result["reasons"]))

    # -- Step 1: 승인 없음 (no approval) -- expressed structurally as state --

    def test_candidate_state_is_not_eligible(self):
        evaluate_promotion = self.evaluate()

        result = evaluate_promotion(
            pattern(state="candidate"), report(), policy=_POLICY
        )

        self.assertFalse(result["recommend"])
        self.assertTrue(any("not eligible" in r for r in result["reasons"]))

    def test_active_suspended_retired_are_not_eligible(self):
        evaluate_promotion = self.evaluate()

        for state in ("active", "suspended", "retired"):
            with self.subTest(state=state):
                result = evaluate_promotion(
                    pattern(state=state), report(), policy=_POLICY
                )
                self.assertFalse(result["recommend"])
                self.assertTrue(any("not eligible" in r for r in result["reasons"]))

    # -- multiple failing conditions are ALL reported, not just the first --

    def test_multiple_failures_are_all_reported(self):
        evaluate_promotion = self.evaluate()

        result = evaluate_promotion(
            pattern(state="candidate"),
            report(distinct=1, disagree=2, match=0),
            policy=_POLICY,
        )

        self.assertFalse(result["recommend"])
        reasons_text = " ".join(result["reasons"])
        self.assertIn("not eligible", reasons_text)
        self.assertIn("insufficient replay", reasons_text)
        self.assertIn("disagreement", reasons_text)

    # -- reasons always present, recommending or not -----------------------

    def test_reasons_are_never_empty(self):
        evaluate_promotion = self.evaluate()

        recommended = evaluate_promotion(pattern(), report(), policy=_POLICY)
        refused = evaluate_promotion(
            pattern(state="candidate"), report(distinct=0), policy=_POLICY
        )

        self.assertTrue(recommended["reasons"])
        self.assertTrue(refused["reasons"])

    # -- boundary values: exactly at the threshold is NOT a failure --------

    def test_distinct_exactly_at_minimum_is_sufficient(self):
        evaluate_promotion = self.evaluate()

        result = evaluate_promotion(pattern(), report(distinct=3), policy=_POLICY)

        self.assertTrue(result["recommend"])

    def test_disagree_exactly_at_maximum_is_not_a_blocker(self):
        evaluate_promotion = self.evaluate()

        result = evaluate_promotion(
            pattern(), report(disagree=0), policy=_POLICY
        )

        self.assertTrue(result["recommend"])

    # -- pattern_id / report mismatch is a caller error ---------------------

    def test_mismatched_pattern_and_report_ids_raise(self):
        evaluate_promotion = self.evaluate()

        with self.assertRaisesRegex(ValueError, "pattern_id"):
            evaluate_promotion(
                pattern(pattern_id="a"), report(pattern_id="b"), policy=_POLICY
            )

    # -- Step 4: this module never promotes ---------------------------------

    def test_module_has_no_write_path_to_active(self):
        # AST-based, not a raw substring search: the module's own docstring
        # explains (in prose) that it does not import pattern_store or call
        # transition() -- a naive text search would trip on that
        # explanation. What actually matters is the real import and call
        # graph, so parse it.
        import ast

        import automation.promotion as promotion_module

        source = Path(promotion_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")

        offending_imports = {name for name in imported if "pattern_store" in name}
        self.assertFalse(
            offending_imports,
            f"promotion.py must not import pattern_store, found: {offending_imports}",
        )

        transition_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "transition"
        ]
        self.assertEqual(
            transition_calls, [], "promotion.py must never call Pattern.transition()"
        )

        self.assertIn("TODO(Task 30)", source)

    def test_recommendation_never_contains_a_pattern_object(self):
        # The recommendation is a plain, JSON-serialisable dict -- it cannot
        # itself carry an `active` Pattern, only a verdict about one.
        evaluate_promotion = self.evaluate()
        from automation.patterns import Pattern

        result = evaluate_promotion(pattern(), report(), policy=_POLICY)

        self.assertNotIsInstance(result, Pattern)
        for value in result.values():
            self.assertNotIsInstance(value, Pattern)


class PromotionPolicyTest(unittest.TestCase):
    def load(self):
        try:
            from automation.promotion import load_promotion_policy
        except ModuleNotFoundError as exc:
            self.fail(f"automation.promotion should exist: {exc}")
        return load_promotion_policy

    def test_bundled_config_has_the_documented_defaults(self):
        load_promotion_policy = self.load()

        policy = load_promotion_policy()

        self.assertEqual(policy["min_distinct"], 3)
        self.assertEqual(policy["max_disagreements"], 0)

    def test_unknown_policy_key_is_rejected(self):
        load_promotion_policy = self.load()

        with self.assertRaisesRegex(ValueError, "unknown"):
            load_promotion_policy(policy=dict(_POLICY, vibes=True))

    def test_min_distinct_must_be_a_non_negative_integer(self):
        load_promotion_policy = self.load()

        for bad in (-1, "3", 1.5, True):
            with self.subTest(min_distinct=bad):
                with self.assertRaisesRegex(ValueError, "min_distinct"):
                    load_promotion_policy(policy=dict(_POLICY, min_distinct=bad))

    def test_max_disagreements_must_be_a_non_negative_integer(self):
        load_promotion_policy = self.load()

        for bad in (-1, "0", 1.5, True):
            with self.subTest(max_disagreements=bad):
                with self.assertRaisesRegex(ValueError, "max_disagreements"):
                    load_promotion_policy(policy=dict(_POLICY, max_disagreements=bad))

    def test_missing_key_is_rejected(self):
        load_promotion_policy = self.load()

        with self.assertRaisesRegex(ValueError, "min_distinct"):
            load_promotion_policy(policy={"max_disagreements": 0})


class ShippedPromotionConfigBehaviourTest(unittest.TestCase):
    # The shipped config/promotion.json, not only an in-test copy, must
    # drive real gate decisions.

    def evaluate(self):
        from automation.promotion import evaluate_promotion

        return evaluate_promotion

    def test_shipped_config_blocks_below_minimum_distinct_replay(self):
        evaluate_promotion = self.evaluate()

        result = evaluate_promotion(pattern(), report(distinct=2))

        self.assertFalse(result["recommend"])

    def test_shipped_config_blocks_any_disagreement(self):
        evaluate_promotion = self.evaluate()

        result = evaluate_promotion(pattern(), report(distinct=5, disagree=1, match=4))

        self.assertFalse(result["recommend"])

    def test_shipped_config_recommends_a_clean_shadow_pattern(self):
        evaluate_promotion = self.evaluate()

        result = evaluate_promotion(pattern(), report(distinct=3, match=3))

        self.assertTrue(result["recommend"])


if __name__ == "__main__":
    unittest.main()

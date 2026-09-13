import unittest

from automation.assessment import Assessment
from automation.approval import load_approval_policy, requires_approval


class ApprovalPolicyTest(unittest.TestCase):
    def test_shipped_policy_gates_every_writing_risk(self):
        policy = load_approval_policy()
        self.assertEqual(policy["read_only"], "auto")
        for risk in set(Assessment.RISKS) - {"read_only"}:
            with self.subTest(risk=risk):
                self.assertEqual(policy[risk], "required")
                self.assertTrue(requires_approval(risk))

    def test_shipped_policy_answers_every_risk_the_contract_allows(self):
        # A risk the policy forgets falls through to the fail-closed default,
        # which would look like an unexplained extra gate rather than a typo.
        self.assertEqual(set(load_approval_policy()), set(Assessment.RISKS))

    def test_read_only_work_needs_no_decision(self):
        self.assertFalse(requires_approval("read_only"))

    def test_an_unknown_risk_is_gated(self):
        self.assertTrue(requires_approval("something_new"))

    def test_a_missing_or_broken_policy_gates_everything(self):
        for broken in ({}, {"read_only": "auto"}, {"read_only": "maybe"}, []):
            with self.subTest(policy=broken):
                self.assertTrue(requires_approval("read_only", policy=broken))

    def test_policy_rejects_an_unknown_risk_key(self):
        full = {risk: "required" for risk in Assessment.RISKS}
        with self.assertRaises(ValueError):
            load_approval_policy(policy={**full, "made_up": "auto"})

    def test_policy_rejects_a_decision_outside_the_vocabulary(self):
        full = {risk: "required" for risk in Assessment.RISKS}
        with self.assertRaises(ValueError):
            load_approval_policy(policy={**full, "read_only": "sometimes"})

    def test_policy_is_data_so_a_gate_can_be_opened_without_code(self):
        # The point of the change: the operator can decide that preparing a
        # popup handoff no longer needs a click, by editing config only.
        opened = {risk: "required" for risk in Assessment.RISKS}
        opened["remote_write"] = "auto"
        self.assertFalse(requires_approval("remote_write", policy=opened))
        self.assertTrue(requires_approval("commit_push", policy=opened))


if __name__ == "__main__":
    unittest.main()

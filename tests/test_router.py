import unittest

from automation.assessment import Assessment


def assessment(**overrides):
    base = {
        "automation_level": "ready",
        "risk": "local_artifact_only",
        "confidence": 0.95,
        "recipe": "heat-shelter-xls-v1",
        "evidence": ["eval:heat-shelter-ready 조건과 일치"],
    }
    base.update(overrides)
    if base["automation_level"] != "ready":
        base.pop("recipe", None)
    return Assessment.from_dict(base)


class RouterTest(unittest.TestCase):
    def route(self):
        try:
            from automation.router import route
        except ModuleNotFoundError as exc:
            self.fail(f"automation.router should exist: {exc}")
        return route

    # Step 1 — the four automation levels.

    def test_ready_executes_recipe(self):
        route = self.route()
        self.assertEqual(route(assessment(automation_level="ready")), "execute_recipe")

    def test_developable_drafts(self):
        route = self.route()
        self.assertEqual(
            route(assessment(automation_level="developable")), "development_draft"
        )

    def test_assisted_prepares_assistance(self):
        route = self.route()
        self.assertEqual(
            route(assessment(automation_level="assisted")), "prepare_assistance"
        )

    def test_manual_hands_off(self):
        route = self.route()
        self.assertEqual(route(assessment(automation_level="manual")), "manual_handoff")

    # Step 1 — low confidence escalates regardless of level.

    def test_low_confidence_needs_hermes(self):
        route = self.route()
        for level in ("ready", "developable", "assisted", "manual"):
            with self.subTest(level=level):
                self.assertEqual(
                    route(assessment(automation_level=level, confidence=0.4)),
                    "needs_hermes",
                )

    def test_confidence_at_floor_routes_normally(self):
        route = self.route()
        self.assertEqual(
            route(assessment(automation_level="ready", confidence=0.7), confidence_floor=0.7),
            "execute_recipe",
        )

    # Step 4 — a Hermes suggestion alone never reaches execution.

    def test_hermes_ready_does_not_execute(self):
        route = self.route()
        self.assertEqual(
            route(assessment(automation_level="ready", confidence=0.99), source="hermes"),
            "needs_hermes",
        )

    def test_hermes_developable_does_not_draft_directly(self):
        route = self.route()
        self.assertEqual(
            route(assessment(automation_level="developable", confidence=0.99), source="hermes"),
            "needs_hermes",
        )

    def test_hermes_assisted_still_prepares_assistance(self):
        route = self.route()
        self.assertEqual(
            route(assessment(automation_level="assisted", confidence=0.9), source="hermes"),
            "prepare_assistance",
        )

    def test_unknown_source_is_rejected(self):
        route = self.route()
        with self.assertRaisesRegex(ValueError, "source"):
            route(assessment(), source="telepathy")

    def test_every_route_name_is_known(self):
        from automation.router import ROUTES

        self.assertEqual(
            ROUTES,
            (
                "classification_only",
                "manual_handoff",
                "prepare_assistance",
                "development_draft",
                "execute_recipe",
                "needs_hermes",
            ),
        )


if __name__ == "__main__":
    unittest.main()

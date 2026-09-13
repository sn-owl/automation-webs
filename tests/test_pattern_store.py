import json
import tempfile
import unittest
from pathlib import Path

from automation.patterns import Pattern, definition_digest
from automation.decisions import PatternDecision
from automation.events import EventLog
from automation.pattern_store import (
    CorruptPatternError,
    PatternConflictError,
    PatternNotFoundError,
    _write_revision,
    active_definitions,
    create_pattern,
    get_pattern,
    list_patterns,
    retire_pattern,
    revise_pattern,
)


SCOPE = "owner-a"
OTHER_SCOPE = "owner-b"
APPROVAL = "pattern-decision-abc123"

SAMPLE = {
    "scope": SCOPE,
    "pattern_id": "heat-shelter-title-rule",
    "kind": "rule",
    "state": "candidate",
    "confidence": 0.72,
    "evidence": [
        "제목에 '무더위쉼터 현행화' 반복 관찰 (12건)",
        "동일 title_pattern이 서로 다른 3개 fixture에서 발견",
    ],
}


def make_pattern(pattern_id: str = "heat-shelter-title-rule", **overrides) -> Pattern:
    return Pattern.from_dict(dict(SAMPLE, pattern_id=pattern_id, **overrides))


def revision_path(directory, pattern_id, revision, scope=SCOPE) -> Path:
    return Path(directory) / "state" / "patterns" / scope / pattern_id / f"{revision:03d}.json"


def activate(directory, pattern_id: str = "heat-shelter-title-rule", scope: str = SCOPE) -> Pattern:
    revise_pattern(scope, pattern_id, "shadow", root=directory)
    pattern, revision, _ = get_pattern(scope, pattern_id, root=directory)
    decision = PatternDecision(
        scope=scope, pattern_id=pattern_id, pattern_revision=revision,
        definition_digest=definition_digest(pattern.to_dict().get("definition")),
        action="approve", actor="reviewer", reason="synthetic approval",
        timestamp="2026-09-11T00:00:00Z",
    )
    EventLog(Path(directory) / "state" / "events.jsonl").append({
        "event_id": APPROVAL, "type": "pattern_decision",
        "scope": scope, "pattern_id": pattern_id, "decision": decision.to_dict(),
    })
    return revise_pattern(scope, pattern_id, "active", root=directory, approval_event_id=APPROVAL)


class PatternStoreCreateTest(unittest.TestCase):
    def test_create_writes_first_revision_under_its_scope(self):
        pattern = make_pattern()

        with tempfile.TemporaryDirectory() as directory:
            create_pattern(pattern, root=directory)

            self.assertTrue(revision_path(directory, pattern.pattern_id, 1).is_file())

    def test_create_returns_the_pattern(self):
        pattern = make_pattern()

        with tempfile.TemporaryDirectory() as directory:
            result = create_pattern(pattern, root=directory)

            self.assertEqual(result, pattern)

    def test_create_is_idempotent_for_identical_content(self):
        pattern = make_pattern()

        with tempfile.TemporaryDirectory() as directory:
            create_pattern(pattern, root=directory)
            path = revision_path(directory, pattern.pattern_id, 1)
            first_bytes = path.read_bytes()

            create_pattern(pattern, root=directory)

            self.assertEqual(path.read_bytes(), first_bytes)
            self.assertEqual(len(list(path.parent.glob("*.json"))), 1)

    def test_create_with_different_content_for_existing_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)

            with self.assertRaises(PatternConflictError):
                create_pattern(make_pattern(confidence=0.9), root=directory)

    def test_the_same_pattern_id_in_another_scope_is_not_a_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)

            create_pattern(make_pattern(scope=OTHER_SCOPE, confidence=0.9), root=directory)

            self.assertTrue(revision_path(directory, "heat-shelter-title-rule", 1).is_file())
            self.assertTrue(
                revision_path(directory, "heat-shelter-title-rule", 1, scope=OTHER_SCOPE).is_file()
            )

    def test_create_rejects_non_candidate_state(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "candidate"):
                create_pattern(make_pattern(state="shadow"), root=directory)

    def test_create_rejects_active_state_even_with_no_other_path_available(self):
        # A pattern must never reach `active` storage without an approval
        # event. create_pattern has no approval parameter at all, so seeding
        # a pattern straight into `active` must be refused here.
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "candidate"):
                create_pattern(make_pattern(state="active"), root=directory)

    def test_invalid_pattern_id_cannot_escape_pattern_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                create_pattern(make_pattern(pattern_id="../outside"), root=directory)

    def test_invalid_scope_cannot_escape_pattern_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                create_pattern(make_pattern(scope="../outside"), root=directory)


class PatternStoreReviseTest(unittest.TestCase):
    def test_revise_writes_a_new_revision_file(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)

            result = revise_pattern(SCOPE, "heat-shelter-title-rule", "shadow", root=directory)

            self.assertEqual(result.state, "shadow")
            self.assertTrue(revision_path(directory, "heat-shelter-title-rule", 2).is_file())

    def test_revise_to_active_requires_an_approval_event(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)
            revise_pattern(SCOPE, "heat-shelter-title-rule", "shadow", root=directory)

            with self.assertRaisesRegex(ValueError, "approval_event_id"):
                revise_pattern(SCOPE, "heat-shelter-title-rule", "active", root=directory)

    def test_forged_approval_cannot_activate_a_pattern(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)
            revise_pattern(SCOPE, "heat-shelter-title-rule", "shadow", root=directory)
            with self.assertRaises(ValueError):
                revise_pattern(
                    SCOPE, "heat-shelter-title-rule", "active",
                    root=directory, approval_event_id="forged",
                )
            self.assertEqual(active_definitions(SCOPE, root=directory), [])
            self.assertEqual(get_pattern(SCOPE, "heat-shelter-title-rule", root=directory)[1], 2)

    def test_revise_to_active_records_the_approval_event(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)

            result = activate(directory)

            self.assertEqual(result.state, "active")
            envelope = json.loads(
                revision_path(directory, "heat-shelter-title-rule", 3).read_text(encoding="utf-8")
            )
            self.assertEqual(envelope["approval_event_id"], APPROVAL)

    def test_revise_without_approval_omits_the_key_rather_than_writing_null(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)

            revise_pattern(SCOPE, "heat-shelter-title-rule", "shadow", root=directory)

            envelope = json.loads(
                revision_path(directory, "heat-shelter-title-rule", 2).read_text(encoding="utf-8")
            )
            self.assertNotIn("approval_event_id", envelope)

    def test_illegal_transition_is_refused_by_pattern_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)

            with self.assertRaisesRegex(ValueError, "illegal transition"):
                revise_pattern(
                    SCOPE,
                    "heat-shelter-title-rule",
                    "active",
                    root=directory,
                    approval_event_id=APPROVAL,
                )

    def test_revise_unknown_pattern_raises_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PatternNotFoundError):
                revise_pattern(SCOPE, "does-not-exist", "shadow", root=directory)

    def test_revise_in_the_wrong_scope_raises_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)

            with self.assertRaises(PatternNotFoundError):
                revise_pattern(OTHER_SCOPE, "heat-shelter-title-rule", "shadow", root=directory)


class PatternStoreGetTest(unittest.TestCase):
    def test_get_returns_the_latest_revision_and_its_number(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)
            revise_pattern(SCOPE, "heat-shelter-title-rule", "shadow", root=directory)

            pattern, revision, approval_event_id = get_pattern(
                SCOPE, "heat-shelter-title-rule", root=directory
            )

            self.assertEqual((pattern.state, revision, approval_event_id), ("shadow", 2, None))

    def test_get_returns_the_approval_event_of_an_active_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)
            activate(directory)

            _, revision, approval_event_id = get_pattern(
                SCOPE, "heat-shelter-title-rule", root=directory
            )

            self.assertEqual((revision, approval_event_id), (3, APPROVAL))

    def test_get_unknown_pattern_raises_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PatternNotFoundError):
                get_pattern(SCOPE, "does-not-exist", root=directory)


class PatternStoreListTest(unittest.TestCase):
    def test_list_patterns_is_empty_for_missing_store(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(list_patterns(SCOPE, root=directory), [])

    def test_list_patterns_returns_current_state_in_id_order(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern("zzz-rule"), root=directory)
            create_pattern(make_pattern("aaa-rule"), root=directory)
            revise_pattern(SCOPE, "zzz-rule", "shadow", root=directory)

            result = list_patterns(SCOPE, root=directory)

            self.assertEqual([p.pattern_id for p in result], ["aaa-rule", "zzz-rule"])
            self.assertEqual(result[1].state, "shadow")

    def test_list_patterns_returns_pattern_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)

            self.assertIsInstance(list_patterns(SCOPE, root=directory)[0], Pattern)

    def test_list_patterns_never_leaks_another_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern("mine-rule"), root=directory)
            create_pattern(make_pattern("theirs-rule", scope=OTHER_SCOPE), root=directory)

            self.assertEqual(
                [p.pattern_id for p in list_patterns(SCOPE, root=directory)], ["mine-rule"]
            )
            self.assertEqual(
                [p.pattern_id for p in list_patterns(OTHER_SCOPE, root=directory)], ["theirs-rule"]
            )


class PatternStoreActiveDefinitionsTest(unittest.TestCase):
    def definition(self, keyword: str) -> dict:
        return {
            "id": "heat-shelter-title-rule",
            "when": {"title_or_body_contains_any": [keyword]},
            "then": {
                "responsibility": "owner",
                "task_type": "document_update",
                "size": "small",
                "confidence": 0.72,
            },
        }

    def test_only_active_rule_patterns_are_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(definition=self.definition("무더위쉼터")), root=directory)
            self.assertEqual(active_definitions(SCOPE, root=directory), [])

            activate(directory)

            self.assertEqual(
                active_definitions(SCOPE, root=directory), [self.definition("무더위쉼터")]
            )

    def test_active_definitions_are_scope_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(definition=self.definition("무더위쉼터")), root=directory)
            activate(directory)

            self.assertEqual(active_definitions(OTHER_SCOPE, root=directory), [])

    def test_a_suspended_pattern_stops_being_an_active_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(definition=self.definition("무더위쉼터")), root=directory)
            activate(directory)

            revise_pattern(SCOPE, "heat-shelter-title-rule", "suspended", root=directory)

            self.assertEqual(active_definitions(SCOPE, root=directory), [])


class PatternStoreRetireTest(unittest.TestCase):
    def test_retire_routes_through_transition_and_writes_a_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)

            result = retire_pattern(SCOPE, "heat-shelter-title-rule", root=directory)

            self.assertEqual(result.state, "retired")
            self.assertTrue(revision_path(directory, "heat-shelter-title-rule", 2).is_file())

    def test_retire_is_terminal_a_second_retire_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)
            retire_pattern(SCOPE, "heat-shelter-title-rule", root=directory)

            with self.assertRaisesRegex(ValueError, "illegal transition"):
                retire_pattern(SCOPE, "heat-shelter-title-rule", root=directory)

    def test_retire_unknown_pattern_raises_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PatternNotFoundError):
                retire_pattern(SCOPE, "does-not-exist", root=directory)


class PatternStoreImmutabilityTest(unittest.TestCase):
    def test_earlier_revisions_are_preserved_byte_for_byte_after_later_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            create_pattern(make_pattern(), root=directory)
            rev1_path = revision_path(directory, "heat-shelter-title-rule", 1)
            rev1_bytes = rev1_path.read_bytes()

            activate(directory)
            retire_pattern(SCOPE, "heat-shelter-title-rule", root=directory)

            self.assertEqual(rev1_path.read_bytes(), rev1_bytes)
            revisions = sorted(p.name for p in rev1_path.parent.glob("*.json"))
            self.assertEqual(revisions, ["001.json", "002.json", "003.json", "004.json"])

    def test_a_write_never_overwrites_an_existing_revision_file(self):
        # revise_pattern always targets max(existing)+1, so a same-slot
        # collision can never arise from ordinary single-threaded calls --
        # it only matters as a guard against a genuine race between two
        # writers computing the same "next" revision concurrently. Exercise
        # the atomic-write primitive directly to prove the guard itself.
        pattern = make_pattern()
        other = pattern.transition("shadow")

        with tempfile.TemporaryDirectory() as directory:
            _write_revision(pattern, revision=1, approval_event_id=None, root=directory)
            path = revision_path(directory, pattern.pattern_id, 1)
            first_bytes = path.read_bytes()

            with self.assertRaises(PatternConflictError):
                _write_revision(other, revision=1, approval_event_id=None, root=directory)

            self.assertEqual(path.read_bytes(), first_bytes)


class PatternStoreCorruptionTest(unittest.TestCase):
    def write_raw(self, directory, payload, pattern_id="heat-shelter-title-rule", scope=SCOPE):
        path = revision_path(directory, pattern_id, 1, scope=scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    def test_corrupt_json_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_raw(directory, "{not-json")

            with self.assertRaises(CorruptPatternError):
                revise_pattern(SCOPE, "heat-shelter-title-rule", "shadow", root=directory)
            with self.assertRaises(CorruptPatternError):
                list_patterns(SCOPE, root=directory)

    def test_pattern_id_mismatch_between_file_and_directory_is_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            mismatched = make_pattern("some-other-id").to_dict()
            self.write_raw(directory, json.dumps({"revision": 1, "pattern": mismatched}))

            with self.assertRaises(CorruptPatternError):
                list_patterns(SCOPE, root=directory)

    def test_scope_mismatch_between_file_and_directory_is_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            mismatched = make_pattern(scope=OTHER_SCOPE).to_dict()
            self.write_raw(directory, json.dumps({"revision": 1, "pattern": mismatched}))

            with self.assertRaises(CorruptPatternError):
                list_patterns(SCOPE, root=directory)

    def test_revision_number_disagreeing_with_the_filename_is_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_raw(
                directory, json.dumps({"revision": 7, "pattern": make_pattern().to_dict()})
            )

            with self.assertRaises(CorruptPatternError):
                list_patterns(SCOPE, root=directory)

    def test_an_unknown_envelope_field_is_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_raw(
                directory,
                json.dumps(
                    {"revision": 1, "pattern": make_pattern().to_dict(), "approval": "legacy"}
                ),
            )

            with self.assertRaises(CorruptPatternError):
                list_patterns(SCOPE, root=directory)


if __name__ == "__main__":
    unittest.main()

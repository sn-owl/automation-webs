import hashlib
import unittest

from automation.identity import execution_key, task_id


class TaskIdentityTest(unittest.TestCase):
    def test_scoped_components_cannot_collide_at_delimiters(self):
        first = task_id("source", "1", scope="alice-team", connector_id="board")
        second = task_id("source", "1", scope="alice", connector_id="team-board")
        self.assertNotEqual(first, second)

    def test_same_source_item_has_same_task_id(self):
        first = task_id("yeonje", "13452")
        second = task_id("yeonje", "13452")

        self.assertEqual(first, second)
        self.assertEqual(first, "yeonje-13452")

    def test_different_source_items_have_different_task_ids(self):
        self.assertNotEqual(task_id("yeonje", "13452"), task_id("yeonje", "13453"))
        self.assertNotEqual(task_id("yeonje", "13452"), task_id("dongnae", "13452"))

    def test_task_id_normalizes_safe_identifier_whitespace(self):
        self.assertEqual(task_id(" Yeonje ", " 13452 "), "yeonje-13452")

    def test_execution_key_is_deterministic_and_changes_with_inputs(self):
        first = execution_key("yeonje-13452", "restarea-xls", "input-hash")
        second = execution_key("yeonje-13452", "restarea-xls", "input-hash")

        self.assertEqual(first, second)
        self.assertEqual(len(first), hashlib.sha256().digest_size * 2)
        self.assertNotEqual(first, execution_key("yeonje-13453", "restarea-xls", "input-hash"))
        self.assertNotEqual(first, execution_key("yeonje-13452", "other-recipe", "input-hash"))
        self.assertNotEqual(first, execution_key("yeonje-13452", "restarea-xls", "other-hash"))

    def test_path_traversal_identifiers_are_rejected(self):
        invalid_values = ("../yeonje", "yeonje/child", r"yeonje\child", ".", "..")

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    task_id(value, "13452")
                with self.assertRaises(ValueError):
                    task_id("yeonje", value)

        with self.assertRaises(ValueError):
            execution_key("yeonje-13452", "../recipe", "input-hash")

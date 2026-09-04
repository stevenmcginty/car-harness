"""Self-test for the default-adapter flag (backend/adapters/registry.py).

The registry persists to one module-level path (ADAPTERS_JSON), so each test
points that at a throwaway file in a temp directory: the real adapters.json in
the project root is never read or written here.

What is asserted is the rule every resolver now shares: a named adapter wins,
an omitted one falls back to the default, an unknown one still resolves to
None, deleting the default leaves no default at all, and setting a new default
clears the old flag rather than leaving two.

Run:  .venv\\Scripts\\python.exe -m unittest backend.tests.test_adapter_default -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.adapters import registry  # noqa: E402


def _instance(instance_id: str, name: str, default: bool = False) -> dict:
    return {
        "id": instance_id,
        "typeId": "elm327-remote",
        "name": name,
        "config": {},
        "lastTest": None,
        "default": default,
    }


class DefaultAdapterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_path = registry.ADAPTERS_JSON
        registry.ADAPTERS_JSON = Path(self._tmp.name) / "adapters.json"
        self._write([_instance("aaa", "Phone Relay"), _instance("bbb", "VeePeak")])

    def tearDown(self):
        registry.ADAPTERS_JSON = self._saved_path
        self._tmp.cleanup()

    def _write(self, items):
        registry.ADAPTERS_JSON.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def _raw(self):
        return json.loads(registry.ADAPTERS_JSON.read_text(encoding="utf-8"))

    # --- the flag itself ---------------------------------------------------
    def test_no_default_by_default(self):
        self.assertIsNone(registry.get_default())
        self.assertFalse(any(i.default for i in registry.list_instances()))

    def test_set_default_persists_and_is_exclusive(self):
        self.assertTrue(registry.set_default("aaa"))
        self.assertEqual(registry.get_default().id, "aaa")
        # persisted, not just in memory
        self.assertEqual([i["default"] for i in self._raw()], [True, False])

        self.assertTrue(registry.set_default("bbb"))
        self.assertEqual(registry.get_default().id, "bbb")
        self.assertEqual([i["default"] for i in self._raw()], [False, True])

    def test_set_default_unknown_id_changes_nothing(self):
        registry.set_default("aaa")
        self.assertFalse(registry.set_default("nope"))
        self.assertEqual(registry.get_default().id, "aaa")

    def test_two_flagged_in_a_hand_edited_file_keeps_the_first(self):
        self._write([_instance("aaa", "A", True), _instance("bbb", "B", True)])
        self.assertEqual(registry.get_default().id, "aaa")
        self.assertEqual([i.default for i in registry.list_instances()], [True, False])

    # --- resolve() ---------------------------------------------------------
    def test_resolve_named_adapter_wins_over_the_default(self):
        registry.set_default("aaa")
        self.assertEqual(registry.resolve("bbb").id, "bbb")

    def test_resolve_without_a_name_uses_the_default(self):
        registry.set_default("aaa")
        for empty in (None, ""):
            with self.subTest(empty=empty):
                self.assertEqual(registry.resolve(empty).id, "aaa")

    def test_resolve_without_a_name_and_no_default_is_none(self):
        self.assertIsNone(registry.resolve(None))

    def test_resolve_unknown_name_is_none_even_with_a_default(self):
        registry.set_default("aaa")
        self.assertIsNone(registry.resolve("nope"))

    # --- removal -----------------------------------------------------------
    def test_removing_the_default_clears_the_default(self):
        registry.set_default("aaa")
        self.assertTrue(registry.remove_instance("aaa"))
        self.assertIsNone(registry.get_default())
        self.assertIsNone(registry.resolve(None))
        # the survivor is not silently promoted
        self.assertEqual([i.id for i in registry.list_instances()], ["bbb"])

    def test_removing_a_non_default_leaves_the_default_alone(self):
        registry.set_default("aaa")
        self.assertTrue(registry.remove_instance("bbb"))
        self.assertEqual(registry.get_default().id, "aaa")

    # --- new instances -----------------------------------------------------
    def test_a_saved_instance_is_not_default(self):
        registry.set_default("aaa")
        saved = registry.save_instance("elm327-remote", "Another", {})
        self.assertFalse(saved.default)
        self.assertEqual(registry.get_default().id, "aaa")


if __name__ == "__main__":
    unittest.main()

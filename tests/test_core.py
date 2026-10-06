import unittest

from lifecycle_service.core import WorkOrder, summarize
from lifecycle_service.validation import validate_transition


class CoreTests(unittest.TestCase):
    def test_create_and_summary_are_deterministic(self):
        item = WorkOrder.create("demo", "v1", "draft", "operator", {"b": "2", "a": "1"})
        self.assertIn("demo@v1|draft|", summarize(item))
        self.assertEqual(summarize(item), summarize(item))

    def test_invalid_input_is_rejected(self):
        with self.assertRaises(ValueError):
            WorkOrder.create("", "v1", "draft", "operator", {})
        with self.assertRaises(ValueError):
            WorkOrder.create("demo", "v1", "unknown", "operator", {})

    def test_state_transition_boundary(self):
        item = WorkOrder.create("demo", "v1", "draft", "operator", {})
        validate_transition(item, "active")
        with self.assertRaises(ValueError):
            validate_transition(item, "draft")


if __name__ == "__main__":
    unittest.main()


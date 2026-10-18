from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

RECIPE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECIPE_DIR))

import planner


class FakeCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.arguments: dict[str, object] | None = None

    def create(self, **arguments: object) -> object:
        self.arguments = arguments
        message = SimpleNamespace(content=self.content)
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice])


class PlannerTest(unittest.TestCase):
    def test_live_plan_uses_canonical_model_and_strict_schema(self) -> None:
        content = json.dumps(
            {
                "skill": "pick_and_place_multi",
                "args": {"objects": ["red", "blue"]},
                "narration": "Moving both blocks.",
            }
        )
        completions = FakeCompletions(content)
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        plan = planner.plan_live("Move red and blue.", client=client)

        self.assertEqual(plan["skill"], "pick_and_place_multi")
        self.assertIsNotNone(completions.arguments)
        assert completions.arguments is not None
        self.assertEqual(completions.arguments["model"], "muse-spark-1.3")
        response_format = completions.arguments["response_format"]
        self.assertTrue(response_format["json_schema"]["strict"])

    def test_validation_rejects_unknown_skills(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported skill"):
            planner.validate_plan(
                {
                    "skill": "delete_table",
                    "args": {"objects": ["red"]},
                    "narration": "No.",
                }
            )

    def test_validation_rejects_duplicate_objects(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicates"):
            planner.validate_plan(
                {
                    "skill": "sort_by_color",
                    "args": {"objects": ["red", "red"]},
                    "narration": "Sorting.",
                }
            )

    def test_cached_plan_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "move_red.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "skill": "pick_and_place_multi",
                        "args": {"objects": ["red"]},
                        "narration": "Moving red.",
                    }
                )
            )
            plan = planner.load_cached_plan(
                "move red",
                plans_dir=Path(directory),
            )
        self.assertEqual(plan["args"], {"objects": ["red"]})


if __name__ == "__main__":
    unittest.main()

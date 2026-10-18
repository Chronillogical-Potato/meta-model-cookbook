from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

RECIPE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECIPE_DIR))

from robot import DEFAULT_BLOCKS, PandaScene
from run_demo import run
from skills import check_sorted


class SimulationTest(unittest.TestCase):
    def test_initial_scene_is_not_already_sorted(self) -> None:
        scene = PandaScene(record_media=False)
        blocks = list(DEFAULT_BLOCKS)
        initial_positions = {block: scene.block_pos(block).copy() for block in blocks}

        check = check_sorted(scene, blocks, initial_positions)

        self.assertFalse(check["success"])

    def test_offline_pick_and_place_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run(
                "put the red block in the bin",
                offline=True,
                output_dir=Path(directory),
                save_media=False,
            )

        self.assertTrue(result["check"]["success"])

    def test_ephemeral_run_does_not_persist_the_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "simulation"
            result = run(
                "put the red block in the bin",
                offline=True,
                output_dir=output,
                save_media=False,
                persist_result=False,
                verbose=False,
            )
            self.assertTrue(result["check"]["success"])
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

RECIPE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECIPE_DIR))

import skills


class FakeScene:
    def __init__(self, positions: dict[str, tuple[float, float, float]]) -> None:
        self.positions = {
            name: np.asarray(position, dtype=float)
            for name, position in positions.items()
        }

    def block_pos(self, name: str) -> np.ndarray:
        return self.positions[name]


class SkillsTest(unittest.TestCase):
    def test_normalize_objects_preserves_order(self) -> None:
        self.assertEqual(
            skills.normalize_objects({"objects": ["blue", "red"]}),
            ["blue_block", "red_block"],
        )

    def test_normalize_objects_rejects_unknown_colors(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown block color"):
            skills.normalize_objects({"objects": ["purple"]})

    def test_unknown_skill_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown skill"):
            skills.run_skill(object(), "launch_block", {"objects": ["red"]})

    def test_bin_check_requires_every_requested_block(self) -> None:
        scene = FakeScene(
            {
                "red_block": (0.55, -0.28, 0.05),
                "blue_block": (0.42, -0.12, 0.05),
            }
        )
        check = skills.check_in_bin(scene, ["red_block", "blue_block"])
        self.assertFalse(check["success"])
        self.assertEqual(
            [item["in_bin"] for item in check["per_object"]],
            [True, False],
        )

    def test_bin_check_rejects_a_block_beyond_the_inner_wall(self) -> None:
        positions = {
            "red_block": (skills.BIN_XY[0] + 0.120, skills.BIN_XY[1], 0.05),
            "green_block": (skills.BIN_XY[0], skills.BIN_XY[1] + 0.080, 0.05),
        }
        scene = FakeScene(positions)
        check = skills.check_in_bin(scene, list(positions))
        self.assertFalse(check["success"])
        self.assertEqual(
            [item["in_bin"] for item in check["per_object"]],
            [False, False],
        )

    def test_bin_check_accepts_blocks_inside_the_inner_wall(self) -> None:
        positions = {
            "red_block": (skills.BIN_XY[0] + 0.110, skills.BIN_XY[1], 0.05),
            "green_block": (skills.BIN_XY[0], skills.BIN_XY[1] + 0.070, 0.05),
        }
        scene = FakeScene(positions)
        self.assertTrue(skills.check_in_bin(scene, list(positions))["success"])

    def test_sorted_check_rejects_an_untouched_scene(self) -> None:
        initial_positions = {
            "red_block": np.asarray((0.45, 0.15, 0.05)),
            "green_block": np.asarray((0.50, 0.02, 0.05)),
            "blue_block": np.asarray((0.42, -0.12, 0.05)),
        }
        scene = FakeScene(
            {name: tuple(position) for name, position in initial_positions.items()}
        )
        check = skills.check_sorted(
            scene,
            list(initial_positions),
            initial_positions,
        )
        self.assertFalse(check["success"])

    def test_sorted_check_rejects_a_block_in_the_bin(self) -> None:
        initial_positions = {
            "red_block": np.asarray((0.45, 0.15, 0.05)),
            "green_block": np.asarray((0.50, 0.02, 0.05)),
            "blue_block": np.asarray((0.42, -0.12, 0.05)),
        }
        final_positions = {
            name: (target[0], target[1], 0.05)
            for name, target in skills.SORT_TARGETS.items()
        }
        final_positions["blue_block"] = (*skills.BIN_XY, 0.05)
        scene = FakeScene(final_positions)

        check = skills.check_sorted(
            scene,
            list(final_positions),
            initial_positions,
        )

        self.assertFalse(check["success"])
        self.assertTrue(check["per_object"][2]["in_bin"])

    def test_sorted_check_accepts_moved_target_positions(self) -> None:
        initial_positions = {
            "red_block": np.asarray((0.45, 0.15, 0.05)),
            "green_block": np.asarray((0.50, 0.02, 0.05)),
            "blue_block": np.asarray((0.42, -0.12, 0.05)),
        }
        final_positions = {
            name: (target[0], target[1], 0.05)
            for name, target in skills.SORT_TARGETS.items()
        }
        scene = FakeScene(final_positions)
        check = skills.check_sorted(
            scene,
            list(final_positions),
            initial_positions,
        )
        self.assertTrue(check["success"])


if __name__ == "__main__":
    unittest.main()

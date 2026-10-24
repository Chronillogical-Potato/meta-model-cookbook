# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import argparse
import json
from pathlib import Path

from planner import get_plan
from robot import PandaScene
from skills import check_in_bin, check_sorted, normalize_objects, run_skill

DEFAULT_SCENARIO = "put the red block in the bin"


def result_check(
    scene: PandaScene,
    skill: str,
    blocks: list[str],
    initial_positions: dict[str, object],
) -> dict[str, object]:
    if skill == "sort_by_color":
        return check_sorted(scene, blocks, initial_positions)
    return check_in_bin(scene, blocks)


def run(
    scenario: str,
    *,
    offline: bool,
    output_dir: Path,
    save_media: bool = True,
    persist_result: bool = True,
    verbose: bool = True,
) -> dict[str, object]:
    plan = get_plan(scenario, offline=offline)
    skill = str(plan["skill"])
    args = plan["args"]
    if not isinstance(args, dict):
        raise TypeError("Planner args must be an object.")
    blocks = normalize_objects(args)

    scene = PandaScene(record_media=save_media)
    initial_positions = {block: scene.block_pos(block).copy() for block in blocks}
    if save_media or persist_result:
        output_dir.mkdir(parents=True, exist_ok=True)
    if save_media:
        scene.record_hold()
        scene.save_png(output_dir / "before.png")

    phases: list[dict[str, str]] = []

    def record_phase(phase: str, block: str) -> None:
        phases.append({"phase": phase, "block": block})
        if verbose:
            print(f"  {phase:<14} {block.removesuffix('_block')}")

    if verbose:
        print(f"Request: {scenario}")
        print(f"Plan: {skill}({', '.join(blocks)})")
        print(f"Muse Spark: {plan['narration']}")
    execution = run_skill(
        scene,
        skill,
        args,
        phase_callback=record_phase,
    )
    if save_media:
        scene.record_hold(30)
        scene.save_png(output_dir / "after.png")
    check = result_check(scene, skill, blocks, initial_positions)

    if save_media:
        scene.save_video(output_dir / "demo.mp4")
        scene.save_gif(output_dir / "demo.gif")

    result = {
        "scenario": scenario,
        "mode": "offline" if offline else "live",
        "plan": plan,
        "execution": execution,
        "check": check,
        "phases": phases,
    }
    if persist_result:
        (output_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    if verbose:
        print(f"Success: {check['success']}")
        if save_media or persist_result:
            print(f"Outputs: {output_dir}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan a robot task with Muse Spark and run it in MuJoCo."
    )
    parser.add_argument("scenario", nargs="?", default=DEFAULT_SCENARIO)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use a bundled plan instead of calling the Model API.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--no-media", action="store_true")
    arguments = parser.parse_args()
    result = run(
        arguments.scenario,
        offline=arguments.offline,
        output_dir=arguments.output_dir,
        save_media=not arguments.no_media,
    )
    return 0 if result["check"]["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

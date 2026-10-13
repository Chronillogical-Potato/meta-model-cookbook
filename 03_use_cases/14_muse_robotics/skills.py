from __future__ import annotations

from collections.abc import Callable, Mapping

import numpy as np

APPROACH_Z = 0.28
GRASP_Z = 0.15
LIFT_Z = 0.32
BIN_XY = (0.55, -0.28)
BIN_DROP_Z = 0.22
BIN_SLOTS = [(0.0, 0.0), (0.06, 0.0), (-0.06, 0.0)]
BIN_HALF_SIZE = (0.15, 0.11)
BIN_WALL_THICKNESS = 0.01
BLOCK_HALF_SIZE = 0.025
BIN_INNER_TOLERANCE = (
    BIN_HALF_SIZE[0] - BIN_WALL_THICKNESS - BLOCK_HALF_SIZE,
    BIN_HALF_SIZE[1] - BIN_WALL_THICKNESS - BLOCK_HALF_SIZE,
)
MAX_POSITION_ERROR = 0.08
SORT_TARGETS = {
    "red_block": (0.55, 0.30, 0.20),
    "green_block": (0.55, 0.17, 0.20),
    "blue_block": (0.55, 0.04, 0.20),
}

PhaseCallback = Callable[[str, str], None]


def color_to_block(color: str) -> str:
    normalized = color.lower().strip()
    if normalized.endswith("_block"):
        normalized = normalized.removesuffix("_block")
    if normalized not in {"red", "green", "blue"}:
        raise ValueError(f"Unknown block color: {color!r}")
    return f"{normalized}_block"


def normalize_objects(args: dict[str, object]) -> list[str]:
    objects = args.get("objects")
    if not isinstance(objects, list) or not objects:
        raise ValueError("The plan must name at least one object.")
    return [color_to_block(str(item)) for item in objects]


def _phase(callback: PhaseCallback | None, name: str, block: str) -> None:
    if callback is not None:
        callback(name, block)


def _move(scene: object, target: list[float], *, duration: float) -> float:
    residual = float(scene.move_to(target, duration=duration))
    if residual > MAX_POSITION_ERROR:
        raise RuntimeError(f"Robot missed waypoint {target}: residual {residual:.3f} m")
    return residual


def _pick_and_place_at(
    scene: object,
    block: str,
    target: tuple[float, float, float],
    *,
    phase_callback: PhaseCallback | None = None,
) -> dict[str, object]:
    start = scene.block_pos(block).copy()
    block_position = scene.block_pos(block)
    target_x, target_y, target_z = target

    _phase(phase_callback, "open_gripper", block)
    scene.set_gripper(opened=True, steps=120)
    _phase(phase_callback, "approach", block)
    _move(scene, [block_position[0], block_position[1], APPROACH_Z], duration=1.3)
    block_position = scene.block_pos(block)
    _phase(phase_callback, "descend", block)
    _move(scene, [block_position[0], block_position[1], GRASP_Z], duration=1.1)
    _phase(phase_callback, "grasp", block)
    scene.set_gripper(opened=False, steps=300)
    _phase(phase_callback, "lift", block)
    _move(scene, [block_position[0], block_position[1], LIFT_Z], duration=1.0)
    _phase(phase_callback, "carry", block)
    _move(scene, [target_x, target_y, LIFT_Z], duration=1.3)
    _phase(phase_callback, "lower", block)
    _move(scene, [target_x, target_y, target_z], duration=0.8)
    _phase(phase_callback, "release", block)
    scene.set_gripper(opened=True, steps=200)
    _phase(phase_callback, "retreat", block)
    _move(scene, [target_x, target_y, LIFT_Z + 0.05], duration=0.8)

    return {
        "object": block,
        "start": start.tolist(),
        "end": scene.block_pos(block).tolist(),
    }


def pick_and_place_multi(
    scene: object,
    args: dict[str, object],
    *,
    phase_callback: PhaseCallback | None = None,
) -> dict[str, object]:
    blocks = normalize_objects(args)
    results = []
    for index, block in enumerate(blocks):
        if index:
            position = scene.block_pos(block)
            _move(scene, [position[0], position[1], APPROACH_Z], duration=1.3)
        offset_x, offset_y = BIN_SLOTS[index]
        results.append(
            _pick_and_place_at(
                scene,
                block,
                (BIN_XY[0] + offset_x, BIN_XY[1] + offset_y, BIN_DROP_Z),
                phase_callback=phase_callback,
            )
        )
    return {"objects": blocks, "per_object": results}


def sort_by_color(
    scene: object,
    args: dict[str, object],
    *,
    phase_callback: PhaseCallback | None = None,
) -> dict[str, object]:
    requested = normalize_objects(args)
    # This order keeps pickup paths clear in the full-mesh demo scene.
    order = ["red_block", "blue_block", "green_block"]
    blocks = [block for block in order if block in requested]
    results = [
        _pick_and_place_at(
            scene,
            block,
            SORT_TARGETS[block],
            phase_callback=phase_callback,
        )
        for block in blocks
    ]
    return {"objects": blocks, "per_object": results}


SKILLS = {
    "pick_and_place_multi": pick_and_place_multi,
    "sort_by_color": sort_by_color,
}


def run_skill(
    scene: object,
    name: str,
    args: dict[str, object],
    *,
    phase_callback: PhaseCallback | None = None,
) -> dict[str, object]:
    try:
        skill = SKILLS[name]
    except KeyError as error:
        raise ValueError(
            f"Unknown skill {name!r}; choose from {sorted(SKILLS)}"
        ) from error
    return skill(scene, args, phase_callback=phase_callback)


def check_in_bin(scene: object, blocks: list[str]) -> dict[str, object]:
    per_object = []
    for block in blocks:
        position = scene.block_pos(block)
        delta_x = abs(float(position[0]) - BIN_XY[0])
        delta_y = abs(float(position[1]) - BIN_XY[1])
        in_bin = (
            delta_x < BIN_INNER_TOLERANCE[0]
            and delta_y < BIN_INNER_TOLERANCE[1]
            and float(position[2]) < 0.14
        )
        per_object.append(
            {
                "object": block,
                "in_bin": in_bin,
                "position": np.round(position, 3).tolist(),
            }
        )
    return {
        "success": all(item["in_bin"] for item in per_object),
        "per_object": per_object,
    }


def check_sorted(
    scene: object,
    blocks: list[str],
    initial_positions: Mapping[str, np.ndarray],
) -> dict[str, object]:
    states = []
    for block in blocks:
        position = scene.block_pos(block)
        target_x, target_y, _ = SORT_TARGETS[block]
        moved = float(np.linalg.norm(position - initial_positions[block])) > 0.04
        in_bin = (
            abs(float(position[0]) - BIN_XY[0]) < BIN_INNER_TOLERANCE[0]
            and abs(float(position[1]) - BIN_XY[1]) < BIN_INNER_TOLERANCE[1]
        )
        sorted_correctly = (
            moved
            and not in_bin
            and abs(float(position[0]) - target_x) < 0.20
            and abs(float(position[1]) - target_y) < 0.08
            and float(position[2]) < 0.12
        )
        states.append(
            {
                "object": block,
                "moved": moved,
                "in_bin": in_bin,
                "sorted": sorted_correctly,
                "position": np.round(position, 3).tolist(),
            }
        )
    return {"success": all(item["sorted"] for item in states), "per_object": states}

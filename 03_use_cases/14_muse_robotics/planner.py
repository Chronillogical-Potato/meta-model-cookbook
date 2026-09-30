# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import json
import os
from pathlib import Path

from openai import OpenAI

MODEL = "muse-spark-1.3"
BASE_URL = "https://api.meta.ai/v1"
PLANS_DIR = Path(__file__).with_name("plans")
ALLOWED_SKILLS = {
    "pick_and_place_multi",
    "sort_by_color",
}
ALLOWED_OBJECTS = {"red", "green", "blue"}

PLAN_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "robot_plan",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "enum": sorted(ALLOWED_SKILLS),
                },
                "args": {
                    "type": "object",
                    "properties": {
                        "objects": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": sorted(ALLOWED_OBJECTS),
                            },
                            "minItems": 1,
                            "maxItems": 3,
                            "uniqueItems": True,
                        },
                    },
                    "required": ["objects"],
                    "additionalProperties": False,
                },
                "narration": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                },
            },
            "required": ["skill", "args", "narration"],
            "additionalProperties": False,
        },
    },
}

SYSTEM_PROMPT = """You plan actions for a simulated robot arm.

The scene contains red, green, and blue blocks plus a bin. Choose exactly one
skill:
- pick_and_place_multi: put the requested block or blocks in the bin, preserving
  the order in which the user names them.
- sort_by_color: arrange the requested blocks into a tidy color-sorted row.

Never invent objects or skills. Return the smallest valid plan matching the
provided JSON schema."""


def validate_plan(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("The model response must be a JSON object.")
    if set(value) != {"skill", "args", "narration"}:
        raise ValueError("The plan must contain only skill, args, and narration.")

    skill = value["skill"]
    args = value["args"]
    narration = value["narration"]
    if not isinstance(skill, str) or skill not in ALLOWED_SKILLS:
        raise ValueError(f"Unsupported skill: {skill!r}")
    if not isinstance(args, dict) or set(args) != {"objects"}:
        raise ValueError("args must contain only objects.")

    objects = args["objects"]
    if not isinstance(objects, list) or not 1 <= len(objects) <= 3:
        raise ValueError("objects must contain one to three colors.")
    if any(
        not isinstance(item, str) or item not in ALLOWED_OBJECTS for item in objects
    ):
        raise ValueError("objects may contain only red, green, and blue.")
    if len(objects) != len(set(objects)):
        raise ValueError("objects must not contain duplicates.")
    if not isinstance(narration, str) or not 1 <= len(narration) <= 160:
        raise ValueError("narration must be a short, non-empty string.")

    return {
        "skill": skill,
        "args": {"objects": list(objects)},
        "narration": narration,
    }


def create_client() -> OpenAI:
    return OpenAI(base_url=BASE_URL, api_key=os.environ["MODEL_API_KEY"])


def plan_live(request: str, *, client: OpenAI | None = None) -> dict[str, object]:
    if not request.strip():
        raise ValueError("The robot request must not be empty.")
    active_client = client or create_client()
    response = active_client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": request},
        ],
        response_format=PLAN_RESPONSE_FORMAT,
    )
    content = response.choices[0].message.content
    if not isinstance(content, str):
        raise TypeError("Muse Spark did not return a text plan.")
    return validate_plan(json.loads(content))


def cache_key(request: str) -> str:
    return "".join(
        character if character.isalnum() else "_" for character in request.lower()
    ).strip("_")[:60]


def load_cached_plan(
    request: str,
    *,
    plans_dir: Path = PLANS_DIR,
) -> dict[str, object]:
    path = plans_dir / f"{cache_key(request)}.json"
    if not path.is_file():
        examples = ", ".join(
            sorted(item.stem.replace("_", " ") for item in plans_dir.glob("*.json"))
        )
        raise FileNotFoundError(
            f"No offline plan matches {request!r}. Available examples: {examples}"
        )
    return validate_plan(json.loads(path.read_text()))


def get_plan(
    request: str,
    *,
    offline: bool = False,
    client: OpenAI | None = None,
) -> dict[str, object]:
    return load_cached_plan(request) if offline else plan_live(request, client=client)

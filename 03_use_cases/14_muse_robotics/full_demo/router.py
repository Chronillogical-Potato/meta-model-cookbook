# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import re

from .models import Intent, Route

COLORS = ("red", "green", "blue")
STYLE_WORDS = {
    "comic": "comic",
    "watercolor": "watercolor",
    "neon": "neon",
    "clay": "clay",
    "sketch": "sketch",
}
NEGATION = re.compile(r"\b(?:do not|don't|dont|never|no|not|isn't|isnt|cancel|stop)\b")


def _objects(text: str) -> tuple[str, ...]:
    found = []
    for match in re.finditer(r"\b(red|green|blue)\b", text):
        color = match.group(1)
        if color not in found:
            found.append(color)
    return tuple(found)


def _negates(text: str, terms: tuple[str, ...]) -> bool:
    for match in NEGATION.finditer(text):
        tail = text[match.end() : match.end() + 48]
        if any(re.search(rf"\b{term}\b", tail) for term in terms):
            return True
    return False


def route_text(request: str) -> Route:
    text = " ".join(request.lower().split())
    if not text:
        raise ValueError("The request must not be empty.")

    print_command = re.search(r"\bprint(?:ing)?\b", text)
    printable_object = re.search(r"\b(?:photo|image|postcard|picture|it|this)\b", text)
    if print_command and _negates(text, ("print", "printing")):
        return Route(Intent.CHAT, request)
    if print_command and printable_object:
        without_qr = "without qr" in text or "no qr" in text
        return Route(Intent.PRINT, request, include_qr=not without_qr)

    selfie_object = re.search(r"\b(?:selfie|portrait|photo|picture)\b", text)
    selfie_command = re.search(
        r"\b(?:take|make|create|capture|style)\b.{0,36}\b(?:selfie|portrait|photo|picture)\b",
        text,
    )
    if selfie_object and _negates(
        text,
        ("take", "make", "create", "capture", "style", "selfie", "portrait", "photo"),
    ):
        return Route(Intent.CHAT, request)
    if selfie_command:
        style = next(
            (value for word, value in STYLE_WORDS.items() if word in text),
            "watercolor",
        )
        return Route(Intent.SELFIE, request, style=style)

    robot_command = re.search(r"\b(?:put|move|sort|clear|pick|place)\b", text)
    if robot_command and _negates(
        text,
        ("put", "move", "sort", "clear", "pick", "place", "block", *COLORS),
    ):
        return Route(Intent.CHAT, request)
    if robot_command:
        objects = _objects(text)
        if "clear the table" in text or "sort" in text:
            objects = COLORS
        if objects:
            return Route(Intent.ROBOT, request, objects=objects)

    return Route(Intent.CHAT, request)

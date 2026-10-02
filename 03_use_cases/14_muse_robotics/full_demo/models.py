# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class Intent(StrEnum):
    ROBOT = "robot"
    SELFIE = "selfie"
    PRINT = "print"
    CHAT = "chat"


class SessionStage(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SEGMENTING = "segmenting"
    EXECUTING = "executing"
    CAPTURING = "capturing"
    STYLING = "styling"
    PRINTING = "printing"
    READY = "ready"
    ERROR = "error"


@dataclass(frozen=True)
class Route:
    intent: Intent
    text: str
    objects: tuple[str, ...] = ()
    style: str = "watercolor"
    include_qr: bool = False


@dataclass(frozen=True)
class Mask:
    width: int
    height: int
    pixels: bytes

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Mask dimensions must be positive.")
        if len(self.pixels) != self.width * self.height:
            raise ValueError("Mask length does not match its dimensions.")


@dataclass
class SessionState:
    run_id: int = 0
    stage: SessionStage = SessionStage.IDLE
    intent: Intent | None = None
    message: str = "Ready"
    transcript: str = ""
    latest_image: str | None = None
    latest_postcard: str | None = None
    sam_preview: str | None = None
    error: str | None = None
    events: list[str] = field(default_factory=list)

    def public_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("transcript", None)
        value["stage"] = self.stage.value
        value["intent"] = self.intent.value if self.intent is not None else None
        return value

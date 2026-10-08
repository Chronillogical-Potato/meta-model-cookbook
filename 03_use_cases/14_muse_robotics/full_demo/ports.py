from __future__ import annotations

from collections.abc import AsyncIterable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol

from .models import Mask


class VoiceTranscriberPort(Protocol):
    async def transcribe(
        self,
        chunks: Iterable[bytes] | AsyncIterable[bytes],
    ) -> str: ...


class ImageStylerPort(Protocol):
    def style(self, image: bytes, style: str) -> bytes: ...


class FaceGatePort(Protocol):
    def contains_face(self, image: bytes) -> bool: ...


class SegmenterPort(Protocol):
    def segment(self, image: bytes, concept: str) -> Mask | None: ...


class ReachyPort(Protocol):
    def react(self, moment: str) -> None: ...

    def capture(self) -> bytes: ...

    def speak(self, text: str) -> None: ...


class RobotRunnerPort(Protocol):
    def run(self, request: str, output_dir: Path) -> Mapping[str, object]: ...


class ChatPort(Protocol):
    def reply(self, request: str) -> str: ...


class PrintBackendPort(Protocol):
    def submit(
        self,
        image: bytes,
        *,
        idempotency_key: str,
        output_dir: Path,
        commit: Callable[[Callable[[], str]], str],
    ) -> str: ...

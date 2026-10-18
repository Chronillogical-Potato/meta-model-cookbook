from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

RECIPE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECIPE_DIR))

from full_demo.muse_voice import (
    FallbackTranscriber,
    FixtureVoiceTranscriber,
    build_handshake,
    transcribe_connected,
)


class FakeSocket:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.sent: list[object] = []
        self._events = iter(json.dumps(event) for event in events)

    async def send(self, value: object) -> None:
        self.sent.append(value)

    async def recv(self) -> str:
        return json.dumps({"sessionId": "session-1"})

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(0)
        try:
            return next(self._events)
        except StopIteration as error:
            raise StopAsyncIteration from error


class FailingSendSocket(FakeSocket):
    async def send(self, value: object) -> None:
        if isinstance(value, bytes):
            raise OSError("socket write failed")
        await super().send(value)


class FailingTranscriber:
    async def transcribe(self, _chunks: object) -> str:
        raise RuntimeError("stream unavailable")


class VoiceContractTest(unittest.IsolatedAsyncioTestCase):
    def test_handshake_uses_public_contract(self) -> None:
        handshake = build_handshake("token", keywords=["MuJoCo", "Reachy"])
        self.assertEqual(handshake["model"], "muse-voice-transcribe-1.0")
        self.assertEqual(handshake["audioEncoding"], "PCM_16KHZ")
        self.assertEqual(handshake["authorization"], {"accessToken": "Bearer token"})
        self.assertEqual(handshake["keywords"], ["MuJoCo", "Reachy"])

    def test_invalid_mode_fails_before_connecting(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            build_handshake("token", mode="GUESS")

    async def test_fallback_replays_the_same_bounded_audio(self) -> None:
        transcriber = FallbackTranscriber(
            FailingTranscriber(),
            FixtureVoiceTranscriber("fallback transcript"),
        )
        result = await transcriber.transcribe([b"one", b"two"])
        self.assertEqual(result, "fallback transcript")

    async def test_cumulative_partial_is_replaced_by_final(self) -> None:
        socket = FakeSocket(
            [
                {"type": "transcript", "transcript": "put red", "final": False},
                {
                    "type": "transcript",
                    "transcript": "put the red block in the bin",
                    "final": True,
                },
            ]
        )
        transcript = await transcribe_connected(
            socket,
            [b"\x00\x00" * 20],
            build_handshake("token"),
        )
        self.assertEqual(transcript, "put the red block in the bin")
        self.assertEqual(
            json.loads(socket.sent[0])["model"], "muse-voice-transcribe-1.0"
        )
        self.assertIn(b"\x00\x00" * 20, socket.sent)
        self.assertIn(json.dumps({"type": "endStream"}), socket.sent)

    async def test_socket_close_does_not_promote_a_partial(self) -> None:
        socket = FakeSocket(
            [{"type": "transcript", "transcript": "partial", "final": False}]
        )
        with self.assertRaisesRegex(RuntimeError, "before a final"):
            await transcribe_connected(socket, [b"\x00\x00"], build_handshake("token"))

    async def test_sender_failure_is_not_swallowed(self) -> None:
        socket = FailingSendSocket(
            [{"type": "transcript", "transcript": "partial", "final": False}]
        )
        with self.assertRaisesRegex(OSError, "write failed"):
            await transcribe_connected(socket, [b"\x00\x00"], build_handshake("token"))


if __name__ == "__main__":
    unittest.main()

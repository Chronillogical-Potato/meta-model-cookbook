# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import wave
from collections.abc import AsyncIterable, Iterable
from typing import Any

STREAM_URL = "wss://api.meta.ai/v1/asr/realtime"
MODEL = "muse-voice-transcribe-1.0"
MODES = {"PUSH_TO_TALK", "ENDPOINTING", "DIARIZATION"}
ENCODINGS = {"PCM_16KHZ", "PCM_24KHZ"}
BYTES_PER_SECOND = {"PCM_16KHZ": 32_000, "PCM_24KHZ": 48_000}


def build_handshake(
    token: str,
    *,
    mode: str = "PUSH_TO_TALK",
    encoding: str = "PCM_16KHZ",
    keywords: list[str] | None = None,
) -> dict[str, object]:
    if mode not in MODES:
        raise ValueError(f"Unsupported Muse Voice mode: {mode}")
    if encoding not in ENCODINGS:
        raise ValueError(f"Unsupported audio encoding: {encoding}")
    credential = token if token.startswith("Bearer ") else f"Bearer {token}"
    value: dict[str, object] = {
        "mode": mode,
        "authorization": {"accessToken": credential},
        "audioEncoding": encoding,
        "model": MODEL,
        "partialMode": "CUMULATIVE",
        "emitAudioProgress": False,
    }
    if keywords:
        value["keywords"] = keywords
    return value


def normalize_event(frame: dict[str, Any]) -> dict[str, Any]:
    if "type" in frame:
        return frame
    if "sessionId" in frame:
        return {"type": "session", "sessionId": frame["sessionId"]}
    return {"type": "unknown", **frame}


async def _iterate(chunks: Iterable[bytes] | AsyncIterable[bytes]):
    if hasattr(chunks, "__aiter__"):
        async for chunk in chunks:
            yield chunk
    else:
        for chunk in chunks:
            yield chunk


async def _send_audio(
    socket: object,
    chunks: Iterable[bytes] | AsyncIterable[bytes],
    *,
    encoding: str,
    pace: bool,
) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    sent = 0
    for_rate = BYTES_PER_SECOND[encoding]
    async for chunk in _iterate(chunks):
        if not isinstance(chunk, bytes) or not chunk:
            raise ValueError("Audio chunks must be non-empty bytes.")
        if pace and sent:
            delay = started + sent / for_rate - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
        await socket.send(chunk)
        sent += len(chunk)
    await socket.send(json.dumps({"type": "endStream"}))


async def _receive_final(socket: object) -> str:
    async for raw in socket:
        if isinstance(raw, bytes):
            continue
        event = normalize_event(json.loads(raw))
        if event["type"] == "error":
            raise RuntimeError(str(event.get("message", "Muse Voice failed")))
        if event["type"] != "transcript":
            continue
        transcript = event.get("transcript")
        if event.get("final") is True:
            if not isinstance(transcript, str) or not transcript.strip():
                raise RuntimeError("Muse Voice returned an empty final transcript.")
            return transcript.strip()
    raise RuntimeError("Muse Voice closed before a final transcript arrived.")


async def transcribe_connected(
    socket: object,
    chunks: Iterable[bytes] | AsyncIterable[bytes],
    handshake: dict[str, object],
    *,
    encoding: str = "PCM_16KHZ",
    pace: bool = False,
) -> str:
    await socket.send(json.dumps(handshake))
    ack = normalize_event(json.loads(await socket.recv()))
    if ack["type"] == "error":
        raise RuntimeError(str(ack.get("message", "Muse Voice handshake rejected")))
    if ack["type"] != "session" or not isinstance(ack.get("sessionId"), str):
        raise RuntimeError("Muse Voice returned an invalid session acknowledgement.")

    sender = asyncio.create_task(
        _send_audio(socket, chunks, encoding=encoding, pace=pace)
    )
    receiver = asyncio.create_task(_receive_final(socket))
    try:
        done, _pending = await asyncio.wait(
            {sender, receiver},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if receiver in done and receiver.exception() is None:
            transcript = receiver.result()
            if not sender.done():
                sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            return transcript
        if sender in done:
            await sender
        return await receiver
    finally:
        for task in (sender, receiver):
            if not task.done():
                task.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)


class MuseVoiceTranscriber:
    def __init__(
        self,
        *,
        token: str | None = None,
        mode: str = "PUSH_TO_TALK",
        encoding: str = "PCM_16KHZ",
        keywords: list[str] | None = None,
        url: str = STREAM_URL,
        connector: object | None = None,
        timeout_seconds: float = 90.0,
        pace_audio: bool = True,
    ) -> None:
        if mode != "PUSH_TO_TALK":
            raise ValueError("This one-turn adapter supports PUSH_TO_TALK mode only.")
        if encoding not in ENCODINGS:
            raise ValueError(f"Unsupported audio encoding: {encoding}")
        self.token = token
        self.mode = mode
        self.encoding = encoding
        self.keywords = keywords
        self.url = url
        self.connector = connector
        self.timeout_seconds = timeout_seconds
        self.pace_audio = pace_audio

    async def transcribe(
        self,
        chunks: Iterable[bytes] | AsyncIterable[bytes],
    ) -> str:
        token = self.token or os.environ.get("MODEL_API_KEY")
        if not token:
            raise RuntimeError("Set MODEL_API_KEY to use Muse Voice.")
        connector = self.connector
        if connector is None:
            import websockets

            connector = websockets.connect
        handshake = build_handshake(
            token,
            mode=self.mode,
            encoding=self.encoding,
            keywords=self.keywords,
        )
        async with connector(self.url, max_size=1024 * 1024) as socket:
            async with asyncio.timeout(self.timeout_seconds):
                return await transcribe_connected(
                    socket,
                    chunks,
                    handshake,
                    encoding=self.encoding,
                    pace=self.pace_audio,
                )


class FixtureVoiceTranscriber:
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript

    async def transcribe(
        self,
        chunks: Iterable[bytes] | AsyncIterable[bytes],
    ) -> str:
        async for _ in _iterate(chunks):
            pass
        return self.transcript


class FallbackTranscriber:
    """Replay one bounded utterance through a local fallback after a live failure."""

    def __init__(
        self,
        primary: object,
        fallback: object,
        *,
        max_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.max_bytes = max_bytes

    async def transcribe(
        self,
        chunks: Iterable[bytes] | AsyncIterable[bytes],
    ) -> str:
        buffered = bytearray()
        async for chunk in _iterate(chunks):
            buffered.extend(chunk)
            if len(buffered) > self.max_bytes:
                raise ValueError("Buffered audio exceeds the fallback limit.")
        encoding = getattr(self.primary, "encoding", "PCM_16KHZ")
        frame_bytes = BYTES_PER_SECOND.get(encoding, BYTES_PER_SECOND["PCM_16KHZ"])
        frame_bytes = frame_bytes * 80 // 1000
        replay = [
            bytes(buffered[index : index + frame_bytes])
            for index in range(0, len(buffered), frame_bytes)
        ]
        try:
            return await self.primary.transcribe(replay)
        except (OSError, RuntimeError, TimeoutError):
            return await self.fallback.transcribe(replay)


class FasterWhisperTranscriber:
    """Optional local fallback; model files are downloaded, not redistributed."""

    def __init__(
        self,
        *,
        model_name: str = "small.en",
        sample_rate: int = 16000,
    ) -> None:
        self.model_name = model_name
        self.sample_rate = sample_rate
        self._model: object | None = None

    def _get_model(self) -> object:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_name,
                device="cpu",
                compute_type="int8",
            )
        return self._model

    def _transcribe(self, pcm: bytes) -> str:
        with tempfile.NamedTemporaryFile(suffix=".wav") as output:
            with wave.open(output.name, "wb") as recording:
                recording.setnchannels(1)
                recording.setsampwidth(2)
                recording.setframerate(self.sample_rate)
                recording.writeframes(pcm)
            segments, _info = self._get_model().transcribe(output.name)
            transcript = " ".join(segment.text.strip() for segment in segments).strip()
        if not transcript:
            raise RuntimeError("Local transcription returned no text.")
        return transcript

    async def transcribe(
        self,
        chunks: Iterable[bytes] | AsyncIterable[bytes],
    ) -> str:
        buffered = bytearray()
        async for chunk in _iterate(chunks):
            buffered.extend(chunk)
        return await asyncio.to_thread(self._transcribe, bytes(buffered))

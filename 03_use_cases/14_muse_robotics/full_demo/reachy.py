# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import io
import threading
import time
from collections.abc import Callable
from typing import ClassVar


class FakeReachy:
    def __init__(self, camera_image: bytes) -> None:
        self.camera_image = camera_image
        self.events: list[str] = []

    def react(self, moment: str) -> None:
        self.events.append(f"react:{moment}")

    def capture(self) -> bytes:
        self.events.append("capture")
        return self.camera_image

    def speak(self, text: str) -> None:
        self.events.append(f"speak:{text}")


class ReachyMiniAdapter:
    """Opt-in Reachy Mini adapter; construction never enables motors."""

    POSES: ClassVar[dict[str, tuple[float, float]]] = {
        "listening": (0.0, -4.0),
        "thinking": (10.0, 2.0),
        "generating": (-8.0, 3.0),
        "celebrate": (0.0, -10.0),
        "confused": (-10.0, 8.0),
        "ready": (0.0, 0.0),
    }

    def __init__(
        self,
        mini: object,
        *,
        manager: object | None = None,
        speech_callback: Callable[[str], None] | None = None,
        motion_enabled: bool = False,
    ) -> None:
        self.mini = mini
        self.manager = manager
        self.speech_callback = speech_callback
        self.motion_enabled = motion_enabled
        self._camera_lock = threading.Lock()

    @classmethod
    def connect(
        cls,
        *,
        media_backend: str = "local",
        enable_motors: bool = False,
        speech_callback: Callable[[str], None] | None = None,
        start_media: bool = True,
    ) -> ReachyMiniAdapter:
        from reachy_mini import ReachyMini

        manager = ReachyMini(media_backend=media_backend)
        mini = manager.__enter__()
        try:
            if start_media:
                mini.media.start_recording()
                mini.media.start_playing()
            if enable_motors:
                mini.enable_motors()
        except Exception:
            manager.__exit__(None, None, None)
            raise
        return cls(
            mini,
            manager=manager,
            speech_callback=speech_callback,
            motion_enabled=enable_motors,
        )

    def close(self) -> None:
        try:
            if self.motion_enabled:
                try:
                    self.react("ready")
                finally:
                    self.mini.disable_motors()
                    self.motion_enabled = False
        finally:
            if self.manager is not None:
                self.manager.__exit__(None, None, None)
                self.manager = None

    def react(self, moment: str) -> None:
        if not self.motion_enabled:
            return
        from reachy_mini.utils import create_head_pose

        yaw, pitch = self.POSES.get(moment, self.POSES["ready"])
        pose = create_head_pose(yaw=yaw, pitch=pitch, degrees=True)
        self.mini.goto_target(head=pose, duration=0.6)

    def capture(self) -> bytes:
        import numpy as np
        from PIL import Image

        raw = None
        for _attempt in range(3):
            with self._camera_lock:
                raw = self.mini.media.get_frame()
            if raw is not None:
                break
            time.sleep(0.05)
        if raw is None:
            raise RuntimeError("Reachy camera returned no frame after three attempts.")
        frame = np.asarray(raw)
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise RuntimeError("Reachy camera returned an invalid frame.")
        rgb = np.ascontiguousarray(frame[:, :, ::-1])
        output = io.BytesIO()
        Image.fromarray(rgb).save(output, format="JPEG", quality=88)
        return output.getvalue()

    def capture_pcm16(self) -> tuple[bytes, int]:
        import numpy as np

        sample = self.mini.media.get_audio_sample()
        if sample is None:
            raise RuntimeError("Reachy microphone returned no audio.")
        values = np.asarray(sample, dtype=np.float32)
        if values.ndim == 2:
            values = values.mean(axis=1)
        if values.ndim != 1 or values.size == 0:
            raise RuntimeError("Reachy microphone returned invalid audio.")
        pcm = (np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2")
        rate = int(self.mini.media.get_input_audio_samplerate() or 16000)
        return pcm.tobytes(), rate

    def record_pcm16(self, duration_seconds: float) -> tuple[bytes, int]:
        if not 0.1 <= duration_seconds <= 30.0:
            raise ValueError(
                "Reachy recording duration must be between 0.1 and 30 seconds."
            )
        deadline = time.monotonic() + duration_seconds
        chunks: list[bytes] = []
        rate: int | None = None
        while time.monotonic() < deadline:
            try:
                chunk, sample_rate = self.capture_pcm16()
            except RuntimeError:
                time.sleep(0.005)
                continue
            if rate is not None and sample_rate != rate:
                raise RuntimeError(
                    "Reachy microphone sample rate changed during capture."
                )
            rate = sample_rate
            chunks.append(chunk)
        if not chunks or rate is None:
            raise RuntimeError("Reachy microphone captured no audio.")
        return b"".join(chunks), rate

    def play_pcm16(self, pcm: bytes, sample_rate: int) -> None:
        import numpy as np

        values = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767.0
        output_rate = int(self.mini.media.get_output_audio_samplerate() or sample_rate)
        if sample_rate != output_rate and values.size:
            indices = np.linspace(
                0,
                values.size - 1,
                round(values.size * output_rate / sample_rate),
            )
            values = np.interp(indices, np.arange(values.size), values).astype(
                np.float32
            )
        self.mini.media.push_audio_sample(values.reshape(-1, 1))

    def speak(self, text: str) -> None:
        if self.speech_callback is None:
            raise RuntimeError(
                "Reachy Mini does not provide text-to-speech; configure an external "
                "speech callback such as Piper or Kokoro."
            )
        self.speech_callback(text)

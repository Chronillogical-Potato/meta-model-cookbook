from __future__ import annotations

import io
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

RECIPE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECIPE_DIR))

from full_demo.reachy import FakeReachy, ReachyMiniAdapter


class FakeMedia:
    def __init__(self) -> None:
        self.played: np.ndarray | None = None

    def get_frame(self) -> np.ndarray:
        frame = np.zeros((8, 10, 3), dtype=np.uint8)
        frame[:, :, 0] = 255
        return frame

    def get_audio_sample(self) -> np.ndarray:
        return np.asarray([[1.0, -1.0], [0.5, 0.5]], dtype=np.float32)

    def get_input_audio_samplerate(self) -> int:
        return 16000

    def get_output_audio_samplerate(self) -> int:
        return 48000

    def push_audio_sample(self, values: np.ndarray) -> None:
        self.played = values


class FakeMini:
    def __init__(self) -> None:
        self.media = FakeMedia()
        self.targets: list[tuple[object, float]] = []
        self.motors_disabled = False

    def disable_motors(self) -> None:
        self.motors_disabled = True

    def goto_target(self, *, head: object, duration: float) -> None:
        self.targets.append((head, duration))


class FakeManager:
    def __init__(self) -> None:
        self.exited = False

    def __exit__(self, *_arguments: object) -> None:
        self.exited = True


class ReachyTest(unittest.TestCase):
    def test_fake_reachy_records_high_level_events(self) -> None:
        reachy = FakeReachy(b"image")
        reachy.react("thinking")
        reachy.speak("hello")
        self.assertEqual(reachy.capture(), b"image")
        self.assertEqual(
            reachy.events,
            ["react:thinking", "speak:hello", "capture"],
        )

    def test_camera_converts_reachy_bgr_to_rgb(self) -> None:
        output = ReachyMiniAdapter(FakeMini()).capture()
        with Image.open(io.BytesIO(output)) as image:
            red, _green, blue = image.getpixel((0, 0))
        self.assertGreater(blue, red)

    def test_reachy_audio_converts_stereo_and_resamples_playback(self) -> None:
        mini = FakeMini()
        adapter = ReachyMiniAdapter(mini)
        pcm, rate = adapter.capture_pcm16()
        self.assertEqual(rate, 16000)
        self.assertEqual(len(pcm), 4)
        adapter.play_pcm16(pcm, rate)
        self.assertIsNotNone(mini.media.played)
        assert mini.media.played is not None
        self.assertEqual(mini.media.played.shape, (6, 1))

    def test_reaction_uses_a_bounded_head_pose(self) -> None:
        fake_utils = types.ModuleType("reachy_mini.utils")
        fake_utils.create_head_pose = lambda **values: values
        mini = FakeMini()
        with patch.dict(sys.modules, {"reachy_mini.utils": fake_utils}):
            ReachyMiniAdapter(mini, motion_enabled=True).react("celebrate")
        self.assertEqual(len(mini.targets), 1)
        pose, duration = mini.targets[0]
        self.assertEqual(pose["yaw"], 0.0)
        self.assertEqual(duration, 0.6)

    def test_reaction_is_a_noop_without_the_motor_capability(self) -> None:
        mini = FakeMini()
        ReachyMiniAdapter(mini, motion_enabled=False).react("celebrate")
        self.assertEqual(mini.targets, [])

    def test_close_returns_neutral_and_disables_motors(self) -> None:
        fake_utils = types.ModuleType("reachy_mini.utils")
        fake_utils.create_head_pose = lambda **values: values
        mini = FakeMini()
        manager = FakeManager()
        adapter = ReachyMiniAdapter(
            mini,
            manager=manager,
            motion_enabled=True,
        )
        with patch.dict(sys.modules, {"reachy_mini.utils": fake_utils}):
            adapter.close()
        self.assertTrue(mini.motors_disabled)
        self.assertTrue(manager.exited)
        self.assertEqual(mini.targets[-1][0]["yaw"], 0.0)
        self.assertFalse(adapter.motion_enabled)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

RECIPE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECIPE_DIR))

from full_demo.muse_image import (
    FixtureImageStyler,
    ImageSizeFaceGate,
    MuseImageStyler,
    extract_image,
    inspect_image,
)


class FakeResponses:
    def __init__(self, result: bytes) -> None:
        self.result = result
        self.arguments: dict[str, object] | None = None

    def create(self, **arguments: object) -> object:
        self.arguments = arguments
        item = SimpleNamespace(
            type="image_generation_call",
            result=base64.b64encode(self.result).decode(),
        )
        return SimpleNamespace(output=[SimpleNamespace(type="reasoning"), item])


class MuseImageTest(unittest.TestCase):
    def make_image(self) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (160, 160), "#F2B38D").save(output, format="PNG")
        return output.getvalue()

    def test_live_adapter_uses_public_responses_contract(self) -> None:
        generated = self.make_image()
        responses = FakeResponses(generated)
        client = SimpleNamespace(responses=responses)
        result = MuseImageStyler(client).style(self.make_image(), "watercolor")
        self.assertEqual(result, generated)
        assert responses.arguments is not None
        self.assertEqual(responses.arguments["model"], "muse-image-1.0")
        self.assertFalse(responses.arguments["store"])
        self.assertEqual(
            responses.arguments["tools"],
            [{"type": "image_generation", "output_format": "png"}],
        )
        content = responses.arguments["input"][0]["content"]
        self.assertEqual(
            [part["type"] for part in content], ["input_text", "input_image"]
        )

    def test_extract_image_rejects_missing_image_item(self) -> None:
        with self.assertRaisesRegex(ValueError, "no generated image"):
            extract_image(SimpleNamespace(output=[]))

    def test_fixture_styler_is_deterministic_and_keeps_input_ephemeral(self) -> None:
        source = self.make_image()
        styler = FixtureImageStyler()
        first = styler.style(source, "comic")
        second = styler.style(source, "comic")
        self.assertEqual(first, second)
        with Image.open(io.BytesIO(first)) as result:
            self.assertEqual(result.size, (160, 160))

    def test_image_gate_rejects_invalid_or_tiny_inputs(self) -> None:
        gate = ImageSizeFaceGate(minimum_side=96)
        self.assertFalse(gate.contains_face(b"not an image"))
        output = io.BytesIO()
        Image.new("RGB", (32, 32)).save(output, format="PNG")
        self.assertFalse(gate.contains_face(output.getvalue()))
        self.assertTrue(gate.contains_face(self.make_image()))

    def test_generated_bytes_must_be_a_supported_image(self) -> None:
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="image_generation_call",
                    result=base64.b64encode(b"not an image").decode(),
                )
            ]
        )
        with self.assertRaisesRegex(ValueError, "could not be decoded"):
            extract_image(response)

    def test_pixel_limit_is_checked_before_full_decode(self) -> None:
        with (
            patch("full_demo.muse_image.MAX_IMAGE_PIXELS", 100),
            self.assertRaisesRegex(ValueError, "megapixel"),
        ):
            inspect_image(self.make_image())


if __name__ == "__main__":
    unittest.main()

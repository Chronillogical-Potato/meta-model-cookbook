from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

RECIPE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECIPE_DIR))

from full_demo.sam_preview import (
    ColorMaskSegmenter,
    MuseSamSegmenter,
    SamPredictorSegmenter,
    overlay_mask,
)


class FakeResponses:
    def __init__(self, output_text: str) -> None:
        self.output_text = output_text
        self.arguments: dict[str, object] | None = None

    def create(self, **arguments: object) -> object:
        self.arguments = arguments
        return SimpleNamespace(status="completed", output_text=self.output_text)


class SamPreviewTest(unittest.TestCase):
    def make_scene(self) -> bytes:
        image = Image.new("RGB", (20, 12), "white")
        for x in range(3, 9):
            for y in range(2, 10):
                image.putpixel((x, y), (230, 20, 20))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def test_offline_segmenter_finds_requested_color(self) -> None:
        scene = self.make_scene()
        mask = ColorMaskSegmenter().segment(scene, "red")
        self.assertIsNotNone(mask)
        assert mask is not None
        self.assertEqual(mask.pixels.count(1), 48)
        preview = overlay_mask(scene, mask)
        with Image.open(io.BytesIO(preview)) as image:
            self.assertEqual(image.size, (20, 12))

    def test_offline_segmenter_returns_none_when_target_is_absent(self) -> None:
        self.assertIsNone(ColorMaskSegmenter().segment(self.make_scene(), "blue"))

    def test_predictor_adapter_rejects_inconsistent_geometry(self) -> None:
        segmenter = SamPredictorSegmenter(
            lambda _image, _concept: [[True], [True, False]]
        )
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            segmenter.segment(self.make_scene(), "red")

    def test_hosted_adapter_uses_public_contract_and_decodes_mask(self) -> None:
        output_text = (
            "<0f>0<|box;x1=4;y1=2;x2=11;y2=7;w=20;h=12|>"
            "<|mask;x=0;y=0;data=2,2,!!!!!&T:u9`|>"
        )
        responses = FakeResponses(output_text)
        client = SimpleNamespace(responses=responses)
        mask = MuseSamSegmenter(client).segment(self.make_scene(), "red block")
        self.assertIsNotNone(mask)
        assert mask is not None
        self.assertEqual((mask.width, mask.height), (20, 12))
        self.assertEqual(mask.pixels.count(1), 24)
        self.assertEqual(mask.pixels[0], 0)
        self.assertEqual(mask.pixels[2 * mask.width + 4], 1)
        self.assertEqual(mask.pixels[2 * mask.width + 10], 0)
        self.assertEqual(mask.pixels[7 * mask.width + 10], 1)
        assert responses.arguments is not None
        self.assertEqual(responses.arguments["model"], "sam-3.1")
        self.assertEqual(responses.arguments["timeout"], 5.0)
        self.assertEqual(
            responses.arguments["metadata"],
            {"mask_encoding": "one_bit"},
        )

    def test_hosted_adapter_accepts_a_valid_empty_result(self) -> None:
        responses = FakeResponses("")
        client = SimpleNamespace(responses=responses)
        self.assertIsNone(MuseSamSegmenter(client).segment(self.make_scene(), "bus"))


if __name__ == "__main__":
    unittest.main()

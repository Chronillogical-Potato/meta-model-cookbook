# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import io
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence

from .models import Mask
from .muse_image import image_data_url, inspect_image

BASE_URL = "https://api.meta.ai/v1"
SAM_MODEL = "sam-3.1"
SAM_TIMEOUT_SECONDS = 5.0
MAX_PIXELS = 4_000_000
TARGET_COLORS = {"red", "green", "blue"}
CONCEPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,79}\Z")


def _field(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _validate_source(image: bytes) -> tuple[int, int]:
    _, _, (width, height) = inspect_image(image)
    if width * height > MAX_PIXELS:
        raise ValueError("Preview image exceeds the four-megapixel limit.")
    return width, height


def _matches(pixel: tuple[int, int, int], concept: str) -> bool:
    red, green, blue = pixel
    if concept == "red":
        return red > 110 and red > green * 1.35 and red > blue * 1.35
    if concept == "green":
        return green > 90 and green > red * 1.2 and green > blue * 1.2
    if concept == "blue":
        return blue > 100 and blue > red * 1.25 and blue > green * 1.15
    raise ValueError(f"Unsupported preview concept: {concept!r}")


class ColorMaskSegmenter:
    """Deterministic offline stand-in for the presentation-only SAM port."""

    def segment(self, image: bytes, concept: str) -> Mask | None:
        from PIL import Image

        concept = concept.lower().removesuffix("_block")
        if concept not in TARGET_COLORS:
            raise ValueError(f"Unsupported preview concept: {concept!r}")
        width, height = _validate_source(image)
        with Image.open(io.BytesIO(image)) as source:
            rgb = source.convert("RGB")
        pixels = bytes(
            1 if _matches(pixel, concept) else 0 for pixel in rgb.get_flattened_data()
        )
        if not any(pixels):
            return None
        return Mask(width, height, pixels)


class SamPredictorSegmenter:
    """Adapter for an externally installed, publicly licensed SAM predictor."""

    def __init__(
        self,
        predictor: Callable[[bytes, str], Sequence[Sequence[bool]]],
    ) -> None:
        self.predictor = predictor

    def segment(self, image: bytes, concept: str) -> Mask | None:
        _validate_source(image)
        rows = self.predictor(image, concept)
        height = len(rows)
        if height == 0:
            return None
        width = len(rows[0])
        if width <= 0 or any(len(row) != width for row in rows):
            raise ValueError("SAM predictor returned inconsistent mask rows.")
        if width * height > MAX_PIXELS:
            raise ValueError("SAM mask exceeds the pixel limit.")
        pixels = bytes(1 if value else 0 for row in rows for value in row)
        if not any(pixels):
            return None
        return Mask(width, height, pixels)


class MuseSamSegmenter:
    """Hosted public SAM 3.1 adapter for presentation-only previews."""

    def __init__(self, client: object | None = None) -> None:
        self._provided_client = client
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._provided_client is not None:
            return self._provided_client
        if self._client is None:
            from openai import OpenAI

            token = os.environ.get("MODEL_API_KEY")
            if not token:
                raise RuntimeError("Set MODEL_API_KEY to use hosted SAM.")
            self._client = OpenAI(
                base_url=BASE_URL,
                api_key=token,
                timeout=SAM_TIMEOUT_SECONDS,
                max_retries=0,
            )
        return self._client

    def segment(self, image: bytes, concept: str) -> Mask | None:
        width, height = _validate_source(image)
        prompt = concept.replace("_", " ").strip()
        if CONCEPT.fullmatch(prompt) is None or "," in prompt:
            raise ValueError("SAM concept must be one short noun phrase.")
        response = self._get_client().responses.create(
            model=SAM_MODEL,
            input=[
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_data_url(image)},
                    ],
                }
            ],
            metadata={"mask_encoding": "one_bit"},
            store=False,
            timeout=SAM_TIMEOUT_SECONDS,
        )
        status = _field(response, "status")
        if status not in {None, "completed"}:
            raise RuntimeError(f"SAM response did not complete: {status}")
        output_text = _field(response, "output_text")
        if not isinstance(output_text, str):
            raise TypeError("SAM returned no output_text field.")
        if not output_text:
            return None
        return _parse_hosted_mask(output_text, width, height)


def _parse_hosted_mask(output_text: str, width: int, height: int) -> Mask | None:
    from meta_sam_parser import (
        CompletedOutcome,
        SegmentationMaskRecord,
        decode_mask_to_raster,
        image_segmentation_format,
    )
    from PIL import Image

    parser = image_segmentation_format().create_parser()
    parser.push(output_text)
    result = parser.finish(CompletedOutcome()).result
    combined = bytearray(width * height)
    found = False
    for record in result.records:
        if not isinstance(record, SegmentationMaskRecord):
            continue
        raster = decode_mask_to_raster(record.mask)
        mask_width = record.mask.width
        mask_height = record.mask.height
        if len(raster) != mask_width * mask_height:
            raise RuntimeError("SAM returned an invalid mask raster.")
        image = Image.frombytes("L", (mask_width, mask_height), raster)
        left = max(0, math.floor(record.bounds.left))
        top = max(0, math.floor(record.bounds.top))
        right = min(width, math.ceil(record.bounds.right))
        bottom = min(height, math.ceil(record.bounds.bottom))
        if right <= left or bottom <= top:
            raise RuntimeError("SAM returned invalid source bounds.")
        box_size = (right - left, bottom - top)
        if image.size != box_size:
            image = image.resize(box_size, Image.Resampling.NEAREST)
        positioned = Image.new("L", (width, height))
        positioned.paste(image, (left, top))
        for index, value in enumerate(positioned.get_flattened_data()):
            if value:
                combined[index] = 1
                found = True
    if not found:
        return None
    return Mask(width, height, bytes(combined))


def overlay_mask(image: bytes, mask: Mask) -> bytes:
    from PIL import Image

    _validate_source(image)
    with Image.open(io.BytesIO(image)) as source:
        base = source.convert("RGBA")
    if base.size != (mask.width, mask.height):
        raise ValueError("Mask geometry does not match the source image.")
    alpha = Image.frombytes(
        "L", base.size, bytes(120 if bit else 0 for bit in mask.pixels)
    )
    color = Image.new("RGBA", base.size, (0, 210, 255, 0))
    color.putalpha(alpha)
    composed = Image.alpha_composite(base, color)
    output = io.BytesIO()
    composed.save(output, format="PNG", optimize=True)
    return output.getvalue()

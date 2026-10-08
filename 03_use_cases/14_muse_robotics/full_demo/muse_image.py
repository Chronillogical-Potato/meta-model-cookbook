from __future__ import annotations

import base64
import binascii
import io
import os
import warnings
from collections.abc import Mapping

BASE_URL = "https://api.meta.ai/v1"
MODEL = "muse-image-1.0"
MAX_IMAGE_PIXELS = 16_000_000
SUPPORTED_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}
STYLE_PROMPTS = {
    "watercolor": "restyle this portrait as a bright watercolor illustration",
    "comic": "restyle this portrait as clean, colorful comic-book art",
    "neon": "restyle this portrait as a tasteful neon-lit futuristic scene",
    "clay": "restyle this portrait as a friendly handcrafted clay figure",
    "sketch": "restyle this portrait as an expressive pencil-and-ink sketch",
}


def inspect_image(image: bytes) -> tuple[str, str, tuple[int, int]]:
    from PIL import Image

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image)) as source:
                image_format = source.format or ""
                width, height = source.size
                if image_format not in SUPPORTED_IMAGE_FORMATS:
                    raise ValueError("Input must be a JPEG, PNG, or WebP image.")
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("Image exceeds the 16-megapixel limit.")
                source.verify()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("Image exceeds the safe decode limit.") from error
    except OSError as error:
        raise ValueError("Image data could not be decoded.") from error
    mime_type, extension = SUPPORTED_IMAGE_FORMATS[image_format]
    return mime_type, extension, (width, height)


def image_extension(image: bytes) -> str:
    return inspect_image(image)[1]


def image_data_url(image: bytes, mime_type: str | None = None) -> str:
    detected_mime, _, _ = inspect_image(image)
    if mime_type is not None and mime_type != detected_mime:
        raise ValueError("Declared image MIME type does not match its bytes.")
    encoded = base64.b64encode(image).decode("ascii")
    return f"data:{detected_mime};base64,{encoded}"


def _field(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def extract_image(response: object) -> bytes:
    output = _field(response, "output")
    if not isinstance(output, (list, tuple)):
        raise TypeError("Muse Image returned no output list.")
    for item in output:
        if _field(item, "type") != "image_generation_call":
            continue
        result = _field(item, "result")
        if not isinstance(result, str):
            continue
        try:
            image = base64.b64decode(result, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError(
                "Muse Image returned invalid base64 image data."
            ) from error
        image_extension(image)
        return image
    raise ValueError("Muse Image returned no generated image.")


class MuseImageStyler:
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
                raise RuntimeError("Set MODEL_API_KEY to use Muse Image.")
            self._client = OpenAI(
                base_url=BASE_URL,
                api_key=token,
            )
        return self._client

    def style(self, image: bytes, style: str) -> bytes:
        try:
            prompt = STYLE_PROMPTS[style]
        except KeyError as error:
            raise ValueError(f"Unknown image style: {style!r}") from error
        response = self._get_client().responses.create(
            model=MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"{prompt}. Preserve identity and framing.",
                        },
                        {
                            "type": "input_image",
                            "image_url": image_data_url(image),
                        },
                    ],
                }
            ],
            tools=[{"type": "image_generation", "output_format": "png"}],
            store=False,
        )
        return extract_image(response)


class FixtureImageStyler:
    """Deterministic local transform used by tests and the offline kiosk."""

    def style(self, image: bytes, style: str) -> bytes:
        from PIL import Image, ImageDraw, ImageEnhance

        if style not in STYLE_PROMPTS:
            raise ValueError(f"Unknown image style: {style!r}")
        inspect_image(image)
        with Image.open(io.BytesIO(image)) as source:
            canvas = ImageEnhance.Color(source.convert("RGB")).enhance(1.35)
        overlay = Image.new("RGBA", canvas.size, (20, 90, 210, 35))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(canvas)
        label = f"OFFLINE {style.upper()} FIXTURE"
        draw.rounded_rectangle((18, 18, 260, 52), radius=12, fill=(5, 20, 48, 220))
        draw.text((30, 29), label, fill="white")
        output = io.BytesIO()
        canvas.convert("RGB").save(output, format="PNG", optimize=True)
        return output.getvalue()


class ImageSizeFaceGate:
    """Safe default size gate for the offline fixture; it is not face detection."""

    def __init__(self, minimum_side: int = 96) -> None:
        self.minimum_side = minimum_side

    def contains_face(self, image: bytes) -> bool:
        try:
            _, _, size = inspect_image(image)
            return min(size) >= self.minimum_side
        except ValueError:
            return False


class OpenCVYuNetFaceGate:
    """Optional face gate; callers provide a separately licensed YuNet model."""

    def __init__(self, model_path: str, score_threshold: float = 0.8) -> None:
        self.model_path = model_path
        self.score_threshold = score_threshold

    def contains_face(self, image: bytes) -> bool:
        import cv2
        import numpy as np

        try:
            inspect_image(image)
        except ValueError:
            return False
        encoded = np.frombuffer(image, dtype=np.uint8)
        frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if frame is None:
            return False
        height, width = frame.shape[:2]
        detector = cv2.FaceDetectorYN.create(
            self.model_path,
            "",
            (width, height),
            self.score_threshold,
        )
        _, faces = detector.detect(frame)
        return faces is not None and len(faces) > 0

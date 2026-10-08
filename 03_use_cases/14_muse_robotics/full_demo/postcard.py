from __future__ import annotations

import hashlib
import io
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

POSTCARD_SIZE = (1748, 1181)
CUPS_COMMAND_TIMEOUT_SECONDS = 10.0
SAFE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


class PostcardComposer:
    def __init__(self, size: tuple[int, int] = POSTCARD_SIZE) -> None:
        self.size = size

    def compose(
        self,
        image: bytes,
        *,
        title: str = "Muse robotics",
        qr_url: str | None = None,
    ) -> bytes:
        from PIL import Image, ImageDraw, ImageOps

        width, height = self.size
        canvas = Image.new("RGB", self.size, "#071B40")
        draw = ImageDraw.Draw(canvas)
        margin = 64
        side_panel = 440
        photo_box = (width - side_panel - margin * 3, height - margin * 2)
        with Image.open(io.BytesIO(image)) as source:
            photo = ImageOps.exif_transpose(source).convert("RGB")
        fitted = ImageOps.contain(photo, photo_box)
        photo_x = margin + (photo_box[0] - fitted.width) // 2
        photo_y = margin + (photo_box[1] - fitted.height) // 2
        canvas.paste(fitted, (photo_x, photo_y))

        panel_x = width - side_panel - margin
        draw.rounded_rectangle(
            (panel_x, margin, width - margin, height - margin),
            radius=34,
            fill="#FFFFFF",
        )
        draw.text((panel_x + 42, margin + 56), title[:48], fill="#071B40")
        draw.text(
            (panel_x + 42, margin + 96),
            "Voice → plan → robot → image",
            fill="#47627F",
        )
        draw.text(
            (panel_x + 42, margin + 126),
            "A hardware-optional cookbook demo",
            fill="#47627F",
        )

        if qr_url:
            if not qr_url.startswith(("https://", "http://")):
                raise ValueError("QR URL must be an explicit HTTP(S) URL.")
            try:
                import qrcode
            except ImportError as error:
                raise RuntimeError("Install qrcode to render a QR postcard.") from error
            qr = qrcode.make(qr_url).convert("RGB")
            qr = qr.resize((320, 320), Image.Resampling.NEAREST)
            canvas.paste(qr, (panel_x + 60, height - margin - 390))
            draw.text(
                (panel_x + 76, height - margin - 52),
                "Explore the recipe",
                fill="#071B40",
            )

        output = io.BytesIO()
        canvas.save(output, format="JPEG", quality=95, dpi=(300, 300), subsampling=0)
        return output.getvalue()


class FilePrinter:
    def __init__(self, output_dir: Path | None = None) -> None:
        self.output_dir = output_dir

    def submit(
        self,
        image: bytes,
        *,
        idempotency_key: str,
        output_dir: Path | None = None,
        commit: Callable[[Callable[[], str]], str] | None = None,
    ) -> str:
        destination = output_dir or self.output_dir
        if destination is None:
            raise ValueError("File printing requires an output directory.")
        if SAFE_KEY.fullmatch(idempotency_key) is None:
            raise ValueError("Invalid print idempotency key.")
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"{idempotency_key}.jpg"

        def write_file() -> str:
            if target.is_symlink():
                raise ValueError("Refusing to write through a symlink.")
            if target.exists():
                if target.read_bytes() != image:
                    raise RuntimeError(
                        "Print idempotency key collides with different data."
                    )
                return target.name
            temporary = destination / f".{idempotency_key}.tmp"
            temporary.write_bytes(image)
            temporary.replace(target)
            return target.name

        return commit(write_file) if commit is not None else write_file()


class CupsPrinter:
    """Explicit physical printer backend; never used by the offline demo."""

    def __init__(
        self,
        printer: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        confirm: Callable[[str], bool] | None = None,
        cooldown_seconds: float = 10.0,
    ) -> None:
        if SAFE_KEY.fullmatch(printer) is None:
            raise ValueError("Printer name contains unsupported characters.")
        self.printer = printer
        self.runner = runner
        self.confirm = confirm
        self.cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._submitted: dict[str, tuple[bytes, str]] = {}
        self._last_submitted_at: float | None = None

    def submit(
        self,
        image: bytes,
        *,
        idempotency_key: str,
        output_dir: Path | None = None,
        commit: Callable[[Callable[[], str]], str] | None = None,
    ) -> str:
        del output_dir
        if SAFE_KEY.fullmatch(idempotency_key) is None:
            raise ValueError("Invalid print idempotency key.")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("A print job is already being submitted.")
        try:
            digest = hashlib.sha256(image).digest()
            previous = self._submitted.get(idempotency_key)
            if previous is not None:
                if previous[0] != digest:
                    raise RuntimeError(
                        "Print idempotency key collides with different data."
                    )
                return previous[1]
            now = time.monotonic()
            if (
                self._last_submitted_at is not None
                and now - self._last_submitted_at < self.cooldown_seconds
            ):
                raise RuntimeError("Physical print cooldown is still active.")
            if self.confirm is None or not self.confirm(idempotency_key):
                raise RuntimeError(
                    "The operator did not confirm this physical print job."
                )

            def operation() -> str:
                return self._submit_locked(
                    image,
                    idempotency_key=idempotency_key,
                )

            receipt = commit(operation) if commit is not None else operation()
            self._submitted[idempotency_key] = (digest, receipt)
            self._last_submitted_at = time.monotonic()
            return receipt
        finally:
            self._lock.release()

    def _submit_locked(self, image: bytes, *, idempotency_key: str) -> str:
        status = self.runner(
            ["lpstat", "-p", self.printer],
            check=False,
            capture_output=True,
            text=True,
            timeout=CUPS_COMMAND_TIMEOUT_SECONDS,
        )
        if status.returncode != 0 or "disabled" in status.stdout.lower():
            detail = status.stderr.strip() or status.stdout.strip()
            raise RuntimeError(f"Printer is unavailable: {detail}")
        accepting = self.runner(
            ["lpstat", "-a", self.printer],
            check=False,
            capture_output=True,
            text=True,
            timeout=CUPS_COMMAND_TIMEOUT_SECONDS,
        )
        accepting_text = accepting.stdout.lower()
        if (
            accepting.returncode != 0
            or self.printer.lower() not in accepting_text
            or "accepting requests" not in accepting_text
        ):
            raise RuntimeError("Printer queue is not accepting jobs.")
        with tempfile.TemporaryDirectory(prefix="muse-postcard-") as directory:
            path = Path(directory) / f"{idempotency_key}.jpg"
            path.write_bytes(image)
            command: Sequence[str] = (
                "lp",
                "-d",
                self.printer,
                "-o",
                "media=Postcard",
                "-o",
                "fit-to-page",
                str(path),
            )
            result = self.runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=CUPS_COMMAND_TIMEOUT_SECONDS,
            )
        if result.returncode != 0:
            raise RuntimeError(f"Print submission failed: {result.stderr.strip()}")
        return f"cups:{result.stdout.strip() or idempotency_key}"

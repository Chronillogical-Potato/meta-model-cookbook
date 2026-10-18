# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

RECIPE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECIPE_DIR))

from full_demo.postcard import (
    POSTCARD_SIZE,
    CupsPrinter,
    FilePrinter,
    PostcardComposer,
)


class PostcardTest(unittest.TestCase):
    def make_image(self) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (800, 450), "#4D9AEA").save(output, format="PNG")
        return output.getvalue()

    def test_composer_outputs_300_dpi_postcard_without_cropping(self) -> None:
        result = PostcardComposer().compose(self.make_image())
        with Image.open(io.BytesIO(result)) as postcard:
            self.assertEqual(postcard.size, POSTCARD_SIZE)
            self.assertEqual(postcard.format, "JPEG")

    def test_qr_requires_explicit_http_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit"):
            PostcardComposer().compose(self.make_image(), qr_url="private-path")

    def test_file_backend_is_idempotent_and_confined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            printer = FilePrinter(Path(directory))
            first = printer.submit(b"first", idempotency_key="run-7")
            second = printer.submit(b"first", idempotency_key="run-7")
            self.assertEqual(first, second)
            self.assertEqual((Path(directory) / first).read_bytes(), b"first")
            with self.assertRaisesRegex(RuntimeError, "collides"):
                printer.submit(b"second", idempotency_key="run-7")
            with self.assertRaisesRegex(ValueError, "Invalid"):
                printer.submit(b"bad", idempotency_key="../outside")

    def test_cups_backend_constructs_a_shell_free_command(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str] | tuple[str, ...], **_arguments: object):
            calls.append(list(command))
            if list(command[:2]) == ["lpstat", "-p"]:
                output = "printer Test_Printer is idle. enabled since now"
            elif list(command[:2]) == ["lpstat", "-a"]:
                output = "Test_Printer accepting requests since now"
            else:
                output = "request id Test_Printer-1"
            return subprocess.CompletedProcess(command, 0, output, "")

        result = CupsPrinter(
            "Test_Printer",
            runner=runner,
            confirm=lambda _key: True,
        ).submit(
            b"jpeg",
            idempotency_key="run-8",
        )
        self.assertEqual(result, "cups:request id Test_Printer-1")
        self.assertEqual(calls[0], ["lpstat", "-p", "Test_Printer"])
        self.assertEqual(calls[1], ["lpstat", "-a", "Test_Printer"])
        self.assertEqual(
            calls[2][:7],
            ["lp", "-d", "Test_Printer", "-o", "media=Postcard", "-o", "fit-to-page"],
        )

    def test_cups_requires_per_job_confirmation(self) -> None:
        printer = CupsPrinter("Test_Printer", confirm=lambda _key: False)
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            printer.submit(b"jpeg", idempotency_key="run-9")

    def test_cups_rejects_a_paused_queue(self) -> None:
        def runner(command: list[str], **_arguments: object):
            return subprocess.CompletedProcess(
                command,
                0,
                "printer Test_Printer disabled since now",
                "",
            )

        printer = CupsPrinter(
            "Test_Printer",
            runner=runner,
            confirm=lambda _key: True,
        )
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            printer.submit(b"jpeg", idempotency_key="run-10")

    def test_cups_deduplicates_before_enforcing_cooldown(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str] | tuple[str, ...], **_arguments: object):
            calls.append(list(command))
            if list(command[:2]) == ["lpstat", "-p"]:
                output = "printer Test_Printer is idle. enabled since now"
            elif list(command[:2]) == ["lpstat", "-a"]:
                output = "Test_Printer accepting requests since now"
            else:
                output = "request id Test_Printer-2"
            return subprocess.CompletedProcess(command, 0, output, "")

        printer = CupsPrinter(
            "Test_Printer",
            runner=runner,
            confirm=lambda _key: True,
            cooldown_seconds=60.0,
        )
        first = printer.submit(b"jpeg", idempotency_key="run-11")
        second = printer.submit(b"jpeg", idempotency_key="run-11")
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 3)
        with self.assertRaisesRegex(RuntimeError, "cooldown"):
            printer.submit(b"jpeg", idempotency_key="run-12")


if __name__ == "__main__":
    unittest.main()

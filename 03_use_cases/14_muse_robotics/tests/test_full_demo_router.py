# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import sys
import unittest
from pathlib import Path

RECIPE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECIPE_DIR))

from full_demo.models import Intent
from full_demo.router import route_text


class RouterTest(unittest.TestCase):
    def test_voice_print_defaults_to_qr(self) -> None:
        route = route_text("Please print my latest photo")
        self.assertEqual(route.intent, Intent.PRINT)
        self.assertTrue(route.include_qr)

    def test_explicit_no_qr_is_preserved(self) -> None:
        route = route_text("Print my photo without QR")
        self.assertFalse(route.include_qr)

    def test_selfie_style_is_bounded(self) -> None:
        route = route_text("Take a neon selfie")
        self.assertEqual(route.intent, Intent.SELFIE)
        self.assertEqual(route.style, "neon")

    def test_robot_objects_preserve_spoken_order(self) -> None:
        route = route_text("Put blue and red blocks in the bin")
        self.assertEqual(route.intent, Intent.ROBOT)
        self.assertEqual(route.objects, ("blue", "red"))

    def test_unknown_request_routes_to_chat(self) -> None:
        route = route_text("What can this demo do?")
        self.assertEqual(route.intent, Intent.CHAT)

    def test_print_requires_an_exact_non_negated_command(self) -> None:
        for request in (
            "show me a blueprint",
            "scan my fingerprint",
            "do not print my photo",
            "never print this image",
        ):
            with self.subTest(request=request):
                self.assertEqual(route_text(request).intent, Intent.CHAT)

    def test_negated_camera_and_robot_requests_are_chat_only(self) -> None:
        for request in (
            "I don't want a selfie",
            "no selfie please",
            "don't take my portrait",
            "don't move the red block",
            "put the block that isn't red in the bin",
        ):
            with self.subTest(request=request):
                self.assertEqual(route_text(request).intent, Intent.CHAT)


if __name__ == "__main__":
    unittest.main()

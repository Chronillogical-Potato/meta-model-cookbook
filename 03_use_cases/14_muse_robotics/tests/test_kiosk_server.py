from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

RECIPE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECIPE_DIR))

from full_demo.muse_image import FixtureImageStyler, ImageSizeFaceGate
from full_demo.muse_spark import FixtureChat
from full_demo.postcard import FilePrinter, PostcardComposer
from full_demo.reachy import FakeReachy
from full_demo.sam_preview import ColorMaskSegmenter
from full_demo.server import TOKEN_HEADER, make_handler
from full_demo.session import DemoSession


class FakeRobot:
    def run(self, request: str, output_dir: Path) -> dict[str, object]:
        return {"check": {"success": True}}


class KioskServerTest(unittest.TestCase):
    def test_kiosk_enforces_origin_token_and_registered_outputs(self) -> None:
        portrait = (RECIPE_DIR / "assets/muse_image_input.png").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            session = DemoSession(
                output_dir=Path(directory),
                robot=FakeRobot(),
                styler=FixtureImageStyler(),
                face_gate=ImageSizeFaceGate(),
                segmenter=ColorMaskSegmenter(),
                reachy=FakeReachy(portrait),
                postcard=PostcardComposer(),
                printer=FilePrinter(),
                chat=FixtureChat(),
            )
            token = "test-kiosk-token"
            handler = make_handler(
                session,
                RECIPE_DIR / "full_demo/static",
                access_token=token,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(f"{base}/") as response:
                    self.assertIn(token, response.read().decode())
                    self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                    self.assertIn(
                        "frame-ancestors 'none'",
                        response.headers["Content-Security-Policy"],
                    )
                with urllib.request.urlopen(
                    f"{base}/asset/muse_robotics_hero.png"
                ) as response:
                    self.assertEqual(response.headers["Content-Type"], "image/png")
                    self.assertTrue(response.read().startswith(b"\x89PNG"))
                with urllib.request.urlopen(
                    f"{base}/asset/muse_robotics_demo.mp4"
                ) as response:
                    self.assertEqual(response.headers["Content-Type"], "video/mp4")
                    self.assertGreater(len(response.read()), 500_000)
                with urllib.request.urlopen(
                    f"{base}/asset/mujoco_scene.png"
                ) as response:
                    self.assertEqual(response.headers["Content-Type"], "image/png")
                with self.assertRaises(urllib.error.HTTPError) as refused_asset:
                    urllib.request.urlopen(f"{base}/asset/architecture.svg")
                self.assertEqual(refused_asset.exception.code, 404)
                with self.assertRaises(urllib.error.HTTPError) as missing_token:
                    urllib.request.urlopen(f"{base}/state")
                self.assertEqual(missing_token.exception.code, 403)

                state_request = urllib.request.Request(
                    f"{base}/state",
                    headers={TOKEN_HEADER: token},
                )
                with urllib.request.urlopen(state_request) as response:
                    state = json.load(response)
                self.assertEqual(state["stage"], "idle")

                body = json.dumps({"request": "take a watercolor selfie"}).encode()
                cross_origin = urllib.request.Request(
                    f"{base}/action",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        TOKEN_HEADER: token,
                        "Origin": "https://attacker.example",
                    },
                )
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    urllib.request.urlopen(cross_origin)
                self.assertEqual(refused.exception.code, 403)

                wrong_type = urllib.request.Request(
                    f"{base}/action",
                    data=body,
                    headers={"Content-Type": "text/plain", TOKEN_HEADER: token},
                )
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    urllib.request.urlopen(wrong_type)
                self.assertEqual(refused.exception.code, 415)

                action = urllib.request.Request(
                    f"{base}/action",
                    data=body,
                    headers={"Content-Type": "application/json", TOKEN_HEADER: token},
                )
                with urllib.request.urlopen(action) as response:
                    state = json.load(response)
                self.assertEqual(state["stage"], "ready")
                self.assertNotIn("transcript", state)
                image = state["latest_image"]
                self.assertIsNotNone(image)
                output_url = (
                    f"{base}/output/{urllib.parse.quote(image)}"
                    f"?token={urllib.parse.quote(token)}"
                )
                with urllib.request.urlopen(output_url) as response:
                    self.assertEqual(response.status, 200)
                rogue = session.output_dir / "unregistered.jpg"
                rogue.write_bytes(b"stale")
                rogue_url = (
                    f"{base}/output/{rogue.name}?token={urllib.parse.quote(token)}"
                )
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    urllib.request.urlopen(rogue_url)
                self.assertEqual(refused.exception.code, 404)

                print_body = json.dumps(
                    {
                        "request": "print my photo",
                        "include_qr": True,
                        "qr_url": "https://attacker.example/override",
                    }
                ).encode()
                print_request = urllib.request.Request(
                    f"{base}/action",
                    data=print_body,
                    headers={"Content-Type": "application/json", TOKEN_HEADER: token},
                )
                with urllib.request.urlopen(print_request) as response:
                    state = json.load(response)
                self.assertEqual(state["stage"], "error")
                self.assertIn("explicit URL", state["error"])

                bad_host = urllib.request.Request(
                    f"{base}/state",
                    headers={"Host": "attacker.example", TOKEN_HEADER: token},
                )
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    urllib.request.urlopen(bad_host)
                self.assertEqual(refused.exception.code, 421)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

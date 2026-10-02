# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

RECIPE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECIPE_DIR))

from full_demo.models import SessionStage
from full_demo.muse_image import FixtureImageStyler, ImageSizeFaceGate
from full_demo.muse_spark import FixtureChat
from full_demo.muse_voice import FixtureVoiceTranscriber
from full_demo.postcard import CupsPrinter, FilePrinter, PostcardComposer
from full_demo.reachy import FakeReachy
from full_demo.sam_preview import ColorMaskSegmenter
from full_demo.session import DemoSession, SessionBusyError


class FakeRobot:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def run(self, request: str, output_dir: Path) -> dict[str, object]:
        self.requests.append(request)
        return {"check": {"success": True}, "output_dir": str(output_dir)}


class BlockingStyler:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def style(self, image: bytes, style: str) -> bytes:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release styler")
        return image


class BlockingTranscriber:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def transcribe(self, _chunks: object) -> str:
        self.started.set()
        await self.release.wait()
        return "take a selfie"


class FailingSegmenter:
    def segment(self, _image: bytes, _concept: str) -> None:
        raise RuntimeError("preview service unavailable")


class SessionTest(unittest.TestCase):
    def image(self, color: str = "#E31B23") -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (160, 160), color).save(output, format="PNG")
        return output.getvalue()

    def build_session(
        self,
        directory: str,
        *,
        styler: object | None = None,
        segmenter: object | None = None,
        printer: object | None = None,
        session_id: str | None = None,
    ) -> tuple[DemoSession, FakeReachy]:
        portrait = self.image("#F2B38D")
        reachy = FakeReachy(portrait)
        session = DemoSession(
            output_dir=Path(directory),
            robot=FakeRobot(),
            styler=styler or FixtureImageStyler(),
            face_gate=ImageSizeFaceGate(),
            segmenter=segmenter or ColorMaskSegmenter(),
            reachy=reachy,
            postcard=PostcardComposer(),
            printer=printer or FilePrinter(Path(directory)),
            chat=FixtureChat(),
            session_id=session_id,
        )
        return session, reachy

    def test_robot_flow_creates_preview_and_verifies_motion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session, reachy = self.build_session(directory)
            state = session.handle(
                "put the red block in the bin",
                image=self.image(),
            )
            self.assertEqual(state.stage, SessionStage.READY)
            self.assertIsNotNone(state.sam_preview)
            assert state.sam_preview is not None
            self.assertTrue((session.output_dir / state.sam_preview).is_file())
            self.assertIn("react:celebrate", reachy.events)

    def test_selfie_then_voice_print_defaults_to_qr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session, _ = self.build_session(directory)
            selfie = session.handle("take a comic selfie", image=self.image("#F2B38D"))
            self.assertEqual(selfie.stage, SessionStage.READY)
            printed = session.handle(
                "print my photo",
                qr_url="https://github.com/meta-models/meta-model-cookbook",
            )
            self.assertEqual(printed.stage, SessionStage.READY)
            assert printed.latest_postcard is not None
            self.assertTrue((session.output_dir / printed.latest_postcard).is_file())

    def test_voice_print_without_url_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session, _ = self.build_session(directory)
            session.handle("take a selfie", image=self.image("#F2B38D"))
            state = session.handle("print my photo")
            self.assertEqual(state.stage, SessionStage.ERROR)
            self.assertIn("explicit URL", state.error or "")

    def test_manual_no_qr_print_remains_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session, _ = self.build_session(directory)
            session.handle("take a selfie", image=self.image("#F2B38D"))
            state = session.handle("print my photo", include_qr=False)
            self.assertEqual(state.stage, SessionStage.READY)

    def test_reset_rejects_a_late_image_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            styler = BlockingStyler()
            session, _ = self.build_session(directory, styler=styler)
            worker = threading.Thread(
                target=session.handle,
                args=("take a selfie",),
                kwargs={"image": self.image("#F2B38D")},
            )
            worker.start()
            self.assertTrue(styler.started.wait(timeout=2))
            reset = session.reset()
            styler.release.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            state = session.snapshot()
            self.assertEqual(state.run_id, reset.run_id)
            self.assertEqual(state.stage, SessionStage.IDLE)
            self.assertIsNone(state.latest_image)

    def test_voice_transcript_routes_into_the_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session, _ = self.build_session(directory)
            state = asyncio.run(
                session.handle_voice(
                    FixtureVoiceTranscriber("put the red block in the bin"),
                    [b"pcm"],
                    image=self.image(),
                )
            )
            self.assertEqual(state.stage, SessionStage.READY)
            self.assertEqual(state.transcript, "put the red block in the bin")

    def test_sam_failure_does_not_block_verified_motion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session, _ = self.build_session(directory, segmenter=FailingSegmenter())
            state = session.handle("put the red block in the bin", image=self.image())
            self.assertEqual(state.stage, SessionStage.READY)
            self.assertIn("sam-preview-unavailable", state.events)
            self.assertIsNone(state.sam_preview)

    def test_only_one_request_runs_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            styler = BlockingStyler()
            session, _ = self.build_session(directory, styler=styler)
            worker = threading.Thread(
                target=session.handle,
                args=("take a selfie",),
                kwargs={"image": self.image("#F2B38D")},
            )
            worker.start()
            self.assertTrue(styler.started.wait(timeout=2))
            with self.assertRaises(SessionBusyError):
                session.handle("What can this demo do?")
            styler.release.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

    def test_voice_reset_invalidates_the_pending_transcript(self) -> None:
        async def scenario(session: DemoSession) -> None:
            transcriber = BlockingTranscriber()
            task = asyncio.create_task(session.handle_voice(transcriber, [b"pcm"]))
            await transcriber.started.wait()
            reset = session.reset()
            transcriber.release.set()
            state = await task
            self.assertEqual(state.run_id, reset.run_id)
            self.assertEqual(state.stage, SessionStage.IDLE)

        with tempfile.TemporaryDirectory() as directory:
            session, _ = self.build_session(directory)
            asyncio.run(scenario(session))

    def test_reset_deletes_registered_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session, _ = self.build_session(directory)
            state = session.handle("take a selfie", image=self.image("#F2B38D"))
            assert state.latest_image is not None
            output = session.output_dir / state.latest_image
            self.assertTrue(output.is_file())
            session.reset()
            self.assertFalse(output.exists())
            self.assertEqual(session.allowed_outputs(), set())

    def test_sessions_use_distinct_output_namespaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first, _ = self.build_session(directory)
            second, _ = self.build_session(directory)
            self.assertNotEqual(first.output_dir, second.output_dir)
            self.assertEqual(first.output_dir.parent, Path(directory))
            self.assertEqual(second.output_dir.parent, Path(directory))

    def test_expired_session_directories_are_removed_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            expired = Path(directory) / f"session-{'a' * 24}"
            expired.mkdir()
            (expired / "visitor.jpg").write_bytes(b"old")
            old = time.time() - 25 * 60 * 60
            os.utime(expired, (old, old))
            self.build_session(directory)
            self.assertFalse(expired.exists())

    def test_new_selfie_clears_the_previous_visitors_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session, _ = self.build_session(directory)
            first = session.handle("take a selfie", image=self.image("#F2B38D"))
            assert first.latest_image is not None
            old_output = session.output_dir / first.latest_image
            styler = BlockingStyler()
            session.styler = styler
            worker = threading.Thread(
                target=session.handle,
                args=("take a selfie",),
                kwargs={"image": self.image("#A9D6E5")},
            )
            worker.start()
            self.assertTrue(styler.started.wait(timeout=2))
            self.assertIsNone(session.snapshot().latest_image)
            self.assertFalse(old_output.exists())
            styler.release.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

    def test_reset_cancels_a_print_waiting_for_operator_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            confirmation_started = threading.Event()
            release_confirmation = threading.Event()
            submissions: list[object] = []

            def confirm(_key: str) -> bool:
                confirmation_started.set()
                return release_confirmation.wait(timeout=5)

            def runner(command: object, **_arguments: object) -> object:
                submissions.append(command)
                raise AssertionError("reset should prevent CUPS submission")

            printer = CupsPrinter(
                "Test_Printer",
                runner=runner,
                confirm=confirm,
            )
            session, _ = self.build_session(directory, printer=printer)
            session.handle("take a selfie", image=self.image("#F2B38D"))
            worker = threading.Thread(
                target=session.handle,
                args=("print my photo",),
                kwargs={"include_qr": False},
            )
            worker.start()
            self.assertTrue(confirmation_started.wait(timeout=2))
            self.assertEqual(session.snapshot().stage, SessionStage.PRINTING)
            started = time.monotonic()
            reset = session.reset()
            self.assertLess(time.monotonic() - started, 1.0)
            release_confirmation.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(reset.stage, SessionStage.IDLE)
            self.assertEqual(submissions, [])

    def test_cancelled_voice_handler_keeps_single_flight_until_execution_ends(
        self,
    ) -> None:
        async def scenario(session: DemoSession, styler: BlockingStyler) -> None:
            task = asyncio.create_task(
                session.handle_voice(
                    FixtureVoiceTranscriber("take a selfie"),
                    [b"pcm"],
                    image=self.image("#F2B38D"),
                )
            )
            started = await asyncio.to_thread(styler.started.wait, 2)
            self.assertTrue(started)
            task.cancel()
            await asyncio.sleep(0)
            with self.assertRaises(SessionBusyError):
                session.handle("What can this demo do?")
            styler.release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with tempfile.TemporaryDirectory() as directory:
            styler = BlockingStyler()
            session, _ = self.build_session(directory, styler=styler)
            asyncio.run(scenario(session, styler))

    def test_session_id_cannot_escape_the_output_root(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(ValueError, "Session ID"),
        ):
            self.build_session(directory, session_id="../../../escape")


if __name__ == "__main__":
    unittest.main()

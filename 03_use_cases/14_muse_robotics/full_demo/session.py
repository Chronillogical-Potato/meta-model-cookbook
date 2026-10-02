# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shutil
import threading
import time
from collections.abc import AsyncIterable, Callable, Iterable
from dataclasses import replace
from pathlib import Path

from .models import Intent, Route, SessionStage, SessionState
from .ports import (
    ChatPort,
    FaceGatePort,
    ImageStylerPort,
    PrintBackendPort,
    ReachyPort,
    RobotRunnerPort,
    SegmenterPort,
    VoiceTranscriberPort,
)
from .postcard import PostcardComposer
from .router import route_text
from .sam_preview import overlay_mask

logger = logging.getLogger(__name__)
SESSION_ID = re.compile(r"[0-9a-f]{24}\Z")
SESSION_DIRECTORY = re.compile(r"session-[0-9a-f]{24}\Z")
DEFAULT_OUTPUT_TTL_SECONDS = 24 * 60 * 60


def remove_expired_sessions(
    output_root: Path,
    *,
    ttl_seconds: float = DEFAULT_OUTPUT_TTL_SECONDS,
    now: float | None = None,
) -> None:
    if not output_root.is_dir() or output_root.is_symlink():
        return
    cutoff = (time.time() if now is None else now) - ttl_seconds
    for candidate in output_root.iterdir():
        if (
            not candidate.is_symlink()
            and candidate.is_dir()
            and SESSION_DIRECTORY.fullmatch(candidate.name)
            and candidate.stat().st_mtime < cutoff
        ):
            shutil.rmtree(candidate)


class DemoRequestError(RuntimeError):
    pass


class PrintCancelledError(RuntimeError):
    pass


class SessionBusyError(RuntimeError):
    pass


class DemoSession:
    def __init__(
        self,
        *,
        output_dir: Path,
        robot: RobotRunnerPort,
        styler: ImageStylerPort,
        face_gate: FaceGatePort,
        segmenter: SegmenterPort,
        reachy: ReachyPort,
        postcard: PostcardComposer,
        printer: PrintBackendPort,
        chat: ChatPort,
        default_scene_image: bytes | None = None,
        session_id: str | None = None,
        output_ttl_seconds: float = DEFAULT_OUTPUT_TTL_SECONDS,
    ) -> None:
        remove_expired_sessions(output_dir, ttl_seconds=output_ttl_seconds)
        if session_id is not None and SESSION_ID.fullmatch(session_id) is None:
            raise ValueError("Session ID must be 24 lowercase hexadecimal characters.")
        self.session_id = session_id or secrets.token_hex(12)
        self.output_dir = output_dir / f"session-{self.session_id}"
        self.robot = robot
        self.styler = styler
        self.face_gate = face_gate
        self.segmenter = segmenter
        self.reachy = reachy
        self.postcard = postcard
        self.printer = printer
        self.chat = chat
        self.default_scene_image = default_scene_image
        self._state_lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._state = SessionState()
        self._latest_image_bytes: bytes | None = None

    def snapshot(self) -> SessionState:
        with self._state_lock:
            return replace(self._state, events=list(self._state.events))

    def allowed_outputs(self) -> set[str]:
        with self._state_lock:
            candidates = {
                self._state.latest_image,
                self._state.latest_postcard,
                self._state.sam_preview,
            }
        return {
            name
            for name in candidates
            if name is not None
            and Path(name).name == name
            and (self.output_dir / name).is_file()
            and not (self.output_dir / name).is_symlink()
        }

    def reset(self) -> SessionState:
        with self._state_lock:
            stale = self.allowed_outputs()
            self._state = SessionState(run_id=self._state.run_id + 1)
            self._latest_image_bytes = None
        for name in stale:
            (self.output_dir / name).unlink(missing_ok=True)
        return self.snapshot()

    def _begin(self, route: Route) -> int:
        with self._state_lock:
            run_id = self._state.run_id + 1
            self._delete_artifact(self._state.sam_preview)
            latest_image = self._state.latest_image
            latest_postcard = self._state.latest_postcard
            if route.intent is Intent.SELFIE:
                self._delete_artifact(latest_image)
                self._delete_artifact(latest_postcard)
                self._latest_image_bytes = None
                latest_image = None
                latest_postcard = None
            self._state = SessionState(
                run_id=run_id,
                stage=SessionStage.THINKING,
                intent=route.intent,
                message="Routing request",
                transcript=route.text,
                latest_image=latest_image,
                latest_postcard=latest_postcard,
                events=["thinking"],
            )
            return run_id

    def _begin_listening(self) -> int:
        with self._state_lock:
            run_id = self._state.run_id + 1
            self._delete_artifact(self._state.sam_preview)
            self._state = SessionState(
                run_id=run_id,
                stage=SessionStage.LISTENING,
                message="Listening",
                latest_image=self._state.latest_image,
                latest_postcard=self._state.latest_postcard,
                events=["listening"],
            )
            return run_id

    def _set_route(self, run_id: int, route: Route) -> bool:
        with self._state_lock:
            if self._state.run_id != run_id:
                return False
            if route.intent is Intent.SELFIE:
                self._delete_artifact(self._state.latest_image)
                self._delete_artifact(self._state.latest_postcard)
                self._state.latest_image = None
                self._state.latest_postcard = None
                self._latest_image_bytes = None
            self._state.stage = SessionStage.THINKING
            self._state.intent = route.intent
            self._state.message = "Routing request"
            self._state.transcript = route.text
            self._state.events.append("thinking")
            return True

    def _is_current(self, run_id: int) -> bool:
        with self._state_lock:
            return self._state.run_id == run_id

    def _commit_if_current(
        self,
        run_id: int,
        operation: Callable[[], str],
    ) -> str:
        with self._state_lock:
            if self._state.run_id != run_id:
                raise PrintCancelledError("Print cancelled by reset.")
            return operation()

    def _delete_artifact(self, name: str | None) -> None:
        if name is None or Path(name).name != name:
            return
        target = self.output_dir / name
        if target.is_file() and not target.is_symlink():
            target.unlink(missing_ok=True)

    def _update(
        self,
        run_id: int,
        *,
        stage: SessionStage,
        message: str,
        event: str,
        latest_image: str | None = None,
        latest_postcard: str | None = None,
        sam_preview: str | None = None,
        error: str | None = None,
    ) -> bool:
        with self._state_lock:
            if self._state.run_id != run_id:
                return False
            if latest_image is not None and latest_image != self._state.latest_image:
                self._delete_artifact(self._state.latest_image)
                self._state.latest_image = latest_image
            if (
                latest_postcard is not None
                and latest_postcard != self._state.latest_postcard
            ):
                self._delete_artifact(self._state.latest_postcard)
                self._state.latest_postcard = latest_postcard
            if sam_preview is not None and sam_preview != self._state.sam_preview:
                self._delete_artifact(self._state.sam_preview)
                self._state.sam_preview = sam_preview
            self._state.stage = stage
            self._state.message = message
            self._state.events.append(event)
            self._state.error = error
            return True

    def _save(self, name: str, content: bytes) -> str:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / name
        if path.is_symlink():
            raise ValueError("Refusing to write through a symlink.")
        path.write_bytes(content)
        return path.name

    def _execute(
        self,
        run_id: int,
        route: Route,
        *,
        image: bytes | None,
        qr_url: str | None,
    ) -> None:
        try:
            self.reachy.react("thinking")
            if route.intent is Intent.ROBOT:
                self._run_robot(run_id, route, image)
            elif route.intent is Intent.SELFIE:
                self._run_selfie(run_id, route, image)
            elif route.intent is Intent.PRINT:
                self._run_print(run_id, route, qr_url)
            else:
                self._run_chat(run_id, route)
        except PrintCancelledError:
            logger.info("Print cancelled before submission")
        except Exception as error:
            if isinstance(error, (DemoRequestError, ValueError)):
                public_error = str(error)
                logger.warning("Demo request refused: %s", error)
            else:
                public_error = "An adapter failed; check the operator log."
                logger.exception("Demo request failed")
            self._update(
                run_id,
                stage=SessionStage.ERROR,
                message="The request could not be completed.",
                event="error",
                error=public_error,
            )
            try:
                self.reachy.react("confused")
            except Exception:
                logger.exception("Reachy error reaction failed")

    def handle(
        self,
        request: str,
        *,
        image: bytes | None = None,
        qr_url: str | None = None,
        include_qr: bool | None = None,
    ) -> SessionState:
        if not self._run_lock.acquire(blocking=False):
            raise SessionBusyError("Another kiosk request is still running.")
        try:
            route = route_text(request)
            if include_qr is not None and route.intent is Intent.PRINT:
                route = replace(route, include_qr=include_qr)
            run_id = self._begin(route)
            self._execute(run_id, route, image=image, qr_url=qr_url)
            return self.snapshot()
        finally:
            self._run_lock.release()

    async def handle_voice(
        self,
        transcriber: VoiceTranscriberPort,
        chunks: Iterable[bytes] | AsyncIterable[bytes],
        **arguments: object,
    ) -> SessionState:
        unexpected = set(arguments) - {"include_qr", "image", "qr_url"}
        if unexpected:
            raise TypeError(f"Unexpected voice arguments: {sorted(unexpected)}")
        if not self._run_lock.acquire(blocking=False):
            raise SessionBusyError("Another kiosk request is still running.")
        run_id: int | None = None
        try:
            run_id = self._begin_listening()
            self.reachy.react("listening")
            transcript = await transcriber.transcribe(chunks)
            if not self._is_current(run_id):
                return self.snapshot()
            route = route_text(transcript)
            include_qr = arguments.get("include_qr")
            if include_qr is not None and route.intent is Intent.PRINT:
                route = replace(route, include_qr=bool(include_qr))
            if not self._set_route(run_id, route):
                return self.snapshot()
            execution = asyncio.create_task(
                asyncio.to_thread(
                    self._execute,
                    run_id,
                    route,
                    image=arguments.get("image"),
                    qr_url=arguments.get("qr_url"),
                )
            )
            try:
                await asyncio.shield(execution)
            except asyncio.CancelledError:
                await execution
                raise
            return self.snapshot()
        except Exception:
            logger.exception("Voice request failed")
            if run_id is not None:
                self._update(
                    run_id,
                    stage=SessionStage.ERROR,
                    message="The voice request could not be completed.",
                    event="voice-error",
                    error="Voice transcription failed; check the operator log.",
                )
            return self.snapshot()
        finally:
            self._run_lock.release()

    def _run_robot(self, run_id: int, route: Route, image: bytes | None) -> None:
        source = image if image is not None else self.default_scene_image
        if source is not None and len(route.objects) == 1:
            if not self._update(
                run_id,
                stage=SessionStage.SEGMENTING,
                message="Preparing a presentation-only SAM preview",
                event="segmenting",
            ):
                return
            try:
                mask = self.segmenter.segment(source, route.objects[0])
                if mask is None:
                    raise RuntimeError("No preview mask was returned.")
                preview = overlay_mask(source, mask)
                if not self._is_current(run_id):
                    return
                path = self._save(
                    f"{self.session_id}-sam-preview-{run_id}.png",
                    preview,
                )
                if not self._update(
                    run_id,
                    stage=SessionStage.EXECUTING,
                    message="Executing deterministic robot skill",
                    event="sam-preview-ready",
                    sam_preview=path,
                ):
                    self._delete_artifact(path)
                    return
            except Exception:
                logger.warning("SAM preview failed open", exc_info=True)
                if not self._update(
                    run_id,
                    stage=SessionStage.EXECUTING,
                    message="Preview unavailable; executing verified skill",
                    event="sam-preview-unavailable",
                ):
                    return
        elif not self._update(
            run_id,
            stage=SessionStage.EXECUTING,
            message="Executing deterministic robot skill",
            event="executing",
        ):
            return
        if not self._is_current(run_id):
            return
        result = self.robot.run(route.text, self.output_dir / f"simulation-{run_id}")
        check = result.get("check")
        success = isinstance(check, dict) and check.get("success") is True
        if not success:
            raise DemoRequestError("The simulator did not satisfy the postcondition.")
        if self._update(
            run_id,
            stage=SessionStage.READY,
            message="Robot task completed and verified",
            event="verified",
        ):
            self.reachy.react("celebrate")

    def _run_selfie(self, run_id: int, route: Route, image: bytes | None) -> None:
        if not self._update(
            run_id,
            stage=SessionStage.CAPTURING,
            message="Capturing a portrait",
            event="capturing",
        ):
            return
        source = image if image is not None else self.reachy.capture()
        if not self.face_gate.contains_face(source):
            raise DemoRequestError("No face-like portrait was detected.")
        if not self._update(
            run_id,
            stage=SessionStage.STYLING,
            message="Styling the portrait with Muse Image",
            event="styling",
        ):
            return
        styled = self.styler.style(source, route.style)
        if not self._is_current(run_id):
            return
        from .muse_image import image_extension

        extension = image_extension(styled)
        path = self._save(
            f"{self.session_id}-styled-{run_id}.{extension}",
            styled,
        )
        with self._state_lock:
            if self._state.run_id != run_id:
                self._delete_artifact(path)
                return
            self._latest_image_bytes = styled
        updated = self._update(
            run_id,
            stage=SessionStage.READY,
            message="Styled portrait ready",
            event="image-ready",
            latest_image=path,
        )
        if not updated:
            self._delete_artifact(path)
            return
        self.reachy.react("celebrate")

    def _run_print(self, run_id: int, route: Route, qr_url: str | None) -> None:
        with self._state_lock:
            latest = self._latest_image_bytes
        if latest is None:
            raise DemoRequestError("Create a styled portrait before printing.")
        if route.include_qr and qr_url is None:
            raise DemoRequestError(
                "Voice printing defaults to QR; provide an explicit URL."
            )
        if not self._update(
            run_id,
            stage=SessionStage.PRINTING,
            message="Composing the postcard",
            event="printing",
        ):
            return
        postcard = self.postcard.compose(
            latest,
            qr_url=qr_url if route.include_qr else None,
        )
        submitted = self.printer.submit(
            postcard,
            idempotency_key=f"{self.session_id}-run-{run_id}",
            output_dir=self.output_dir,
            commit=lambda operation: self._commit_if_current(run_id, operation),
        )
        self._update(
            run_id,
            stage=SessionStage.READY,
            message="Postcard ready",
            event="postcard-ready",
            latest_postcard=submitted,
        )

    def _run_chat(self, run_id: int, route: Route) -> None:
        reply = self.chat.reply(route.text)
        if self._update(
            run_id,
            stage=SessionStage.READY,
            message=reply,
            event="reply-ready",
        ):
            try:
                self.reachy.speak(reply)
            except RuntimeError:
                pass

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from full_demo.models import SessionStage
from full_demo.muse_image import (
    FixtureImageStyler,
    ImageSizeFaceGate,
    MuseImageStyler,
    OpenCVYuNetFaceGate,
)
from full_demo.muse_spark import FixtureChat, MuseSparkChat
from full_demo.muse_voice import FixtureVoiceTranscriber, MuseVoiceTranscriber
from full_demo.postcard import CupsPrinter, FilePrinter, PostcardComposer
from full_demo.reachy import FakeReachy, ReachyMiniAdapter
from full_demo.sam_preview import ColorMaskSegmenter, MuseSamSegmenter
from full_demo.server import serve
from full_demo.session import DemoSession
from run_demo import run as run_robot_demo

RECIPE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = RECIPE_DIR / "outputs" / "full_demo"


class MuJoCoRunner:
    def __init__(self, *, offline: bool) -> None:
        self.offline = offline

    def run(self, request: str, output_dir: Path) -> dict[str, object]:
        return run_robot_demo(
            request,
            offline=self.offline,
            output_dir=output_dir,
            save_media=False,
            persist_result=False,
            verbose=False,
        )


def build_session(
    *,
    output_dir: Path,
    live_models: bool,
    reachy: object,
    printer: object,
    scene: bytes,
    yunet_model: Path | None = None,
) -> DemoSession:
    face_gate = (
        OpenCVYuNetFaceGate(str(yunet_model))
        if yunet_model is not None
        else ImageSizeFaceGate()
    )
    return DemoSession(
        output_dir=output_dir,
        robot=MuJoCoRunner(offline=not live_models),
        styler=MuseImageStyler() if live_models else FixtureImageStyler(),
        face_gate=face_gate,
        segmenter=MuseSamSegmenter() if live_models else ColorMaskSegmenter(),
        reachy=reachy,
        postcard=PostcardComposer(),
        printer=printer,
        chat=MuseSparkChat() if live_models else FixtureChat(),
        default_scene_image=scene,
    )


async def run_fixture_flow(
    session: DemoSession,
    portrait: bytes,
    scene: bytes,
    qr_url: str | None,
) -> list[dict[str, object]]:
    voice = FixtureVoiceTranscriber("put the red block in the bin")
    robot = await session.handle_voice(voice, [b"fixture-pcm"], image=scene)
    selfie = session.handle("take a watercolor selfie", image=portrait)
    states = [robot.public_dict(), selfie.public_dict()]
    if qr_url:
        states.append(session.handle("print my photo", qr_url=qr_url).public_dict())
    return states


def _pcm_chunks(pcm: bytes, sample_rate: int) -> list[bytes]:
    frame_bytes = sample_rate * 2 * 80 // 1000
    return [
        pcm[index : index + frame_bytes] for index in range(0, len(pcm), frame_bytes)
    ]


async def run_voice_file(
    session: DemoSession,
    path: Path,
    *,
    qr_url: str | None,
) -> dict[str, object]:
    import wave

    with wave.open(str(path), "rb") as recording:
        if (
            recording.getnchannels() != 1
            or recording.getsampwidth() != 2
            or recording.getframerate() != 16000
        ):
            raise ValueError("Voice input must be mono, 16-bit PCM at 16 kHz.")
        pcm = recording.readframes(recording.getnframes())
    state = await session.handle_voice(
        MuseVoiceTranscriber(),
        _pcm_chunks(pcm, 16000),
        qr_url=qr_url,
    )
    return state.public_dict()


async def run_reachy_voice(
    session: DemoSession,
    reachy: ReachyMiniAdapter,
    *,
    duration_seconds: float,
    qr_url: str | None,
) -> dict[str, object]:
    pcm, sample_rate = await asyncio.to_thread(
        reachy.record_pcm16,
        duration_seconds,
    )
    encoding = {16000: "PCM_16KHZ", 24000: "PCM_24KHZ"}.get(sample_rate)
    if encoding is None:
        raise ValueError("Muse Voice accepts Reachy audio at 16 kHz or 24 kHz only.")
    state = await session.handle_voice(
        MuseVoiceTranscriber(encoding=encoding),
        _pcm_chunks(pcm, sample_rate),
        qr_url=qr_url,
    )
    return state.public_dict()


def _has_error(result: object) -> bool:
    if isinstance(result, list):
        return any(_has_error(item) for item in result)
    return isinstance(result, dict) and result.get("stage") == SessionStage.ERROR.value


def _confirm_print(idempotency_key: str) -> bool:
    phrase = f"PRINT {idempotency_key}"
    answer = input(f"Physical print requested. Type {phrase!r} to submit: ")
    return answer.strip() == phrase


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live-models", action="store_true")
    voice = parser.add_mutually_exclusive_group()
    voice.add_argument("--voice-wav", type=Path)
    voice.add_argument("--reachy-voice-seconds", type=float)
    parser.add_argument("--qr-url")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--reachy", action="store_true")
    parser.add_argument("--enable-reachy-motors", action="store_true")
    parser.add_argument("--yunet-model", type=Path)
    parser.add_argument("--cups-printer")
    parser.add_argument("--allow-physical-print", action="store_true")
    arguments = parser.parse_args()

    if arguments.enable_reachy_motors and not arguments.reachy:
        parser.error("--enable-reachy-motors requires --reachy")
    if arguments.reachy_voice_seconds is not None and not arguments.reachy:
        parser.error("--reachy-voice-seconds requires --reachy")
    if (
        arguments.voice_wav or arguments.reachy_voice_seconds
    ) and not arguments.live_models:
        parser.error("Live voice input requires --live-models")

    portrait = (RECIPE_DIR / "assets/muse_image_input.png").read_bytes()
    scene = (RECIPE_DIR / "assets/mujoco_scene.png").read_bytes()
    reachy = (
        ReachyMiniAdapter.connect(enable_motors=arguments.enable_reachy_motors)
        if arguments.reachy
        else FakeReachy(portrait)
    )
    if arguments.cups_printer:
        if not arguments.allow_physical_print:
            parser.error("--cups-printer requires --allow-physical-print")
        printer = CupsPrinter(arguments.cups_printer, confirm=_confirm_print)
    else:
        printer = FilePrinter()
    session = build_session(
        output_dir=arguments.output_dir,
        live_models=arguments.live_models,
        reachy=reachy,
        printer=printer,
        scene=scene,
        yunet_model=arguments.yunet_model,
    )

    try:
        if arguments.serve:
            serve(
                session,
                host=arguments.host,
                port=arguments.port,
                qr_url=arguments.qr_url,
            )
            return 0
        if arguments.voice_wav:
            result: object = asyncio.run(
                run_voice_file(
                    session,
                    arguments.voice_wav,
                    qr_url=arguments.qr_url,
                )
            )
        elif arguments.reachy_voice_seconds is not None:
            if not isinstance(reachy, ReachyMiniAdapter):
                raise RuntimeError("Reachy adapter was not configured.")
            result = asyncio.run(
                run_reachy_voice(
                    session,
                    reachy,
                    duration_seconds=arguments.reachy_voice_seconds,
                    qr_url=arguments.qr_url,
                )
            )
        else:
            result = asyncio.run(
                run_fixture_flow(session, portrait, scene, arguments.qr_url)
            )
        print(json.dumps(result, indent=2))
        return 1 if _has_error(result) else 0
    finally:
        if isinstance(reachy, ReachyMiniAdapter):
            reachy.close()


if __name__ == "__main__":
    raise SystemExit(main())

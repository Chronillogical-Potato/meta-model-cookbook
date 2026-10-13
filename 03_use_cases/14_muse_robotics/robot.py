from __future__ import annotations

from pathlib import Path

import imageio.v2 as imageio
import mujoco
import numpy as np
from PIL import Image

SCENE_FILE = Path(__file__).with_name("scene.xml")
ARM_QPOS = slice(0, 7)
BLOCK_QPOS = {"red_block": 9, "green_block": 16, "blue_block": 23}
DEFAULT_BLOCKS = {
    "red_block": (0.45, 0.15, 0.05),
    "green_block": (0.50, 0.02, 0.05),
    "blue_block": (0.42, -0.12, 0.05),
}
GRIP_OPEN = 255.0
GRIP_CLOSED = 0.0


class PandaScene:
    def __init__(
        self,
        *,
        width: int = 960,
        height: int = 544,
        record_media: bool = True,
    ) -> None:
        self.model = mujoco.MjModel.from_xml_path(str(SCENE_FILE))
        self.data = mujoco.MjData(self.model)
        self.width = width
        self.height = height
        self.record_media = record_media
        self.frames: list[np.ndarray] = []
        self._renderer: mujoco.Renderer | None = None
        self.hand_body = mujoco.mj_name2id(
            self.model,
            mujoco.mjtObj.mjOBJ_BODY,
            "hand",
        )
        home = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_KEY, "home")
        mujoco.mj_resetDataKeyframe(self.model, self.data, home)
        mujoco.mj_forward(self.model, self.data)
        self.down_quaternion = np.zeros(4)
        mujoco.mju_mat2Quat(
            self.down_quaternion,
            self.data.xmat[self.hand_body],
        )
        self.reset()

    def reset(
        self, blocks: dict[str, tuple[float, float, float]] | None = None
    ) -> None:
        home = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_KEY, "home")
        mujoco.mj_resetDataKeyframe(self.model, self.data, home)
        for name, position in (blocks or DEFAULT_BLOCKS).items():
            address = BLOCK_QPOS[name]
            self.data.qpos[address : address + 3] = position
            self.data.qpos[address + 3 : address + 7] = [1, 0, 0, 0]
        self.data.qvel[:] = 0
        mujoco.mj_forward(self.model, self.data)
        self.frames.clear()

    def block_pos(self, name: str) -> np.ndarray:
        body = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, name)
        return self.data.xpos[body].copy()

    def hand_pos(self) -> np.ndarray:
        return self.data.xpos[self.hand_body].copy()

    def _get_renderer(self) -> mujoco.Renderer:
        if self._renderer is None:
            self._renderer = mujoco.Renderer(
                self.model,
                height=self.height,
                width=self.width,
            )
        return self._renderer

    def render(self, camera: str = "hero") -> np.ndarray:
        renderer = self._get_renderer()
        renderer.update_scene(self.data, camera=camera)
        return renderer.render()

    def record(self, camera: str = "hero") -> None:
        if self.record_media:
            self.frames.append(self.render(camera))

    def record_hold(self, count: int = 20, camera: str = "hero") -> None:
        if not self.record_media:
            return
        frame = self.render(camera)
        self.frames.extend(frame.copy() for _ in range(count))

    def set_gripper(
        self,
        opened: bool,
        *,
        steps: int = 300,
        record_every: int = 8,
    ) -> None:
        self.data.ctrl[7] = GRIP_OPEN if opened else GRIP_CLOSED
        for step in range(steps):
            mujoco.mj_step(self.model, self.data)
            if record_every and step % record_every == 0:
                self.record()

    def _inverse_kinematics(
        self,
        target: np.ndarray,
        *,
        iterations: int = 300,
        tolerance: float = 1e-3,
    ) -> np.ndarray:
        saved_qpos = self.data.qpos.copy()
        position_jacobian = np.zeros((3, self.model.nv))
        rotation_jacobian = np.zeros((3, self.model.nv))
        current_quaternion = np.zeros(4)
        negated_quaternion = np.zeros(4)
        quaternion_error = np.zeros(4)

        for _ in range(iterations):
            mujoco.mj_forward(self.model, self.data)
            error = np.zeros(6)
            error[:3] = target - self.data.xpos[self.hand_body]
            mujoco.mju_mat2Quat(
                current_quaternion,
                self.data.xmat[self.hand_body],
            )
            mujoco.mju_negQuat(negated_quaternion, current_quaternion)
            mujoco.mju_mulQuat(
                quaternion_error,
                self.down_quaternion,
                negated_quaternion,
            )
            mujoco.mju_quat2Vel(error[3:], quaternion_error, 0.6)
            if np.linalg.norm(error[:3]) < tolerance:
                break

            mujoco.mj_jacBody(
                self.model,
                self.data,
                position_jacobian,
                rotation_jacobian,
                self.hand_body,
            )
            jacobian = np.vstack(
                [position_jacobian[:, ARM_QPOS], rotation_jacobian[:, ARM_QPOS]]
            )
            damped = jacobian @ jacobian.T + 1e-4 * np.eye(6)
            self.data.qpos[ARM_QPOS] += jacobian.T @ np.linalg.solve(damped, error)
            for joint in range(7):
                lower, upper = self.model.jnt_range[joint]
                self.data.qpos[joint] = np.clip(
                    self.data.qpos[joint],
                    lower,
                    upper,
                )

        solution = self.data.qpos[ARM_QPOS].copy()
        self.data.qpos[:] = saved_qpos
        mujoco.mj_forward(self.model, self.data)
        return solution

    def move_to(
        self,
        target: list[float] | np.ndarray,
        *,
        duration: float = 1.2,
        record_every: int = 8,
    ) -> float:
        target_array = np.asarray(target, dtype=float)
        goal = self._inverse_kinematics(target_array)
        start = self.data.ctrl[ARM_QPOS].copy()
        steps = max(1, int(duration / self.model.opt.timestep))
        for step in range(steps):
            progress = (step + 1) / steps
            progress = 3 * progress**2 - 2 * progress**3
            self.data.ctrl[ARM_QPOS] = (1 - progress) * start + progress * goal
            mujoco.mj_step(self.model, self.data)
            if record_every and step % record_every == 0:
                self.record()
        for step in range(120):
            mujoco.mj_step(self.model, self.data)
            if record_every and step % record_every == 0:
                self.record()
        return float(np.linalg.norm(self.hand_pos() - target_array))

    def save_png(self, path: Path, *, camera: str = "hero") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(self.render(camera)).save(path, optimize=True)

    def save_video(self, path: Path, *, fps: int = 30) -> None:
        if not self.frames:
            raise RuntimeError("No frames were recorded.")
        path.parent.mkdir(parents=True, exist_ok=True)
        imageio.mimwrite(path, self.frames, fps=fps, quality=8, codec="libx264")

    def save_gif(
        self,
        path: Path,
        *,
        fps: int = 15,
        max_frames: int = 80,
        width: int = 640,
    ) -> None:
        if not self.frames:
            raise RuntimeError("No frames were recorded.")
        indices = np.linspace(
            0,
            len(self.frames) - 1,
            min(max_frames, len(self.frames)),
        ).astype(int)
        frames = []
        for index in indices:
            frame = Image.fromarray(self.frames[index])
            height = round(frame.height * width / frame.width)
            resized = frame.resize((width, height), Image.Resampling.LANCZOS)
            frames.append(resized.quantize(colors=96))
        path.parent.mkdir(parents=True, exist_ok=True)
        frames[0].save(
            path,
            save_all=True,
            append_images=frames[1:],
            optimize=True,
            duration=round(1000 / fps),
            loop=0,
            disposal=2,
        )

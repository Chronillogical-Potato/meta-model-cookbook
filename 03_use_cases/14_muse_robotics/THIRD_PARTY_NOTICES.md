<!--
  Copyright (c) Meta Platforms, Inc. and affiliates.
  All rights reserved.

  This source code is licensed under the license found in the
  LICENSE file in the root directory of this source tree.
-->

# Third-party notices

## Franka Emika Panda MuJoCo model

`panda.xml`, `third_party/franka_panda/panda.png`, and the files under `third_party/franka_panda/assets/` are based on the Franka Emika Panda model in [MuJoCo Menagerie at reviewed revision `367e3d9`](https://github.com/google-deepmind/mujoco_menagerie/tree/367e3d9884401dcf6f9c27fa69f118992539039f/franka_emika_panda). The model is derived from Franka's publicly available `franka_ros` URDF and is distributed under the Apache License 2.0.

This recipe preserves the upstream `LICENSE`, `README.md`, and `CHANGELOG.md`. The cookbook copy changes the XML mesh path to the vendored directory and retains the working demo's wrist camera. `scene.xml` is the demo-specific pick-and-place scene with a table, bin, colored blocks, lighting, and cameras.

## Installed Python dependencies

`requirements.txt` installs, but does not vendor, these third-party packages: OpenAI's Python SDK, MuJoCo, NumPy, Pillow, ImageIO, imageio-ffmpeg, qrcode, websockets, and Meta's `meta_sam_parser`. Their upstream distributions and license metadata govern use. In particular, `meta_sam_parser` is distributed under the [SAM License](https://pypi.org/project/meta-sam-parser/), and imageio-ffmpeg may obtain or bundle an FFmpeg executable governed by its own build configuration and licenses.

## Optional integrations not redistributed

This recipe includes adapter code but does not bundle a YuNet face-detection model, local SAM checkpoint, Reachy Mini SDK or recorded-move dataset, Faster Whisper model, Piper/Kokoro voice model, OpenCV package, CUPS/Canon printer driver, or visitor media. Obtain optional models, packages, and hardware SDKs from their publishers and review their licenses before use. Hosted SAM uses public model `sam-3.1`; no model weights are included here.

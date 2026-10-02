# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import os

MODEL = "muse-spark-1.3"
BASE_URL = "https://api.meta.ai/v1"


class MuseSparkChat:
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
                raise RuntimeError("Set MODEL_API_KEY to use Muse Spark.")
            self._client = OpenAI(
                base_url=BASE_URL,
                api_key=token,
            )
        return self._client

    def reply(self, request: str) -> str:
        client = self._get_client()
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are the concise host of a robotics demo. Answer in "
                        "one or two sentences and never claim that hardware acted."
                    ),
                },
                {"role": "user", "content": request},
            ],
        )
        choices = getattr(response, "choices", None)
        if not isinstance(choices, (list, tuple)) or not choices:
            raise RuntimeError("Muse Spark returned no choices.")
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Muse Spark returned no reply.")
        return content.strip()


class FixtureChat:
    def reply(self, request: str) -> str:
        return (
            "Try asking me to move a colored block, take a styled selfie, "
            "or print the latest image."
        )

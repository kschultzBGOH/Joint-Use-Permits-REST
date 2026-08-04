"""Loads the local Qwen vision-language model and prompts it to read pole
ID labels from a rendered PDF page image.

UNVERIFIED: this hasn't been run against a real Qwen3-VL checkpoint yet.
The model class (AutoModelForImageTextToText), chat-template usage, and
generation kwargs follow the standard transformers pattern for Qwen-VL
family models, but the exact API can vary by checkpoint/transformers
version -- adjust load_vision_model/read_pole_labels if loading or
generation fails against the actual model directory.
"""

from __future__ import annotations

import gc
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image


@dataclass
class VisionModelBundle:
    model: Any
    processor: Any
    device: str


class VisionModelError(RuntimeError):
    """Raised when the vision model can't be loaded or fails to run."""


POLE_READING_PROMPT = (
    "You are looking at one page of an electric utility plan set. "
    "Find every visible pole ID label on this image -- short alphanumeric "
    "tags near utility pole symbols, often prefixed with \"POLE\", "
    "\"POLE#\", or similar. "
    "Return ONLY a JSON array, no other text, where each item is "
    '{"text": "<exact label as written>", "confidence": <0.0-1.0>}. '
    "Return an empty array [] if no pole ID labels are visible."
)


def load_vision_model(model_dir: Path) -> VisionModelBundle:
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32

    try:
        processor = AutoProcessor.from_pretrained(str(model_dir))
        model = AutoModelForImageTextToText.from_pretrained(
            str(model_dir),
            torch_dtype=dtype,
            device_map=device,
        )
    except Exception as exc:
        raise VisionModelError(f"Failed to load vision model from {model_dir}: {exc}") from exc

    model.eval()
    return VisionModelBundle(model=model, processor=processor, device=device)


def unload_vision_model(bundle: VisionModelBundle) -> None:
    import torch

    del bundle.model
    del bundle.processor
    gc.collect()

    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def read_pole_labels(bundle: VisionModelBundle, image: Image.Image) -> list[dict[str, Any]]:
    import torch

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": POLE_READING_PROMPT},
            ],
        }
    ]

    try:
        text_prompt = bundle.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = bundle.processor(text=[text_prompt], images=[image], return_tensors="pt").to(
            bundle.device
        )

        with torch.no_grad():
            output_ids = bundle.model.generate(**inputs, max_new_tokens=512)

        generated_text = bundle.processor.batch_decode(
            output_ids[:, inputs["input_ids"].shape[1] :],
            skip_special_tokens=True,
        )[0]
    except Exception as exc:
        raise VisionModelError(f"Vision model inference failed: {exc}") from exc

    return _parse_readings(generated_text)


def _parse_readings(generated_text: str) -> list[dict[str, Any]]:
    match = re.search(r"\[.*\]", generated_text, re.DOTALL)
    if not match:
        return []

    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []

    if not isinstance(parsed, list):
        return []

    return [item for item in parsed if isinstance(item, dict) and "text" in item]

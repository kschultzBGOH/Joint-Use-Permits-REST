"""Vision-model loading utilities for the local Qwen3-VL checkpoint.

The loader never contacts Hugging Face -- local_files_only=True is used for
both the processor and model. BF16 is selected when CUDA reports support
for it; otherwise FP16 is used. CUDA is required: this is an 8B vision
model and isn't a reasonable CPU workload.
"""

from __future__ import annotations

import gc
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

logger = logging.getLogger(__name__)

PreferredDtype = Literal["auto", "bfloat16", "float16"]


@dataclass(slots=True)
class VisionModelBundle:
    """The loaded Qwen model, processor, and runtime settings."""

    model_dir: Path
    model: Any
    processor: Any
    device: str
    dtype: str
    attention_backend: str


class VisionRuntimeInfo(TypedDict):
    model_dir: str
    device: str
    dtype: str
    attention_backend: str
    cuda_available: bool
    gpu_name: str | None
    gpu_total_memory_gib: float | None
    gpu_allocated_memory_gib: float | None
    gpu_reserved_memory_gib: float | None


class VisionModelLoadError(RuntimeError):
    """Raised when the local vision model cannot be loaded safely."""


def load_vision_model(
    model_dir: str | Path,
    *,
    preferred_dtype: PreferredDtype = "auto",
    device_map: str | dict[str, object] = "auto",
    attention_backend: str = "sdpa",
    require_cuda: bool = True,
) -> VisionModelBundle:
    """Load Qwen3-VL and its processor from a local checkpoint."""

    resolved_model_dir = validate_model_directory(model_dir)

    try:
        import torch
        from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
    except ImportError as exc:
        raise VisionModelLoadError(
            "Vision-model dependencies are missing. Install torch, transformers, "
            "accelerate, safetensors, and pillow in this environment."
        ) from exc

    cuda_available = torch.cuda.is_available()

    if require_cuda and not cuda_available:
        raise VisionModelLoadError("CUDA is not available. Will not load the 8B vision model on CPU.")

    selected_dtype = _select_torch_dtype(
        torch_module=torch, preferred_dtype=preferred_dtype, cuda_available=cuda_available
    )

    logger.info(
        "Loading Qwen3-VL from %s using dtype=%s and device_map=%s.",
        resolved_model_dir,
        _dtype_name(selected_dtype),
        device_map,
    )

    try:
        processor = AutoProcessor.from_pretrained(
            resolved_model_dir, local_files_only=True, trust_remote_code=False
        )
        model = Qwen3VLForConditionalGeneration.from_pretrained(
            resolved_model_dir,
            dtype=selected_dtype,
            device_map=device_map,
            attn_implementation=attention_backend,
            local_files_only=True,
            trust_remote_code=False,
            low_cpu_mem_usage=True,
        )
        model.eval()

    except torch.cuda.OutOfMemoryError as exc:
        _clear_cuda_cache(torch)
        raise VisionModelLoadError(
            "CUDA ran out of memory while loading Qwen3-VL. Close other GPU applications and try again."
        ) from exc

    except Exception as exc:
        _clear_cuda_cache(torch)
        raise VisionModelLoadError(f"Unable to load the local Qwen3-VL model from {resolved_model_dir}: {exc}") from exc

    bundle = VisionModelBundle(
        model_dir=resolved_model_dir,
        model=model,
        processor=processor,
        device=_model_device(model),
        dtype=_dtype_name(selected_dtype),
        attention_backend=attention_backend,
    )

    runtime_info = get_vision_runtime_info(bundle)
    logger.info(
        "Qwen3-VL loaded on %s (%s); allocated GPU memory: %s GiB.",
        runtime_info["device"],
        runtime_info["gpu_name"] or "GPU unavailable",
        runtime_info["gpu_allocated_memory_gib"] if runtime_info["gpu_allocated_memory_gib"] is not None else "n/a",
    )

    return bundle


def validate_model_directory(model_dir: str | Path) -> Path:
    """Validate the local Qwen3-VL checkpoint without loading its weights."""

    resolved_model_dir = Path(model_dir).expanduser().resolve()

    if not resolved_model_dir.exists():
        raise FileNotFoundError(f"Vision model directory does not exist: {resolved_model_dir}")

    if not resolved_model_dir.is_dir():
        raise VisionModelLoadError(f"Vision model path is not a directory: {resolved_model_dir}")

    required_files = ("config.json", "preprocessor_config.json", "tokenizer_config.json")
    missing_files = [
        file_name for file_name in required_files if not (resolved_model_dir / file_name).is_file()
    ]
    if missing_files:
        raise VisionModelLoadError(
            "Vision model directory is missing required files: " + ", ".join(missing_files)
        )

    weight_files = sorted(resolved_model_dir.glob("*.safetensors"))
    if not weight_files:
        raise VisionModelLoadError(f"No safetensors weights were found in {resolved_model_dir}.")

    if len(weight_files) > 1 and not (resolved_model_dir / "model.safetensors.index.json").is_file():
        raise VisionModelLoadError(
            "The model uses multiple safetensors shards, but model.safetensors.index.json is missing."
        )

    total_weight_bytes = sum(weight_file.stat().st_size for weight_file in weight_files)
    if total_weight_bytes < 1_000_000_000:
        raise VisionModelLoadError("The local model weights appear incomplete. Their combined size is less than 1 GB.")

    _validate_model_type(resolved_model_dir / "config.json")
    return resolved_model_dir


def get_vision_runtime_info(bundle: VisionModelBundle) -> VisionRuntimeInfo:
    """Return current model placement and CUDA memory statistics."""

    try:
        import torch
    except ImportError as exc:
        raise VisionModelLoadError("PyTorch is unavailable.") from exc

    cuda_available = torch.cuda.is_available()
    gpu_name: str | None = None
    total_memory: float | None = None
    allocated_memory: float | None = None
    reserved_memory: float | None = None

    if cuda_available:
        device_index = torch.cuda.current_device()
        properties = torch.cuda.get_device_properties(device_index)
        gpu_name = properties.name
        total_memory = _bytes_to_gib(properties.total_memory)
        allocated_memory = _bytes_to_gib(torch.cuda.memory_allocated(device_index))
        reserved_memory = _bytes_to_gib(torch.cuda.memory_reserved(device_index))

    return {
        "model_dir": str(bundle.model_dir),
        "device": bundle.device,
        "dtype": bundle.dtype,
        "attention_backend": bundle.attention_backend,
        "cuda_available": cuda_available,
        "gpu_name": gpu_name,
        "gpu_total_memory_gib": total_memory,
        "gpu_allocated_memory_gib": allocated_memory,
        "gpu_reserved_memory_gib": reserved_memory,
    }


def unload_vision_model(bundle: VisionModelBundle) -> None:
    """Release a loaded model and clear unused CUDA cache blocks."""

    bundle.model = None
    bundle.processor = None
    gc.collect()

    try:
        import torch
    except ImportError:
        return

    _clear_cuda_cache(torch)
    logger.info("Qwen3-VL model resources were released.")


def _validate_model_type(config_path: Path) -> None:
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            model_config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise VisionModelLoadError(f"Unable to read model configuration: {config_path}") from exc

    model_type = str(model_config.get("model_type") or "").strip()
    if model_type != "qwen3_vl":
        raise VisionModelLoadError(f"Expected model_type 'qwen3_vl', found '{model_type or 'missing'}'.")


def _select_torch_dtype(torch_module: Any, preferred_dtype: PreferredDtype, cuda_available: bool) -> Any:
    if preferred_dtype == "bfloat16":
        if cuda_available and not torch_module.cuda.is_bf16_supported():
            raise VisionModelLoadError("BF16 was requested, but the active CUDA device does not report BF16 support.")
        return torch_module.bfloat16

    if preferred_dtype == "float16":
        return torch_module.float16

    if preferred_dtype != "auto":
        raise VisionModelLoadError(f"Unsupported preferred dtype: {preferred_dtype}")

    if cuda_available and torch_module.cuda.is_bf16_supported():
        return torch_module.bfloat16

    return torch_module.float16


def _model_device(model: Any) -> str:
    try:
        first_parameter = next(model.parameters())
        return str(first_parameter.device)
    except (AttributeError, StopIteration, TypeError):
        return "unknown"


def _dtype_name(dtype: Any) -> str:
    return str(dtype).removeprefix("torch.")


def _clear_cuda_cache(torch_module: Any) -> None:
    if torch_module.cuda.is_available():
        torch_module.cuda.empty_cache()


def _bytes_to_gib(byte_count: int) -> float:
    return round(byte_count / (1024**3), 3)

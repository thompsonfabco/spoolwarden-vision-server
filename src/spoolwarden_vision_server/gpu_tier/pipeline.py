"""End-to-end GPU VLM Inference Pipeline (Qwen2.5-VL-7B-Instruct 4-bit + LoRA Adapter + XML Schema Validator)."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from ..catalog.matcher import CatalogMatcher
from ..cpu_tier.pipeline import CPUHybridPipeline, merge_prediction_item
from ..xml_io import CONFIGS_DIR, PROJECT_ROOT, FilamentItem, IntakeReadResult, parse_xml_file
from .prompts import SYSTEM_PROMPT, USER_PROMPT

log = logging.getLogger(__name__)


_VERIFIED_BASE_CACHE: dict[str, tuple[Path, dict[str, tuple[int, int]]]] = {}
_VERIFIED_BASE_LOCK = threading.Lock()


def verify_pinned_base_model(manifest_path: Path | None = None) -> Path:
    """Download/locate the pinned GPU base model revision and verify every file's size and SHA-256."""
    from huggingface_hub import snapshot_download

    candidates = (
        [Path(manifest_path)]
        if manifest_path
        else [
            PROJECT_ROOT / "models" / "gpu" / "base-model.json",
            CONFIGS_DIR / "base-model.json",
        ]
    )
    resolved_manifest = next((p for p in candidates if p.is_file()), None)
    if resolved_manifest is None:
        raise RuntimeError("Missing pinned base model manifest (base-model.json)")

    data = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    model_id = str(data["model_id"])
    revision = str(data["revision"])
    expected_files: dict[str, dict[str, Any]] = data["files"]
    cache_key = f"{model_id}@{revision}"

    with _VERIFIED_BASE_LOCK:
        cached = _VERIFIED_BASE_CACHE.get(cache_key)
        if cached is not None:
            cached_dir, file_stats = cached
            if all(
                (cached_dir / rel_name).is_file()
                and ((st := (cached_dir / rel_name).stat()).st_size, st.st_mtime_ns) == exp_st
                for rel_name, exp_st in file_stats.items()
            ):
                return cached_dir

    snap_dir = Path(
        snapshot_download(
            repo_id=model_id,
            revision=revision,
            allow_patterns=list(expected_files.keys()),
        )
    )
    verified_stats: dict[str, tuple[int, int]] = {}
    for rel_name, meta in expected_files.items():
        fp = snap_dir / rel_name
        if not fp.is_file():
            raise RuntimeError(f"Missing pinned base model file: {rel_name}")
        st = fp.stat()
        actual_size = st.st_size
        if actual_size != int(meta["size"]):
            raise RuntimeError(
                f"Size mismatch for {rel_name}: expected {meta['size']}, got {actual_size}"
            )
        digest = hashlib.sha256()
        with fp.open("rb") as fh:
            for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        actual_sha = digest.hexdigest()
        if actual_sha != str(meta["sha256"]):
            raise RuntimeError(
                f"SHA-256 mismatch for {rel_name}: expected {meta['sha256']}, got {actual_sha}"
            )
        verified_stats[rel_name] = (st.st_size, st.st_mtime_ns)

    with _VERIFIED_BASE_LOCK:
        _VERIFIED_BASE_CACHE[cache_key] = (snap_dir, verified_stats)
    return snap_dir


class GPUVLMPipeline:
    """High-accuracy GPU Vision-Language Model pipeline with automatic idle VRAM unload."""

    def __init__(
        self,
        adapter_dir: str | Path | None = None,
        base_model_id: str | None = None,
        device: str | None = None,
        load_in_4bit: bool | None = None,
        idle_unload_seconds: float | None = None,
        cpu_helper: CPUHybridPipeline | None = None,
        matcher: CatalogMatcher | None = None,
    ):
        self.adapter_dir = Path(adapter_dir) if adapter_dir is not None else (PROJECT_ROOT / "models" / "gpu" / "qwen25_vl_7b_filament_lora")
        self.base_model_id = base_model_id or "Qwen/Qwen2.5-VL-7B-Instruct"
        self.device = device or "cuda:0"
        self.load_in_4bit = True if load_in_4bit is None else bool(load_in_4bit)
        self.min_pixels = 256 * 28 * 28
        self.max_pixels = 640 * 28 * 28
        self.max_new_tokens = 165
        self.do_sample = False
        xml_idle = 300.0

        cfg_path = CONFIGS_DIR / "gpu_vlm_lora.xml"
        if cfg_path.is_file():
            try:
                cfg_root = parse_xml_file(cfg_path)
                bm_el = cfg_root.find("base-model")
                if bm_el is not None:
                    if base_model_id is None and bm_el.attrib.get("repo-id"):
                        self.base_model_id = bm_el.attrib["repo-id"]
                    if adapter_dir is None and bm_el.attrib.get("adapter-dir"):
                        self.adapter_dir = PROJECT_ROOT / bm_el.attrib["adapter-dir"]
                    self.min_pixels = int(bm_el.attrib.get("min-pixels", str(self.min_pixels)))
                    self.max_pixels = int(bm_el.attrib.get("max-pixels", str(self.max_pixels)))
                inf_el = cfg_root.find("inference")
                if inf_el is not None:
                    if device is None and inf_el.attrib.get("default-device"):
                        self.device = inf_el.attrib["default-device"]
                    if load_in_4bit is None and "low-vram-4bit" in inf_el.attrib:
                        self.load_in_4bit = inf_el.attrib["low-vram-4bit"].lower() == "true"
                    self.max_new_tokens = int(inf_el.attrib.get("max-new-tokens", str(self.max_new_tokens)))
                    self.do_sample = inf_el.attrib.get("do-sample", "false").lower() == "true"
                    xml_idle = float(inf_el.attrib.get("idle-unload-seconds", str(xml_idle)))
            except Exception as exc:
                log.warning("Failed to parse %s, using defaults: %s", cfg_path, exc)

        self.idle_unload_seconds = xml_idle if idle_unload_seconds is None else float(idle_unload_seconds)

        self._cpu_helper = cpu_helper
        self.matcher = matcher or (cpu_helper.matcher if cpu_helper is not None else CatalogMatcher())

        self._model: Any = None
        self._processor: Any = None
        self._last_used: float = 0.0
        self._lock = threading.Lock()
        self._infer_lock = threading.Lock()

    @property
    def cpu_helper(self) -> CPUHybridPipeline:
        if self._cpu_helper is None:
            with self._lock:
                if self._cpu_helper is None:
                    self._cpu_helper = CPUHybridPipeline()
        return self._cpu_helper

    def _ensure_loaded(self) -> None:
        with self._lock:
            self._last_used = time.time()
            if self._model is not None and self._processor is not None:
                return

            import torch
            from peft import PeftModel
            from transformers import AutoProcessor, BitsAndBytesConfig, Qwen2_5_VLForConditionalGeneration

            if not torch.cuda.is_available():
                raise RuntimeError("CUDA GPU is not available for GPUVLMPipeline")

            verified_base_dir = verify_pinned_base_model()

            has_proc_cfg = (
                (self.adapter_dir / "processor_config.json").is_file()
                or (self.adapter_dir / "preprocessor_config.json").is_file()
            )
            proc_src = str(self.adapter_dir) if has_proc_cfg else str(verified_base_dir)
            self._processor = AutoProcessor.from_pretrained(
                proc_src,
                min_pixels=self.min_pixels,
                max_pixels=self.max_pixels,
            )

            quant_cfg = None
            if self.load_in_4bit:
                quant_cfg = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_compute_dtype=torch.bfloat16,
                    bnb_4bit_quant_type="nf4",
                )

            base = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                str(verified_base_dir),
                torch_dtype=torch.bfloat16,
                quantization_config=quant_cfg,
                device_map=self.device,
            )
            if (self.adapter_dir / "adapter_config.json").is_file():
                peft_model = PeftModel.from_pretrained(base, str(self.adapter_dir))
                self._model = peft_model.merge_and_unload() if not self.load_in_4bit else peft_model
            else:
                self._model = base
            self._model.eval()

    def unload_if_idle(self, force: bool = False) -> bool:
        """Free GPU VRAM if idle for more than idle_unload_seconds (or immediately if force=True)."""
        acquired = self._infer_lock.acquire(blocking=force)
        if not acquired:
            return False
        try:
            with self._lock:
                if self._model is None:
                    return False
                if not force and (time.time() - self._last_used) < self.idle_unload_seconds:
                    return False
                import torch

                del self._model
                del self._processor
                self._model = None
                self._processor = None
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                return True
        finally:
            self._infer_lock.release()

    def predict_image(
        self,
        image_input: str | Path | bytes | np.ndarray,
        photo_index: int = 0,
        cpu_prior: FilamentItem | None = None,
        hint: str = "",
    ) -> FilamentItem:
        import torch

        if isinstance(image_input, (str, Path, bytes)):
            bgr = CPUHybridPipeline._load_bgr(image_input)
            pil_img = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        else:
            pil_img = Image.fromarray(cv2.cvtColor(image_input, cv2.COLOR_BGR2RGB))

        # Resize to <= 672px max side for fast VLM patch tokenization
        w, h = pil_img.size
        if max(w, h) > 672:
            scale = 672.0 / max(w, h)
            pil_img = pil_img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)

        # Reuse escalated CPU prior if already computed, otherwise run CPU helper
        if cpu_prior is None:
            cpu_prior = self.cpu_helper.predict_image(image_input, photo_index=photo_index)
        hint_line = f"User hint: {hint.strip()}\n" if hint and hint.strip() else ""
        hint_prompt = (
            f"{USER_PROMPT}\n"
            f"{hint_line}"
            f"OCR/Visual prior hint: vendor={cpu_prior.vendor}, line={cpu_prior.line}, "
            f"material={cpu_prior.material}, color={cpu_prior.color_name} ({cpu_prior.color_family}). "
            "Verify against the image and output the exact <intake-read> XML."
        )

        messages = [
            {"role": "system", "content": [{"type": "text", "text": SYSTEM_PROMPT}]},
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": pil_img},
                    {"type": "text", "text": hint_prompt},
                ],
            },
        ]

        from transformers import StoppingCriteria, StoppingCriteriaList

        with self._infer_lock:
            self._ensure_loaded()
            text_in = self._processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = self._processor(text=[text_in], images=[pil_img], padding=True, return_tensors="pt").to(self.device)
            stop_ids = self._processor.tokenizer.encode("</intake-read>", add_special_tokens=False)

            class _XmlEndStop(StoppingCriteria):
                def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs: Any) -> bool:
                    if len(stop_ids) > 0 and input_ids.shape[1] >= len(stop_ids):
                        tail = input_ids[0, -len(stop_ids):].tolist()
                        if tail == stop_ids:
                            return True
                    return False

            with torch.no_grad():
                gen_ids = self._model.generate(
                    **inputs,
                    max_new_tokens=self.max_new_tokens,
                    do_sample=self.do_sample,
                    stopping_criteria=StoppingCriteriaList([_XmlEndStop()]),
                )
            self._last_used = time.time()
            out_ids = [out[len(inp):] for inp, out in zip(inputs.input_ids, gen_ids)]
            xml_reply = self._processor.batch_decode(out_ids, skip_special_tokens=True)[0]

        try:
            parsed = IntakeReadResult.from_xml(xml_reply)
            if parsed.items:
                it = parsed.items[0]
                it.photos = [photo_index]
                # If VLM left vendor Unknown when CPU OCR found a known vendor, preserve the known vendor
                if it.vendor == "Unknown" and cpu_prior.vendor != "Unknown":
                    it.vendor = cpu_prior.vendor
                # If CPU prior identified a bare opened spool without bag packaging, prevent VLM bag hallucination
                if not cpu_prior.sealed and cpu_prior.sealed_reason == "bare spool":
                    has_bag = any(w in xml_reply.lower() for w in ("desiccant", "silica", "vacuum bag", "vacuum sealed"))
                    if not has_bag:
                        it.sealed = False
                        it.sealed_reason = "bare spool"
                return self.matcher.enrich_item(it)
        except Exception:
            pass

        return cpu_prior

    def predict(
        self,
        images: list[str | Path | bytes | np.ndarray],
        cpu_priors: list[FilamentItem] | None = None,
        hint: str = "",
    ) -> IntakeReadResult:
        t0 = time.perf_counter()
        items: list[FilamentItem] = []
        questions: list[str] = []

        for idx, img in enumerate(images):
            prior = cpu_priors[idx] if (cpu_priors is not None and idx < len(cpu_priors)) else None
            it = self.predict_image(img, photo_index=idx, cpu_prior=prior, hint=hint)
            merge_prediction_item(items, it, idx, self.matcher)

        for it in items:
            if it.vendor == "Unknown":
                first_photo = it.photos[0] if it.photos else 0
                questions.append(f"Photo {first_photo}: Please confirm filament brand (detected {it.line} {it.color_name}).")

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return IntakeReadResult(
            items=items,
            questions=questions,
            tier="gpu-vlm",
            latency_ms=elapsed_ms,
            model_name="qwen25-vl-7b-filament-lora",
        )

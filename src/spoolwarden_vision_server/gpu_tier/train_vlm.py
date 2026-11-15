"""QLoRA 4-bit fine-tuning for Qwen2.5-VL-7B-Instruct (Apache-2.0) producing <intake-read> XML."""
from __future__ import annotations

import random
import xml.etree.ElementTree as StdET
from pathlib import Path
from typing import Any

import torch
from PIL import Image
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

from ..xml_io import (
    CONFIGS_DIR,
    DATA_DIR,
    PROJECT_ROOT,
    AnnotatedSample,
    IntakeReadResult,
    load_dataset_xml,
    load_training_samples_with_feedback,
    parse_xml_file,
    write_xml_atomic,
)
from .prompts import SYSTEM_PROMPT, USER_PROMPT


def _format_target_xml(sample: AnnotatedSample) -> str:
    res = IntakeReadResult(
        items=[sample.item],
        questions=[],
        tier="gpu-vlm",
        latency_ms=0.0,
        model_name="qwen25-vl-7b-filament-lora",
    )
    return res.to_xml(declaration=False).strip()


def train_gpu_vlm_lora(
    train_xml: str | Path = DATA_DIR / "annotations" / "train_split.xml",
    val_xml: str | Path = DATA_DIR / "annotations" / "val_split.xml",
    output_dir: str | Path = PROJECT_ROOT / "models" / "gpu" / "qwen25_vl_7b_filament_lora",
    base_model_id: str = "Qwen/Qwen2.5-VL-7B-Instruct",
    max_steps: int = 240,
    lr: float | None = None,
    grad_accum: int | None = None,
    seed: int = 42,
    use_4bit: bool = True,
) -> dict[str, Any]:
    """Fine-tune Qwen2.5-VL-7B-Instruct LoRA adapters in 4-bit NF4 QLoRA on CUDA GPU to emit <intake-read> XML."""
    from transformers import AutoProcessor, BitsAndBytesConfig, Qwen2_5_VLForConditionalGeneration, get_cosine_schedule_with_warmup

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU required for Qwen2.5-VL-7B LoRA training")

    cfg_lr, cfg_grad_accum, cfg_warmup = 2e-4, 4, 0.05
    lora_r, lora_alpha, lora_dropout = 16, 32, 0.05
    cfg_min_px, cfg_max_px = 256 * 28 * 28, 640 * 28 * 28
    target_modules = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
    cfg_path = CONFIGS_DIR / "gpu_vlm_lora.xml"
    if cfg_path.is_file():
        try:
            cfg_root = parse_xml_file(cfg_path)
            bm_el = cfg_root.find("base-model")
            if bm_el is not None:
                cfg_min_px = int(bm_el.attrib.get("min-pixels", str(cfg_min_px)))
                cfg_max_px = int(bm_el.attrib.get("max-pixels", str(cfg_max_px)))
            lora_el = cfg_root.find("lora")
            if lora_el is not None:
                lora_r = int(lora_el.attrib.get("r", str(lora_r)))
                lora_alpha = int(lora_el.attrib.get("alpha", str(lora_alpha)))
                lora_dropout = float(lora_el.attrib.get("dropout", str(lora_dropout)))
                tms = [(m.text or "").strip() for m in lora_el.findall("target-module") if (m.text or "").strip()]
                if tms:
                    target_modules = tms
            tr_el = cfg_root.find("training")
            if tr_el is not None:
                cfg_grad_accum = int(tr_el.attrib.get("grad-accum", str(cfg_grad_accum)))
                cfg_lr = float(tr_el.attrib.get("lr", str(cfg_lr)))
                cfg_warmup = float(tr_el.attrib.get("warmup-ratio", str(cfg_warmup)))
        except Exception:
            pass

    eff_lr = cfg_lr if lr is None else float(lr)
    eff_grad_accum = cfg_grad_accum if grad_accum is None else int(grad_accum)

    random.seed(seed)
    torch.manual_seed(seed)

    train_samples = [s for s in load_training_samples_with_feedback(train_xml) if (PROJECT_ROOT / s.image_path).is_file()]
    val_samples = [s for s in load_dataset_xml(val_xml) if (PROJECT_ROOT / s.image_path).is_file()]

    # Prioritize real harvested samples + diverse synthetic samples
    real_samples = [s for s in train_samples if not s.sample_id.startswith("syn_")]
    syn_samples = [s for s in train_samples if s.sample_id.startswith("syn_")]
    random.shuffle(real_samples)
    random.shuffle(syn_samples)
    curated_train = real_samples + syn_samples[:160]
    if not curated_train:
        raise ValueError("No valid training samples with existing images found")
    random.shuffle(curated_train)

    from .pipeline import verify_pinned_base_model

    verified_src = (
        str(verify_pinned_base_model())
        if base_model_id == "Qwen/Qwen2.5-VL-7B-Instruct"
        else base_model_id
    )

    device = torch.device("cuda:0")
    print(f"[GPU-VLM] Loading {base_model_id} ({'4-bit NF4' if use_4bit else 'bfloat16'}) on {torch.cuda.get_device_name(0)}...")
    processor = AutoProcessor.from_pretrained(
        verified_src,
        min_pixels=cfg_min_px,
        max_pixels=cfg_max_px,
    )
    quant_cfg = (
        BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
        )
        if use_4bit
        else None
    )
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        verified_src,
        torch_dtype=torch.bfloat16,
        quantization_config=quant_cfg,
        device_map="cuda:0",
    )
    if use_4bit:
        model = prepare_model_for_kbit_training(model)

    lora_cfg = LoraConfig(
        r=lora_r,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        bias="none",
        target_modules=target_modules,
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_cfg)
    model.print_trainable_parameters()
    model.train()

    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=eff_lr, weight_decay=0.01)
    total_opt_steps = max(1, (max_steps + eff_grad_accum - 1) // eff_grad_accum)
    scheduler = get_cosine_schedule_with_warmup(
        optimizer,
        num_warmup_steps=max(2, int(round(total_opt_steps * cfg_warmup))),
        num_training_steps=total_opt_steps,
    )

    step = 0
    running_loss = 0.0
    final_loss = 0.0
    optimizer.zero_grad()

    while step < max_steps:
        for s in curated_train:
            if step >= max_steps:
                break
            img_path = PROJECT_ROOT / s.image_path
            pil_img = Image.open(img_path).convert("RGB").resize((560, 560), Image.Resampling.LANCZOS)
            target_xml = _format_target_xml(s)

            # Align training prompt format with inference prompt (with 25% vendor dropout so model learns to verify image)
            hint_vendor = "Unknown" if random.random() < 0.25 else s.item.vendor
            hint_prompt = (
                f"{USER_PROMPT}\n"
                f"OCR/Visual prior hint: vendor={hint_vendor}, line={s.item.line}, "
                f"material={s.item.material}, color={s.item.color_name} ({s.item.color_family}). "
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
                {"role": "assistant", "content": [{"type": "text", "text": target_xml}]},
            ]
            full_text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
            prompt_only = processor.apply_chat_template(messages[:-1], tokenize=False, add_generation_prompt=True)

            inputs = processor(text=[full_text], images=[pil_img], padding=True, return_tensors="pt").to(device)
            prompt_ids = processor(text=[prompt_only], images=[pil_img], padding=True, return_tensors="pt").input_ids
            prompt_len = prompt_ids.shape[1]

            labels = inputs.input_ids.clone()
            labels[:, :prompt_len] = -100

            outputs = model(**inputs, labels=labels)
            loss = outputs.loss / eff_grad_accum
            loss.backward()
            running_loss += float(outputs.loss.item())

            if (step + 1) % eff_grad_accum == 0 or (step + 1) == max_steps:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad()

            step += 1
            if step % 15 == 0 or step == max_steps:
                avg_l = running_loss / 15.0 if step % 15 == 0 else running_loss / max(1, step % 15)
                final_loss = avg_l
                running_loss = 0.0
                print(f"[GPU-VLM] Step {step:03d}/{max_steps} | Loss={avg_l:.4f}")

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(out_path))
    processor.save_pretrained(str(out_path))

    out_res = out_path.resolve()
    proj_res = PROJECT_ROOT.resolve()
    rel_adapter = str(out_res.relative_to(proj_res)) if out_res.is_relative_to(proj_res) else str(out_path)
    meta_root = StdET.Element(
        "gpu-vlm-meta",
        attrib={
            "version": "1",
            "base-model": base_model_id,
            "adapter-dir": rel_adapter.replace("\\", "/"),
            "steps": str(max_steps),
            "final-loss": f"{final_loss:.4f}",
            "val-samples": str(len(val_samples)),
        },
    )
    write_xml_atomic(out_path / "training_meta.xml", meta_root)

    del model
    torch.cuda.empty_cache()
    return {
        "adapter_dir": str(out_path),
        "steps": max_steps,
        "final_loss": round(final_loss, 4),
    }

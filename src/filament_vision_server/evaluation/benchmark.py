"""Evaluates CPU Hybrid and GPU VLM models on the Gold Benchmark Split and writes benchmark_report.xml."""
from __future__ import annotations

import time
import xml.etree.ElementTree as StdET
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from ..cpu_tier.pipeline import CPUHybridPipeline
from ..gpu_tier.pipeline import GPUVLMPipeline
from ..xml_io import DATA_DIR, PROJECT_ROOT, AnnotatedSample, load_dataset_xml, write_xml_atomic


def _eval_samples(
    samples: list[AnnotatedSample],
    predictor: Any,
    tier_name: str,
) -> dict[str, Any]:
    total = len(samples)
    if total == 0:
        return {"tier": tier_name, "samples": 0}

    vendor_exact = 0
    vendor_fuzzy = 0
    material_exact = 0
    line_fuzzy = 0
    color_family_exact = 0
    color_name_fuzzy = 0
    sealed_exact = 0
    latencies: list[float] = []

    for s in samples:
        img_path = PROJECT_ROOT / s.image_path
        if not img_path.is_file():
            continue
        t0 = time.perf_counter()
        pred = predictor.predict_image(img_path)
        lat_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(lat_ms)

        gt = s.item
        if pred.vendor.lower() == gt.vendor.lower():
            vendor_exact += 1
            vendor_fuzzy += 1
        elif fuzz.WRatio(pred.vendor.lower(), gt.vendor.lower()) >= 82.0:
            vendor_fuzzy += 1

        if pred.material.upper() == gt.material.upper():
            material_exact += 1

        if fuzz.token_set_ratio(pred.line.lower(), gt.line.lower()) >= 80.0:
            line_fuzzy += 1

        if pred.color_family.lower() == gt.color_family.lower():
            color_family_exact += 1

        if (
            fuzz.token_set_ratio(pred.color_name.lower(), gt.color_name.lower()) >= 78.0
            or pred.color_family.lower() == gt.color_family.lower()
        ):
            color_name_fuzzy += 1

        if pred.sealed == gt.sealed:
            sealed_exact += 1

    n = max(1, len(latencies))
    return {
        "tier": tier_name,
        "samples": len(latencies),
        "vendor_exact_acc": round(vendor_exact / n, 4),
        "vendor_fuzzy_acc": round(vendor_fuzzy / n, 4),
        "material_acc": round(material_exact / n, 4),
        "line_acc": round(line_fuzzy / n, 4),
        "color_family_acc": round(color_family_exact / n, 4),
        "color_name_acc": round(color_name_fuzzy / n, 4),
        "sealed_acc": round(sealed_exact / n, 4),
        "mean_latency_ms": round(sum(latencies) / n, 1),
        "p95_latency_ms": round(sorted(latencies)[int(0.95 * (n - 1))], 1),
    }


def run_benchmark(
    gold_xml: str | Path = DATA_DIR / "annotations" / "gold_test_split.xml",
    val_xml: str | Path = DATA_DIR / "annotations" / "val_split.xml",
    output_xml: str | Path = DATA_DIR / "annotations" / "benchmark_report.xml",
    include_gpu: bool = True,
    max_gpu_samples: int = 24,
) -> dict[str, Any]:
    """Run benchmark on Gold Real-World Split and Validation Split and save XML report."""
    gold_samples = load_dataset_xml(gold_xml) if Path(gold_xml).is_file() else []
    val_samples = load_dataset_xml(val_xml) if Path(val_xml).is_file() else []

    cpu_pipe = CPUHybridPipeline()
    results: dict[str, Any] = {}

    results["cpu_gold"] = _eval_samples(gold_samples, cpu_pipe, "cpu-hybrid-gold")
    results["cpu_val"] = _eval_samples(val_samples[:60], cpu_pipe, "cpu-hybrid-val")

    if include_gpu:
        try:
            import torch

            if torch.cuda.is_available():
                gpu_pipe = GPUVLMPipeline()
                results["gpu_gold"] = _eval_samples(gold_samples[:max_gpu_samples], gpu_pipe, "gpu-vlm-gold")
                gpu_pipe.unload_if_idle(force=True)
        except Exception as exc:
            results["gpu_error"] = str(exc)

    root = StdET.Element("benchmark-report", attrib={"version": "1"})
    for key, metrics in results.items():
        if not isinstance(metrics, dict):
            continue
        el = StdET.SubElement(
            root,
            "evaluation",
            attrib={k.replace("_", "-"): str(v) for k, v in metrics.items()},
        )
        el.text = key

    write_xml_atomic(output_xml, root)
    return results

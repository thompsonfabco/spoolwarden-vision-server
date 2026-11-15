"""Trains an Apache-2.0 RF-DETR Nano region detector on box/spool/winding bounding boxes and exports to ONNX."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from PIL import Image

from ..xml_io import CONFIGS_DIR, DATA_DIR, PROJECT_ROOT, AnnotatedSample, load_dataset_xml, load_training_samples_with_feedback, parse_xml_file

DETECTOR_CLASSES = ["box_label", "spool_label", "spool_windings", "spool_flange"]
CLASS_TO_ID = {name: idx for idx, name in enumerate(DETECTOR_CLASSES)}


def _prepare_coco_split(samples: list[AnnotatedSample], split_dir: Path) -> int:
    split_dir.mkdir(parents=True, exist_ok=True)

    categories = [
        {"id": idx, "name": name, "supercategory": "filament"}
        for idx, name in enumerate(DETECTOR_CLASSES)
    ]
    images: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    ann_id = 1
    img_id = 1

    for s in samples:
        src_img = PROJECT_ROOT / s.image_path
        if not src_img.is_file() or not s.boxes:
            continue

        file_name = f"{s.sample_id}.jpg"
        dst_img = split_dir / file_name
        if not dst_img.is_file():
            shutil.copy2(src_img, dst_img)

        try:
            with Image.open(dst_img) as im:
                w, h = im.size
        except Exception:
            continue

        images.append({
            "id": img_id,
            "file_name": file_name,
            "width": w,
            "height": h,
        })

        for b in s.boxes:
            cid = CLASS_TO_ID.get(b.label, 0)
            x1 = max(0.0, min(1.0, b.x1)) * w
            y1 = max(0.0, min(1.0, b.y1)) * h
            x2 = max(0.0, min(1.0, b.x2)) * w
            y2 = max(0.0, min(1.0, b.y2)) * h
            bw = max(1.0, x2 - x1)
            bh = max(1.0, y2 - y1)
            annotations.append({
                "id": ann_id,
                "image_id": img_id,
                "category_id": cid,
                "bbox": [round(x1, 2), round(y1, 2), round(bw, 2), round(bh, 2)],
                "area": round(bw * bh, 2),
                "iscrowd": 0,
            })
            ann_id += 1

        img_id += 1

    coco_payload = {
        "categories": categories,
        "images": images,
        "annotations": annotations,
    }
    (split_dir / "_annotations.coco.json").write_text(
        json.dumps(coco_payload), encoding="utf-8"
    )
    return len(images)


def train_and_export_detector(
    train_xml: str | Path = DATA_DIR / "annotations" / "train_split.xml",
    val_xml: str | Path = DATA_DIR / "annotations" / "val_split.xml",
    output_onnx: str | Path = PROJECT_ROOT / "models" / "cpu" / "detector_rfdetr_n.onnx",
    epochs: int | None = None,
    batch: int | None = None,
) -> dict[str, Any]:
    """Train RF-DETR-N (Apache-2.0) on CUDA/CPU and export to ONNX for CPU inference."""
    from rfdetr import RFDETRNano

    cfg_epochs, cfg_batch, cfg_lr = 8, 8, 1e-4
    cfg_path = CONFIGS_DIR / "cpu_pipeline.xml"
    if cfg_path.is_file():
        try:
            cfg_root = parse_xml_file(cfg_path)
            tr_el = cfg_root.find("./detector/training")
            if tr_el is not None:
                cfg_epochs = int(tr_el.attrib.get("epochs", str(cfg_epochs)))
                cfg_batch = int(tr_el.attrib.get("batch-size", str(cfg_batch)))
                cfg_lr = float(tr_el.attrib.get("lr0", str(cfg_lr)))
        except Exception:
            pass

    eff_epochs = cfg_epochs if epochs is None else int(epochs)
    eff_batch = cfg_batch if batch is None else int(batch)

    coco_root = DATA_DIR / "rfdetr_build"
    train_samples = load_training_samples_with_feedback(train_xml)
    val_samples = load_dataset_xml(val_xml)

    n_tr = _prepare_coco_split(train_samples, coco_root / "train")
    n_va = _prepare_coco_split(val_samples, coco_root / "valid")

    runs_dir = DATA_DIR / "training_runs" / "rfdetr_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    model = RFDETRNano()
    model.train(
        dataset_dir=str(coco_root),
        output_dir=str(runs_dir),
        epochs=eff_epochs,
        batch_size=eff_batch,
        grad_accum_steps=2,
        lr=cfg_lr,
        num_workers=0,
        run_test=False,
        checkpoint_interval=eff_epochs,
    )

    # Load best checkpoint if saved, then export to ONNX
    best_ckpt = runs_dir / "checkpoint_best_total.pth"
    if not best_ckpt.is_file():
        best_ckpt = runs_dir / "checkpoint_best_ema.pth"
    if best_ckpt.is_file():
        export_model = RFDETRNano(pretrain_weights=str(best_ckpt))
    else:
        export_model = model

    export_dir = runs_dir / "onnx_export"
    export_dir.mkdir(parents=True, exist_ok=True)
    exported_path = export_model.export(
        output_dir=str(export_dir),
        format="onnx",
        opset_version=17,
        fp16=False,
    )

    out_path = Path(output_onnx)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if exported_path and Path(exported_path).is_file():
        shutil.copy2(exported_path, out_path)
    else:
        # Search for any .onnx in export_dir
        onnx_files = list(export_dir.glob("*.onnx"))
        if onnx_files:
            shutil.copy2(onnx_files[0], out_path)

    # Strip embedded build paths and node stack traces (doc_string / metadata_props)
    if out_path.is_file():
        import onnx

        def _clear_graph_docs(graph: Any) -> None:
            graph.doc_string = ""
            for node in graph.node:
                node.doc_string = ""
                for attr in node.attribute:
                    if attr.g is not None and attr.g.ByteSize() > 0:
                        _clear_graph_docs(attr.g)
                    for sg in attr.graphs:
                        _clear_graph_docs(sg)
            for tensor in list(graph.initializer) + list(graph.input) + list(graph.output) + list(graph.value_info):
                tensor.doc_string = ""

        onnx_model = onnx.load(str(out_path))
        onnx_model.doc_string = ""
        onnx_model.domain = ""
        del onnx_model.metadata_props[:]
        onnx_model.producer_name = "spoolwarden-vision-server"
        _clear_graph_docs(onnx_model.graph)
        onnx.save(onnx_model, str(out_path))

    return {
        "onnx_path": str(out_path),
        "train_images": n_tr,
        "val_images": n_va,
    }

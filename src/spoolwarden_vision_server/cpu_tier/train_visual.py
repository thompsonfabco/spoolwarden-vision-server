"""Multi-head visual attribute classifier (MobileNetV3-Large) trained on GPU and exported to ONNX for CPU inference."""
from __future__ import annotations

import logging
import random
import xml.etree.ElementTree as StdET
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import timm
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset

from ..xml_io import CONFIGS_DIR, DATA_DIR, PROJECT_ROOT, AnnotatedSample, load_dataset_xml, load_training_samples_with_feedback, parse_xml_file, write_xml_atomic
from .labels import COLOR_FAMILIES, SEALED_STATES, VISUAL_FINISHES, VISUAL_VENDORS

log = logging.getLogger(__name__)


class MultiHeadFilamentNet(nn.Module):
    """MobileNetV3-Large backbone (Apache-2.0) with 4 classification heads for ONNX CPU inference."""

    def __init__(self, pretrained: bool = True):
        super().__init__()
        self.backbone = timm.create_model(
            "mobilenetv3_large_100",
            pretrained=pretrained,
            num_classes=0,
            global_pool="avg",
        )
        with torch.no_grad():
            feat_dim = int(self.backbone(torch.zeros(1, 3, 224, 224)).shape[-1])
        self.dropout = nn.Dropout(0.15)
        self.head_color = nn.Linear(feat_dim, len(COLOR_FAMILIES))
        self.head_finish = nn.Linear(feat_dim, len(VISUAL_FINISHES))
        self.head_vendor = nn.Linear(feat_dim, len(VISUAL_VENDORS))
        self.head_sealed = nn.Linear(feat_dim, len(SEALED_STATES))

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        feat = self.dropout(self.backbone(x))
        return (
            self.head_color(feat),
            self.head_finish(feat),
            self.head_vendor(feat),
            self.head_sealed(feat),
        )


class VisualAttributeDataset(Dataset):
    def __init__(self, samples: list[AnnotatedSample], augment: bool = False):
        valid_samples: list[AnnotatedSample] = []
        skipped = 0
        for s in samples:
            img_path = PROJECT_ROOT / s.image_path
            if img_path.is_file() and cv2.imread(str(img_path)) is not None:
                valid_samples.append(s)
            else:
                skipped += 1
        if skipped:
            log.warning("Skipped %d visual training samples with missing or unreadable images", skipped)
        if not valid_samples:
            raise ValueError("No valid visual training samples with readable images found")
        self.samples = valid_samples
        self.augment = augment
        self.color_to_idx = {c: i for i, c in enumerate(COLOR_FAMILIES)}
        self.finish_to_idx = {f: i for i, f in enumerate(VISUAL_FINISHES)}
        self.vendor_to_idx = {v: i for i, v in enumerate(VISUAL_VENDORS)}

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        s = self.samples[idx]
        img_path = PROJECT_ROOT / s.image_path
        img_bgr = cv2.imread(str(img_path))
        if img_bgr is None:
            raise RuntimeError(f"Unreadable image during training: {img_path}")
        if self.augment and s.boxes and random.random() < 0.40:
            b = random.choice(s.boxes)
            ih, iw = img_bgr.shape[:2]
            px1 = max(0, int((b.x1 - 0.05) * iw))
            py1 = max(0, int((b.y1 - 0.05) * ih))
            px2 = min(iw, int((b.x2 + 0.05) * iw))
            py2 = min(ih, int((b.y2 + 0.05) * ih))
            if (px2 - px1) >= 48 and (py2 - py1) >= 48:
                img_bgr = img_bgr[py1:py2, px1:px2]
        img_bgr = cv2.resize(img_bgr, (224, 224), interpolation=cv2.INTER_AREA)

        if self.augment:
            if random.random() < 0.5:
                img_bgr = cv2.flip(img_bgr, 1)
            if random.random() < 0.5:
                img_bgr = cv2.flip(img_bgr, 0)
            # Slight brightness/contrast jitter preserving hue
            alpha = random.uniform(0.88, 1.14)
            beta = random.uniform(-12, 12)
            img_bgr = np.clip(img_bgr.astype(np.float32) * alpha + beta, 0, 255).astype(np.uint8)

        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        norm = (img_rgb - mean) / std
        tensor_x = torch.from_numpy(norm.transpose(2, 0, 1))

        c_idx = self.color_to_idx.get(s.item.color_family, 0)
        f_idx = self.finish_to_idx.get(s.visual_finish, 0)
        v_idx = self.vendor_to_idx.get(s.item.vendor, len(VISUAL_VENDORS) - 1)
        if not s.item.sealed:
            s_idx = 0
        elif s.item.source_type == "box_label" or "box" in s.item.sealed_reason:
            s_idx = 2
        else:
            s_idx = 1

        return (
            tensor_x,
            torch.tensor(c_idx, dtype=torch.long),
            torch.tensor(f_idx, dtype=torch.long),
            torch.tensor(v_idx, dtype=torch.long),
            torch.tensor(s_idx, dtype=torch.long),
        )


def train_and_export_visual_classifier(
    train_xml: str | Path = DATA_DIR / "annotations" / "train_split.xml",
    val_xml: str | Path = DATA_DIR / "annotations" / "val_split.xml",
    output_onnx: str | Path = PROJECT_ROOT / "models" / "cpu" / "visual_multihead.onnx",
    epochs: int | None = None,
    batch_size: int | None = None,
    lr: float | None = None,
) -> dict[str, Any]:
    """Train MultiHeadFilamentNet on GPU and export to ONNX + XML metadata."""
    cfg_epochs, cfg_batch, cfg_lr, cfg_wd = 35, 32, 1e-3, 1e-4
    head_weights = {"color_family": 1.0, "visual_finish": 0.8, "visual_vendor": 1.2, "sealed_state": 0.6}
    cfg_path = CONFIGS_DIR / "cpu_pipeline.xml"
    if cfg_path.is_file():
        try:
            cfg_root = parse_xml_file(cfg_path)
            vc_el = cfg_root.find("visual-classifier")
            if vc_el is not None:
                tr_el = vc_el.find("training")
                if tr_el is not None:
                    cfg_epochs = int(tr_el.attrib.get("epochs", str(cfg_epochs)))
                    cfg_batch = int(tr_el.attrib.get("batch-size", str(cfg_batch)))
                    cfg_lr = float(tr_el.attrib.get("lr", str(cfg_lr)))
                    cfg_wd = float(tr_el.attrib.get("weight-decay", str(cfg_wd)))
                for h_el in vc_el.findall("./heads/head"):
                    hname = h_el.attrib.get("name", "")
                    if hname in head_weights and h_el.attrib.get("loss-weight"):
                        head_weights[hname] = float(h_el.attrib["loss-weight"])
        except Exception as exc:
            log.warning("Failed to read visual training defaults from %s: %s", cfg_path, exc)

    eff_epochs = cfg_epochs if epochs is None else int(epochs)
    eff_batch = cfg_batch if batch_size is None else int(batch_size)
    eff_lr = cfg_lr if lr is None else float(lr)

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    train_samples = load_training_samples_with_feedback(train_xml)
    val_samples = load_dataset_xml(val_xml)

    train_loader = DataLoader(VisualAttributeDataset(train_samples, augment=True), batch_size=eff_batch, shuffle=True)
    val_loader = DataLoader(VisualAttributeDataset(val_samples, augment=False), batch_size=eff_batch, shuffle=False)

    model = MultiHeadFilamentNet(pretrained=True).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=eff_lr, weight_decay=cfg_wd)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=eff_epochs)
    criterion = nn.CrossEntropyLoss()

    best_val_acc = 0.0
    out_path = Path(output_onnx)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ckpt_dir = DATA_DIR / "training_runs"
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = ckpt_dir / f"{out_path.stem}_best.pt"

    for epoch in range(1, eff_epochs + 1):
        model.train()
        total_loss = 0.0
        for x, yc, yf, yv, ys in train_loader:
            x, yc, yf, yv, ys = x.to(device), yc.to(device), yf.to(device), yv.to(device), ys.to(device)
            optimizer.zero_grad()
            pc, pf, pv, ps = model(x)
            loss = (
                head_weights["color_family"] * criterion(pc, yc)
                + head_weights["visual_finish"] * criterion(pf, yf)
                + head_weights["visual_vendor"] * criterion(pv, yv)
                + head_weights["sealed_state"] * criterion(ps, ys)
            )
            loss.backward()
            optimizer.step()
            total_loss += float(loss.item()) * len(x)
        scheduler.step()

        model.eval()
        correct_c, correct_f, correct_v, correct_s, total = 0, 0, 0, 0, 0
        with torch.no_grad():
            for x, yc, yf, yv, ys in val_loader:
                x, yc, yf, yv, ys = x.to(device), yc.to(device), yf.to(device), yv.to(device), ys.to(device)
                pc, pf, pv, ps = model(x)
                correct_c += int((pc.argmax(1) == yc).sum().item())
                correct_f += int((pf.argmax(1) == yf).sum().item())
                correct_v += int((pv.argmax(1) == yv).sum().item())
                correct_s += int((ps.argmax(1) == ys).sum().item())
                total += len(x)

        val_c_acc = correct_c / max(total, 1)
        val_f_acc = correct_f / max(total, 1)
        val_v_acc = correct_v / max(total, 1)
        val_s_acc = correct_s / max(total, 1)
        mean_acc = (val_c_acc + val_f_acc + val_v_acc + val_s_acc) / 4.0
        if mean_acc >= best_val_acc:
            best_val_acc = mean_acc
            torch.save(model.state_dict(), ckpt_path)

        if epoch % 5 == 0 or epoch == eff_epochs:
            print(
                f"[VisualClassifier] Epoch {epoch:02d}/{eff_epochs} | "
                f"ColorAcc={val_c_acc:.3f} FinishAcc={val_f_acc:.3f} "
                f"VendorAcc={val_v_acc:.3f} SealedAcc={val_s_acc:.3f}"
            )

    if ckpt_path.is_file():
        model.load_state_dict(torch.load(ckpt_path, map_location="cpu", weights_only=True))
    model.cpu().eval()

    dummy = torch.randn(1, 3, 224, 224, dtype=torch.float32)
    torch.onnx.export(
        model,
        dummy,
        str(out_path),
        input_names=["image"],
        output_names=["color_logits", "finish_logits", "vendor_logits", "sealed_logits"],
        dynamic_axes={"image": {0: "batch"}},
        opset_version=17,
        dynamo=False,
    )

    # Strip any embedded build paths / doc_strings from ONNX metadata
    import onnx

    onnx_model = onnx.load(str(out_path))
    onnx_model.doc_string = ""
    onnx_model.domain = ""
    del onnx_model.metadata_props[:]
    onnx_model.producer_name = "spoolwarden-vision-server"
    onnx_model.graph.doc_string = ""
    for node in onnx_model.graph.node:
        node.doc_string = ""
    for tensor in list(onnx_model.graph.initializer) + list(onnx_model.graph.input) + list(onnx_model.graph.output) + list(onnx_model.graph.value_info):
        tensor.doc_string = ""
    onnx.save(onnx_model, str(out_path))

    # Save class mapping metadata in XML
    meta_root = StdET.Element("visual-classifier-meta", attrib={"version": "1", "onnx": out_path.name})
    for tag, items in (
        ("color-families", COLOR_FAMILIES),
        ("visual-finishes", VISUAL_FINISHES),
        ("visual-vendors", VISUAL_VENDORS),
        ("sealed-states", SEALED_STATES),
    ):
        sub = StdET.SubElement(meta_root, tag)
        for i, name in enumerate(items):
            c_el = StdET.SubElement(sub, "class", attrib={"id": str(i), "name": name})
            c_el.text = name
    write_xml_atomic(out_path.with_name("visual_meta.xml"), meta_root)

    return {
        "onnx_path": str(out_path),
        "best_val_acc": round(best_val_acc, 4),
    }

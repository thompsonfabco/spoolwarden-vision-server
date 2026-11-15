# Third-Party Notices

`spoolwarden-vision-server` is licensed under the **GNU General Public License v3.0 or later** (`LICENSE`).
It relies on the following open-source libraries, runtime engines, and optional GPU model components, each distributed under its own license:

## Core Runtime Dependencies

- **ONNX Runtime** (`onnxruntime`) — MIT License — CPU/GPU ONNX model inference for detector and visual classifier heads.
- **RapidOCR** (`rapidocr`) — Apache License 2.0 — On-device label OCR text extraction.
- **OpenCV (`opencv-python-headless`)** — Apache License 2.0 — Image decoding, resizing, and color-space conversion.
- **Pillow** (`Pillow`) — HPND License — Image decoding, EXIF orientation normalization, and sanitization.
- **pillow-heif** (`pillow-heif` / `libheif`) — LGPL-3.0 — HEIC/HEIF image decoding from mobile cameras.
- **NumPy** (`numpy`) — BSD-3-Clause License — Array computation and color analysis.
- **scikit-learn** (`scikit-learn`) — BSD-3-Clause License — Text classifier and vectorization utilities.
- **RapidFuzz** (`rapidfuzz`) — MIT License — Fast fuzzy catalog and natural-language matching.
- **defusedxml** (`defusedxml`) — PSF-2.0 License — Safe XML configuration and catalog parsing.

## Optional GPU & Training Extras

- **PyTorch** (`torch`, `torchvision`) — BSD-3-Clause License
- **Hugging Face Transformers, PEFT & Accelerate** — Apache License 2.0
- **timm** & **RF-DETR** — Apache License 2.0
- **bitsandbytes** — MIT License
- **Qwen2.5-VL-7B-Instruct & `qwen-vl-utils`** — Apache License 2.0
- **ONNX & onnxscript** — Apache License 2.0 / MIT License

"""Local web UI for reviewing and editing XML dataset annotations."""
from __future__ import annotations

from pathlib import Path

from ..xml_io import DATA_DIR, PROJECT_ROOT, load_dataset_xml, save_dataset_xml


def launch_review_ui(xml_path: str | Path = DATA_DIR / "annotations" / "seed_labels.xml", port: int = 7860) -> None:
    """Launch a Gradio dataset inspection & active-learning editor for seed_labels.xml."""
    import gradio as gr

    samples = load_dataset_xml(xml_path)

    def _get_record(idx: int):
        idx = max(0, min(len(samples) - 1, int(idx)))
        s = samples[idx]
        img_file = str(PROJECT_ROOT / s.image_path)
        it = s.item
        return (
            img_file,
            s.sample_id,
            s.split,
            it.vendor,
            it.line,
            it.material,
            it.color_name,
            it.color_family,
            it.color_hex or "",
            it.sealed,
            "\n".join(s.ocr_lines),
        )

    def _save_record(
        idx: int,
        vendor: str,
        line: str,
        material: str,
        color_name: str,
        color_family: str,
        color_hex: str,
        sealed: bool,
    ):
        idx = max(0, min(len(samples) - 1, int(idx)))
        s = samples[idx]
        s.item.vendor = vendor.strip()
        s.item.line = line.strip()
        s.item.material = material.strip()
        s.item.color_name = color_name.strip()
        s.item.color_family = color_family.strip()
        s.item.color_hex = color_hex.strip().lstrip("#").upper() or None
        s.item.sealed = bool(sealed)
        save_dataset_xml(xml_path, samples, split_name="all_seed")
        return f"Saved {s.sample_id} to {Path(xml_path).name}"

    with gr.Blocks(title="filament-vision-server Dataset Reviewer") as demo:
        gr.Markdown("## `filament-vision-server` XML Dataset Reviewer")
        with gr.Row():
            idx_slider = gr.Slider(0, max(0, len(samples) - 1), step=1, value=0, label="Sample Index")
            status_box = gr.Textbox(label="Status", interactive=False)
        with gr.Row():
            img_view = gr.Image(label="Spool / Box Photo", type="filepath")
            with gr.Column():
                sid_box = gr.Textbox(label="Sample ID", interactive=False)
                split_box = gr.Textbox(label="Split", interactive=False)
                vendor_box = gr.Textbox(label="Vendor")
                line_box = gr.Textbox(label="Product Line (extra.line)")
                mat_box = gr.Textbox(label="Base Material (Spoolman)")
                cname_box = gr.Textbox(label="Color Name")
                cfam_box = gr.Textbox(label="Color Family")
                chex_box = gr.Textbox(label="Color Hex (6 chars)")
                sealed_chk = gr.Checkbox(label="Sealed (in box / vacuum bag)")
                ocr_box = gr.Textbox(label="Detected OCR Lines", lines=4, interactive=False)
                save_btn = gr.Button("Save Changes to XML", variant="primary")

        idx_slider.change(
            _get_record,
            inputs=[idx_slider],
            outputs=[
                img_view, sid_box, split_box, vendor_box, line_box,
                mat_box, cname_box, cfam_box, chex_box, sealed_chk, ocr_box,
            ],
        )
        save_btn.click(
            _save_record,
            inputs=[idx_slider, vendor_box, line_box, mat_box, cname_box, cfam_box, chex_box, sealed_chk],
            outputs=[status_box],
        )

    demo.launch(server_name="127.0.0.1", server_port=port)

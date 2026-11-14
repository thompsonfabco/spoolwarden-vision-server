"""Command-line interface for filament-vision-server (serve, predict, config, status, sync-catalog, train, benchmark)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..catalog.library_sync import DEFAULT_LIBRARY_ROOT, build_master_catalog_xml
from ..evaluation.benchmark import run_benchmark
from .daemon import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    FilamentRecognizer,
    LocalIntakeClient,
    load_service_config,
    run_daemon_forever,
    save_service_config,
)
from .feedback import ingest_feedback_bundles
from .updater import run_one_click_update


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="filament-vision-server",
        description="Local dual-tier (CPU ONNX + GPU VLM) 3D printer filament recognition engine",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # serve
    p_serve = sub.add_parser("serve", help="Start the local HTTP recognition daemon")
    p_serve.add_argument("--host", default=None, help="Bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=None, help=f"Bind port (default: {DEFAULT_PORT})")
    p_serve.add_argument("--tier", choices=["auto", "cpu", "gpu"], default=None, help="Inference tier")

    # predict
    p_pred = sub.add_parser("predict", help="Recognize filament from one or more image files")
    p_pred.add_argument("images", nargs="+", help="Path(s) to filament spool or box photos")
    p_pred.add_argument("--tier", choices=["auto", "cpu", "gpu"], default="auto")
    p_pred.add_argument("--format", choices=["xml", "filament-kit"], default="xml")

    # config
    p_cfg = sub.add_parser("config", help="View or update local_intake service configuration (XML)")
    p_cfg.add_argument("--tier", choices=["auto", "cpu", "gpu"], default=None)
    p_cfg.add_argument("--host", default=None)
    p_cfg.add_argument("--port", type=int, default=None)
    p_cfg.add_argument("--idle-unload-seconds", type=int, default=None)
    p_cfg.add_argument("--feedback-opt-in", choices=["true", "false"], default=None)
    p_cfg.add_argument("--feedback-endpoint", default=None)

    # status
    sub.add_parser("status", help="Check local daemon health, trained ONNX/VLM weights, and GPU status")

    # sync-catalog
    p_sync = sub.add_parser("sync-catalog", help="Enrich filament-library and rebuild filament_catalog.xml")
    p_sync.add_argument("--library-root", default=str(DEFAULT_LIBRARY_ROOT))

    # train
    p_train = sub.add_parser("train", help="Train CPU Hybrid ONNX models and/or GPU VLM LoRA adapters")
    p_train.add_argument("--tier", choices=["cpu", "gpu", "all"], default="all")

    # benchmark
    p_bench = sub.add_parser("benchmark", help="Run evaluation on the Gold Test Split and write benchmark_report.xml")
    p_bench.add_argument("--no-gpu", action="store_true")

    # playground
    p_play = sub.add_parser("playground", help="Start interactive browser UI to drag-and-drop images and test models")
    p_play.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_play.add_argument("--port", type=int, default=7861, help="Bind port (default: 7861)")

    # ingest-feedback
    p_ingest = sub.add_parser("ingest-feedback", help="Import downloaded user-feedback .zip bundles into verified training XML")
    p_ingest.add_argument("source", help="Path to a feedback .zip file or directory of .zip files")

    # update-models (one-click training update)
    p_upd = sub.add_parser(
        "update-models",
        help="One-click update: ingest Drive feedback zips, check batch threshold, retrain, and benchmark",
    )
    p_upd.add_argument("--source", action="append", default=None, help="Folder(s) or .zip(s) to ingest (default: data/feedback_incoming + Downloads)")
    p_upd.add_argument("--min-new-samples", type=int, default=10, help="Minimum new feedback samples before retraining (default: 10)")
    p_upd.add_argument("--force", action="store_true", help="Retrain even if below --min-new-samples")
    p_upd.add_argument("--tier", choices=["cpu", "gpu", "all"], default="all")
    p_upd.add_argument("--skip-benchmark", action="store_true")
    p_upd.add_argument("--release-version", default=None, help="Optionally build & sign a release after training")
    p_upd.add_argument("--key-file", default=None, help="Ed25519 PEM private key for --release-version")

    args = parser.parse_args(argv)
    cfg = load_service_config()

    if args.cmd == "serve":
        host = args.host or cfg.get("host", DEFAULT_HOST)
        port = args.port or int(cfg.get("port", DEFAULT_PORT))
        tier = args.tier or cfg.get("tier", "auto")
        run_daemon_forever(
            host=host,
            port=port,
            tier=tier,
            idle_unload_seconds=float(cfg.get("idle_unload_seconds", 300)),
            auth_token=str(cfg.get("token", "")),
            feedback_opt_in=bool(cfg.get("feedback_opt_in", False)),
            feedback_endpoint=str(cfg.get("feedback_endpoint", "")),
        )
        return 0

    if args.cmd == "predict":
        rec = FilamentRecognizer(default_tier=args.tier)
        res, _, _ = rec.predict([Path(p) for p in args.images], tier=args.tier)
        if args.format == "filament-kit":
            print(json.dumps(res.to_filament_kit_dict(), indent=2))
        else:
            sys.stdout.write(res.to_xml())
        return 0

    if args.cmd == "config":
        changed = False
        if args.tier is not None:
            cfg["tier"] = args.tier
            changed = True
        if args.host is not None:
            cfg["host"] = args.host
            changed = True
        if args.port is not None:
            cfg["port"] = args.port
            changed = True
        if args.idle_unload_seconds is not None:
            cfg["idle_unload_seconds"] = args.idle_unload_seconds
            changed = True
        if args.feedback_opt_in is not None:
            cfg["feedback_opt_in"] = args.feedback_opt_in == "true"
            changed = True
        if args.feedback_endpoint is not None:
            cfg["feedback_endpoint"] = args.feedback_endpoint.strip()
            changed = True
        out_path = save_service_config(cfg)
        print(f"{'Updated' if changed else 'Current'} config ({out_path}): {cfg}")
        return 0

    if args.cmd == "status":
        client = LocalIntakeClient(base_url=f"http://{cfg['host']}:{cfg['port']}")
        try:
            h = client.health()
            print(f"Daemon: RUNNING at http://{cfg['host']}:{cfg['port']} -> {h}")
        except Exception:
            rec = FilamentRecognizer(default_tier=cfg["tier"])
            print(
                f"Daemon: STOPPED (configured for http://{cfg['host']}:{cfg['port']}, "
                f"tier={cfg['tier']}, gpu_available={rec.gpu_available()})"
            )
        return 0

    if args.cmd == "sync-catalog":
        out = build_master_catalog_xml(library_root=args.library_root, enrich_source_library=True)
        print(f"Synced and built master catalog XML: {out}")
        return 0

    if args.cmd == "train":
        if args.tier in ("cpu", "all"):
            from ..cpu_tier.train_detector import train_and_export_detector
            from ..cpu_tier.train_text_parser import train_and_export_text_parser
            from ..cpu_tier.train_visual import train_and_export_visual_classifier

            print("Training CPU Text Parser:", train_and_export_text_parser())
            print("Training CPU Visual Multi-Head Classifier:", train_and_export_visual_classifier())
            print("Training CPU RF-DETR-N Detector:", train_and_export_detector())
        if args.tier in ("gpu", "all"):
            from ..gpu_tier.train_vlm import train_gpu_vlm_lora

            print("Training GPU Qwen2.5-VL-7B LoRA:", train_gpu_vlm_lora())
        return 0

    if args.cmd == "benchmark":
        metrics = run_benchmark(include_gpu=not args.no_gpu)
        print(json.dumps(metrics, indent=2))
        return 0

    if args.cmd == "playground":
        from .playground_ui import run_playground_server

        run_playground_server(host=args.host, port=args.port)
        return 0

    if args.cmd == "ingest-feedback":
        summary = ingest_feedback_bundles(args.source)
        print(json.dumps(summary, indent=2))
        return 0

    if args.cmd == "update-models":
        summary = run_one_click_update(
            source_dirs=args.source,
            min_new_samples=args.min_new_samples,
            force=args.force,
            tier=args.tier,
            skip_benchmark=args.skip_benchmark,
            release_version=args.release_version,
            key_file=args.key_file,
        )
        print(json.dumps(summary, indent=2))
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

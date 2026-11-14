"""End-to-end tests for the standalone filament-vision-server HTTP daemon & Web UI."""
from __future__ import annotations

import base64
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile

import pytest
from PIL import Image, ImageDraw

from filament_vision_server.service import daemon as daemon_mod


def _make_sample_spool_jpeg() -> bytes:
    img = Image.new("RGB", (640, 480), color=(240, 240, 240))
    draw = ImageDraw.Draw(img)
    # Dark outer spool ring + bright orange center filament swatch + white label rectangle
    draw.ellipse((80, 40, 560, 440), fill=(35, 35, 38))
    draw.ellipse((150, 95, 490, 385), fill=(255, 102, 0))
    draw.ellipse((245, 165, 395, 315), fill=(40, 40, 42))
    draw.rectangle((190, 200, 450, 290), fill=(250, 250, 250))
    draw.text((205, 220), "HATCHBOX PLA 1.75mm 1kg", fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


@pytest.fixture()
def standalone_server(tmp_path, monkeypatch):
    cfg_path = tmp_path / "service_config.xml"
    fb_dir = tmp_path / "feedback_queue"
    monkeypatch.setattr(daemon_mod, "DEFAULT_SERVICE_CONFIG_XML", cfg_path)

    server = daemon_mod.start_daemon(
        host="127.0.0.1",
        port=0,
        tier="cpu",
        feedback_opt_in=False,
        feedback_dir=fb_dir,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}"
    try:
        yield base_url, cfg_path, fb_dir
    finally:
        server.shutdown()
        server.server_close()


def _http_request(url: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30.0) as resp:
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, hdrs, resp.read()


def test_web_ui_served_on_root_and_ui(standalone_server):
    base_url, _, _ = standalone_server
    for path in ("/", "/ui"):
        status, hdrs, raw = _http_request(f"{base_url}{path}")
        html = raw.decode("utf-8")
        assert status == 200
        assert "text/html" in hdrs.get("content-type", "")
        assert 'id="dropzone"' in html
        assert "cameraInput" in html
        assert "Export JSON" in html
        assert "Export Spoolman" in html


def test_health_and_cors_preflight(standalone_server):
    base_url, _, _ = standalone_server
    status, hdrs, raw = _http_request(f"{base_url}/v1/health")
    assert status == 200
    assert hdrs.get("access-control-allow-origin") == "*"
    payload = json.loads(raw.decode("utf-8"))
    assert payload["service"] == "filament-vision-server"
    assert payload["module_version"] == "1.0.0"
    assert payload["status"] == "ok"
    assert payload["tiers"]["cpu"]["usable"] is True

    opt_status, opt_hdrs, _ = _http_request(f"{base_url}/v1/read", method="OPTIONS")
    assert opt_status == 204
    assert opt_hdrs.get("access-control-allow-origin") == "*"
    assert "POST" in opt_hdrs.get("access-control-allow-methods", "")


def test_read_and_revise_cpu_tier(standalone_server):
    base_url, _, _ = standalone_server
    jpg_b64 = base64.b64encode(_make_sample_spool_jpeg()).decode("ascii")

    status, _, raw = _http_request(
        f"{base_url}/v1/read",
        method="POST",
        payload={
            "api_version": "1.0",
            "tier": "cpu",
            "images_b64": [jpg_b64],
            "hint": "Hatchbox PLA Orange 1kg",
        },
    )
    assert status == 200
    data = json.loads(raw.decode("utf-8"))
    assert isinstance(data.get("items"), list) and len(data["items"]) >= 1
    first = data["items"][0]
    assert "vendor" in first and "material" in first and "diameter" in first
    assert data["reader"]["module_version"] == "1.0.0"

    rev_status, _, rev_raw = _http_request(
        f"{base_url}/v1/revise",
        method="POST",
        payload={
            "api_version": "1.0",
            "previous": first,
            "note": "Actually Prusament PETG Jet Black",
        },
    )
    assert rev_status == 200
    rev = json.loads(rev_raw.decode("utf-8"))
    assert rev["item"]["material"] == "PETG"


def test_config_persistence_and_feedback_lifecycle(standalone_server):
    base_url, cfg_path, _ = standalone_server

    # Disabled initially -> enqueue returns 403
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _http_request(
            f"{base_url}/v1/feedback",
            method="POST",
            payload={"api_version": "1.0", "images_b64": [], "predicted": {}, "corrected": {}},
        )
    assert exc_info.value.code == 403

    # Update config via POST /v1/config and confirm persistence + GET /v1/config
    st_post, _, raw_post = _http_request(
        f"{base_url}/v1/config",
        method="POST",
        payload={"api_version": "1.0", "feedback_opt_in": True, "idle_unload_seconds": 180},
    )
    assert st_post == 200
    post_cfg = json.loads(raw_post.decode("utf-8"))
    assert post_cfg["ok"] is True
    assert post_cfg["feedback_opt_in"] is True
    assert post_cfg["idle_unload_seconds"] == 180
    assert cfg_path.is_file()

    st_get, _, raw_get = _http_request(f"{base_url}/v1/config")
    assert st_get == 200
    get_cfg = json.loads(raw_get.decode("utf-8"))
    assert get_cfg["feedback_opt_in"] is True
    assert get_cfg["idle_unload_seconds"] == 180

    # Enqueue one EXIF-stripped correction, download bundle zip, then delete entry
    jpg_b64 = base64.b64encode(_make_sample_spool_jpeg()).decode("ascii")
    st_enq, _, raw_enq = _http_request(
        f"{base_url}/v1/feedback",
        method="POST",
        payload={
            "api_version": "1.0",
            "images_b64": [jpg_b64],
            "predicted": {"vendor": "Unknown", "material": "PLA"},
            "corrected": {"vendor": "Hatchbox", "material": "PLA", "color_name": "Orange", "color_hex": "#FF6600"},
            "note": "verified test sample",
        },
    )
    assert st_enq == 200
    enq = json.loads(raw_enq.decode("utf-8"))
    entry_id = enq["id"]
    assert enq["queued_count"] == 1

    st_zip, _, zip_bytes = _http_request(f"{base_url}/v1/feedback/bundle.zip")
    assert st_zip == 200
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        names = zf.namelist()
        assert any(entry_id in n for n in names)

    st_del, _, raw_del = _http_request(f"{base_url}/v1/feedback/{entry_id}", method="DELETE")
    assert st_del == 200
    del_res = json.loads(raw_del.decode("utf-8"))
    assert del_res["ok"] is True
    assert del_res["queued_count"] == 0

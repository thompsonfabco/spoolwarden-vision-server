"""End-to-end tests for the standalone filament-vision-server HTTP daemon & Web UI."""
from __future__ import annotations

import base64
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from filament_vision_server.service import daemon as daemon_mod
from filament_vision_server.service import feedback as feedback_mod


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


def _http_request(
    url: str,
    method: str = "GET",
    payload: dict | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    data = None
    headers = dict(extra_headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30.0) as resp:
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, hdrs, resp.read()


def _http_request_any(
    url: str,
    method: str = "GET",
    payload: dict | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    try:
        return _http_request(url, method=method, payload=payload, extra_headers=extra_headers)
    except urllib.error.HTTPError as exc:
        hdrs = {k.lower(): v for k, v in exc.headers.items()}
        return exc.code, hdrs, exc.read()


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
        assert "sessionStorage" in html


def test_health_and_cors_preflight(standalone_server):
    base_url, _, _ = standalone_server
    status, hdrs, raw = _http_request(f"{base_url}/v1/health")
    assert status == 200
    assert "access-control-allow-origin" not in hdrs
    payload = json.loads(raw.decode("utf-8"))
    assert payload["service"] == "filament-vision-server"
    assert payload["module_version"] == "0.2.3"
    assert payload["status"] == "ok"
    assert payload["tiers"]["cpu"]["usable"] is True

    opt_status, opt_hdrs, _ = _http_request(
        f"{base_url}/v1/read",
        method="OPTIONS",
        extra_headers={"Origin": "http://localhost:3000"},
    )
    assert opt_status == 204
    assert opt_hdrs.get("access-control-allow-origin") == "http://localhost:3000"
    assert opt_hdrs.get("vary") == "Origin"
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
    assert data["api_version"] == "1.1"
    assert data["reader"]["module_version"] == "0.2.3"

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


def test_installer_dependency_checks_present():
    root = Path(__file__).resolve().parent.parent
    sh_path = root / "install" / "install.sh"
    ps_path = root / "install" / "install.ps1"

    assert sh_path.is_file()
    assert ps_path.is_file()

    sh_bytes = sh_path.read_bytes()
    assert b"\r" not in sh_bytes, "install.sh must use LF line endings"
    sh_text = sh_bytes.decode("utf-8")
    for required in (
        "ensure_linux_system_deps",
        "bootstrap_python_with_uv",
        "ensure_python_runtime",
        "ensure_gpu_deps",
        "ensure_model_assets",
        "verify_python_deps",
        "prompt_manual_dependency",
    ):
        assert required in sh_text

    ps_text = ps_path.read_text(encoding="utf-8")
    for required in (
        "Ensure-VcRedist",
        "Ensure-PythonRuntime",
        "Ensure-GpuDependencies",
        "Ensure-ModelAssets",
        "Test-AndRepairPythonDependencies",
        "Prompt-ManualDependency",
    ):
        assert required in ps_text


def test_wsl_harness_script_present():
    root = Path(__file__).resolve().parent.parent
    wsl_script = root / "tests" / "wsl_test_installer.sh"
    wsl_bat = root / "tools" / "run_wsl_installer_test.bat"

    assert wsl_script.is_file()
    assert wsl_bat.is_file()

    raw = wsl_script.read_bytes()
    assert b"\r" not in raw, "tests/wsl_test_installer.sh must use LF line endings"
    text = raw.decode("utf-8")
    for token in ("--no-systemd", "--non-interactive", "/v1/health", "/v1/read", "/v1/revise", "--uninstall"):
        assert token in text


def test_platform_specific_bundle_builder():
    root = Path(__file__).resolve().parent.parent
    builder = root / "tools" / "build_standalone_bundle.py"
    assert builder.is_file()
    text = builder.read_text(encoding="utf-8")
    for token in ("_pack_zip", "_pack_tar_gz", '"windows"', '"linux"', "--platform", "--build-release"):
        assert token in text


def test_feedback_multi_batch_splitting(standalone_server, monkeypatch):
    import http.server
    import xml.etree.ElementTree as ET

    base_url, _, _ = standalone_server
    _http_request(
        f"{base_url}/v1/config",
        method="POST",
        payload={"api_version": "1.0", "feedback_opt_in": True},
    )

    jpg_b64 = base64.b64encode(_make_sample_spool_jpeg()).decode("ascii")
    entry_ids = []
    for idx in range(3):
        _, _, raw_enq = _http_request(
            f"{base_url}/v1/feedback",
            method="POST",
            payload={
                "api_version": "1.0",
                "images_b64": [jpg_b64],
                "predicted": {"vendor": "Unknown", "material": "PLA"},
                "corrected": {"vendor": "Hatchbox", "material": "PLA", "color_name": f"Orange {idx}"},
            },
        )
        entry_ids.append(json.loads(raw_enq.decode("utf-8"))["id"])

    # Local bundle download must work uncapped even when MAX_FEEDBACK_ZIP_BYTES is smaller than the full zip
    _, _, full_zip = _http_request(f"{base_url}/v1/feedback/bundle.zip")
    with zipfile.ZipFile(io.BytesIO(full_zip), "r") as zf:
        root_all = ET.fromstring(zf.read("pending_feedback.xml"))
        assert len(root_all.findall("entry")) == 3

    # Force 1-entry batches on upload: pick a limit strictly between 1 entry zip and full 3-entry zip
    monkeypatch.setattr(feedback_mod, "MAX_FEEDBACK_ZIP_BYTES", len(full_zip) // 2)
    _, _, capped_full_zip = _http_request(f"{base_url}/v1/feedback/bundle.zip")
    assert len(capped_full_zip) == len(full_zip)

    received_batches: list[dict] = []

    class MockUploadHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            received_batches.append(body)
            resp = json.dumps({"ok": True, "batch_received": len(received_batches)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, format, *args):
            pass

    mock_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockUploadHandler)
    mock_thread = threading.Thread(target=mock_srv.serve_forever, daemon=True)
    mock_thread.start()
    try:
        m_host, m_port = mock_srv.server_address[:2]
        st_sub, _, raw_sub = _http_request(
            f"{base_url}/v1/feedback/submit",
            method="POST",
            payload={"api_version": "1.0", "endpoint": f"http://{m_host}:{m_port}/exec"},
        )
        assert st_sub == 200
        sub_res = json.loads(raw_sub.decode("utf-8"))
        assert sub_res["ok"] is True
        assert sub_res["submitted_count"] == 3
        assert sub_res["batches_submitted"] == 3
        assert len(received_batches) == 3

        seen_ids = []
        for idx, b in enumerate(received_batches, start=1):
            assert f"part{idx}of3.zip" in b["filename"]
            zraw = base64.b64decode(b["zip_b64"])
            assert len(zraw) <= len(full_zip) // 2
            with zipfile.ZipFile(io.BytesIO(zraw), "r") as zf:
                sub_root = ET.fromstring(zf.read("pending_feedback.xml"))
                ents = sub_root.findall("entry")
                assert len(ents) == 1
                seen_ids.append(ents[0].attrib["id"])
        assert sorted(seen_ids) == sorted(entry_ids)

        _, _, raw_fb = _http_request(f"{base_url}/v1/feedback")
        assert json.loads(raw_fb.decode("utf-8"))["count"] == 0
    finally:
        mock_srv.shutdown()
        mock_srv.server_close()


def test_cors_origin_protection(standalone_server, monkeypatch):
    base_url, _, _ = standalone_server

    # Untrusted Origin must be rejected on OPTIONS, POST, DELETE
    st_opt, _, raw_opt = _http_request_any(
        f"{base_url}/v1/read",
        method="OPTIONS",
        extra_headers={"Origin": "https://evil.example"},
    )
    assert st_opt == 403
    assert json.loads(raw_opt.decode("utf-8"))["code"] == "forbidden_origin"

    st_post, _, raw_post = _http_request_any(
        f"{base_url}/v1/config",
        method="POST",
        payload={"api_version": "1.0", "tier": "cpu"},
        extra_headers={"Origin": "https://evil.example"},
    )
    assert st_post == 403
    assert json.loads(raw_post.decode("utf-8"))["code"] == "forbidden_origin"

    st_del, _, raw_del = _http_request_any(
        f"{base_url}/v1/feedback/nonexistent",
        method="DELETE",
        extra_headers={"Origin": "null"},
    )
    assert st_del == 403
    assert json.loads(raw_del.decode("utf-8"))["code"] == "forbidden_origin"

    # Explicit FILAMENT_VISION_SERVER_ALLOWED_ORIGINS opt-in works
    monkeypatch.setenv("FILAMENT_VISION_SERVER_ALLOWED_ORIGINS", "https://spoolman.lan:7912")
    st_ok, hdr_ok, _ = _http_request_any(
        f"{base_url}/v1/read",
        method="OPTIONS",
        extra_headers={"Origin": "https://spoolman.lan:7912"},
    )
    assert st_ok == 204
    assert hdr_ok.get("access-control-allow-origin") == "https://spoolman.lan:7912"


def test_bearer_token_auth_and_endpoint_gate(tmp_path, monkeypatch):
    cfg_path = tmp_path / "service_config.xml"
    fb_dir = tmp_path / "feedback_queue"
    monkeypatch.setattr(daemon_mod, "DEFAULT_SERVICE_CONFIG_XML", cfg_path)

    server = daemon_mod.start_daemon(
        host="127.0.0.1",
        port=0,
        tier="cpu",
        auth_token="super-secret-token",
        feedback_opt_in=True,
        feedback_dir=fb_dir,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}"
    try:
        # Health stays unauthenticated
        st_h, _, raw_h = _http_request_any(f"{base_url}/v1/health")
        assert st_h == 200
        assert json.loads(raw_h.decode("utf-8"))["auth_required"] is True

        # Protected routes fail 401 without or with wrong bearer token
        st_cfg, _, _ = _http_request_any(f"{base_url}/v1/config")
        assert st_cfg == 401
        st_bad, _, _ = _http_request_any(
            f"{base_url}/v1/config",
            extra_headers={"Authorization": "Bearer wrong-token"},
        )
        assert st_bad == 401

        st_ok, _, raw_ok = _http_request_any(
            f"{base_url}/v1/config",
            extra_headers={"Authorization": "Bearer super-secret-token"},
        )
        assert st_ok == 200
        assert json.loads(raw_ok.decode("utf-8"))["tier"] == "cpu"
    finally:
        server.shutdown()
        server.server_close()


def test_unauthenticated_custom_feedback_endpoint_blocked(standalone_server):
    base_url, _, _ = standalone_server
    _http_request(
        f"{base_url}/v1/config",
        method="POST",
        payload={"api_version": "1.0", "feedback_opt_in": True},
    )
    st, _, raw = _http_request_any(
        f"{base_url}/v1/feedback/submit",
        method="POST",
        payload={"api_version": "1.0", "endpoint": "https://attacker.example/exfil"},
    )
    assert st == 403
    assert json.loads(raw.decode("utf-8"))["code"] == "forbidden_endpoint"


def test_read_data_uri_bad_image_and_bad_content_length(standalone_server):
    import http.client
    from urllib.parse import urlparse

    base_url, _, _ = standalone_server
    jpg_b64 = base64.b64encode(_make_sample_spool_jpeg()).decode("ascii")

    # data-URI prefix must decode cleanly
    st_ok, _, _ = _http_request_any(
        f"{base_url}/v1/read",
        method="POST",
        payload={
            "api_version": "1.0",
            "tier": "cpu",
            "images_b64": [f"data:image/jpeg;base64,{jpg_b64}"],
        },
    )
    assert st_ok == 200

    # Strict base64 validation returns 415 bad_image
    st_b64, _, raw_b64 = _http_request_any(
        f"{base_url}/v1/read",
        method="POST",
        payload={"api_version": "1.0", "tier": "cpu", "images_b64": ["!!!not-base64!!!"]},
    )
    assert st_b64 == 415
    assert json.loads(raw_b64.decode("utf-8"))["code"] == "bad_image"

    # Non-image bytes return 415 bad_image
    not_img = base64.b64encode(b"definitely not a real image file").decode("ascii")
    st_corrupt, _, raw_corrupt = _http_request_any(
        f"{base_url}/v1/read",
        method="POST",
        payload={"api_version": "1.0", "tier": "cpu", "images_b64": [not_img]},
    )
    assert st_corrupt == 415
    assert json.loads(raw_corrupt.decode("utf-8"))["code"] == "bad_image"

    # Invalid / negative Content-Length returns 400 instead of 500 ValueError
    parsed = urlparse(base_url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        conn.putrequest("POST", "/v1/read")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "not-a-number")
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 400
    finally:
        conn.close()


def test_revise_null_fields_pla_plus_color_and_spoolman(standalone_server):
    base_url, _, _ = standalone_server

    # None numeric fields + vendor switch + PLA+ + note color + Spoolman matching
    st1, _, raw1 = _http_request_any(
        f"{base_url}/v1/revise",
        method="POST",
        payload={
            "api_version": "1.0",
            "previous": {
                "vendor": "Bambu Lab",
                "material": "PLA",
                "line": "PLA Basic",
                "color_name": "Jade White",
                "net": None,
                "tare": None,
                "density": None,
                "count": None,
                "nozzle_range": "190-220",
            },
            "note": "SUNLU PLA+ Red",
            "filaments": [
                {
                    "id": 77,
                    "name": "PLA+ Red",
                    "vendor": {"name": "SUNLU"},
                    "spool_weight": 185,
                    "density": 1.25,
                    "color_hex": "#EE1122",
                }
            ],
        },
    )
    assert st1 == 200
    item1 = json.loads(raw1.decode("utf-8"))["item"]
    assert item1["vendor"] == "SUNLU"
    assert "PLA+" in item1["line"]
    assert item1["color_name"] == "Red"
    assert item1["existing_filament_id"] == 77
    assert item1["tare"] == 185

    # Bambu Lab PLA Basic Bambu Green matches catalog product color & hex
    st2, _, raw2 = _http_request_any(
        f"{base_url}/v1/revise",
        method="POST",
        payload={
            "api_version": "1.0",
            "previous": {"vendor": "Unknown", "material": "PLA", "line": "PLA"},
            "note": "Bambu Lab PLA Basic Bambu Green",
        },
    )
    assert st2 == 200
    item2 = json.loads(raw2.decode("utf-8"))["item"]
    assert item2["vendor"] == "Bambu Lab"
    assert item2["line"] == "PLA Basic"
    assert item2["color_name"] == "Bambu Green"
    assert item2["color_hex"] == "00AE42"


def test_feedback_fail_closed_delete_many_and_ingest_empty_xml(tmp_path):
    import http.server
    import xml.etree.ElementTree as ET

    fb_dir = tmp_path / "fb"
    q = feedback_mod.FeedbackQueue(queue_dir=fb_dir)
    jpg_b64 = base64.b64encode(_make_sample_spool_jpeg()).decode("ascii")

    e1 = q.enqueue([jpg_b64], {"vendor": "Unknown"}, {"vendor": "Hatchbox", "material": "PLA"})
    q.enqueue([jpg_b64], {"vendor": "Unknown"}, {"vendor": "Prusament", "material": "PETG"})
    assert q.status()["count"] == 2

    # delete_many removes only specified entries
    assert q.delete_many([e1["id"]]) == 1
    assert q.status()["count"] == 1

    # Mock fail-closed endpoints (HTML 200 response + ok:false response)
    mode = {"html": True}

    class BadUploadHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            self.rfile.read(length)
            if mode["html"]:
                body = b"<html><body>Sign in</body></html>"
                ctype = "text/html"
            else:
                body = json.dumps({"ok": False, "error": "drive_quota_exceeded"}).encode("utf-8")
                ctype = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BadUploadHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        h, p = srv.server_address[:2]
        with pytest.raises(RuntimeError, match="non-JSON"):
            q.submit(f"http://{h}:{p}/exec")
        assert q.status()["count"] == 1

        mode["html"] = False
        with pytest.raises(RuntimeError, match="drive_quota_exceeded"):
            q.submit(f"http://{h}:{p}/exec")
        assert q.status()["count"] == 1
    finally:
        srv.shutdown()
        srv.server_close()

    # Craft a ZIP bundle with empty <net></net> XML elements and ingest cleanly
    bundle_bytes = q.build_bundle_zip()
    patched_zip = tmp_path / "patched_feedback.zip"
    with zipfile.ZipFile(io.BytesIO(bundle_bytes), "r") as zin:
        xml_root = ET.fromstring(zin.read("pending_feedback.xml"))
        corr = xml_root.find("./entry/corrected")
        for empty_tag in ("net", "tare", "density", "nozzle"):
            el = corr.find(empty_tag)
            if el is None:
                el = ET.SubElement(corr, empty_tag)
            el.text = ""
        with zipfile.ZipFile(patched_zip, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == "pending_feedback.xml":
                    zout.writestr("pending_feedback.xml", ET.tostring(xml_root, encoding="utf-8"))
                else:
                    zout.writestr(item.filename, zin.read(item.filename))

    dest_xml = tmp_path / "data" / "annotations" / "verified.xml"
    dest_imgs = tmp_path / "data" / "feedback_verified"
    ingested = feedback_mod.ingest_feedback_bundles(patched_zip, dest_xml=dest_xml, dest_images_dir=dest_imgs)
    assert ingested["imported_entries"] == 1
    assert ingested["imported_images"] == 1
    assert "feedback_verified/" in dest_xml.read_text(encoding="utf-8")


def test_catalog_matcher_unknown_color_keeps_visual_hex():
    from filament_vision_server.catalog.matcher import CatalogMatcher

    matcher = CatalogMatcher()
    cname, chex, cfam, hsrc = matcher.resolve_color(
        "Weird Custom Tint",
        product=None,
        visual_hex="12AB34",
        visual_family="Green",
    )
    assert cname == "Weird Custom Tint"
    assert chex == "12AB34"
    assert cfam == "Green"
    assert hsrc == "estimate"


def _make_oversized_png_bytes(width: int = 12000, height: int = 12000) -> bytes:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    comp = zlib.compressobj(level=6)
    row = b"\x00" + (b"\x00" * width)
    idat_parts = [comp.compress(row) for _ in range(height)]
    idat_parts.append(comp.flush())
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", b"".join(idat_parts)) + chunk(b"IEND", b"")


def test_config_endpoint_ssrf_and_unauthenticated_override_blocked(standalone_server):
    base_url, _, _ = standalone_server
    st, _, raw = _http_request_any(
        f"{base_url}/v1/config",
        method="POST",
        payload={
            "api_version": "1.1",
            "feedback_opt_in": True,
            "feedback_endpoint": "https://attacker.example/steal",
        },
    )
    assert st == 403
    assert json.loads(raw.decode("utf-8"))["code"] == "forbidden_endpoint"

    for bad_url in (
        "https://169.254.169.254/latest/meta-data",
        "https://192.168.1.10/hook",
        "http://attacker.example/plain",
    ):
        with pytest.raises(ValueError):
            feedback_mod._validate_upload_url(bad_url)


def test_decompression_bomb_and_zip_ingest_limits(standalone_server, tmp_path, monkeypatch):
    from filament_vision_server.cpu_tier.pipeline import CPUHybridPipeline

    base_url, _, _ = standalone_server
    bomb_png = _make_oversized_png_bytes(12000, 12000)
    with pytest.raises(ValueError, match="exceed"):
        CPUHybridPipeline._load_bgr(bomb_png)

    bomb_b64 = base64.b64encode(bomb_png).decode("ascii")
    st, _, raw = _http_request_any(
        f"{base_url}/v1/read",
        method="POST",
        payload={"api_version": "1.1", "tier": "cpu", "images_b64": [bomb_b64]},
    )
    assert st == 413
    assert json.loads(raw.decode("utf-8"))["code"] == "too_large"

    zip_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pending_feedback.xml", "<feedback-queue>" + ("x" * 2048) + "</feedback-queue>")

    monkeypatch.setattr(feedback_mod, "MAX_INGEST_XML_BYTES", 128)
    with pytest.raises(ValueError, match="exceeds"):
        feedback_mod.ingest_feedback_bundles(
            zip_path,
            dest_xml=tmp_path / "out.xml",
            dest_images_dir=tmp_path / "imgs",
        )


def test_unlabeled_color_preserves_estimate_and_windings_refinement(monkeypatch):
    import numpy as np
    from filament_vision_server.catalog.matcher import CatalogMatcher
    from filament_vision_server.cpu_tier import pipeline as cpu_pipe_mod
    from filament_vision_server.data_engine.auto_labeler import parse_ocr_heuristic
    from filament_vision_server.xml_io import BoundingBox

    blank = np.zeros((200, 200, 3), dtype=np.uint8)
    item, _ = parse_ocr_heuristic([], blank, CatalogMatcher(), is_spool=True)
    assert item.color_name == "unknown"
    assert item.color_hex_source == "estimate"

    # Dark frame with a vivid blue swatch inside spool_windings (BGR: 230, 60, 20)
    img = np.full((240, 240, 3), 25, dtype=np.uint8)
    img[60:180, 60:180] = (230, 60, 20)
    pipe = cpu_pipe_mod.CPUHybridPipeline()
    monkeypatch.setattr(
        pipe,
        "_detect_regions",
        lambda _im: [
            BoundingBox(label="spool_windings", x1=0.25, y1=0.25, x2=0.75, y2=0.75, confidence=0.95),
            BoundingBox(label="spool_flange", x1=0.05, y1=0.05, x2=0.95, y2=0.95, confidence=0.90),
        ],
    )
    monkeypatch.setattr(cpu_pipe_mod, "extract_multi_angle_ocr_and_boxes", lambda _im, _o: ([], []))
    monkeypatch.setattr(pipe, "_run_visual_onnx", lambda _im: {})

    refined = pipe.predict_image(img)
    assert refined.color_hex_source == "estimate"
    assert refined.color_family == "Blue"


def test_visual_vendor_confidence_and_gpu_available_in_cpu_tier(monkeypatch):
    import sys
    import types
    import numpy as np
    from filament_vision_server.cpu_tier import pipeline as cpu_pipe_mod

    pipe = cpu_pipe_mod.CPUHybridPipeline()
    img = np.zeros((200, 200, 3), dtype=np.uint8)
    monkeypatch.setattr(pipe, "_detect_regions", lambda _im: [])
    monkeypatch.setattr(cpu_pipe_mod, "extract_multi_angle_ocr_and_boxes", lambda _im, _o: ([], []))
    monkeypatch.setattr(
        pipe,
        "_run_visual_onnx",
        lambda _im: {
            "visual_vendor": ("Prusament", 0.50),
            "color_family": ("Orange", 0.80),
            "visual_finish": ("Basic", 0.80),
            "sealed_state": ("bare_spool", 0.80),
        },
    )

    it = pipe.predict_image(img)
    assert it.vendor == "Prusament"
    assert it.confidence < 0.78

    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: True)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    rec = daemon_mod.FilamentRecognizer(default_tier="cpu")
    assert rec.gpu_available() is True


def test_catalog_matcher_generic_material_product_line_ranking():
    from filament_vision_server.catalog.matcher import CatalogMatcher

    matcher = CatalogMatcher()
    bambu = matcher.match_product_line("Bambu Lab", "PLA", "PLA")
    polymaker = matcher.match_product_line("Polymaker", "PLA", "PLA")
    overture = matcher.match_product_line("Overture", "PLA", "PLA")

    assert bambu is not None and bambu.line == "PLA Basic"
    assert polymaker is not None and polymaker.line == "PolyLite PLA"
    assert overture is not None and overture.line == "PLA"


def test_ipv6_bind_chunked_encoding_and_api_contract_doc(standalone_server):
    import http.client
    from urllib.parse import urlparse

    base_url, _, _ = standalone_server
    parsed = urlparse(base_url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        conn.putrequest("POST", "/v1/read")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 411
        assert body["code"] == "length_required"
    finally:
        conn.close()

    srv6 = daemon_mod.start_daemon(host="::", port=0, tier="cpu")
    try:
        assert srv6.server_address[1] > 0
    finally:
        srv6.server_close()

    doc_path = Path(__file__).resolve().parent.parent / "docs" / "api-contract.md"
    assert doc_path.is_file()
    assert "api_version" in doc_path.read_text(encoding="utf-8")

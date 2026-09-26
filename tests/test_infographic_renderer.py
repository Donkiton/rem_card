from __future__ import annotations

import os
from pathlib import Path

import pytest

from rem_card.services.analytics import infographic_renderer


def test_render_infographic_returns_worker_png_and_removes_input(monkeypatch, tmp_path):
    seen = {}

    def fake_reserve(prefix, suffix):
        return str(tmp_path / f"{prefix}{suffix}")

    def fake_worker(input_path, output_path):
        from PIL import Image

        seen["input"] = input_path
        payload = Path(input_path).read_text(encoding="utf-8")
        assert "Длительность лечения" in payload
        image = Image.new("RGB", (2, 1), "white")
        image.putpixel((1, 0), (18, 52, 86))
        image.save(output_path, format="PNG")
        return True

    monkeypatch.setattr(infographic_renderer, "_reserve_temp_path", fake_reserve)
    monkeypatch.setattr(infographic_renderer, "_invoke_worker", fake_worker)

    result = infographic_renderer.render_infographic(
        [{"label": "Длительность лечения", "value": 4.5, "unit": "сут."}],
        title="Ключевые показатели",
        colors=["#123456"],
    )

    assert result == str((tmp_path / "graph_.png").resolve())
    assert not Path(seen["input"]).exists()
    assert Path(result).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_render_infographic_fails_closed_and_removes_partial_output(monkeypatch, tmp_path):
    output_path = tmp_path / "graph_failed.png"
    monkeypatch.setattr(
        infographic_renderer, "_reserve_temp_path", lambda *_: str(output_path)
    )

    def failed_worker(_input_path, output_path):
        Path(output_path).write_bytes(b"partial")
        return False

    monkeypatch.setattr(infographic_renderer, "_invoke_worker", failed_worker)

    assert infographic_renderer.render_infographic(
        [{"label": "Исход", "value": 3}], title="Исходы", colors=[]
    ) is None
    assert not output_path.exists()


@pytest.mark.parametrize(
    "items",
    (
        [],
        [{"label": "A", "value": 1}] * 7,
        [{"label": "A", "value": float("nan")}],
        [{"label": "A", "value": 10**10_000}],
        [{"label": "", "value": 1}],
    ),
)
def test_render_infographic_rejects_invalid_payload_without_worker(monkeypatch, items):
    monkeypatch.setattr(
        infographic_renderer,
        "_invoke_worker",
        lambda *_: pytest.fail("worker must not start"),
    )
    assert infographic_renderer.render_infographic(
        items, title="Показатели", colors=["#1783FF"]
    ) is None


def test_worker_json_escapes_script_termination():
    from rem_card.services.analytics.infographic_worker import _script_json

    encoded = _script_json(
        {"title": "</script><script>alert(1)</script>", "label": "Русский текст"}
    )
    assert "</script>" not in encoded
    assert "\\u003c/script\\u003e" in encoded
    assert "Русский текст" in encoded


def test_failed_worker_removes_even_complete_png_and_job_files(monkeypatch, tmp_path):
    from PIL import Image

    target = tmp_path / "graph_failed.png"
    seen = {}
    monkeypatch.setattr(infographic_renderer, "_reserve_temp_path", lambda *_: str(target))

    def fail_after_export(request, output):
        job = Path(request).parent
        seen["job"] = job
        (job / "page.html").write_text("temporary aggregate data", encoding="utf-8")
        picture = Image.new("RGB", (2, 1), "white")
        picture.putpixel((1, 0), (0, 0, 0))
        picture.save(output)
        return False

    monkeypatch.setattr(infographic_renderer, "_invoke_worker", fail_after_export)
    assert infographic_renderer.render_infographic([{"label": "Исход", "value": 3}], title="Исходы", colors=[]) is None
    assert not target.exists()
    assert not seen["job"].exists()


def test_temp_storage_failure_uses_fallback(monkeypatch):
    def unavailable(*_args):
        raise OSError("test filesystem error")

    monkeypatch.setattr(infographic_renderer, "_reserve_temp_path", unavailable)
    assert infographic_renderer.render_infographic([{"label": "Исход", "value": 3}], title="Исходы", colors=[]) is None


def test_worker_timeout_terminates_its_process_tree(monkeypatch):
    import subprocess

    class Process:
        finished = False

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("renderer", timeout)

        def poll(self):
            return 1 if self.finished else None

    process = Process()
    terminated = []
    def terminate(target):
        terminated.append(target)
        target.finished = True

    monkeypatch.setattr(infographic_renderer.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(infographic_renderer, "_terminate_process_tree", terminate)
    assert not infographic_renderer._invoke_worker("input.json", "output.png")
    assert terminated == [process]


def test_frozen_helper_relaunches_same_executable_without_normal_startup(monkeypatch):
    monkeypatch.setattr(infographic_renderer.sys, "frozen", True, raising=False)
    monkeypatch.setattr(infographic_renderer.sys, "executable", "C:/Bundle/RemCard.exe")
    assert infographic_renderer._worker_command("input.json", "output.png") == [
        "C:/Bundle/RemCard.exe", "--remcard-infographic-worker", "input.json", "output.png",
    ]


def test_vendored_bundle_matches_pinned_version():
    import hashlib
    from rem_card.services.analytics.infographic_worker import _ASSET_PATH

    assert hashlib.sha256(_ASSET_PATH.read_bytes()).hexdigest() == "2890e658d6018b9ab385b4da9775b917a99e113ea333c59f03e538290e302b13"
    assert (_ASSET_PATH.parent / "LICENSE").is_file()


@pytest.mark.skipif(
    os.environ.get("REMCARD_RUN_ANTV_SMOKE") != "1",
    reason="real QtWebEngine smoke is opt-in",
)
def test_real_offline_antv_renderer_smoke():
    from PIL import Image

    output = infographic_renderer.render_infographic(
        [
            {
                "label": "Очень длинное кириллическое название показателя",
                "value": 17,
                "unit": "случаев",
                "caption": "34% от общего числа",
            },
            {"label": "Благоприятный исход", "value": 33, "unit": "случая"},
            {"label": "Переведены", "value": 9, "unit": "случаев"},
        ],
        title=(
            "Результаты лечения пациентов отделения за выбранный отчётный период "
            "с проверкой переноса длинного заголовка"
        ),
        colors=["#1783FF", "#00C9C9", "#F0884D"],
    )
    assert output is not None
    try:
        with Image.open(output) as image:
            assert image.format == "PNG"
            assert image.width >= 1_500
            assert image.height >= 300
            assert image.getbbox() is not None
    finally:
        if output:
            Path(output).unlink(missing_ok=True)

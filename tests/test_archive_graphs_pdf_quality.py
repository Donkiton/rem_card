from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from PySide6.QtWidgets import QApplication
from pypdf import PdfReader

from rem_card.services.analytics.graphs_service import GraphsBuildResult, build_graphs_pdf
from rem_card.ui.archive_center import graphs_page as graphs_module
from rem_card.ui.archive_center.graphs_page import ArchiveGraphsPage


def test_graph_preview_uses_scaled_copy_but_pdf_embeds_original_png(monkeypatch, tmp_path):
    """The UI preview must not replace the image source later handed to PDF."""
    app = QApplication.instance() or QApplication([])
    original_path = Path(tempfile.gettempdir()) / f"graph_pdf_source_{uuid.uuid4().hex}.png"
    Image.new("RGB", (1600, 800), (32, 48, 64)).save(original_path)
    original_html = f"<html><body><img src='{original_path}' width='860'></body></html>"
    rendered = GraphsBuildResult(html=original_html, image_paths=[str(original_path)])
    manager = object()

    monkeypatch.setattr(graphs_module, "get_analytics_base_manager", lambda **_kwargs: manager)
    monkeypatch.setattr(graphs_module, "resolve_readonly_analytics_manager", lambda *_args, **_kwargs: (manager, None))
    monkeypatch.setattr(
        graphs_module,
        "StatisticsRepository",
        lambda *_args, **_kwargs: SimpleNamespace(clinical_fingerprints=lambda: ()),
    )
    monkeypatch.setattr(graphs_module, "build_graphs_snapshot", lambda *_args, **_kwargs: SimpleNamespace(results={}))
    monkeypatch.setattr(graphs_module, "analytics_context_html", lambda **_kwargs: "")
    monkeypatch.setattr(graphs_module, "build_graphs_html", lambda *_args, **_kwargs: rendered)

    page = ArchiveGraphsPage()
    try:
        result = page._build_graphs("2026-01-01", "2026-01-02", ["g1"], False, preview_width=400)

        assert result.html == original_html
        assert result.preview_html is not None and str(original_path) not in result.preview_html
        preview_path = next(path for path in result.image_paths if path != str(original_path))
        with Image.open(preview_path) as preview_image:
            assert preview_image.size == (400, 200)

        page._graphs_ready(page._request_token, result, False)
        assert page._latest_html == original_html

        pdf_path = tmp_path / "graphs.pdf"
        build_graphs_pdf(page._latest_html, pdf_path)
        embedded_images = [image.image.size for image in PdfReader(str(pdf_path)).pages[0].images]
        assert embedded_images == [(1600, 800)]

        page.shutdown()
        assert not original_path.exists()
        assert not Path(preview_path).exists()
    finally:
        page.close()
        app.processEvents()
        original_path.unlink(missing_ok=True)


def test_stale_graph_result_removes_original_and_preview_pngs(tmp_path):
    app = QApplication.instance() or QApplication([])
    original = tmp_path / "graph_source.png"
    preview = tmp_path / "graph_preview_400.png"
    original.write_bytes(b"original")
    preview.write_bytes(b"preview")
    result = GraphsBuildResult(
        html=f"<img src='{original}'>",
        image_paths=[str(original), str(preview)],
        preview_html=f"<img src='{preview}'>",
    )
    page = ArchiveGraphsPage()
    try:
        page.shutdown()
        page._graphs_ready(page._request_token - 1, result, False)
        assert not original.exists()
        assert not preview.exists()
    finally:
        page.close()
        app.processEvents()

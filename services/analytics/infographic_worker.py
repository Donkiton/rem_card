"""QtWebEngine worker for the offline AntV renderer."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Sequence


_INTERNAL_TIMEOUT_MS = 20_000
_MAX_INPUT_BYTES = 128 * 1024
_MAX_PNG_BYTES = 32 * 1024 * 1024
_ASSET_PATH = Path(__file__).resolve().parent / "assets" / "antv" / "infographic.umd.min.js"


def main(arguments: Sequence[str] | None = None) -> int:
    args = list(arguments or ())
    if len(args) != 2:
        return 2
    input_path, output_path = map(Path, args)
    try:
        if input_path.stat().st_size > _MAX_INPUT_BYTES:
            return 2
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        if not _ASSET_PATH.is_file():
            return 3
        return _render(payload, output_path, input_path.parent)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 1


def _render(payload: object, output_path: Path, work_directory: Path) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

    try:
        from PySide6.QtCore import QTimer, QUrl
        from PySide6.QtWebEngineCore import (
            QWebEnginePage,
            QWebEngineProfile,
            QWebEngineSettings,
            QWebEngineUrlRequestInterceptor,
        )
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return 4

    class OfflineInterceptor(QWebEngineUrlRequestInterceptor):
        def __init__(self, allowed_files: set[str], parent=None):
            super().__init__(parent)
            self._allowed_files = allowed_files

        def interceptRequest(self, info):  # noqa: N802 - Qt virtual method
            url = info.requestUrl()
            scheme = url.scheme().lower()
            if scheme == "file":
                local_path = os.path.normcase(os.path.abspath(url.toLocalFile()))
                info.block(local_path not in self._allowed_files)
                return
            info.block(scheme not in {"about", "data", "blob"})

    class QuietPage(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line_number, source_id):  # noqa: N802
            return None

    html_path = _write_html(payload, work_directory)
    if html_path is None:
        return 2
    allowed_files = {
        os.path.normcase(os.path.abspath(str(html_path))),
        os.path.normcase(os.path.abspath(str(_ASSET_PATH))),
    }
    app = QApplication.instance() or QApplication(["RemCard infographic renderer"])
    profile = QWebEngineProfile(app)
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
    )
    interceptor = OfflineInterceptor(allowed_files, profile)
    profile.setUrlRequestInterceptor(interceptor)
    settings = profile.settings()
    settings.setAttribute(
        QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False
    )
    settings.setAttribute(
        QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False
    )
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)

    page = QuietPage(profile, app)
    result = {"exit_code": 1, "finished": False}

    def finish(exit_code: int) -> None:
        if result["finished"]:
            return
        result["finished"] = True
        result["exit_code"] = exit_code
        app.quit()

    def receive_status(status) -> None:
        if result["finished"]:
            return
        if isinstance(status, str):
            try:
                status = json.loads(status)
            except (TypeError, ValueError):
                status = None
        if not isinstance(status, dict) or status.get("status") == "pending":
            QTimer.singleShot(50, poll)
            return
        if status.get("status") != "ok":
            finish(1)
            return
        try:
            encoded = str(status.get("data") or "")
            prefix = "data:image/png;base64,"
            if not encoded.startswith(prefix):
                finish(1)
                return
            png = base64.b64decode(encoded[len(prefix):], validate=True)
            if not 8 <= len(png) <= _MAX_PNG_BYTES or not png.startswith(
                b"\x89PNG\r\n\x1a\n"
            ):
                finish(1)
                return
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temp_output = work_directory / "output.png.part"
            temp_output.write_bytes(png)
            os.replace(temp_output, output_path)
        except (OSError, ValueError, TypeError):
            finish(1)
            return
        finish(0)

    def poll() -> None:
        page.runJavaScript(
            "JSON.stringify(window.__remcardResult || {status:'pending'})",
            0,
            receive_status,
        )

    def loaded(ok: bool) -> None:
        if not ok:
            finish(1)
            return
        poll()

    page.loadFinished.connect(loaded)
    QTimer.singleShot(_INTERNAL_TIMEOUT_MS, lambda: finish(1))
    page.load(QUrl.fromLocalFile(str(html_path)))
    app.exec()
    page.deleteLater()
    profile.deleteLater()
    try:
        html_path.unlink()
    except OSError:
        pass
    return int(result["exit_code"])


def _write_html(payload: object, work_directory: Path) -> Path | None:
    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    colors = payload.get("colors")
    title = payload.get("title")
    if not isinstance(items, list) or not 1 <= len(items) <= 6:
        return None
    if not isinstance(colors, list) or not colors or not isinstance(title, str):
        return None
    safe_json = _script_json(payload)
    asset_url = _ASSET_PATH.as_uri()
    columns = min(2, len(items))
    html = rf"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<style>html,body{{margin:0;background:#fff}}#chart{{display:inline-block}}</style>
<script src="{asset_url}"></script></head><body><div id="chart"></div>
<script>
'use strict';
window.__remcardResult = {{status: 'pending'}};
const payload = {safe_json};
const fail = () => {{ window.__remcardResult = {{status: 'error'}}; }};
window.addEventListener('error', fail);
window.addEventListener('unhandledrejection', fail);
try {{
  const infographic = new AntVInfographic.Infographic({{
    container: '#chart',
    padding: 28,
    editable: false,
    plugins: [],
    interactions: [],
    design: {{
      title: 'default',
      structure: {{type: 'list-grid', columns: {columns}, gap: 28}},
      items: [{{type: 'badge-card', width: 480, height: 180, gap: 12}}]
    }},
    data: {{items: payload.items}},
    theme: 'light',
    themeConfig: {{
      colorBg: '#ffffff',
      colorPrimary: payload.colors[0],
      palette: payload.colors,
      base: {{text: {{'font-family': 'Arial'}}}},
      title: {{'font-family': 'Arial', 'font-weight': 700}},
      item: {{
        label: {{'font-family': 'Arial', 'font-size': 20}},
        value: {{'font-family': 'Arial'}},
        desc: {{'font-family': 'Arial'}}
      }}
    }},
    svg: {{background: true}}
  }});
  infographic.on('error', fail);
  infographic.on('rendered', () => {{
    setTimeout(async () => {{
      try {{
        if (document.fonts && document.fonts.ready) await document.fonts.ready;
        const svg = document.querySelector('#chart svg');
        if (!svg) throw new Error('svg');
        const ns = 'http://www.w3.org/2000/svg';
        const box = svg.viewBox.baseVal;
        const titleLines = payload.title.split('\n').filter(Boolean);
        const titleArea = titleLines.length ? titleLines.length * 34 + 22 : 0;
        const newY = box.y - titleArea;
        const background = document.createElementNS(ns, 'rect');
        background.setAttribute('x', String(box.x));
        background.setAttribute('y', String(newY));
        background.setAttribute('width', String(box.width));
        background.setAttribute('height', String(box.height + titleArea));
        background.setAttribute('fill', '#ffffff');
        svg.insertBefore(background, svg.firstChild);
        const titleGroup = document.createElementNS(ns, 'g');
        titleGroup.setAttribute('aria-label', payload.title.replace(/\n/g, ' '));
        titleLines.forEach((line, index) => {{
          const text = document.createElementNS(ns, 'text');
          text.setAttribute('x', String(box.x + box.width / 2));
          text.setAttribute('y', String(newY + 30 + index * 34));
          text.setAttribute('text-anchor', 'middle');
          text.setAttribute('font-family', 'Arial');
          text.setAttribute('font-size', index === 0 ? '24' : '20');
          text.setAttribute('font-weight', index === 0 ? '700' : '500');
          text.setAttribute('fill', index === 0 ? '#252525' : '#595959');
          text.textContent = line;
          titleGroup.appendChild(text);
        }});
        svg.appendChild(titleGroup);
        svg.setAttribute(
          'viewBox',
          [box.x, newY, box.width, box.height + titleArea].join(' ')
        );
        const valueHolders = Array.from(
          svg.querySelectorAll('[data-element-type="item-value"]')
        );
        const cards = Array.from(svg.querySelectorAll('rect')).filter((rect) =>
          Number(rect.getAttribute('width')) === 480 &&
          Number(rect.getAttribute('height')) === 180
        );
        if (cards.length !== payload.items.length || valueHolders.length !== payload.items.length) {{
          throw new Error('cards');
        }}
        const rootMatrix = svg.getCTM();
        if (!rootMatrix) throw new Error('matrix');
        const positions = cards.map((card) => {{
          const matrix = card.getCTM();
          if (!matrix) throw new Error('matrix');
          const relativeMatrix = rootMatrix.inverse().multiply(matrix);
          const point = svg.createSVGPoint();
          point.x = 240;
          point.y = 112;
          return point.matrixTransform(relativeMatrix);
        }});
        valueHolders.forEach((holder) => holder.setAttribute('visibility', 'hidden'));
        svg.querySelectorAll('[data-element-type="item-desc"]').forEach(
          (holder) => holder.setAttribute('visibility', 'hidden')
        );
        positions.forEach((position, index) => {{
          const value = document.createElementNS(ns, 'text');
          value.setAttribute('x', String(position.x));
          value.setAttribute('y', String(position.y));
          value.setAttribute('text-anchor', 'middle');
          value.setAttribute('dominant-baseline', 'middle');
          value.setAttribute('font-family', 'Arial');
          value.setAttribute('font-size', '32');
          value.setAttribute('font-weight', '700');
          value.setAttribute('fill', payload.colors[index % payload.colors.length]);
          value.textContent = String(payload.items[index].value);
          svg.appendChild(value);
          String(payload.items[index].desc || '').split('\n').filter(Boolean).forEach(
            (line, lineIndex) => {{
              const description = document.createElementNS(ns, 'text');
              description.setAttribute('x', String(position.x));
              description.setAttribute('y', String(position.y + 36 + lineIndex * 20));
              description.setAttribute('text-anchor', 'middle');
              description.setAttribute('font-family', 'Arial');
              description.setAttribute('font-size', '18');
              description.setAttribute('fill', '#595959');
              description.textContent = line;
              svg.appendChild(description);
            }}
          );
        }});
        const normalizeText = (value) => String(value || '').replace(/\s+/g, '');
        const visibleText = normalizeText(svg.textContent || '');
        const expected = [payload.title, ...payload.items.map((item) => item.label)];
        if (!expected.every((text) => visibleText.includes(normalizeText(text)))) throw new Error('text');
        const width = svg.viewBox && svg.viewBox.baseVal ? svg.viewBox.baseVal.width : 0;
        if (!(width > 0)) throw new Error('size');
        const dpr = Math.min(6, Math.max(2, 2200 / width));
        const data = await infographic.toDataURL({{type: 'png', dpr}});
        window.__remcardResult = {{status: 'ok', data}};
      }} catch (error) {{ fail(); }}
    }}, 0);
  }});
  infographic.render();
}} catch (error) {{ fail(); }}
</script></body></html>"""
    descriptor, path = tempfile.mkstemp(
        prefix="page_", suffix=".html", dir=str(work_directory)
    )
    os.close(descriptor)
    html_path = Path(path)
    try:
        html_path.write_text(html, encoding="utf-8")
    except OSError:
        try:
            html_path.unlink()
        except OSError:
            pass
        return None
    return html_path


def _script_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


__all__ = ["main"]

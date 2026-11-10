"""Servidor web local (biblioteca estándar) de ExaCell Sizer.

Por defecto escucha solo en 127.0.0.1. No contacta ningún Exadata: trabaja con las capturas
que el usuario sube y con datos simulados de demostración.
"""
from __future__ import annotations

import html
import json
import mimetypes
import re
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from .. import collector as coll
from ..sizing import ScenarioError, load_scenario
from .workspace import Jobs, Workspace

STATIC = Path(__file__).resolve().parent / "static"
DOCS = {
    "guia": Path(__file__).resolve().parents[1] / "resources" / "GUIA_INTERPRETACION.md",
}
REPO_DOCS = Path(__file__).resolve().parents[3] / "docs"
EXAMPLE_SCENARIO = Path(__file__).resolve().parents[3] / "examples" / "escenario_ejemplo.json"
MAX_UPLOAD = 8 * 1024 ** 3  # 8 GB

DEFAULT_SCENARIO = {
    "scenario_name": "Nuevo escenario", "statistic": "max", "combination": "envelope", "align": "utc",
    "growth_pct": 20, "reserve_pct": 20, "latency_target_us": None,
    "n_minus_1": {"enabled": True, "recovery_overhead_pct": 15, "reserve_pct_during_failure": 0},
    "min_coverage_pct": 95, "min_capture_hours": 24, "critical_periods_confirmed": False,
    "approximations": [], "source_system": {"model": "", "storage_type": ""},
    "candidates": [{"id": "candidato-1", "model": "X10M", "storage_type": "HC"}], "tested_capacities": [],
}


# ----------------------------------------------------------------------------- Markdown mínimo

def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*]+)\*(?!\w)", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<span class="doclink">\1</span>', s)
    return s


def markdown_to_html(md: str) -> str:
    out, lines, i = [], md.splitlines(), 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            j = i + 1
            buf = []
            while j < len(lines) and not lines[j].startswith("```"):
                buf.append(html.escape(lines[j]))
                j += 1
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            i = j + 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head, body = rows[0], [r for r in rows[1:] if not all(re.fullmatch(r":?-+:?", c) for c in r)]
            out.append("<div class='tablewrap'><table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                                                          for r in body) + "</tbody></table></div>")
            continue
        if re.match(r"^\s*[-*]\s+", ln):
            items = []
            while i < len(lines) and (re.match(r"^\s*[-*]\s+", lines[i]) or (lines[i].startswith("  ") and items)):
                if re.match(r"^\s*[-*]\s+", lines[i]):
                    items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]))
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
            continue
        if re.match(r"^\d+\.\s+", ln):
            items = []
            while i < len(lines) and (re.match(r"^\d+\.\s+", lines[i]) or (lines[i].startswith("   ") and items)):
                if re.match(r"^\d+\.\s+", lines[i]):
                    items.append(re.sub(r"^\d+\.\s+", "", lines[i]))
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            out.append("<ol>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ol>")
            continue
        if ln.startswith(">"):
            out.append(f"<blockquote>{_inline(ln.lstrip('> '))}</blockquote>")
            i += 1
            continue
        if not ln.strip():
            i += 1
            continue
        buf = [ln]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||```|\s*[-*]\s|\d+\.\s|>)", lines[i]):
            buf.append(lines[i])
            i += 1
        out.append("<p>" + _inline(" ".join(buf)) + "</p>")
    return "\n".join(out)


# ----------------------------------------------------------------------------- manejador

def make_handler(ws: Workspace, jobs: Jobs):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"ExaCellSizer/{__version__}"

        def log_message(self, fmt, *args):  # silencioso
            pass

        # -------------------------------------------------------------- utilidades
        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

        def _error(self, code, msg):
            self._json({"error": msg}, code)

        def _body_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 5 * 1024 * 1024:
                raise ValueError("cuerpo JSON demasiado grande")
            return json.loads(self.rfile.read(n) or b"{}")

        def _file(self, p: Path, download_name: Optional[str] = None):
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or p.suffix in (".js", ".json", ".md", ".csv"):
                ctype = {".js": "text/javascript", ".json": "application/json", ".md": "text/markdown",
                         ".csv": "text/csv"}.get(p.suffix, ctype) + "; charset=utf-8"
            headers = {"Content-Disposition": f'attachment; filename="{download_name}"'} if download_name else None
            self._send(200, p.read_bytes(), ctype, headers)

        # -------------------------------------------------------------- GET
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            path, q = u.path, parse_qs(u.query)
            try:
                if path in ("/", "/index.html"):
                    return self._file(STATIC / "index.html")
                m = re.fullmatch(r"/static/([A-Za-z0-9_.-]+)", path)
                if m:
                    p = STATIC / m.group(1)
                    return self._file(p) if p.is_file() else self._error(404, "no encontrado")
                if path == "/api/info":
                    return self._json({"version": __version__, "collector_version": coll.script_version(),
                                       "catalog_version": ws.catalog.version, "workspace": str(ws.root)})
                if path == "/api/captures":
                    return self._json(ws.list_captures())
                m = re.fullmatch(r"/api/captures/(\d+)", path)
                if m:
                    return self._json(ws.capture_detail(int(m.group(1))))
                m = re.fullmatch(r"/api/captures/(\d+)/series", path)
                if m:
                    return self._json(ws.series(int(m.group(1)), q.get("measure", [""])[0], q.get("host", ["*"])[0]))
                m = re.fullmatch(r"/api/jobs/([0-9a-f]{12})", path)
                if m:
                    j = jobs.get(m.group(1))
                    return self._json(j) if j else self._error(404, "trabajo no encontrado")
                if path == "/api/runs":
                    return self._json(ws.list_runs())
                m = re.fullmatch(r"/api/runs/([A-Za-z0-9_]+)", path)
                if m:
                    return self._json(ws.get_run(m.group(1)))
                m = re.fullmatch(r"/runs/([A-Za-z0-9_]+)/([A-Za-z0-9_.-]+)", path)
                if m:
                    p = ws.run_file(m.group(1), m.group(2))
                    dl = None if q.get("view") else f"{m.group(1)}_{p.name}"
                    return self._file(p, dl)
                if path == "/api/catalog":
                    return self._json(ws.catalog_view())
                if path == "/api/collector":
                    return self._json({"version": coll.script_version(), "sha256": coll.script_sha256(),
                                       "script": coll.script_text()})
                if path == "/api/collector/download":
                    return self._file(coll.script_path(), "exacell_collect.sh")
                if path == "/api/scenario/default":
                    return self._json(DEFAULT_SCENARIO)
                if path == "/api/scenario/example":
                    if EXAMPLE_SCENARIO.exists():
                        return self._json(json.loads(EXAMPLE_SCENARIO.read_text(encoding="utf-8")))
                    return self._json(DEFAULT_SCENARIO)
                m = re.fullmatch(r"/api/docs/([a-z]+)", path)
                if m:
                    name = m.group(1)
                    p = DOCS.get(name) or {"metodologia": REPO_DOCS / "METODOLOGIA.md",
                                           "limitaciones": REPO_DOCS / "LIMITACIONES.md",
                                           "recolector": REPO_DOCS / "RECOLECTOR.md"}.get(name)
                    if not p or not p.exists():
                        return self._error(404, "documento no disponible")
                    return self._json({"name": name, "html": markdown_to_html(p.read_text(encoding="utf-8"))})
                return self._error(404, "no encontrado")
            except FileNotFoundError:
                return self._error(404, "no encontrado")
            except (ValueError, KeyError) as e:
                return self._error(400, str(e))

        # -------------------------------------------------------------- POST
        def do_POST(self):
            path = urlparse(self.path).path
            try:
                if path == "/api/captures/upload":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n <= 0 or n > MAX_UPLOAD:
                        return self._error(400, "tamaño de archivo no válido")
                    name = unquote(self.headers.get("X-Filename", ""))
                    label = unquote(self.headers.get("X-Label", "")) or None
                    role = self.headers.get("X-Role", "origen")
                    if role not in ("origen", "destino", "otro"):
                        return self._error(400, "rol no válido")
                    if label and ws.label_exists(label):
                        self.rfile.read(n)
                        return self._error(409, f"ya existe una captura con la etiqueta {label!r}")
                    saved = ws.save_upload(name, self.rfile, n)
                    jid = jobs.start("import", ws.import_file, saved, label or Path(name).name, role)
                    return self._json({"job": jid}, 202)
                if path == "/api/captures/simulate":
                    body = self._body_json()
                    if body.get("label") and ws.label_exists(body["label"]):
                        return self._error(409, f"ya existe una captura con la etiqueta {body['label']!r}")
                    jid = jobs.start("simulate", ws.simulate, body)
                    return self._json({"job": jid}, 202)
                if path == "/api/sizing":
                    body = self._body_json()
                    scenario = body.get("scenario") or {}
                    load_scenario(scenario)  # validación inmediata (errores claros antes del trabajo)
                    if not body.get("source"):
                        return self._error(400, "seleccione la captura de origen")
                    jid = jobs.start("sizing", ws.run_sizing, scenario, body["source"], body.get("dest"))
                    return self._json({"job": jid}, 202)
                if path == "/api/scenario/validate":
                    load_scenario(self._body_json().get("scenario") or {})
                    return self._json({"ok": True})
                return self._error(404, "no encontrado")
            except ScenarioError as e:
                return self._error(400, f"Escenario no válido: {e}")
            except (ValueError, json.JSONDecodeError) as e:
                return self._error(400, str(e))

    return Handler


def make_server(workspace: str, host: str = "127.0.0.1", port: int = 8765):
    ws = Workspace(workspace)
    jobs = Jobs()
    httpd = ThreadingHTTPServer((host, port), make_handler(ws, jobs))
    httpd.daemon_threads = True
    return httpd, ws, jobs


def serve(workspace: str, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    httpd, ws, _ = make_server(workspace, host, port)
    ws.warm_up()
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{httpd.server_address[1]}/"
    print(f"ExaCell Sizer {__version__} — aplicación web en {url}")
    print(f"Espacio de trabajo: {ws.root}")
    print("Pulse Ctrl+C para detener.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")
    finally:
        httpd.server_close()

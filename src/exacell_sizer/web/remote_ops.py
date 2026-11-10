"""Operaciones de recogida remota por SSH para el espacio de trabajo web (mixin de Workspace)."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from .. import remote as rmt


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class RemoteOps:
    """Requiere en la clase final: root, label_exists(), import_file(), SAFE."""

    _passwords: Dict[str, str]
    _remote_lock: threading.Lock

    def _conn(self, d: dict) -> "rmt.Conn":
        d = dict(d or {})
        key = f"{d.get('user', '')}@{d.get('host', '')}:{int(d.get('port') or 22)}"
        if d.get("auth") == "password":
            if d.get("password"):
                self._passwords[key] = d["password"]
            else:
                d["password"] = self._passwords.get(key)
                if not d["password"]:
                    raise rmt.RemoteError("Introduzca de nuevo la contraseña SSH (no se guarda en disco).")
        return rmt.Conn.from_dict(d)

    def _ssh(self, d: dict) -> "rmt.SSH":
        return rmt.SSH(self._conn(d), askpass_dir=str(self.root / ".askpass"))

    def _runs_file(self) -> Path:
        return self.root / "remote_runs.json"

    def _load_runs(self) -> List[dict]:
        f = self._runs_file()
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []

    def _save_runs(self, runs: List[dict]) -> None:
        tmp = self._runs_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._runs_file())

    def ssh_test(self, conn: dict, rdir: str, group: str) -> dict:
        return rmt.probe(self._ssh(conn), rdir, group)

    def ssh_dryrun(self, conn: dict, params: dict) -> dict:
        return {"output": rmt.dry_run(self._ssh(conn), params)}

    def ssh_start(self, conn: dict, params: dict) -> dict:
        c = self._conn(conn)
        label = (params.get("label") or "").strip()
        if label and self.label_exists(label):
            raise ValueError(f"ya existe una captura con la etiqueta {label!r}")
        run_id = f"r{time.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:4]}"
        info = rmt.start(rmt.SSH(c, askpass_dir=str(self.root / ".askpass")), params, run_id)
        rec = {"id": run_id, "created_utc": _now(), "conn": c.public(),
               "params": {k: params.get(k) for k in ("dir", "group", "cell_user", "start", "end", "parallel")},
               "label": label or None, "role": params.get("role", "origen"), **info,
               "status": {"state": "running"}, "imported_capture_id": None}
        with self._remote_lock:
            runs = self._load_runs()
            runs.insert(0, rec)
            self._save_runs(runs)
        return rec

    def ssh_runs(self) -> List[dict]:
        return self._load_runs()

    def _get_run(self, run_id: str) -> dict:
        for r in self._load_runs():
            if r["id"] == run_id:
                return r
        raise FileNotFoundError(run_id)

    def _update_run(self, run_id: str, **changes) -> dict:
        with self._remote_lock:
            runs = self._load_runs()
            for r in runs:
                if r["id"] == run_id:
                    r.update(changes)
                    self._save_runs(runs)
                    return r
        raise FileNotFoundError(run_id)

    def _run_conn(self, rec: dict, password: Optional[str]) -> dict:
        d = dict(rec["conn"])
        if password:
            d["password"] = password
        return d

    def ssh_status(self, run_id: str, password: Optional[str] = None) -> dict:
        rec = self._get_run(run_id)
        st = rmt.status(self._ssh(self._run_conn(rec, password)), rec["params"]["dir"], rec["pid"], rec["log"])
        st["checked_utc"] = _now()
        return self._update_run(run_id, status=st)

    def ssh_cancel(self, run_id: str, password: Optional[str] = None) -> dict:
        rec = self._get_run(run_id)
        rmt.cancel(self._ssh(self._run_conn(rec, password)), rec["pid"])
        return self.ssh_status(run_id, password)

    def _download_and_import(self, conn: dict, rdir: str, name: str, label: str, role: str, log) -> dict:
        ssh = self._ssh(conn)
        dest = self.root / "uploads" / f"{time.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}_{self.SAFE.sub('_', name)}.tar.gz"
        log(f"Descargando {name} desde {ssh.conn.host} (tar por SSH, sin temporales en el dbnode)…")
        rmt.download(ssh, rdir, name, dest)
        h = hashlib.sha256(dest.read_bytes()).hexdigest()
        os.chmod(dest, 0o444)
        (dest.parent / (dest.name + ".meta.json")).write_text(json.dumps(
            {"original_name": name, "bytes": dest.stat().st_size, "sha256": h, "uploaded_utc": _now(),
             "source": f"ssh://{ssh.conn.user}@{ssh.conn.host}:{ssh.conn.port}{rdir}/{name}"}))
        log(f"Descargados {dest.stat().st_size / 1048576:.1f} MB (SHA-256 {h[:16]}…)")
        return self.import_file(dest, label, role, log)

    def ssh_import_run(self, run_id: str, password: Optional[str], log=lambda m: None) -> dict:
        rec = self.ssh_status(run_id, password)
        st = rec["status"]
        if st["state"] == "running":
            raise rmt.RemoteError("La recolección sigue en curso.")
        if not st.get("capture_dir"):
            raise rmt.RemoteError("No se encuentra la carpeta de captura en el dbnode (revise el log).")
        name = st["capture_dir"].rstrip("/").split("/")[-1]
        label = rec.get("label") or name
        res = self._download_and_import(self._run_conn(rec, password), rec["params"]["dir"], name, label, rec["role"], log)
        self._update_run(run_id, imported_capture_id=res["capture_id"])
        return res

    def ssh_list_captures(self, conn: dict, rdir: str) -> List[dict]:
        return rmt.list_captures(self._ssh(conn), rdir)

    def ssh_fetch(self, conn: dict, rdir: str, name: str, label: Optional[str], role: str, log=lambda m: None) -> dict:
        if label and self.label_exists(label):
            raise ValueError(f"ya existe una captura con la etiqueta {label!r}")
        return self._download_and_import(conn, rdir, name, label or name, role, log)


"""Recogida remota por SSH: la aplicación se conecta SOLO al dbnode y ejecuta allí el recolector.

Diseño y garantías:
  * Usa el cliente OpenSSH del sistema (``ssh``), sin dependencias: respeta ~/.ssh/config, el
    agente, las claves y known_hosts del usuario.
  * Nunca se conecta a las celdas: es el recolector, en el dbnode, quien usa dcli/ssh contra ellas
    en solo lectura (ver exacell_collect.sh).
  * Única escritura en el dbnode: el script ``exacell_collect_<versión>_<sha8>.sh`` y el log de la
    ejecución en el directorio indicado por el usuario (además de la captura que crea el propio
    recolector). Se verifica el SHA-256 del script copiado.
  * Clave de host: ``StrictHostKeyChecking=yes`` por defecto; ``accept-new`` solo si el usuario lo
    marca explícitamente.
  * Contraseñas: solo en memoria, entregadas a ssh mediante SSH_ASKPASS (OpenSSH >= 8.4); nunca se
    escriben en disco ni en registros.
  * Todo valor interpolado en comandos remotos se valida y se entrecomilla (shlex.quote).
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import collector as coll

HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252})$|^\[?[0-9a-fA-F:.]+\]?$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,31}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/+-]{0,1000}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2}|Z)$")
CAPNAME_RE = re.compile(r"^exacell_capture_[A-Za-z0-9._-]+$")


class RemoteError(Exception):
    pass


def _check(cond: bool, msg: str):
    if not cond:
        raise RemoteError(msg)


def ssh_binary() -> str:
    b = os.environ.get("EXACELL_SSH_BIN") or shutil.which("ssh")
    _check(bool(b), "No se encuentra el cliente OpenSSH ('ssh'). Instálelo (en Windows: característica opcional "
                    "'Cliente OpenSSH').")
    return b


@dataclass
class Conn:
    host: str
    user: str
    port: int = 22
    auth: str = "agent"              # agent | key | password
    key_path: Optional[str] = None
    password: Optional[str] = field(default=None, repr=False)
    accept_new: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "Conn":
        c = cls(host=str(d.get("host", "")).strip(), user=str(d.get("user", "")).strip(),
                port=int(d.get("port") or 22), auth=d.get("auth") or "agent",
                key_path=(d.get("key_path") or None), password=d.get("password") or None,
                accept_new=bool(d.get("accept_new")))
        c.validate()
        return c

    def validate(self):
        _check(bool(HOST_RE.match(self.host)), f"host no válido: {self.host!r}")
        _check(bool(USER_RE.match(self.user)), f"usuario no válido: {self.user!r}")
        _check(1 <= self.port <= 65535, "puerto no válido")
        _check(self.auth in ("agent", "key", "password"), "método de autenticación no válido")
        if self.auth == "key":
            _check(bool(self.key_path), "indique el archivo de clave privada")
            _check(Path(self.key_path).expanduser().is_file(), f"no existe el archivo de clave {self.key_path!r}")
        if self.auth == "password":
            _check(bool(self.password), "indique la contraseña (solo se mantiene en memoria)")

    @property
    def key(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"

    def public(self) -> dict:
        return {"host": self.host, "user": self.user, "port": self.port, "auth": self.auth,
                "key_path": self.key_path, "accept_new": self.accept_new}


class SSH:
    """Ejecuta comandos en el dbnode con el cliente OpenSSH del sistema."""

    def __init__(self, conn: Conn, askpass_dir: Optional[str] = None):
        self.conn = conn
        self.bin = ssh_binary()
        self.askpass_dir = askpass_dir

    def _argv(self, remote_cmd: str) -> List[str]:
        c = self.conn
        a = [self.bin, "-p", str(c.port), "-T",
             "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
             "-o", f"StrictHostKeyChecking={'accept-new' if c.accept_new else 'yes'}",
             "-o", "LogLevel=ERROR"]
        if c.auth == "password":
            a += ["-o", "BatchMode=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
                  "-o", "NumberOfPasswordPrompts=1", "-o", "PubkeyAuthentication=no"]
        else:
            a += ["-o", "BatchMode=yes"]
        if c.auth == "key":
            a += ["-i", str(Path(c.key_path).expanduser()), "-o", "IdentitiesOnly=yes"]
        a += [f"{c.user}@{c.host}", remote_cmd]
        return a

    def _env(self) -> Tuple[dict, Optional[str]]:
        env = dict(os.environ)
        helper = None
        if self.conn.auth == "password":
            d = Path(self.askpass_dir or tempfile.gettempdir())
            d.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                helper = d / "exacell_askpass.cmd"
                helper.write_text(f'@"{sys.executable}" -c "import os,sys;sys.stdout.write(os.environ[\'EXACELL_SSH_PASSWORD\'])"\r\n')
            else:
                helper = d / "exacell_askpass.sh"
                helper.write_text("#!/bin/sh\nprintf '%s\\n' \"$EXACELL_SSH_PASSWORD\"\n")
                helper.chmod(stat.S_IRWXU)
            env.update(SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE="force", DISPLAY=env.get("DISPLAY", ":0"),
                       EXACELL_SSH_PASSWORD=self.conn.password)
        return env, (str(helper) if helper else None)

    def run(self, remote_cmd: str, data: Optional[bytes] = None, timeout: int = 120) -> Tuple[int, str, str]:
        env, _ = self._env()
        try:
            p = subprocess.run(self._argv(remote_cmd), input=data if data is not None else b"",
                               capture_output=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise RemoteError(f"tiempo de espera agotado ({timeout}s) conectando con {self.conn.host}")
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def stream_to_file(self, remote_cmd: str, dest: Path, timeout: int = 7200) -> Tuple[int, str]:
        env, _ = self._env()
        with open(dest, "wb") as fh:
            try:
                p = subprocess.run(self._argv(remote_cmd), stdin=subprocess.DEVNULL, stdout=fh,
                                   stderr=subprocess.PIPE, timeout=timeout, env=env)
            except subprocess.TimeoutExpired:
                raise RemoteError("tiempo de espera agotado durante la descarga")
        return p.returncode, p.stderr.decode("utf-8", "replace")


def explain_ssh_error(rc: int, err: str) -> str:
    e = err.strip()
    if "Host key verification failed" in e or "REMOTE HOST IDENTIFICATION HAS CHANGED" in e:
        return ("La clave del host no está en known_hosts o ha cambiado. Verifique la huella con el "
                "administrador; si es nueva y legítima, marque «Aceptar clave nueva del host». " + e)
    if "Permission denied" in e:
        return "Autenticación rechazada (usuario, clave o contraseña). " + e
    if "Could not resolve" in e or "Name or service not known" in e:
        return "No se resuelve el nombre del host. " + e
    if "timed out" in e or "Connection refused" in e or "No route" in e:
        return "No hay conexión con el dbnode (red, puerto o cortafuegos). " + e
    return f"SSH terminó con código {rc}. {e}"


# ----------------------------------------------------------------------------- operaciones remotas

def q(s: str) -> str:
    return shlex.quote(s)


def _kv(out: str) -> Dict[str, List[str]]:
    d: Dict[str, List[str]] = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            d.setdefault(k.strip(), []).append(v.strip())
    return d


def validate_dir(p: str, what: str) -> str:
    _check(bool(PATH_RE.match(p or "")) and ".." not in p.split("/"), f"{what}: ruta absoluta no válida {p!r}")
    return p.rstrip("/") or "/"


def probe(ssh: SSH, rdir: str, group: str) -> dict:
    rdir, group = validate_dir(rdir, "directorio"), validate_dir(group, "cell_group")
    cmd = ("set +e; echo HOST=$(hostname); echo USER=$(id -un); echo OS=$(uname -sr); "
           "command -v dcli >/dev/null 2>&1 && echo DCLI=yes || echo DCLI=no; "
           "command -v sha256sum >/dev/null 2>&1 && echo SHA=yes || echo SHA=no; "
           f"if [ -d {q(rdir)} ] && [ -w {q(rdir)} ]; then echo DIR=ok; else echo DIR=bad; fi; "
           f"if [ -r {q(group)} ]; then echo GROUP=ok; sed -e 's/#.*//' -e 's/[[:space:]]//g' {q(group)} | "
           "sed '/^$/d' | sed 's/^/CELL=/'; else echo GROUP=bad; fi")
    rc, out, err = ssh.run(cmd, timeout=60)
    if rc != 0:
        raise RemoteError(explain_ssh_error(rc, err))
    kv = _kv(out)
    g = lambda k: (kv.get(k) or [""])[0]
    checks = [
        ("Conexión SSH al dbnode", True, f"{g('USER')}@{g('HOST')} · {g('OS')}"),
        ("dcli disponible en el dbnode", g("DCLI") == "yes", "necesario para la prevalidación de celdas"),
        ("sha256sum disponible", g("SHA") == "yes", "necesario para las sumas de verificación"),
        ("Directorio existe y es escribible", g("DIR") == "ok", rdir),
        ("cell_group legible", g("GROUP") == "ok", group),
        ("cell_group con celdas", bool(kv.get("CELL")), f"{len(kv.get('CELL', []))} celdas"),
    ]
    return {"host": g("HOST"), "user": g("USER"), "os": g("OS"), "cells": kv.get("CELL", []),
            "checks": [{"check": c, "ok": ok, "detail": d} for c, ok, d in checks],
            "ready": all(ok for _, ok, _ in checks)}


def script_remote_path(rdir: str) -> str:
    return f"{rdir}/exacell_collect_{coll.script_version()}_{coll.script_sha256()[:8]}.sh"


def install_script(ssh: SSH, rdir: str) -> str:
    """Copia el recolector al dbnode (si no está ya, idéntico) y verifica su SHA-256."""
    rdir = validate_dir(rdir, "directorio")
    path = script_remote_path(rdir)
    sha = coll.script_sha256()
    cmd = (f"f={q(path)}; if [ -f \"$f\" ] && [ \"$(sha256sum \"$f\" | cut -d' ' -f1)\" = {q(sha)} ]; "
           "then cat >/dev/null; echo STATE=reused; else umask 077; t=\"$f.tmp.$$\"; cat > \"$t\" && chmod 700 \"$t\" "
           "&& mv \"$t\" \"$f\" && echo STATE=installed; fi; echo SHA=$(sha256sum \"$f\" | cut -d' ' -f1)")
    rc, out, err = ssh.run(cmd, data=coll.script_path().read_bytes(), timeout=120)
    if rc != 0:
        raise RemoteError(explain_ssh_error(rc, err))
    kv = _kv(out)
    _check((kv.get("SHA") or [""])[0] == sha, "El SHA-256 del script en el dbnode no coincide: copia fallida.")
    return path


def collector_args(p: dict) -> List[str]:
    rdir = validate_dir(p.get("dir", ""), "directorio")
    group = validate_dir(p.get("group", ""), "cell_group")
    _check(p.get("cell_user") in ("root", "celladmin"), "usuario de celdas: root o celladmin")
    _check(bool(ISO_RE.match(p.get("start", ""))), "inicio: ISO-8601 con zona horaria")
    _check(bool(ISO_RE.match(p.get("end", ""))), "fin: ISO-8601 con zona horaria")
    par = int(p.get("parallel") or 4)
    _check(1 <= par <= 32, "paralelo entre 1 y 32")
    return ["-d", rdir, "-g", group, "-u", p["cell_user"], "-s", p["start"], "-e", p["end"], "-p", str(par)]


def dry_run(ssh: SSH, p: dict) -> str:
    args = collector_args(p)
    path = install_script(ssh, p["dir"])
    rc, out, err = ssh.run("bash " + " ".join(q(x) for x in [path] + args + ["-n"]), timeout=60)
    if rc != 0:
        raise RemoteError((out + err).strip() or explain_ssh_error(rc, err))
    return out


def start(ssh: SSH, p: dict, run_id: str) -> dict:
    _check(bool(re.fullmatch(r"[A-Za-z0-9_]+", run_id)), "id de ejecución no válido")
    args = collector_args(p)
    rdir = p["dir"].rstrip("/")
    path = install_script(ssh, rdir)
    log = f"{rdir}/exacell_run_{run_id}.log"
    # ';' y no '&&': con 'a && b &' bash enviaría toda la lista a una subshell y $! no sería el recolector.
    cmd = (f"cd {q(rdir)} || exit 1; nohup bash {' '.join(q(x) for x in [path] + args)} > {q(log)} 2>&1 < /dev/null & "
           "echo PID=$!")
    rc, out, err = ssh.run(cmd, timeout=60)
    if rc != 0:
        raise RemoteError(explain_ssh_error(rc, err))
    pid = (_kv(out).get("PID") or [""])[0]
    _check(pid.isdigit(), f"no se obtuvo el PID del recolector: {out!r}")
    return {"pid": int(pid), "log": log, "script": path}


def status(ssh: SSH, rdir: str, pid: int, log: str) -> dict:
    rdir = validate_dir(rdir, "directorio")
    log = validate_dir(log, "log")
    cmd = (f"set +e; P={int(pid)}; "
           "if kill -0 $P 2>/dev/null && grep -q exacell_collect /proc/$P/cmdline 2>/dev/null; then echo STATE=running; "
           "else echo STATE=finished; fi; "
           f"C=$(ls -1d {q(rdir)}/exacell_capture_*_$P 2>/dev/null | head -1); echo CAP=$C; "
           "if [ -n \"$C\" ] && [ -f \"$C/manifest.txt\" ]; then grep -E '^(capture_status|exit_code|expected_blocks|recorded_blocks)=' \"$C/manifest.txt\"; fi; "
           "if [ -n \"$C\" ]; then echo DONE_BLOCKS=$(cat \"$C\"/cells/*/blocks.tsv 2>/dev/null | wc -l); fi; "
           f"echo EXPECTED=$(grep -o 'Bloques esperados: [0-9]*' {q(log)} 2>/dev/null | tail -1 | sed 's/.*: //'); "
           f"echo '---LOG---'; tail -n 40 {q(log)} 2>/dev/null")
    rc, out, err = ssh.run(cmd, timeout=60)
    if rc != 0:
        raise RemoteError(explain_ssh_error(rc, err))
    head, _, logtxt = out.partition("---LOG---")
    kv = _kv(head)
    g = lambda k: (kv.get(k) or [""])[0]
    expected = int(g("expected_blocks") or g("EXPECTED") or 0)
    done = int(g("recorded_blocks") or g("DONE_BLOCKS") or 0)
    st = {"state": g("STATE"), "capture_dir": g("CAP") or None, "capture_status": g("capture_status") or None,
          "exit_code": g("exit_code") or None, "expected_blocks": expected, "done_blocks": done,
          "progress": round(done / expected, 4) if expected else None, "log_tail": logtxt.strip()[-6000:]}
    if st["state"] == "finished" and not st["capture_status"]:
        st["capture_status"] = "SIN_MANIFIESTO"
    return st


def cancel(ssh: SSH, pid: int) -> None:
    cmd = (f"P={int(pid)}; if kill -0 $P 2>/dev/null && grep -q exacell_collect /proc/$P/cmdline 2>/dev/null; "
           "then kill -TERM $P && echo CANCELLED=yes; else echo CANCELLED=no; fi")
    rc, out, err = ssh.run(cmd, timeout=60)
    if rc != 0:
        raise RemoteError(explain_ssh_error(rc, err))


def list_captures(ssh: SSH, rdir: str) -> List[dict]:
    rdir = validate_dir(rdir, "directorio")
    cmd = (f"set +e; for c in {q(rdir)}/exacell_capture_*; do [ -d \"$c\" ] || continue; "
           "echo \"---$(basename \"$c\")\"; echo SIZE_KB=$(du -sk \"$c\" | cut -f1); "
           "[ -f \"$c/manifest.txt\" ] && grep -E '^(capture_status|exit_code|period_start|period_end|cell_count|created_utc)=' \"$c/manifest.txt\"; done")
    rc, out, err = ssh.run(cmd, timeout=120)
    if rc != 0:
        raise RemoteError(explain_ssh_error(rc, err))
    caps = []
    for chunk in out.split("---")[1:]:
        lines = chunk.strip().splitlines()
        if not lines:
            continue
        name = lines[0].strip()
        if not CAPNAME_RE.match(name):
            continue
        kv = _kv("\n".join(lines[1:]))
        g = lambda k: (kv.get(k) or [None])[0]
        caps.append({"name": name, "path": f"{rdir}/{name}", "size_kb": int(g("SIZE_KB") or 0),
                     "capture_status": g("capture_status") or "EN_CURSO_O_SIN_MANIFIESTO", "exit_code": g("exit_code"),
                     "period_start": g("period_start"), "period_end": g("period_end"), "cell_count": g("cell_count"),
                     "created_utc": g("created_utc")})
    return sorted(caps, key=lambda c: c["name"], reverse=True)


def download(ssh: SSH, rdir: str, name: str, dest: Path) -> Path:
    """Descarga la carpeta de captura como .tar.gz por la salida estándar (sin temporales en el dbnode)."""
    rdir = validate_dir(rdir, "directorio")
    _check(bool(CAPNAME_RE.match(name)), f"nombre de captura no válido: {name!r}")
    rc, err = ssh.stream_to_file(f"cd {q(rdir)} && test -f {q(name)}/manifest.txt && tar czf - {q(name)}", dest)
    if rc != 0:
        dest.unlink(missing_ok=True)
        raise RemoteError("No se pudo descargar la captura (¿sigue en curso o no existe?). " + explain_ssh_error(rc, err))
    _check(dest.stat().st_size > 0, "descarga vacía")
    return dest

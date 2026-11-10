import hashlib
from pathlib import Path


def resign(cap: Path):
    """Recalcula SHA256SUMS tras modificar a propósito una COPIA de prueba."""
    sums = []
    for f in sorted(p for p in cap.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        sums.append(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  ./{f.relative_to(cap)}")
    (cap / "SHA256SUMS").write_text("\n".join(sums) + "\n")

"""Interfaz de línea de comandos de ExaCell Sizer."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from . import collector as coll


def _cmd_collector(args) -> int:
    if args.action == "show":
        sys.stdout.write(coll.script_text())
        return 0
    if args.action == "info":
        print(f"Recolector: {coll.script_path()}")
        print(f"Versión:    {coll.script_version()}")
        print(f"SHA-256:    {coll.script_sha256()}")
        print("Uso en el dbnode: exacell_collect.sh -h")
        return 0
    if args.action == "export":
        if not args.dest:
            print("export requiere --dest", file=sys.stderr)
            return 1
        try:
            p = coll.export_script(args.dest)
        except FileExistsError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"Script exportado a {p} (SHA-256 {coll.script_sha256()})")
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="exacell-sizer", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collector", help="Visualizar o exportar el script recolector para el dbnode")
    c.add_argument("action", choices=["show", "info", "export"])
    c.add_argument("--dest", help="Archivo o directorio de destino para export")
    c.set_defaults(func=_cmd_collector)

    from . import cli_commands  # registro de comandos de fases posteriores
    cli_commands.register(sub)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())

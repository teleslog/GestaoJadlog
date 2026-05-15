"""
backup_data.py — backup do diretório /data (users.db + xlsx/zip operacionais
e financeiros) em um único tarball comprimido com manifest.

Uso:
    python tools/backup_data.py                  # usa DATA_DIR do env ou raiz do projeto
    python tools/backup_data.py --data-dir PATH  # override do data dir
    python tools/backup_data.py --out PATH       # destino do tarball
    python tools/backup_data.py --quiet          # menos output

Conteúdo gravado dentro do tarball (paths relativos):
    manifest.json
    users.db                          (se existir)
    dados/operacional/<arquivos>
    dados/financeiro/<arquivos>

manifest.json contém:
    - version            (vYYYY.MM.DD.<git-short> ou vYYYY.MM.DD.HHmm)
    - created_at         (ISO 8601 UTC)
    - data_dir           (caminho absoluto de origem)
    - total_files
    - total_size_bytes
    - files: [{path, size, sha256}]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

# Conjunto de arquivos a incluir no backup (paths relativos ao DATA_DIR).
# Diretórios são percorridos recursivamente; arquivos soltos vão direto.
_INCLUDE_PATHS = ("users.db", "dados/operacional", "dados/financeiro")


def _detect_version(repo_root: Path) -> str:
    """Espelha _get_version() do main.py — sem importar o módulo."""
    today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root), stderr=subprocess.DEVNULL, timeout=3,
        ).decode().strip()
        if commit:
            return f"v{today}.{commit}"
    except Exception:  # noqa: BLE001
        pass
    return datetime.now(timezone.utc).strftime("v%Y.%m.%d.%H%M")


def _iter_files(data_dir: Path):
    """Itera (path_abs, path_rel) dos arquivos a incluir no backup."""
    for entry in _INCLUDE_PATHS:
        p = data_dir / entry
        if not p.exists():
            continue
        if p.is_file():
            yield p, p.relative_to(data_dir).as_posix()
        else:
            for f in sorted(p.rglob("*")):
                if f.is_file() and not f.name.startswith("~$"):
                    yield f, f.relative_to(data_dir).as_posix()


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_backup(data_dir: Path, out_path: Path, repo_root: Path | None = None,
                 quiet: bool = False) -> dict:
    """Gera tarball em `out_path`. Retorna o manifest dict."""
    repo_root = repo_root or Path(__file__).resolve().parents[1]
    data_dir = data_dir.resolve()
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    files_info: list[dict] = []
    total_size = 0
    for abs_path, rel_path in _iter_files(data_dir):
        size = abs_path.stat().st_size
        sha = _sha256_of(abs_path)
        files_info.append({"path": rel_path, "size": size, "sha256": sha})
        total_size += size
        if not quiet:
            print(f"  + {rel_path}  ({size:,} bytes  sha256={sha[:12]}...)")

    manifest = {
        "version": _detect_version(repo_root),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_dir": str(data_dir),
        "total_files": len(files_info),
        "total_size_bytes": total_size,
        "files": files_info,
    }

    with tarfile.open(out_path, "w:gz") as tar:
        # manifest primeiro
        manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        tar.addfile(info, io.BytesIO(manifest_bytes))
        # arquivos do data_dir
        for abs_path, rel_path in _iter_files(data_dir):
            tar.add(str(abs_path), arcname=rel_path)

    if not quiet:
        size_mb = out_path.stat().st_size / 1024 / 1024
        print(f"\n[ok] backup gerado: {out_path}  ({size_mb:.2f} MB)")
        print(f"     versão: {manifest['version']}")
        print(f"     arquivos: {manifest['total_files']}")
        print(f"     conteúdo original: {manifest['total_size_bytes']:,} bytes")

    return manifest


def _default_out_path(repo_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return repo_root / "backups" / f"backup-{stamp}.tar.gz"


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    default_data = Path(os.environ.get("DATA_DIR", str(repo_root)))

    ap = argparse.ArgumentParser(description="Backup do diretório /data.")
    ap.add_argument("--data-dir", type=Path, default=default_data,
                    help=f"diretório /data (default: {default_data})")
    ap.add_argument("--out", type=Path, default=None,
                    help="arquivo de saída .tar.gz (default: backups/backup-<ts>.tar.gz)")
    ap.add_argument("--quiet", action="store_true", help="silencia output")
    args = ap.parse_args(argv)

    out_path = args.out or _default_out_path(repo_root)
    data_dir = args.data_dir.resolve()

    if not data_dir.exists():
        print(f"[erro] data_dir não existe: {data_dir}", file=sys.stderr)
        return 2

    if not args.quiet:
        print(f"[backup] origem: {data_dir}")
        print(f"[backup] destino: {out_path}\n")

    build_backup(data_dir, out_path, repo_root=repo_root, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
restore_data.py — restore seguro de backup gerado por backup_data.py.

Por padrão:
  - Recusa rodar se detectar ambiente de produção (env RAILWAY_ENVIRONMENT).
  - Exige confirmação interativa (--yes pula).
  - Valida sha256 de cada arquivo antes de extrair.
  - Cria backup automático do estado atual ANTES de tocar em qualquer arquivo.

Uso:
    python tools/restore_data.py <backup.tar.gz>                # local, com confirmação
    python tools/restore_data.py <backup.tar.gz> --yes          # sem confirmação
    python tools/restore_data.py <backup.tar.gz> --dry-run      # só lista, não escreve
    python tools/restore_data.py <backup.tar.gz> --allow-production
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

# Reaproveita helpers do backup
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup_data import build_backup  # noqa: E402


def _is_production() -> bool:
    """Detecta ambiente de produção via env. Mantenha conservador."""
    return bool(os.environ.get("RAILWAY_ENVIRONMENT") or
                os.environ.get("RENDER") or
                os.environ.get("PRODUCTION"))


def _sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest(tar: tarfile.TarFile) -> dict:
    try:
        info = tar.getmember("manifest.json")
    except KeyError as e:
        raise ValueError("backup inválido: manifest.json não encontrado") from e
    f = tar.extractfile(info)
    if not f:
        raise ValueError("manifest.json ilegível")
    return json.loads(f.read().decode("utf-8"))


def _validate_sha256s(tar: tarfile.TarFile, manifest: dict) -> list[str]:
    """Verifica sha256 de cada arquivo do manifest contra o conteúdo do tar.
    Retorna lista de erros (vazia = OK)."""
    errors: list[str] = []
    members_by_name = {m.name: m for m in tar.getmembers()}
    for entry in manifest.get("files", []):
        rel = entry["path"]
        info = members_by_name.get(rel)
        if info is None:
            errors.append(f"arquivo '{rel}' está no manifest mas não no tarball")
            continue
        f = tar.extractfile(info)
        if not f:
            errors.append(f"arquivo '{rel}' ilegível no tarball")
            continue
        actual = _sha256_bytes(f.read())
        expected = entry.get("sha256", "")
        if actual != expected:
            errors.append(
                f"sha256 não bate para '{rel}': esperado={expected[:12]}... obtido={actual[:12]}..."
            )
    return errors


def _confirm(prompt: str) -> bool:
    """Confirmação interativa. Aborta se stdin não for tty (segurança)."""
    if not sys.stdin.isatty():
        return False
    resp = input(prompt).strip().lower()
    return resp in ("y", "yes", "sim", "s")


def restore(
    tarball: Path,
    data_dir: Path,
    *,
    assume_yes: bool = False,
    allow_production: bool = False,
    dry_run: bool = False,
    repo_root: Path | None = None,
) -> dict:
    """
    Executa o restore. Retorna dict com {ok, manifest, prebackup, restored, errors}.
    Levanta RuntimeError se erro irrecuperável (validação, prod sem flag).
    """
    repo_root = repo_root or Path(__file__).resolve().parents[1]
    tarball = tarball.resolve()
    data_dir = data_dir.resolve()

    if _is_production() and not allow_production:
        raise RuntimeError(
            "ambiente de produção detectado; restore bloqueado. "
            "Use --allow-production explicitamente se realmente sabe o que está fazendo."
        )

    if not tarball.exists():
        raise FileNotFoundError(f"backup não encontrado: {tarball}")

    with tarfile.open(tarball, "r:gz") as tar:
        manifest = _load_manifest(tar)
        errors = _validate_sha256s(tar, manifest)
        if errors:
            raise RuntimeError(
                "validação sha256 falhou:\n  - " + "\n  - ".join(errors)
            )

        print(f"[restore] backup válido: {manifest['total_files']} arquivos, "
              f"{manifest['total_size_bytes']:,} bytes, versão {manifest['version']}")
        print(f"[restore] origem do backup: {manifest['data_dir']}")
        print(f"[restore] destino do restore: {data_dir}")

        if dry_run:
            print("[restore] --dry-run: nenhuma alteração feita.")
            return {
                "ok": True, "manifest": manifest, "prebackup": None,
                "restored": [], "errors": [], "dry_run": True,
            }

        if not assume_yes:
            files_n = manifest["total_files"]
            if not _confirm(f"\nConfirma sobrescrever {data_dir} com {files_n} arquivos? [y/N] "):
                print("[restore] cancelado pelo usuário.")
                return {"ok": False, "manifest": manifest, "prebackup": None,
                        "restored": [], "errors": [], "cancelled": True}

        # 1) Backup automático do estado atual
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        prebackup_path = repo_root / "backups" / f"prebackup-{stamp}.tar.gz"
        if data_dir.exists():
            print(f"[restore] criando pre-backup em {prebackup_path}")
            build_backup(data_dir, prebackup_path, repo_root=repo_root, quiet=True)

        # 2) Extração
        restored: list[str] = []
        for entry in manifest.get("files", []):
            rel = entry["path"]
            try:
                member = tar.getmember(rel)
            except KeyError:
                continue
            dest = data_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with dest.open("wb") as out:
                shutil.copyfileobj(src, out)
            restored.append(rel)

        print(f"[restore] {len(restored)} arquivos restaurados.")
        return {
            "ok": True, "manifest": manifest, "prebackup": str(prebackup_path),
            "restored": restored, "errors": [], "dry_run": False,
        }


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    default_data = Path(os.environ.get("DATA_DIR", str(repo_root)))

    ap = argparse.ArgumentParser(description="Restore de backup gerado por backup_data.py.")
    ap.add_argument("tarball", type=Path, help="arquivo .tar.gz gerado pelo backup_data.py")
    ap.add_argument("--data-dir", type=Path, default=default_data,
                    help=f"diretório /data destino (default: {default_data})")
    ap.add_argument("--yes", action="store_true", help="pula confirmação interativa")
    ap.add_argument("--allow-production", action="store_true",
                    help="permite restore em produção (RAILWAY_ENVIRONMENT etc.)")
    ap.add_argument("--dry-run", action="store_true", help="não escreve nada, só valida")
    args = ap.parse_args(argv)

    try:
        result = restore(
            tarball=args.tarball,
            data_dir=args.data_dir,
            assume_yes=args.yes,
            allow_production=args.allow_production,
            dry_run=args.dry_run,
            repo_root=repo_root,
        )
    except RuntimeError as exc:
        print(f"[erro] {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"[erro] {exc}", file=sys.stderr)
        return 2

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

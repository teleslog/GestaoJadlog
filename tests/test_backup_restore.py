"""
Testes dos scripts tools/backup_data.py e tools/restore_data.py.

Sem depender do `main` ou de qualquer estado de servidor. Cada teste cria
seu próprio data_dir sintético em tmp_path.
"""
from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path

import pytest

# Permite importar tools/ como módulos
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import backup_data  # noqa: E402
import restore_data  # noqa: E402

# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_synthetic_data_dir(root: Path) -> Path:
    """Cria estrutura típica: users.db + dados/operacional/x.xlsx + dados/financeiro/y.xlsx."""
    (root / "dados" / "operacional").mkdir(parents=True)
    (root / "dados" / "financeiro").mkdir(parents=True)
    (root / "users.db").write_bytes(b"SQLite-FAKE-DB")
    (root / "dados" / "operacional" / "Performance.xlsx").write_bytes(b"FAKE-XLSX-OP-1234")
    (root / "dados" / "operacional" / "Performance.zip").write_bytes(b"FAKE-ZIP")
    (root / "dados" / "financeiro" / "Relatorio.xlsx").write_bytes(b"FAKE-XLSX-FIN")
    return root


# ── Backup ───────────────────────────────────────────────────────────────────

def test_backup_gera_tarball_com_manifest(tmp_path):
    data_dir = _build_synthetic_data_dir(tmp_path / "data")
    out = tmp_path / "out" / "backup.tar.gz"
    manifest = backup_data.build_backup(data_dir, out, repo_root=ROOT, quiet=True)

    assert out.exists()
    assert manifest["total_files"] == 4
    assert manifest["data_dir"] == str(data_dir)
    assert manifest["created_at"].endswith("Z")

    with tarfile.open(out, "r:gz") as tar:
        names = set(tar.getnames())
    assert "manifest.json" in names
    assert "users.db" in names
    assert "dados/operacional/Performance.xlsx" in names
    assert "dados/operacional/Performance.zip" in names
    assert "dados/financeiro/Relatorio.xlsx" in names


def test_backup_manifest_tem_sha256_de_cada_arquivo(tmp_path):
    data_dir = _build_synthetic_data_dir(tmp_path / "data")
    out = tmp_path / "out" / "backup.tar.gz"
    manifest = backup_data.build_backup(data_dir, out, repo_root=ROOT, quiet=True)

    for entry in manifest["files"]:
        assert "sha256" in entry and len(entry["sha256"]) == 64
        assert entry["size"] > 0
        assert entry["path"] in {
            "users.db",
            "dados/operacional/Performance.xlsx",
            "dados/operacional/Performance.zip",
            "dados/financeiro/Relatorio.xlsx",
        }


def test_backup_ignora_arquivos_temp_excel(tmp_path):
    data_dir = _build_synthetic_data_dir(tmp_path / "data")
    (data_dir / "dados" / "operacional" / "~$lock.xlsx").write_bytes(b"temp")
    out = tmp_path / "out" / "backup.tar.gz"
    manifest = backup_data.build_backup(data_dir, out, repo_root=ROOT, quiet=True)
    paths = {e["path"] for e in manifest["files"]}
    assert "~$lock.xlsx" not in paths
    assert all(not p.startswith("~$") and "/~$" not in p for p in paths)


def test_backup_data_dir_inexistente_falha(tmp_path):
    out = tmp_path / "out" / "backup.tar.gz"
    rc = backup_data.main(["--data-dir", str(tmp_path / "nope"), "--out", str(out), "--quiet"])
    assert rc == 2


# ── Restore ──────────────────────────────────────────────────────────────────

def test_restore_dry_run_nao_escreve_nada(tmp_path):
    data_dir = _build_synthetic_data_dir(tmp_path / "data")
    out = tmp_path / "out" / "backup.tar.gz"
    backup_data.build_backup(data_dir, out, repo_root=ROOT, quiet=True)

    # Destino diferente, vazio
    dest = tmp_path / "dest"
    result = restore_data.restore(
        tarball=out, data_dir=dest,
        assume_yes=True, dry_run=True, repo_root=tmp_path,
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert not dest.exists() or not any(dest.rglob("*"))


def test_restore_com_yes_escreve_arquivos(tmp_path):
    data_dir = _build_synthetic_data_dir(tmp_path / "data")
    out = tmp_path / "out" / "backup.tar.gz"
    backup_data.build_backup(data_dir, out, repo_root=ROOT, quiet=True)

    dest = tmp_path / "dest"
    result = restore_data.restore(
        tarball=out, data_dir=dest,
        assume_yes=True, repo_root=tmp_path,
    )
    assert result["ok"] is True
    assert len(result["restored"]) == 4
    assert (dest / "users.db").read_bytes() == b"SQLite-FAKE-DB"
    assert (dest / "dados" / "operacional" / "Performance.xlsx").read_bytes() == b"FAKE-XLSX-OP-1234"
    assert (dest / "dados" / "financeiro" / "Relatorio.xlsx").read_bytes() == b"FAKE-XLSX-FIN"


def test_restore_cria_prebackup_quando_dest_tem_dados(tmp_path):
    data_dir = _build_synthetic_data_dir(tmp_path / "data")
    out = tmp_path / "out" / "backup.tar.gz"
    backup_data.build_backup(data_dir, out, repo_root=ROOT, quiet=True)

    # Dest já tem dados de outra "produção" — restore precisa preservar.
    dest = tmp_path / "dest"
    (dest / "dados" / "operacional").mkdir(parents=True)
    (dest / "users.db").write_bytes(b"OLD-DB")
    (dest / "dados" / "operacional" / "antigo.xlsx").write_bytes(b"OLD-XLSX")

    result = restore_data.restore(
        tarball=out, data_dir=dest,
        assume_yes=True, repo_root=tmp_path,
    )
    assert result["ok"] is True
    prebackup = Path(result["prebackup"])
    assert prebackup.exists()
    # Pré-backup deve conter o conteúdo antigo
    with tarfile.open(prebackup, "r:gz") as tar:
        names = set(tar.getnames())
        assert "users.db" in names
        # Conteúdo antigo
        f = tar.extractfile("users.db")
        assert f and f.read() == b"OLD-DB"


def test_restore_falha_com_sha256_corrompido(tmp_path):
    data_dir = _build_synthetic_data_dir(tmp_path / "data")
    out = tmp_path / "out" / "backup.tar.gz"
    backup_data.build_backup(data_dir, out, repo_root=ROOT, quiet=True)

    # Corrompe o manifest: troca um sha256 por um falso
    with tarfile.open(out, "r:gz") as tar:
        members = list(tar.getmembers())
        data_by_name = {m.name: tar.extractfile(m).read() for m in members}  # type: ignore[union-attr]
    manifest = json.loads(data_by_name["manifest.json"].decode())
    manifest["files"][0]["sha256"] = "0" * 64
    data_by_name["manifest.json"] = json.dumps(manifest).encode()

    corrupted = tmp_path / "out" / "corrupted.tar.gz"
    import io
    with tarfile.open(corrupted, "w:gz") as tar:
        for name, raw in data_by_name.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))

    dest = tmp_path / "dest"
    with pytest.raises(RuntimeError, match="sha256"):
        restore_data.restore(
            tarball=corrupted, data_dir=dest,
            assume_yes=True, repo_root=tmp_path,
        )


def test_restore_falha_sem_manifest(tmp_path):
    """Tarball aleatório sem manifest.json é rejeitado."""
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        import io
        info = tarfile.TarInfo(name="random.bin")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"junk"))
    with pytest.raises(ValueError, match="manifest"):
        restore_data.restore(
            tarball=bad, data_dir=tmp_path / "dest",
            assume_yes=True, repo_root=tmp_path,
        )


def test_restore_recusa_producao_sem_flag(tmp_path, monkeypatch):
    data_dir = _build_synthetic_data_dir(tmp_path / "data")
    out = tmp_path / "out" / "backup.tar.gz"
    backup_data.build_backup(data_dir, out, repo_root=ROOT, quiet=True)

    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="produção"):
        restore_data.restore(
            tarball=out, data_dir=tmp_path / "dest",
            assume_yes=True, repo_root=tmp_path,
        )


def test_restore_aceita_producao_com_allow_flag(tmp_path, monkeypatch):
    data_dir = _build_synthetic_data_dir(tmp_path / "data")
    out = tmp_path / "out" / "backup.tar.gz"
    backup_data.build_backup(data_dir, out, repo_root=ROOT, quiet=True)

    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    result = restore_data.restore(
        tarball=out, data_dir=tmp_path / "dest",
        assume_yes=True, allow_production=True, repo_root=tmp_path,
    )
    assert result["ok"] is True


def test_restore_arquivo_inexistente(tmp_path):
    with pytest.raises(FileNotFoundError):
        restore_data.restore(
            tarball=tmp_path / "nao_existe.tar.gz",
            data_dir=tmp_path / "dest",
            assume_yes=True, repo_root=tmp_path,
        )

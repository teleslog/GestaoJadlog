"""
Testes dos snapshots temporais SLA.

Cobre:
- _take_sla_snapshot grava JSONL com estrutura esperada
- Dedup por SNAPSHOT_MIN_INTERVAL_S
- force=True bypassa o dedup
- SNAPSHOT_ENABLED=False não grava
- _cleanup_old_snapshots remove arquivos com data antiga
- _read_sla_history lê pontos
- Endpoint /health/sla/history retorna JSON correto
- Filtros op e interval
- Arquivo inexistente → lista vazia
- Linhas corrompidas no JSONL são ignoradas
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main as backend

# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def snap_env(tmp_path, monkeypatch):
    """SNAP_DIR isolado em tmp; reseta last_snapshot_at."""
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir(parents=True)
    monkeypatch.setattr(backend, "SNAP_DIR", snap_dir)
    monkeypatch.setattr(backend, "SNAPSHOT_ENABLED", True)
    monkeypatch.setattr(backend, "SNAPSHOT_MIN_INTERVAL_S", 0)  # default: sem dedup
    monkeypatch.setattr(backend, "SNAPSHOT_RETENTION_DAYS", 30)
    backend._last_snapshot_at = 0.0
    return snap_dir


def _populate_cache(rows_by_op, atualizado=None):
    if atualizado is None:
        atualizado = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for key, rows in rows_by_op.items():
        backend._cache.setdefault("operacional", {})[key] = {
            "key": key, "op": backend.OPS[key][0],
            "opIdx": backend.OPS[key][1], "tipo": "operacional",
            "atualizado": atualizado, "linhas": len(rows),
            "dados": rows, "arquivo": "synthetic.xlsx", "erro": None,
        }


# ── _take_sla_snapshot ──────────────────────────────────────────────────────

def test_snapshot_grava_jsonl_com_estrutura(snap_env, make_row, today):
    _populate_cache({"curvelo": [
        make_row(codigo="A", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
    ]})
    snap = backend._take_sla_snapshot(duration_refresh_ms=12345.0)
    assert snap is not None
    assert "ts" in snap
    assert snap["duration_refresh_ms"] == 12345.0
    assert "global" in snap and "por_operacao" in snap
    # campos esperados
    g = snap["global"]
    assert "sla_percentual" in g
    assert "total_sla" in g
    assert "vencidas" in g and "vencem_hoje" in g
    # por_operacao tem as 4 keys
    assert set(snap["por_operacao"].keys()) == {"gv", "itabira", "jm", "curvelo"}
    # arquivo foi escrito
    files = list(snap_env.glob("sla-*.jsonl"))
    assert len(files) == 1


def test_snapshot_disabled_nao_grava(snap_env, monkeypatch):
    monkeypatch.setattr(backend, "SNAPSHOT_ENABLED", False)
    assert backend._take_sla_snapshot() is None
    assert list(snap_env.glob("*.jsonl")) == []


def test_snapshot_dedup_por_intervalo(snap_env, monkeypatch, make_row, today):
    """Com MIN_INTERVAL=60, dois snapshots seguidos viram 1."""
    monkeypatch.setattr(backend, "SNAPSHOT_MIN_INTERVAL_S", 60)
    _populate_cache({"curvelo": [
        make_row(codigo="A", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
    ]})
    s1 = backend._take_sla_snapshot()
    s2 = backend._take_sla_snapshot()
    assert s1 is not None
    assert s2 is None  # dedup pulou
    files = list(snap_env.glob("sla-*.jsonl"))
    assert len(files) == 1
    # JSONL tem 1 linha só
    lines = files[0].read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1


def test_snapshot_force_bypassa_dedup(snap_env, monkeypatch, make_row, today):
    monkeypatch.setattr(backend, "SNAPSHOT_MIN_INTERVAL_S", 60)
    _populate_cache({"curvelo": [
        make_row(codigo="A", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
    ]})
    assert backend._take_sla_snapshot() is not None
    assert backend._take_sla_snapshot(force=True) is not None  # bypass
    files = list(snap_env.glob("sla-*.jsonl"))
    lines = files[0].read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2


# ── _cleanup_old_snapshots ──────────────────────────────────────────────────

def test_cleanup_remove_arquivos_antigos(snap_env, monkeypatch):
    monkeypatch.setattr(backend, "SNAPSHOT_RETENTION_DAYS", 30)
    today_d = datetime.now(timezone.utc).date()
    # Arquivo de hoje (mantém), 15 dias atrás (mantém), 60 dias atrás (apaga)
    (snap_env / f"sla-{today_d}.jsonl").write_text("{}\n")
    (snap_env / f"sla-{today_d - timedelta(days=15)}.jsonl").write_text("{}\n")
    (snap_env / f"sla-{today_d - timedelta(days=60)}.jsonl").write_text("{}\n")
    # Arquivo malformado também é ignorado (sem date)
    (snap_env / "sla-lixo.jsonl").write_text("{}\n")

    removed = backend._cleanup_old_snapshots()
    assert removed == 1
    remaining = {p.name for p in snap_env.glob("*.jsonl")}
    assert f"sla-{today_d}.jsonl" in remaining
    assert f"sla-{today_d - timedelta(days=15)}.jsonl" in remaining
    assert f"sla-{today_d - timedelta(days=60)}.jsonl" not in remaining
    assert "sla-lixo.jsonl" in remaining  # nome não-parseável é preservado


def test_cleanup_desligado_quando_retention_zero(snap_env, monkeypatch):
    monkeypatch.setattr(backend, "SNAPSHOT_RETENTION_DAYS", 0)
    today_d = datetime.now(timezone.utc).date()
    (snap_env / f"sla-{today_d - timedelta(days=365)}.jsonl").write_text("{}\n")
    assert backend._cleanup_old_snapshots() == 0


# ── _read_sla_history ───────────────────────────────────────────────────────

def test_read_history_arquivo_inexistente(snap_env):
    assert backend._read_sla_history("2099-01-01") == []


def test_read_history_le_global(snap_env, make_row, today):
    _populate_cache({"curvelo": [
        make_row(codigo="A", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
    ]})
    snap = backend._take_sla_snapshot()
    assert snap is not None
    # Derivar a data do próprio snapshot — independente do relógio do CI.
    # (O fixture `today` mocka backend.datetime mas não o `datetime` deste módulo.)
    today_d = snap["ts"][:10]
    points = backend._read_sla_history(today_d)
    assert len(points) == 1
    assert "sla_percentual" in points[0]
    assert "ts" in points[0]


def test_read_history_por_operacao(snap_env, make_row, today):
    _populate_cache({"gv": [
        make_row(codigo="G", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO GOV VALADARES 01",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
    ]})
    snap = backend._take_sla_snapshot(force=True)
    assert snap is not None
    today_d = snap["ts"][:10]
    points_gv = backend._read_sla_history(today_d, op="gv")
    points_jm = backend._read_sla_history(today_d, op="jm")
    assert len(points_gv) == 1
    assert points_gv[0]["total_sla"] == 1
    # jm não tem dados; ainda assim o snapshot tem entry para jm com zeros
    assert len(points_jm) == 1
    assert points_jm[0]["total_sla"] == 0


def test_read_history_linhas_corrompidas_ignoradas(snap_env):
    today_d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = snap_env / f"sla-{today_d}.jsonl"
    # 2 linhas válidas + 1 lixo
    path.write_text(
        '{"ts":"2026-05-15T10:00:00Z","global":{"sla_percentual":95},"por_operacao":{}}\n'
        'lixo invalido\n'
        '{"ts":"2026-05-15T11:00:00Z","global":{"sla_percentual":97},"por_operacao":{}}\n',
        encoding="utf-8",
    )
    points = backend._read_sla_history(today_d)
    assert len(points) == 2
    assert points[0]["sla_percentual"] == 95
    assert points[1]["sla_percentual"] == 97


def test_read_history_interval_amostra(snap_env):
    today_d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = snap_env / f"sla-{today_d}.jsonl"
    # 4 pontos espaçados 10min
    snaps = [
        {"ts": f"2026-05-15T10:{m:02d}:00Z",
         "global": {"sla_percentual": 90 + m},
         "por_operacao": {}}
        for m in (0, 10, 20, 30)
    ]
    path.write_text("\n".join(json.dumps(s) for s in snaps) + "\n", encoding="utf-8")
    # interval=30 → buckets de 30 minutos → deve voltar ~2 pontos
    points = backend._read_sla_history(today_d, interval_min=30)
    assert 1 <= len(points) <= 2


# ── Endpoint /health/sla/history ────────────────────────────────────────────

@pytest.fixture
def client(snap_env, tmp_path, monkeypatch):
    backend.users_lock = asyncio.Lock()
    backend._cache_lock = asyncio.Lock()
    monkeypatch.setattr(backend, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(backend, "JWT_EXPIRE_HOURS", 24 * 365 * 10)
    backend._init_db()
    backend.users_db.clear()
    u = backend.User(id=str(uuid.uuid4()), login="op1", nome="Op",
                     password_hash=backend._hash("senha-123"), role="OPERACIONAL",
                     ativo=True, must_change_password=False)
    backend.users_db[u.id] = u
    backend._upsert_user(u)
    with backend._login_attempts_lock:
        backend._login_attempts.clear()
    return TestClient(backend.app)


def _login(client, login="op1"):
    r = client.post("/auth/login", json={"login": login, "password": "senha-123"},
                    headers={"X-Forwarded-For": "1.2.3.4"})
    return r.json()["access_token"]


def test_endpoint_history_default_hoje(client, snap_env):
    today_d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (snap_env / f"sla-{today_d}.jsonl").write_text(
        '{"ts":"' + today_d + 'T10:00:00Z","global":{"sla_percentual":95,"total_sla":100},"por_operacao":{}}\n',
        encoding="utf-8",
    )
    token = _login(client)
    r = client.get("/health/sla/history", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == today_d
    assert body["count"] == 1
    assert body["points"][0]["sla_percentual"] == 95


def test_endpoint_history_filtro_op(client, snap_env):
    today_d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap = {
        "ts": today_d + "T10:00:00Z",
        "global": {"sla_percentual": 90, "total_sla": 200},
        "por_operacao": {
            "gv": {"sla_percentual": 95, "total_sla": 100},
            "jm": {"sla_percentual": 85, "total_sla": 100},
        },
    }
    (snap_env / f"sla-{today_d}.jsonl").write_text(json.dumps(snap) + "\n", encoding="utf-8")
    token = _login(client)
    r = client.get("/health/sla/history?op=gv",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    pts = r.json()["points"]
    assert len(pts) == 1
    assert pts[0]["sla_percentual"] == 95


def test_endpoint_history_dia_sem_dados(client, snap_env):
    token = _login(client)
    r = client.get("/health/sla/history?date=2099-01-01",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["points"] == []


def test_endpoint_history_exige_auth(client):
    r = client.get("/health/sla/history")
    assert r.status_code in (401, 403)

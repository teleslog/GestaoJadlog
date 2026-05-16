"""
Testes dos health endpoints — funções helper testadas direto.
Sem subir o servidor (sem httpx/TestClient).
"""
from __future__ import annotations

import main as backend


def _populate_cache(rows_by_op: dict[str, list[dict]], atualizado: str = "2026-05-15T20:00:00Z"):
    for key, rows in rows_by_op.items():
        backend._cache.setdefault("operacional", {})[key] = {
            "key": key, "op": backend.OPS[key][0],
            "opIdx": backend.OPS[key][1], "tipo": "operacional",
            "atualizado": atualizado, "linhas": len(rows),
            "dados": rows, "arquivo": "synthetic.xlsx", "erro": None,
        }


# ── /healthz ────────────────────────────────────────────────────────────────

def test_healthz_retorna_ok_e_version(monkeypatch):
    monkeypatch.setattr(backend, "APP_VERSION", "v2026.05.15.test")
    p = backend._healthz_payload()
    assert p["status"] == "ok"
    assert p["version"] == "v2026.05.15.test"
    assert "ts" in p and p["ts"].endswith("Z")


def test_healthz_sem_version_marca_unknown(monkeypatch):
    monkeypatch.setattr(backend, "APP_VERSION", "")
    p = backend._healthz_payload()
    assert p["status"] == "ok"
    assert p["version"] == "unknown"


# ── /readyz ─────────────────────────────────────────────────────────────────

def test_readyz_cache_vazio_not_ready(monkeypatch):
    """Sem cache populado → not_ready."""
    monkeypatch.setattr(backend, "APP_VERSION", "v2026.05.15.test")
    payload, ok = backend._readyz_payload()
    assert ok is False
    assert payload["status"] == "not_ready"
    assert payload["checks"]["cache_loaded"] is False


def test_readyz_cache_populado_ok(monkeypatch, make_row, tmp_path):
    monkeypatch.setattr(backend, "APP_VERSION", "v2026.05.15.test")
    # Aponta DATA_ROOT para tmp_path para passar no check de data_dir_acessivel
    monkeypatch.setattr(backend, "_DATA_ROOT", tmp_path)
    op_dir = tmp_path / "dados" / "operacional"
    op_dir.mkdir(parents=True)
    monkeypatch.setattr(backend, "OP_DIR", op_dir)
    # SQLite em tmp
    monkeypatch.setattr(backend, "DB_PATH", tmp_path / "users.db")
    backend._init_db()

    _populate_cache({
        "gv": [make_row(codigo="X1", status="EM ROTA",
                        dt_evento="15/05/2026 09:00:00",
                        hist_ponto="CO GOV VALADARES 01")],
    })
    payload, ok = backend._readyz_payload()
    assert ok is True
    assert payload["status"] == "ok"
    assert "gv" in payload["checks"]["operacoes_com_dados"]
    assert payload["checks"]["users_db_acessivel"] is True
    assert payload["checks"]["data_dir_acessivel"] is True


def test_readyz_data_dir_inexistente_marca_falha(monkeypatch, tmp_path, make_row):
    monkeypatch.setattr(backend, "APP_VERSION", "v2026.05.15.test")
    monkeypatch.setattr(backend, "_DATA_ROOT", tmp_path / "nao_existe")
    monkeypatch.setattr(backend, "OP_DIR", tmp_path / "nao_existe" / "dados" / "operacional")
    monkeypatch.setattr(backend, "DB_PATH", tmp_path / "users.db")
    backend._init_db()
    _populate_cache({"gv": [make_row(codigo="X", status="EM ROTA",
                                     dt_evento="15/05/2026 09:00:00",
                                     hist_ponto="CO GOV VALADARES 01")]})
    payload, ok = backend._readyz_payload()
    assert ok is False
    assert payload["checks"]["data_dir_acessivel"] is False


# ── /health/sla ─────────────────────────────────────────────────────────────

def test_health_sla_ok_quando_dados_recentes_e_sla_alto(monkeypatch, make_row, today):
    monkeypatch.setattr(backend, "APP_VERSION", "v2026.05.15.test")
    # Atualizado AGORA (idade ~0) — usa o backend.datetime (mockado) para alinhar.
    from datetime import timezone
    mock_now = backend.datetime.now(timezone.utc)
    atualizado = mock_now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Dados: 95 entregues + 5 vencendo hoje → SLA = 95%
    rows = []
    for i in range(95):
        rows.append(make_row(codigo=f"E{i}", status="ENTREGUE",
                             dt_evento="15/05/2026 11:00:00",
                             previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                             **{"__primeira_entrada": "14/05/2026 09:00:00",
                                "__pe_proxy": False}))
    for i in range(5):
        rows.append(make_row(codigo=f"V{i}", status="EM ROTA",
                             dt_evento="15/05/2026 09:00:00",
                             previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                             **{"__primeira_entrada": "14/05/2026 09:00:00",
                                "__pe_proxy": False}))
    _populate_cache({"curvelo": rows}, atualizado=atualizado)
    p = backend._health_sla_payload()
    assert p["status"] == "ok"
    assert p["sla"]["total_sla"] == 100
    assert p["sla"]["entregues_sla"] == 95
    assert p["sla"]["vencem_hoje"] == 5
    assert p["sla"]["inconsistencias"] == 0
    assert p["cache"]["idade_cache_segundos"] is not None
    assert p["cache"]["idade_cache_segundos"] < 60


def test_health_sla_warning_quando_cache_antigo(monkeypatch, make_row, today):
    monkeypatch.setattr(backend, "APP_VERSION", "v2026.05.15.test")
    # Atualizado há 40min → warning
    from datetime import timedelta, timezone
    mock_now = backend.datetime.now(timezone.utc)
    atualizado = (mock_now - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [make_row(codigo="E1", status="ENTREGUE",
                     dt_evento="15/05/2026 11:00:00",
                     previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                     **{"__primeira_entrada": "14/05/2026 09:00:00",
                        "__pe_proxy": False})]
    _populate_cache({"curvelo": rows}, atualizado=atualizado)
    p = backend._health_sla_payload()
    assert p["status"] == "warning"


def test_health_sla_critical_quando_cache_muito_antigo(monkeypatch, make_row, today):
    monkeypatch.setattr(backend, "APP_VERSION", "v2026.05.15.test")
    from datetime import timedelta, timezone
    mock_now = backend.datetime.now(timezone.utc)
    atualizado = (mock_now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [make_row(codigo="E1", status="ENTREGUE",
                     dt_evento="15/05/2026 11:00:00",
                     previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                     **{"__primeira_entrada": "14/05/2026 09:00:00",
                        "__pe_proxy": False})]
    _populate_cache({"curvelo": rows}, atualizado=atualizado)
    p = backend._health_sla_payload()
    assert p["status"] == "critical"


def test_health_sla_critical_quando_sla_baixo(monkeypatch, make_row, today):
    monkeypatch.setattr(backend, "APP_VERSION", "v2026.05.15.test")
    from datetime import timezone
    mock_now = backend.datetime.now(timezone.utc)
    atualizado = mock_now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # 5 entregues + 95 vencidas → SLA = 5% → critical
    rows = []
    for i in range(5):
        rows.append(make_row(codigo=f"E{i}", status="ENTREGUE",
                             dt_evento="15/05/2026 11:00:00",
                             previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                             **{"__primeira_entrada": "14/05/2026 09:00:00",
                                "__pe_proxy": False}))
    for i in range(95):
        rows.append(make_row(codigo=f"V{i}", status="EM ROTA",
                             dt_evento="14/05/2026 09:00:00",
                             previsao="14/05/2026", hist_ponto="CO CURVELO 02",
                             **{"__primeira_entrada": "14/05/2026 09:00:00",
                                "__pe_proxy": False}))
    _populate_cache({"curvelo": rows}, atualizado=atualizado)
    p = backend._health_sla_payload()
    assert p["status"] == "critical"
    assert p["sla"]["sla_percentual"] < 85


def test_health_sla_critical_quando_cache_vazio(monkeypatch, today):
    monkeypatch.setattr(backend, "APP_VERSION", "v2026.05.15.test")
    # Sem _populate_cache → cache vazio
    p = backend._health_sla_payload()
    assert p["status"] == "critical"
    assert p["sla"]["total_sla"] == 0


def test_classify_sla_status_inconsistencia_e_critical():
    # Mesmo com SLA alto e cache fresco, qualquer inconsistência = critical
    assert backend._classify_sla_status(sla_pct=99.0, idade_segundos=10,
                                         inconsistencias=1, total_sla=100) == "critical"


def test_classify_sla_status_sla_baixo_e_critical():
    assert backend._classify_sla_status(sla_pct=80.0, idade_segundos=10,
                                         inconsistencias=0, total_sla=100) == "critical"


def test_classify_sla_status_warning_por_idade():
    assert backend._classify_sla_status(sla_pct=98.0, idade_segundos=2000,
                                         inconsistencias=0, total_sla=100) == "warning"


def test_classify_sla_status_warning_por_sla():
    assert backend._classify_sla_status(sla_pct=92.0, idade_segundos=10,
                                         inconsistencias=0, total_sla=100) == "warning"


def test_classify_sla_status_ok():
    assert backend._classify_sla_status(sla_pct=98.0, idade_segundos=10,
                                         inconsistencias=0, total_sla=100) == "ok"

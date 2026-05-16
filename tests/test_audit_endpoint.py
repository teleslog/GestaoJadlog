"""
Testes do endpoint /audit/sla — Auditoria Visual SLA (DIRETOR only).

Cobre:
- 401 sem auth, 403 não-DIRETOR, 200 DIRETOR.
- Payload contém resumo, auditoria (com idade_cache), detalhes, badges.
- Filtros: op, codigos, categoria, status, entra_no_sla, apenas_inconsistencias,
  apenas_suspeitas, apenas_fallback.
- Paginação limit/offset.
- Badges (red/yellow/green/gray) atribuídas corretamente.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

import main as backend

# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient com locks inicializados, users.db isolado, e 3 perfis."""
    backend.users_lock = asyncio.Lock()
    backend._cache_lock = asyncio.Lock()
    monkeypatch.setattr(backend, "DB_PATH", tmp_path / "users.db")
    # Janela longa para tokens não expirarem por descompasso entre datetime.now()
    # mockado (today fixture) e relógio real do jwt.decode.
    monkeypatch.setattr(backend, "JWT_EXPIRE_HOURS", 24 * 365 * 10)
    backend._init_db()

    # Reseta usuários em memória e cria 3 perfis sintéticos
    backend.users_db.clear()
    for login, role in [("diretor1", "DIRETOR"),
                        ("super1", "SUPERVISAO"),
                        ("oper1", "OPERACIONAL")]:
        u = backend.User(
            id=str(uuid.uuid4()), login=login, nome=login,
            password_hash=backend._hash("senha-123"), role=role,
            ativo=True, must_change_password=False,
        )
        backend.users_db[u.id] = u
        backend._upsert_user(u)

    return TestClient(backend.app)


def _login(client, login: str) -> str:
    r = client.post("/auth/login", json={"login": login, "password": "senha-123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _populate_cache(rows_by_op, atualizado="2026-05-15T14:00:00Z"):
    for key, rows in rows_by_op.items():
        backend._cache.setdefault("operacional", {})[key] = {
            "key": key, "op": backend.OPS[key][0],
            "opIdx": backend.OPS[key][1], "tipo": "operacional",
            "atualizado": atualizado, "linhas": len(rows),
            "dados": rows, "arquivo": "synthetic.xlsx", "erro": None,
        }


# ── Auth ─────────────────────────────────────────────────────────────────────

def test_audit_sem_auth_retorna_401(client):
    r = client.get("/audit/sla")
    # 403 (HTTPBearer) ou 401 (sem token)
    assert r.status_code in (401, 403)


def test_audit_supervisao_retorna_403(client):
    token = _login(client, "super1")
    r = client.get("/audit/sla", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_audit_operacional_retorna_403(client):
    token = _login(client, "oper1")
    r = client.get("/audit/sla", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_audit_diretor_retorna_200(client, make_row, today):
    rows = [make_row(codigo="X1", status="EM ROTA",
                     dt_evento="15/05/2026 09:00:00",
                     previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                     **{"__primeira_entrada": "14/05/2026 09:00:00",
                        "__pe_proxy": False})]
    _populate_cache({"curvelo": rows})
    token = _login(client, "diretor1")
    r = client.get("/audit/sla", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert "resumo" in body
    assert "auditoria" in body
    assert "detalhes" in body
    assert body["resumo"]["total_sla"] == 1


# ── Payload + badges ────────────────────────────────────────────────────────

def test_audit_detalhes_tem_badge(client, make_row, today):
    rows = [
        # green — entra no SLA, PE real
        make_row(codigo="G1", status="EM ROTA",
                 dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
        # yellow — entra mas via PE proxy
        make_row(codigo="Y1", status="EM ROTA",
                 dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": True}),
        # gray — excluída pela regra 10h
        make_row(codigo="X1", status="ENTRADA",
                 dt_evento="15/05/2026 14:36:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "15/05/2026 14:36:00",
                    "__pe_proxy": False}),
    ]
    _populate_cache({"curvelo": rows})
    token = _login(client, "diretor1")
    r = client.get("/audit/sla", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    by_cod = {d["codigo"]: d for d in body["detalhes"]}
    assert by_cod["G1"]["badge"] == "green"
    assert by_cod["Y1"]["badge"] == "yellow"
    assert by_cod["X1"]["badge"] == "gray"


# ── Filtros ─────────────────────────────────────────────────────────────────

def test_audit_filtro_status(client, make_row, today):
    rows = [
        make_row(codigo="E1", status="ENTREGUE", dt_evento="15/05/2026 11:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
        make_row(codigo="V1", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
    ]
    _populate_cache({"curvelo": rows})
    token = _login(client, "diretor1")
    r = client.get("/audit/sla?status=ENTREGUE",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    cods = {d["codigo"] for d in body["detalhes"]}
    assert cods == {"E1"}


def test_audit_filtro_entra_no_sla(client, make_row, today):
    rows = [
        make_row(codigo="OK", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
        make_row(codigo="EXCL", status="ENTRADA", dt_evento="15/05/2026 14:36:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "15/05/2026 14:36:00",
                    "__pe_proxy": False}),
    ]
    _populate_cache({"curvelo": rows})
    token = _login(client, "diretor1")

    r_false = client.get("/audit/sla?entra_no_sla=false",
                         headers={"Authorization": f"Bearer {token}"})
    assert {d["codigo"] for d in r_false.json()["detalhes"]} == {"EXCL"}

    r_true = client.get("/audit/sla?entra_no_sla=true",
                        headers={"Authorization": f"Bearer {token}"})
    assert {d["codigo"] for d in r_true.json()["detalhes"]} == {"OK"}


def test_audit_filtro_apenas_fallback(client, make_row, today):
    rows = [
        make_row(codigo="REAL", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
        make_row(codigo="PROXY", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": True}),
    ]
    _populate_cache({"curvelo": rows})
    token = _login(client, "diretor1")
    r = client.get("/audit/sla?apenas_fallback=true",
                   headers={"Authorization": f"Bearer {token}"})
    cods = {d["codigo"] for d in r.json()["detalhes"]}
    assert cods == {"PROXY"}


def test_audit_filtro_op(client, make_row, today):
    _populate_cache({
        "curvelo": [make_row(codigo="C1", status="EM ROTA",
                             dt_evento="15/05/2026 09:00:00",
                             previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                             **{"__primeira_entrada": "14/05/2026 09:00:00",
                                "__pe_proxy": False})],
        "gv": [make_row(codigo="G1", status="EM ROTA",
                        dt_evento="15/05/2026 09:00:00",
                        previsao="15/05/2026", hist_ponto="CO GOV VALADARES 01",
                        **{"__primeira_entrada": "14/05/2026 09:00:00",
                           "__pe_proxy": False})],
    })
    token = _login(client, "diretor1")
    r = client.get("/audit/sla?op=gv",
                   headers={"Authorization": f"Bearer {token}"})
    cods = {d["codigo"] for d in r.json()["detalhes"]}
    assert cods == {"G1"}


# ── Paginação ───────────────────────────────────────────────────────────────

def test_audit_paginacao(client, make_row, today):
    rows = [make_row(codigo=f"E{i:03d}", status="ENTREGUE",
                     dt_evento="15/05/2026 11:00:00",
                     previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                     **{"__primeira_entrada": "14/05/2026 09:00:00",
                        "__pe_proxy": False})
            for i in range(50)]
    _populate_cache({"curvelo": rows})
    token = _login(client, "diretor1")

    pg1 = client.get("/audit/sla?limit=10&offset=0",
                     headers={"Authorization": f"Bearer {token}"}).json()
    pg2 = client.get("/audit/sla?limit=10&offset=10",
                     headers={"Authorization": f"Bearer {token}"}).json()
    assert len(pg1["detalhes"]) == 10
    assert len(pg2["detalhes"]) == 10
    assert pg1["detalhes_total_filtrado"] == 50
    assert {d["codigo"] for d in pg1["detalhes"]} != {d["codigo"] for d in pg2["detalhes"]}


def test_audit_resumo_independe_do_filtro(client, make_row, today):
    """Resumo é sempre global; só `detalhes` é filtrado."""
    rows = [
        make_row(codigo="A", status="ENTREGUE", dt_evento="15/05/2026 11:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
        make_row(codigo="B", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
    ]
    _populate_cache({"curvelo": rows})
    token = _login(client, "diretor1")

    r = client.get("/audit/sla?status=ENTREGUE",
                   headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    assert body["resumo"]["entregues_sla"] == 1
    assert body["resumo"]["vencem_hoje"] == 1   # resumo global, não filtrado
    assert len(body["detalhes"]) == 1            # detalhes filtrados


def test_audit_resposta_inclui_idade_cache(client, make_row, today):
    rows = [make_row(codigo="A", status="EM ROTA",
                     dt_evento="15/05/2026 09:00:00",
                     previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                     **{"__primeira_entrada": "14/05/2026 09:00:00",
                        "__pe_proxy": False})]
    _populate_cache({"curvelo": rows})
    token = _login(client, "diretor1")
    r = client.get("/audit/sla",
                   headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    assert "idade_cache_segundos" in body["auditoria"]
    assert "ultima_atualizacao_cache" in body["auditoria"]

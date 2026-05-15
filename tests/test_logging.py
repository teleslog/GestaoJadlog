"""
Testes de logging estruturado + request_id.

Cobre:
- ContextVars (request_id, user_login, user_role).
- Filter injetando contextvars no LogRecord.
- TextFormatter e JsonFormatter.
- Middleware HTTP via TestClient: request_id gerado/recebido, header devolvido.
- Não logar dados sensíveis (senha).
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

import main as backend

# ── ContextVars + Filter ────────────────────────────────────────────────────

def _make_record(msg="hello"):
    return logging.LogRecord(
        name="api", level=logging.INFO, pathname=__file__,
        lineno=1, msg=msg, args=(), exc_info=None,
    )


def test_filter_injeta_contextvars(monkeypatch):
    backend._request_id_ctx.set("rid-abc")
    backend._user_login_ctx.set("ana")
    backend._user_role_ctx.set("DIRETOR")
    f = backend._ContextFilter()
    rec = _make_record()
    assert f.filter(rec) is True
    assert rec.request_id == "rid-abc"
    assert rec.user_login == "ana"
    assert rec.user_role == "DIRETOR"


def test_text_formatter_inclui_request_id_e_user():
    backend._request_id_ctx.set("rid-xyz")
    backend._user_login_ctx.set("bob")
    rec = _make_record("msg de teste")
    backend._ContextFilter().filter(rec)
    out = backend._TextFormatter().format(rec)
    assert "rid-xyz" in out
    assert "bob" in out
    assert "msg de teste" in out
    assert "INFO" in out


def test_json_formatter_produz_json_valido():
    backend._request_id_ctx.set("rid-1")
    backend._user_login_ctx.set("supervisor")
    backend._user_role_ctx.set("SUPERVISAO")
    rec = _make_record("evento qualquer")
    # Simula extra={}
    rec.event = "upload_started"
    rec.tipo = "operacional"
    rec.arquivo = "perf.xlsx"
    backend._ContextFilter().filter(rec)
    out = backend._JsonFormatter().format(rec)
    parsed = json.loads(out)
    assert parsed["msg"] == "evento qualquer"
    assert parsed["request_id"] == "rid-1"
    assert parsed["user"] == "supervisor"
    assert parsed["role"] == "SUPERVISAO"
    assert parsed["level"] == "INFO"
    assert parsed["event"] == "upload_started"
    assert parsed["tipo"] == "operacional"
    assert parsed["arquivo"] == "perf.xlsx"


def test_contextvars_isolam_entre_coroutines():
    """Cada call a `set()` no contexto de uma task não vaza pra outra."""
    import asyncio

    async def task(rid: str) -> str:
        backend._request_id_ctx.set(rid)
        await asyncio.sleep(0)
        return backend._request_id_ctx.get()

    async def runner():
        a, b = await asyncio.gather(task("A"), task("B"))
        return a, b

    a, b = asyncio.run(runner())
    assert a == "A"
    assert b == "B"


# ── Middleware via TestClient ───────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient com locks inicializados e users.db isolado em tmp_path.
    Não roda refresh_all (caro). Cache vazio é OK para testar middleware."""
    import asyncio
    backend.users_lock = asyncio.Lock()
    backend._cache_lock = asyncio.Lock()
    monkeypatch.setattr(backend, "DB_PATH", tmp_path / "users.db")
    backend._init_db()
    return TestClient(backend.app)


def test_middleware_devolve_x_request_id_quando_gera(client, caplog):
    """Sem header X-Request-ID na requisição → middleware gera um."""
    with caplog.at_level(logging.INFO, logger="api"):
        r = client.get("/healthz")
    assert r.status_code == 200
    assert "x-request-id" in {k.lower() for k in r.headers.keys()}
    rid = r.headers["x-request-id"]
    assert len(rid) == 12  # uuid4().hex[:12]


def test_middleware_preserva_x_request_id_quando_recebido(client):
    r = client.get("/healthz", headers={"X-Request-ID": "client-supplied"})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "client-supplied"


def test_middleware_loga_request_com_status_e_duracao(client, caplog):
    with caplog.at_level(logging.INFO, logger="api"):
        client.get("/diag/sla")  # 400 (mode/codigos faltando) ou 401 (sem auth)
    # Qualquer status: o middleware loga
    log_msgs = [rec.getMessage() for rec in caplog.records if "/diag/sla" in rec.getMessage()]
    assert any(
        rec.getMessage().startswith("GET /diag/sla")
        and getattr(rec, "event", None) == "http_request"
        for rec in caplog.records
    ), f"esperava log http_request, recebido: {log_msgs}"


def test_middleware_nao_loga_healthz_para_evitar_poluicao(client, caplog):
    with caplog.at_level(logging.INFO, logger="api"):
        client.get("/healthz")
    msgs = [r.getMessage() for r in caplog.records
            if getattr(r, "event", None) == "http_request"]
    assert all("/healthz" not in m for m in msgs)


def test_login_falho_nao_loga_senha(client, caplog):
    """Cobertura defensiva: login com senha errada não pode vazar a senha."""
    with caplog.at_level(logging.WARNING, logger="api"):
        r = client.post("/auth/login", json={"login": "nao-existe", "password": "minha-senha-secreta-XYZ"})
    assert r.status_code == 401
    for rec in caplog.records:
        msg = rec.getMessage()
        assert "minha-senha-secreta-XYZ" not in msg
        # Atributos extras também não devem vazar a senha
        for _k, v in vars(rec).items():
            assert "minha-senha-secreta-XYZ" not in str(v)

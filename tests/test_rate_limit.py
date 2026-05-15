"""
Testes do rate-limit do /auth/login.

Cobre:
- Login normal continua passando dentro do limite.
- Após N falhas no mesmo IP, retorna 429 com Retry-After.
- Sucesso limpa o contador (não pune usuário legítimo).
- Janela deslizante: após expirar, libera.
- IPs diferentes não compartilham contador (proxy x forwarded-for).
- Configuração via env (RATE_LIMIT_LOGIN_ATTEMPTS / WINDOW).
- Atraso anti-brute-force em login inválido (>=200ms).
- Nunca loga senha.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import main as backend

# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient com locks/db isolados + usuário diretor + rate-limit limpo."""
    backend.users_lock = asyncio.Lock()
    backend._cache_lock = asyncio.Lock()
    monkeypatch.setattr(backend, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(backend, "JWT_EXPIRE_HOURS", 24 * 365 * 10)
    backend._init_db()
    backend.users_db.clear()

    u = backend.User(
        id=str(uuid.uuid4()), login="ana", nome="Ana",
        password_hash=backend._hash("senha-123"), role="DIRETOR",
        ativo=True, must_change_password=False,
    )
    backend.users_db[u.id] = u
    backend._upsert_user(u)

    # Reseta contador de rate-limit entre testes.
    with backend._login_attempts_lock:
        backend._login_attempts.clear()

    return TestClient(backend.app)


def _post_login(client, login: str, senha: str, ip: str = "1.2.3.4"):
    """POST /auth/login com X-Forwarded-For controlado."""
    return client.post("/auth/login",
                       json={"login": login, "password": senha},
                       headers={"X-Forwarded-For": ip})


# ── Normal ──────────────────────────────────────────────────────────────────

def test_login_normal_sucesso_dentro_do_limite(client):
    r = _post_login(client, "ana", "senha-123")
    assert r.status_code == 200
    assert "access_token" in r.json()


def test_4_falhas_em_seguida_ainda_permite_5a_tentativa(client):
    """Limite default = 5. As 5 primeiras tentativas (mesmo erradas) recebem 401, não 429."""
    for _ in range(4):
        r = _post_login(client, "ana", "errada")
        assert r.status_code == 401
    # 5ª tentativa ainda passa pelo rate-limit
    r5 = _post_login(client, "ana", "errada")
    assert r5.status_code == 401


# ── Bloqueio ────────────────────────────────────────────────────────────────

def test_6a_tentativa_retorna_429_com_retry_after(client):
    """Após 5 falhas, a 6ª deve ser bloqueada com 429 + Retry-After."""
    for _ in range(5):
        _post_login(client, "ana", "errada")
    r = _post_login(client, "ana", "errada")
    assert r.status_code == 429
    body = r.json()
    assert body["error"] == "rate_limited"
    assert "retry_after_s" in body
    assert body["retry_after_s"] > 0
    assert "Retry-After" in r.headers
    assert int(r.headers["Retry-After"]) > 0


def test_429_bloqueia_mesmo_senha_certa(client):
    """Depois de bater o limite, senha correta ainda recebe 429.
    (rate-limit é checado ANTES da verificação de credenciais.)"""
    for _ in range(5):
        _post_login(client, "ana", "errada")
    r = _post_login(client, "ana", "senha-123")
    assert r.status_code == 429


def test_sucesso_limpa_contador(client):
    """4 falhas + 1 sucesso → contador zera para esse IP."""
    for _ in range(4):
        _post_login(client, "ana", "errada")
    r_ok = _post_login(client, "ana", "senha-123")
    assert r_ok.status_code == 200
    # Após sucesso, deve poder errar 5x novamente antes de 429
    for _ in range(5):
        r = _post_login(client, "ana", "errada")
        assert r.status_code == 401


def test_ips_diferentes_nao_compartilham_contador(client):
    """IP A bate 5 falhas → IP B não é afetado."""
    for _ in range(5):
        _post_login(client, "ana", "errada", ip="10.0.0.1")
    r_a = _post_login(client, "ana", "errada", ip="10.0.0.1")
    r_b = _post_login(client, "ana", "errada", ip="10.0.0.2")
    assert r_a.status_code == 429
    assert r_b.status_code == 401


# ── Janela deslizante ────────────────────────────────────────────────────────

def test_janela_libera_apos_expirar(client, monkeypatch):
    """Janela = X segundos; depois de bater o limite, esperar > X libera."""
    # Cada falha leva ~700ms (bcrypt + delay anti-brute-force); janela de 10s
    # cobre as 5 falhas, mas espera de 11s força expiração de TODOS os timestamps.
    monkeypatch.setattr(backend, "RATE_LIMIT_LOGIN_WINDOW", 10)
    with backend._login_attempts_lock:
        backend._login_attempts.clear()
    for _ in range(5):
        _post_login(client, "ana", "errada")
    assert _post_login(client, "ana", "errada").status_code == 429
    # Espera a janela toda expirar
    time.sleep(11)
    r = _post_login(client, "ana", "errada")
    assert r.status_code == 401


# ── Configuração ─────────────────────────────────────────────────────────────

def test_rate_limit_desligado_quando_attempts_zero(client, monkeypatch):
    monkeypatch.setattr(backend, "RATE_LIMIT_LOGIN_ATTEMPTS", 0)
    with backend._login_attempts_lock:
        backend._login_attempts.clear()
    # 20 falhas seguidas, todas 401 (não 429)
    for _ in range(20):
        assert _post_login(client, "ana", "errada").status_code == 401


def test_rate_limit_limite_configuravel_attempts_2(client, monkeypatch):
    monkeypatch.setattr(backend, "RATE_LIMIT_LOGIN_ATTEMPTS", 2)
    with backend._login_attempts_lock:
        backend._login_attempts.clear()
    assert _post_login(client, "ana", "errada").status_code == 401
    assert _post_login(client, "ana", "errada").status_code == 401
    assert _post_login(client, "ana", "errada").status_code == 429


# ── Atraso anti-brute-force ─────────────────────────────────────────────────

def test_login_falho_tem_atraso(client):
    """Login inválido deve levar pelo menos 200ms (atraso 300-500ms)."""
    t0 = time.perf_counter()
    r = _post_login(client, "ana", "errada")
    elapsed = time.perf_counter() - t0
    assert r.status_code == 401
    assert elapsed >= 0.2, f"Atraso muito curto: {elapsed:.3f}s"


def test_login_sucesso_nao_tem_atraso_significativo(client):
    """Login ok não pune com delay (não é punição)."""
    t0 = time.perf_counter()
    r = _post_login(client, "ana", "senha-123")
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    # bcrypt + JWT custam ~100ms cada; sem o delay forçado deve ficar <0.6s
    assert elapsed < 1.0, f"Login sucesso lento: {elapsed:.3f}s"


# ── Logs ─────────────────────────────────────────────────────────────────────

def test_log_rate_limited_inclui_ip_e_retry_after(client, caplog):
    for _ in range(5):
        _post_login(client, "ana", "errada", ip="9.9.9.9")
    with caplog.at_level(logging.WARNING, logger="api"):
        _post_login(client, "ana", "errada", ip="9.9.9.9")
    eventos = [r for r in caplog.records
               if getattr(r, "event", None) == "login_rate_limited"]
    assert len(eventos) == 1
    rec = eventos[0]
    assert getattr(rec, "ip", None) == "9.9.9.9"
    assert getattr(rec, "retry_after_s", 0) > 0


def test_log_login_failed_inclui_ip(client, caplog):
    with caplog.at_level(logging.WARNING, logger="api"):
        _post_login(client, "ana", "errada", ip="7.7.7.7")
    fails = [r for r in caplog.records
             if getattr(r, "event", None) == "login_failed"]
    assert len(fails) == 1
    assert getattr(fails[0], "ip", None) == "7.7.7.7"


def test_log_nunca_inclui_senha_no_rate_limited(client, caplog):
    """Defensivo: nenhum log (sucesso, falha, rate-limit) pode conter a senha."""
    senha_secreta = "PASSWORD-MUITO-SECRETA-XYZ-9999"
    with caplog.at_level(logging.INFO, logger="api"):
        for _ in range(7):
            _post_login(client, "ana", senha_secreta)
    for rec in caplog.records:
        msg = rec.getMessage()
        assert senha_secreta not in msg
        for _k, v in vars(rec).items():
            assert senha_secreta not in str(v)

"""
Fixtures comuns para todos os testes.

Princípios:
- Importa `main` como módulo (sem subir o servidor).
- "Hoje" é sempre fixo: 2026-05-15 14:00 (depois das 10h, para exercitar a regra).
  Testes que precisam alterar essa data usam o fixture `fake_today` parametrizado.
- Cache global é resetado entre testes.
- Sem I/O em disco real: xlsx sintéticos são gerados em memória ou em tmp_path.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main as backend  # noqa: E402

# ── Today fixo ───────────────────────────────────────────────────────────────

# 15/05/2026 — `today` é sempre "começo do dia" (00:00). A hora relevante para
# a regra das 10h vem da PE/DtEvento das linhas, não de `today`.
DEFAULT_TODAY = datetime(2026, 5, 15, 0, 0, 0)


@pytest.fixture
def today(monkeypatch):
    """Trava datetime.now() em main.py para DEFAULT_TODAY."""
    return _patch_today(monkeypatch, DEFAULT_TODAY)


@pytest.fixture
def fake_today(monkeypatch):
    """Versão parametrizada: chame `fake_today(datetime(...))` no teste."""
    def _apply(dt: datetime) -> datetime:
        return _patch_today(monkeypatch, dt)
    return _apply


def _patch_today(monkeypatch, dt: datetime) -> datetime:
    real_dt = backend.datetime

    class _FakeDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return dt if tz is None else dt.replace(tzinfo=tz)

    monkeypatch.setattr(backend, "datetime", _FakeDT)
    return dt


# ── Cache limpo entre testes ─────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_cache():
    """Garante que cada teste começa com `_cache` vazio."""
    saved = {tipo: dict(ops) for tipo, ops in backend._cache.items()}
    backend._cache = {t: {} for t in backend.TIPOS}
    yield
    backend._cache = saved


@pytest.fixture
def cache_lock():
    """Locks como o main.py inicializaria no startup."""
    backend._cache_lock = asyncio.Lock()
    backend.users_lock = asyncio.Lock()
    return backend._cache_lock


# ── Helpers exportados ───────────────────────────────────────────────────────

@pytest.fixture
def make_row():
    """Factory de uma linha do xlsx (dict)."""
    from .helpers import make_row as _f
    return _f


@pytest.fixture
def write_xlsx(tmp_path):
    """Escreve um xlsx com cabeçalho compatível com _read_xlsx em tmp_path."""
    from .helpers import write_xlsx as _f
    def _wrapper(rows, name="snapshot.xlsx"):
        return _f(tmp_path / name, rows)
    return _wrapper

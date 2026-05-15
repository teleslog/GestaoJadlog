"""
Testes de merge_incremental — upload incremental no cache.

Cobre:
- Novo código entra com PE correta (real ou proxy).
- Atualização preserva PE existente quando relevante.
- PE proxy nunca sobrescreve PE real anterior.
- Filtro Hist=nossa op no proxy.
- "Cache never-shrinks": atualização não apaga códigos.
"""
from __future__ import annotations

import asyncio

import pytest

import main as backend


@pytest.fixture
def setup_cache(cache_lock):
    """Garante locks e cache vazio."""
    return backend


def _run(coro):
    """Wrapper sync para chamar coroutines do main.py em testes simples."""
    return asyncio.get_event_loop().run_until_complete(coro) if asyncio.get_event_loop().is_running() else asyncio.run(coro)


def test_novo_codigo_status_entrada_apos_10h_recebe_pe_real(setup_cache, write_xlsx, make_row, today):
    """ENTRADA hoje >=10h → PE_real=hoje >=10h, __pe_proxy=False, deve EXCLUIR do SLA."""
    path = write_xlsx([
        make_row(codigo="N1", status="ENTRADA", dt_evento="15/05/2026 14:36:00",
                 hist_ponto="CO CURVELO 02", previsao="15/05/2026"),
    ])
    asyncio.run(backend.merge_incremental("operacional", "curvelo", path))
    rows = backend._cache["operacional"]["curvelo"]["dados"]
    n1 = {r["Codigo"]: r for r in rows}["N1"]
    assert n1["__primeira_entrada"] == "15/05/2026 14:36:00"
    assert n1["__pe_proxy"] is False


def test_novo_codigo_status_pendente_outra_torre_recebe_pe_vazia(setup_cache, write_xlsx, make_row, today):
    """
    Bug 879a787: código que aparece só com Hist=outra torre não recebe PE proxy
    daquela data — sem evidência de presença na nossa op.
    """
    path = write_xlsx([
        make_row(codigo="N1", status="TRANSFERENCIA", dt_evento="14/05/2026 12:00:00",
                 hist_ponto="TC TECA MATRIZ", previsao="15/05/2026"),
    ])
    asyncio.run(backend.merge_incremental("operacional", "curvelo", path))
    rows = backend._cache["operacional"]["curvelo"]["dados"]
    n1 = {r["Codigo"]: r for r in rows}["N1"]
    assert n1.get("__primeira_entrada", "") == ""


def test_re_upload_nao_recontamina_cache(setup_cache, write_xlsx, make_row, today):
    """
    Sequência: A1 entrou ontem na nossa torre (PE_real=ontem 09h). Hoje uploadamos
    novamente o mesmo arquivo (sem novidade). A PE não pode regredir nem mudar.
    """
    # Estado inicial: cache populado via primeiro upload
    path1 = write_xlsx([
        make_row(codigo="A1", status="ENTRADA", dt_evento="14/05/2026 09:00:00",
                 hist_ponto="CO CURVELO 02", previsao="15/05/2026"),
    ], name="s1.xlsx")
    asyncio.run(backend.merge_incremental("operacional", "curvelo", path1))
    pe_inicial = {r["Codigo"]: r["__primeira_entrada"]
                  for r in backend._cache["operacional"]["curvelo"]["dados"]}["A1"]
    assert pe_inicial == "14/05/2026 09:00:00"

    # Re-upload do mesmo arquivo (mesmo conteúdo, mesma PE)
    asyncio.run(backend.merge_incremental("operacional", "curvelo", path1))
    pe_final = {r["Codigo"]: r["__primeira_entrada"]
                for r in backend._cache["operacional"]["curvelo"]["dados"]}["A1"]
    assert pe_final == "14/05/2026 09:00:00"


def test_atualizacao_preserva_pe_real_antiga(setup_cache, write_xlsx, make_row, today):
    """
    Sequência:
      Snapshot 1: A1 ENTRADA na nossa torre 14/05 09:00 → PE_real = 14/05 09:00
      Snapshot 2: A1 EM ROTA na nossa torre 15/05 11:00 (sem ENTRADA na linha)
                  → PE deve permanecer 14/05 09:00 (real).
    """
    s1 = write_xlsx([
        make_row(codigo="A1", status="ENTRADA", dt_evento="14/05/2026 09:00:00",
                 hist_ponto="CO CURVELO 02", previsao="15/05/2026"),
    ], name="s1.xlsx")
    s2 = write_xlsx([
        make_row(codigo="A1", status="EM ROTA", dt_evento="15/05/2026 11:00:00",
                 hist_ponto="CO CURVELO 02", previsao="15/05/2026"),
    ], name="s2.xlsx")
    asyncio.run(backend.merge_incremental("operacional", "curvelo", s1))
    asyncio.run(backend.merge_incremental("operacional", "curvelo", s2))
    a1 = {r["Codigo"]: r for r in backend._cache["operacional"]["curvelo"]["dados"]}["A1"]
    assert a1["Status"] == "EM ROTA"
    assert a1["__primeira_entrada"] == "14/05/2026 09:00:00"
    assert a1["__pe_proxy"] is False


def test_pe_real_substitui_pe_proxy_quando_aparece(setup_cache, write_xlsx, make_row, today):
    """
    Sequência:
      Snapshot 1: A1 EM ROTA Hist=GV 14/05 16:00 → PE_proxy=14/05 16:00 (sem ENTRADA real)
      Snapshot 2: A1 ENTRADA Hist=GV 14/05 09:00 → PE_real=14/05 09:00 substitui proxy.
    """
    s1 = write_xlsx([
        make_row(codigo="A1", status="EM ROTA", dt_evento="14/05/2026 16:00:00",
                 hist_ponto="CO GOV VALADARES 01", previsao="15/05/2026"),
    ], name="s1.xlsx")
    s2 = write_xlsx([
        make_row(codigo="A1", status="ENTRADA", dt_evento="14/05/2026 09:00:00",
                 hist_ponto="CO GOV VALADARES 01", previsao="15/05/2026"),
    ], name="s2.xlsx")
    asyncio.run(backend.merge_incremental("operacional", "gv", s1))
    a1_pos_s1 = {r["Codigo"]: r for r in backend._cache["operacional"]["gv"]["dados"]}["A1"]
    assert a1_pos_s1["__pe_proxy"] is True

    asyncio.run(backend.merge_incremental("operacional", "gv", s2))
    a1_pos_s2 = {r["Codigo"]: r for r in backend._cache["operacional"]["gv"]["dados"]}["A1"]
    assert a1_pos_s2["__primeira_entrada"] == "14/05/2026 09:00:00"
    assert a1_pos_s2["__pe_proxy"] is False


def test_cache_never_shrinks(setup_cache, write_xlsx, make_row, today):
    """Códigos que somem do snapshot novo permanecem no cache."""
    s1 = write_xlsx([
        make_row(codigo="A1", status="EM ROTA", dt_evento="14/05/2026 09:00:00",
                 hist_ponto="CO CURVELO 02"),
        make_row(codigo="A2", status="EM ROTA", dt_evento="14/05/2026 10:00:00",
                 hist_ponto="CO CURVELO 02"),
    ], name="s1.xlsx")
    s2 = write_xlsx([
        make_row(codigo="A1", status="ENTREGUE", dt_evento="15/05/2026 11:00:00",
                 hist_ponto="CO CURVELO 02"),
    ], name="s2.xlsx")
    asyncio.run(backend.merge_incremental("operacional", "curvelo", s1))
    asyncio.run(backend.merge_incremental("operacional", "curvelo", s2))
    by = {r["Codigo"]: r for r in backend._cache["operacional"]["curvelo"]["dados"]}
    assert "A1" in by and "A2" in by
    assert by["A1"]["Status"] == "ENTREGUE"
    assert by["A2"]["Status"] == "EM ROTA"


@pytest.mark.regression
def test_regression_22_codigos_entrada_hoje_apos_10h(setup_cache, write_xlsx, make_row, today):
    """
    Caso do incidente original: 22 códigos chegando como Status=ENTRADA hoje 14:36
    na nossa torre, sem histórico nosso. PE_real deve ser hoje 14:36, regra das 10h
    deve EXCLUIR. Cobertura representativa com 3 dos códigos reais.
    """
    rows = [
        make_row(codigo=cod, status="ENTRADA",
                 dt_evento="15/05/2026 14:36:00",
                 hist_ponto="CO GOV VALADARES 01",
                 previsao="20/05/2026")
        for cod in ("18159608537507", "18121604619044", "18121604625987")
    ]
    path = write_xlsx(rows)
    asyncio.run(backend.merge_incremental("operacional", "gv", path))

    cache = {r["Codigo"]: r for r in backend._cache["operacional"]["gv"]["dados"]}
    for cod in ("18159608537507", "18121604619044", "18121604625987"):
        assert cache[cod]["__primeira_entrada"] == "15/05/2026 14:36:00"
        assert cache[cod]["__pe_proxy"] is False
        # Regra 10h aplicada via _diag_entra_no_sla (a mesma lógica do frontend)
        entra, motivo = backend._diag_entra_no_sla(cache[cod], today)
        assert entra is False, f"{cod}: deveria EXCLUIR; motivo={motivo}"

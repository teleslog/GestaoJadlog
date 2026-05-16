"""
Testes de regressão — cada bug que apareceu vira um teste permanente.

Cada teste cita o commit ou contexto do bug original. Se um desses falhar
no futuro, é sinal de regressão direta de uma classe de bug já conhecida.
"""
from __future__ import annotations

import asyncio

import pytest

import main as backend


@pytest.mark.regression
def test_879a787_proxy_filtra_hist_nossa_op(write_xlsx, make_row):
    """
    BUG (pré-879a787): _read_and_merge e merge_incremental calculavam proxy
    de PE percorrendo TODAS as linhas, incluindo Hist=outra torre. Pacotes em
    trânsito viravam PE=Dt Evento em outra torre, e entraNoSlaHoje incluía
    indevidamente no SLA.

    FIX: filtrar por Hist=nossa op tanto na PE real quanto no proxy.
    """
    s_old = write_xlsx([
        make_row(codigo="REG1", status="TRANSFERENCIA",
                 dt_evento="13/05/2026 22:32:57",
                 hist_ponto="FL BELO HORIZONTE",  # outra torre
                 previsao="15/05/2026"),
    ], name="s_old.xlsx")
    s_new = write_xlsx([
        make_row(codigo="REG1", status="EM ROTA PICKUP",
                 dt_evento="15/05/2026 15:15:48",
                 hist_ponto="CO GOV VALADARES 01",  # nossa torre
                 previsao="15/05/2026"),
    ], name="s_new.xlsx")
    rows = backend._read_and_merge([s_new, s_old], "CO GOV VALADARES 01")
    r = {x["Codigo"]: x for x in rows}["REG1"]
    assert r["__primeira_entrada"] == "15/05/2026 15:15:48"
    assert r["__pe_proxy"] is True


@pytest.mark.regression
def test_3e5ef2f_pe_min_sem_priorizar_hoje(make_row):
    """
    BUG: _compute_primeira_entrada priorizava ENTRADA HOJE sobre histórica,
    fazendo um pacote com ENTRADA ontem e re-scan hoje virar PE=hoje.

    FIX: PE = MIN absoluto de Dt Evento entre linhas com Status=ENTRADA.
    """
    rows = [
        make_row(codigo="REG2", status="ENTRADA", dt_evento="13/05/2026 09:00:00",
                 hist_ponto="CO CURVELO 02"),
        make_row(codigo="REG2", status="ENTRADA", dt_evento="15/05/2026 14:36:00",
                 hist_ponto="CO CURVELO 02"),
    ]
    pe = backend._compute_primeira_entrada(rows, "CO CURVELO 02")
    assert pe == {"REG2": "13/05/2026 09:00:00"}


@pytest.mark.regression
def test_4403b6b_mergeop_nao_cascateia_pe_antiga():
    """
    BUG: mergeOp do frontend aplicava enforce MIN(prevPE, newPE), travando o
    opMap em uma PE antiga errada mesmo quando o backend retornava a nova.

    FIX: frontend confia no backend. Aqui validamos a regra equivalente: a
    PE no backend, depois do fix de Hist, é a fonte de verdade. Se o backend
    diz "PE=15/05 14:36", esse é o valor que deve ser consumido.
    """
    # Simula o cache do backend pós-fix
    cache_row = {
        "Codigo": "REG3", "Status": "ENTRADA",
        "Dt Evento": "15/05/2026 14:36:00",
        "Previsao": "20/05/2026",
        "__primeira_entrada": "15/05/2026 14:36:00",
        "__pe_proxy": False,
    }
    from datetime import datetime
    entra, motivo = backend._diag_entra_no_sla(cache_row, datetime(2026, 5, 15))
    assert entra is False
    assert "EXCLUI" in motivo


@pytest.mark.regression
def test_879a787_merge_incremental_filtra_hist_no_proxy(cache_lock, write_xlsx, make_row, today):
    """
    BUG: merge_incremental computava proxy `new_min_dt` percorrendo TODAS
    as linhas do xlsx, sem filtrar Hist. Códigos chegando hoje >=10h herdavam
    proxy=Dt Evento de quando estavam em outra torre.

    FIX: filtro Hist no loop new_min_dt.
    """
    path = write_xlsx([
        # Mesmo código aparece em outra torre (snapshot em trânsito) e na nossa
        make_row(codigo="REG4", status="TRANSFERENCIA",
                 dt_evento="13/05/2026 22:00:00",
                 hist_ponto="FL BELO HORIZONTE",
                 previsao="20/05/2026"),
        make_row(codigo="REG4", status="ENTRADA",
                 dt_evento="15/05/2026 14:36:00",
                 hist_ponto="CO GOV VALADARES 01",
                 previsao="20/05/2026"),
    ])
    asyncio.run(backend.merge_incremental("operacional", "gv", path))
    cache = {r["Codigo"]: r for r in backend._cache["operacional"]["gv"]["dados"]}
    # PE real captura ENTRADA na nossa torre — não a Dt Evento de FL BH.
    assert cache["REG4"]["__primeira_entrada"] == "15/05/2026 14:36:00"
    assert cache["REG4"]["__pe_proxy"] is False

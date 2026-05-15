"""
Testes de _compute_primeira_entrada (PE real a partir de Status=ENTRADA).

Invariantes:
- Sempre retorna MIN(Dt Evento) onde Status=ENTRADA.
- Quando op_name é informado, filtra Hist. ultimo ponto == op_name.
- Nunca retorna PE para códigos sem Status=ENTRADA em nenhuma linha.
"""
from __future__ import annotations

import pytest

import main as backend


def test_min_entrada_quando_so_uma_linha(make_row):
    rows = [make_row(codigo="A1", status="ENTRADA",
                     dt_evento="14/05/2026 14:36:00",
                     hist_ponto="CO CURVELO 02")]
    pe = backend._compute_primeira_entrada(rows, "CO CURVELO 02")
    assert pe == {"A1": "14/05/2026 14:36:00"}


def test_min_entre_multiplas_entradas_mesmo_codigo(make_row):
    rows = [
        make_row(codigo="A1", status="ENTRADA", dt_evento="14/05/2026 14:36:00", hist_ponto="CO CURVELO 02"),
        make_row(codigo="A1", status="ENTRADA", dt_evento="13/05/2026 09:00:00", hist_ponto="CO CURVELO 02"),
        make_row(codigo="A1", status="ENTRADA", dt_evento="14/05/2026 11:00:00", hist_ponto="CO CURVELO 02"),
    ]
    pe = backend._compute_primeira_entrada(rows, "CO CURVELO 02")
    assert pe == {"A1": "13/05/2026 09:00:00"}


def test_status_diferente_de_entrada_e_ignorado(make_row):
    rows = [
        make_row(codigo="A1", status="EM ROTA", dt_evento="13/05/2026 09:00:00", hist_ponto="CO CURVELO 02"),
        make_row(codigo="A1", status="ENTREGUE", dt_evento="14/05/2026 16:00:00", hist_ponto="CO CURVELO 02"),
    ]
    pe = backend._compute_primeira_entrada(rows, "CO CURVELO 02")
    assert pe == {}


def test_filtro_hist_descarta_outra_torre(make_row):
    """Bug que foi corrigido em 879a787: ENTRADA em outra torre não pode virar PE da nossa."""
    rows = [
        # ENTRADA antiga em outra torre (FL BELO HORIZONTE)
        make_row(codigo="A1", status="ENTRADA", dt_evento="13/05/2026 09:00:00", hist_ponto="FL BELO HORIZONTE"),
        # ENTRADA hoje na nossa torre
        make_row(codigo="A1", status="ENTRADA", dt_evento="15/05/2026 14:36:00", hist_ponto="CO CURVELO 02"),
    ]
    pe = backend._compute_primeira_entrada(rows, "CO CURVELO 02")
    assert pe == {"A1": "15/05/2026 14:36:00"}


def test_sem_op_name_nao_filtra_hist(make_row):
    """Comportamento legado: op_name=None percorre tudo (usado para financeiro)."""
    rows = [
        make_row(codigo="A1", status="ENTRADA", dt_evento="13/05/2026 09:00:00", hist_ponto="FL BELO HORIZONTE"),
    ]
    pe = backend._compute_primeira_entrada(rows, None)
    assert pe == {"A1": "13/05/2026 09:00:00"}


def test_codigo_vazio_e_ignorado(make_row):
    rows = [
        make_row(codigo="", status="ENTRADA", dt_evento="14/05/2026 14:36:00"),
        make_row(codigo="   ", status="ENTRADA", dt_evento="14/05/2026 14:36:00"),
    ]
    pe = backend._compute_primeira_entrada(rows, "CO CURVELO 02")
    assert pe == {}


def test_dt_evento_invalida_e_ignorada(make_row):
    rows = [make_row(codigo="A1", status="ENTRADA", dt_evento="lixo")]
    pe = backend._compute_primeira_entrada(rows, "CO CURVELO 02")
    assert pe == {}


def test_multiplos_codigos(make_row):
    rows = [
        make_row(codigo="A1", status="ENTRADA", dt_evento="14/05/2026 14:36:00", hist_ponto="CO CURVELO 02"),
        make_row(codigo="A2", status="ENTRADA", dt_evento="14/05/2026 09:00:00", hist_ponto="CO CURVELO 02"),
        make_row(codigo="A3", status="EM ROTA", dt_evento="14/05/2026 14:36:00", hist_ponto="CO CURVELO 02"),
    ]
    pe = backend._compute_primeira_entrada(rows, "CO CURVELO 02")
    assert pe == {
        "A1": "14/05/2026 14:36:00",
        "A2": "14/05/2026 09:00:00",
        # A3 não está (Status != ENTRADA)
    }


@pytest.mark.regression
def test_regression_879a787_proxy_outra_torre_nao_vira_pe(make_row):
    """
    Bug original (pré-879a787): código que esteve em FL BELO HORIZONTE com Dt Evento=13/05
    virava PE proxy=13/05, fazendo entraNoSlaHoje retornar TRUE indevidamente. Apesar
    de esse caso ser cobertura do PROXY (não _compute), validamos aqui a base do fix:
    _compute_primeira_entrada não considera Hist=outra torre.
    """
    rows = [
        make_row(codigo="18015953996727", status="TRANSFERENCIA",
                 dt_evento="13/05/2026 22:32:57", hist_ponto="FL BELO HORIZONTE"),
        make_row(codigo="18015953996727", status="EM ROTA PICKUP",
                 dt_evento="15/05/2026 15:15:48", hist_ponto="CO GOV VALADARES 01"),
    ]
    pe = backend._compute_primeira_entrada(rows, "CO GOV VALADARES 01")
    # Nenhuma ENTRADA real na nossa torre → nada retornado (proxy cuida do resto).
    assert pe == {}

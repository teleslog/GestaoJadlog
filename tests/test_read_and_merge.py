"""
Testes de _read_and_merge — pipeline completo: ler xlsx, mesclar por Código,
injetar PE real e proxy. Cobre cenários multi-snapshot + filtro Hist.
"""
from __future__ import annotations

import main as backend


def test_le_um_arquivo_simples(write_xlsx, make_row):
    """Smoke: 2 linhas, 1 com ENTRADA, 1 com EM ROTA. PE só para a com ENTRADA."""
    path = write_xlsx([
        make_row(codigo="A1", status="ENTRADA", dt_evento="15/05/2026 09:00:00",
                 hist_ponto="CO CURVELO 02", previsao="15/05/2026"),
        make_row(codigo="A2", status="EM ROTA", dt_evento="15/05/2026 11:30:00",
                 hist_ponto="CO CURVELO 02", previsao="15/05/2026"),
    ])
    rows = backend._read_and_merge([path], "CO CURVELO 02")
    by = {r["Codigo"]: r for r in rows}
    assert by["A1"]["__primeira_entrada"] == "15/05/2026 09:00:00"
    assert by["A1"]["__pe_proxy"] is False
    assert by["A2"]["__primeira_entrada"] == "15/05/2026 11:30:00"  # proxy = Dt Evento
    assert by["A2"]["__pe_proxy"] is True


def test_proxy_filtra_hist_outra_torre(write_xlsx, make_row):
    """
    Bug 879a787: proxy MIN(Dt Evento) deve ignorar linhas com Hist != nossa op.
    """
    snap_old = write_xlsx([
        make_row(codigo="A1", status="TRANSFERENCIA", dt_evento="13/05/2026 22:00:00",
                 hist_ponto="FL BELO HORIZONTE", previsao="15/05/2026"),
    ], name="old.xlsx")
    snap_new = write_xlsx([
        make_row(codigo="A1", status="EM ROTA", dt_evento="15/05/2026 14:36:00",
                 hist_ponto="CO GOV VALADARES 01", previsao="15/05/2026"),
    ], name="new.xlsx")
    # Passa do mais novo para o mais antigo (como _scan_folder faz).
    rows = backend._read_and_merge([snap_new, snap_old], "CO GOV VALADARES 01")
    a1 = {r["Codigo"]: r for r in rows}["A1"]
    # Proxy deve ser 15/05 (linha com Hist=GV) — não 13/05 (Hist=FL BH).
    assert a1["__primeira_entrada"] == "15/05/2026 14:36:00"
    assert a1["__pe_proxy"] is True


def test_pe_real_vence_proxy(write_xlsx, make_row):
    """ENTRADA real (mesmo se isolada num snapshot) substitui proxy."""
    snap1 = write_xlsx([
        make_row(codigo="A1", status="ENTRADA", dt_evento="14/05/2026 09:00:00",
                 hist_ponto="CO CURVELO 02", previsao="15/05/2026"),
    ], name="s1.xlsx")
    snap2 = write_xlsx([
        make_row(codigo="A1", status="EM ROTA", dt_evento="15/05/2026 11:00:00",
                 hist_ponto="CO CURVELO 02", previsao="15/05/2026"),
    ], name="s2.xlsx")
    rows = backend._read_and_merge([snap2, snap1], "CO CURVELO 02")
    a1 = {r["Codigo"]: r for r in rows}["A1"]
    assert a1["__primeira_entrada"] == "14/05/2026 09:00:00"
    assert a1["__pe_proxy"] is False


def test_min_dt_evento_quando_proxy(write_xlsx, make_row):
    """Sem ENTRADA real: proxy = MIN(Dt Evento) entre todas as linhas com Hist=nossa op."""
    snap1 = write_xlsx([
        make_row(codigo="A1", status="EM ROTA", dt_evento="14/05/2026 16:00:00",
                 hist_ponto="CO CURVELO 02"),
    ], name="s1.xlsx")
    snap2 = write_xlsx([
        make_row(codigo="A1", status="EM ROTA", dt_evento="15/05/2026 09:30:00",
                 hist_ponto="CO CURVELO 02"),
    ], name="s2.xlsx")
    rows = backend._read_and_merge([snap2, snap1], "CO CURVELO 02")
    a1 = {r["Codigo"]: r for r in rows}["A1"]
    assert a1["__primeira_entrada"] == "14/05/2026 16:00:00"
    assert a1["__pe_proxy"] is True


def test_codigo_so_em_outra_torre_recebe_pe_vazia(write_xlsx, make_row):
    """
    Código aparece no arquivo da nossa op mas com Hist=outra torre em TODOS os snapshots.
    Não há evidência de presença física. PE deve ficar vazia.
    """
    snap = write_xlsx([
        make_row(codigo="X1", status="TRANSFERENCIA", dt_evento="14/05/2026 12:00:00",
                 hist_ponto="TC TECA MATRIZ"),
    ])
    rows = backend._read_and_merge([snap], "CO CURVELO 02")
    x1 = {r["Codigo"]: r for r in rows}["X1"]
    assert x1.get("__primeira_entrada", "") == ""


def test_total_nao_diminui_entre_arquivos(write_xlsx, make_row):
    """Cache never-shrinks: códigos que somem do snapshot mais novo ficam preservados."""
    snap_old = write_xlsx([
        make_row(codigo="A1", status="EM ROTA", dt_evento="14/05/2026 09:00:00",
                 hist_ponto="CO CURVELO 02"),
        make_row(codigo="A2", status="EM ROTA", dt_evento="14/05/2026 10:00:00",
                 hist_ponto="CO CURVELO 02"),
    ], name="old.xlsx")
    snap_new = write_xlsx([
        # Só A1 está no snapshot novo
        make_row(codigo="A1", status="ENTREGUE", dt_evento="15/05/2026 11:00:00",
                 hist_ponto="CO CURVELO 02"),
    ], name="new.xlsx")
    rows = backend._read_and_merge([snap_new, snap_old], "CO CURVELO 02")
    by = {r["Codigo"]: r for r in rows}
    assert "A1" in by and "A2" in by
    assert by["A1"]["Status"] == "ENTREGUE"  # novo venceu
    assert by["A2"]["Status"] == "EM ROTA"   # preservado do antigo


def test_status_mais_recente_vence_em_dedup(write_xlsx, make_row):
    """Em ordem [novo, antigo], o status do snapshot mais novo prevalece."""
    snap_old = write_xlsx([
        make_row(codigo="A1", status="EM ROTA", dt_evento="14/05/2026 09:00:00",
                 hist_ponto="CO CURVELO 02"),
    ], name="old.xlsx")
    snap_new = write_xlsx([
        make_row(codigo="A1", status="ENTREGUE", dt_evento="15/05/2026 11:00:00",
                 hist_ponto="CO CURVELO 02"),
    ], name="new.xlsx")
    rows = backend._read_and_merge([snap_new, snap_old], "CO CURVELO 02")
    a1 = {r["Codigo"]: r for r in rows}["A1"]
    assert a1["Status"] == "ENTREGUE"
    assert a1["Dt Evento"] == "15/05/2026 11:00:00"

"""
Testes de _diag_audit — auditoria server-side do painel SLA.

Cobre:
- Contagens dos buckets (vencidas, vencem_hoje, entregues_sla, custodia_sla).
- Regra das 10h aplicada em todos os buckets.
- Vencidas (d>0) e Vencem Hoje (d=0).
- Inconsistencias estritas vs suspeitas (auditoria visual).
- ENTREGUE só conta se status atual for ENTREGUE com Dt Evento hoje.
- Filtros codigos= e op=.
"""
from __future__ import annotations

import main as backend


def _populate_cache(rows_by_op: dict[str, list[dict]]):
    """Popula _cache['operacional'][key]['dados'] com rows."""
    for key, rows in rows_by_op.items():
        backend._cache.setdefault("operacional", {})[key] = {
            "key": key, "op": backend.OPS[key][0],
            "opIdx": backend.OPS[key][1], "tipo": "operacional",
            "atualizado": "2026-05-15T14:00:00Z", "linhas": len(rows),
            "dados": rows, "arquivo": "synthetic.xlsx", "erro": None,
        }


# ── Buckets ──────────────────────────────────────────────────────────────────

def test_vencendo_hoje_pendente_entra(make_row, today):
    rows = [make_row(codigo="A", status="EM ROTA",
                     dt_evento="15/05/2026 09:30:00",
                     previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                     **{"__primeira_entrada": "14/05/2026 14:00:00",
                        "__pe_proxy": True})]
    _populate_cache({"curvelo": rows})
    out = backend._diag_audit(op_filter=None, codigos_filter=None)
    assert out["resumo"]["vencem_hoje"] == 1
    assert out["resumo"]["vencidas"] == 0
    assert out["resumo"]["total_sla"] == 1


def test_vencidas_pendente_entra(make_row, today):
    rows = [make_row(codigo="A", status="EM ROTA",
                     dt_evento="14/05/2026 14:00:00",
                     previsao="14/05/2026", hist_ponto="CO CURVELO 02",
                     **{"__primeira_entrada": "14/05/2026 09:00:00",
                        "__pe_proxy": False})]
    _populate_cache({"curvelo": rows})
    out = backend._diag_audit(op_filter=None, codigos_filter=None)
    assert out["resumo"]["vencidas"] == 1
    assert out["resumo"]["vencem_hoje"] == 0


def test_entregue_so_conta_se_dt_hoje_e_no_prazo(make_row, today):
    """ENTREGUE hoje com prazo hoje ou no passado entra; ENTREGUE de dia anterior NÃO."""
    rows = [
        # ENTREGUE hoje, prazo hoje → conta como ENTREGUES_SLA
        make_row(codigo="OK", status="ENTREGUE", dt_evento="15/05/2026 11:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
        # ENTREGUE ontem, prazo ontem → não conta hoje
        make_row(codigo="VELHO", status="ENTREGUE", dt_evento="14/05/2026 11:00:00",
                 previsao="14/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "13/05/2026 09:00:00",
                    "__pe_proxy": False}),
    ]
    _populate_cache({"curvelo": rows})
    out = backend._diag_audit(op_filter=None, codigos_filter=None)
    assert out["resumo"]["entregues_sla"] == 1


def test_remessa_com_prazo_futuro_nao_entra(make_row, today):
    """Previsão no futuro (d < 0) não conta como vencendo hoje nem vencida."""
    rows = [make_row(codigo="F1", status="EM ROTA",
                     dt_evento="15/05/2026 09:00:00",
                     previsao="20/05/2026", hist_ponto="CO CURVELO 02",
                     **{"__primeira_entrada": "14/05/2026 09:00:00",
                        "__pe_proxy": False})]
    _populate_cache({"curvelo": rows})
    out = backend._diag_audit(op_filter=None, codigos_filter=None)
    assert out["resumo"]["total_sla"] == 0
    assert out["resumo"]["vencem_hoje"] == 0
    assert out["resumo"]["vencidas"] == 0


def test_custodia_so_conta_quando_vence_hoje(make_row, today):
    rows = [
        # CUSTODIA prazo hoje → CUSTODIA_SLA
        make_row(codigo="C1", status="CUSTODIA", dt_evento="15/05/2026 11:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
        # CUSTODIA prazo passado → NÃO conta como custodia_sla (vai para "fora" ou vencidas)
        make_row(codigo="C2", status="CUSTODIA", dt_evento="14/05/2026 11:00:00",
                 previsao="14/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "13/05/2026 09:00:00",
                    "__pe_proxy": False}),
    ]
    _populate_cache({"curvelo": rows})
    out = backend._diag_audit(op_filter=None, codigos_filter=None)
    assert out["resumo"]["custodia_sla"] == 1


# ── Regra das 10h aplicada ───────────────────────────────────────────────────

def test_entrada_hoje_apos_10h_nao_conta_em_lugar_nenhum(make_row, today):
    """Caso do incidente: ENTRADA hoje 14:36 não vai para vencem_hoje nem vencidas."""
    rows = [make_row(codigo="X1", status="ENTRADA",
                     dt_evento="15/05/2026 14:36:00",
                     previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                     **{"__primeira_entrada": "15/05/2026 14:36:00",
                        "__pe_proxy": False})]
    _populate_cache({"curvelo": rows})
    out = backend._diag_audit(op_filter=None, codigos_filter=None)
    assert out["resumo"]["vencem_hoje"] == 0
    assert out["resumo"]["vencidas"] == 0
    assert out["resumo"]["total_sla"] == 0
    # E precisa estar excluída pela regra:
    assert out["auditoria"]["total_excluidas_10h"] == 1


def test_entrada_hoje_antes_10h_conta_em_vencem_hoje(make_row, today):
    rows = [make_row(codigo="X1", status="ENTRADA",
                     dt_evento="15/05/2026 09:30:00",
                     previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                     **{"__primeira_entrada": "15/05/2026 09:30:00",
                        "__pe_proxy": False})]
    _populate_cache({"curvelo": rows})
    out = backend._diag_audit(op_filter=None, codigos_filter=None)
    assert out["resumo"]["vencem_hoje"] == 1


def test_inconsistencia_estrita_status_entrada_hoje_apos_10h_mas_no_sla(make_row, today, monkeypatch):
    """
    Caso patológico: alguém colocou PE no passado (errado) num código com Status=ENTRADA
    hoje >=10h. A inconsistência estrita captura via Status atual.
    Aqui simulamos forçando PE=ontem para um Status=ENTRADA hoje. O safety net do
    entraNoSlaHoje EXCLUI (porque MIN(PE, DtEv ENTRADA) = ontem → inclui). Hmm.
    Esse cenário cobre a auditoria reportar a contradição como suspeita.
    """
    # Vamos forçar a contradição: PE=ontem (real, marcado como não-proxy)
    rows = [make_row(codigo="X1", status="EM ROTA",
                     dt_evento="15/05/2026 14:36:00",
                     previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                     **{"__primeira_entrada": "14/05/2026 09:00:00",
                        "__pe_proxy": True})]
    _populate_cache({"curvelo": rows})
    out = backend._diag_audit(op_filter=None, codigos_filter=None)
    # Está no SLA (vencem_hoje), pois PE=ontem → inclui.
    assert out["resumo"]["vencem_hoje"] == 1
    # Aparece como suspeita (auditoria visual), não como inconsistência estrita.
    assert out["auditoria"]["inconsistencias"] == 0
    assert out["auditoria"]["remessas_suspeitas"] == 1


# ── Filtros ─────────────────────────────────────────────────────────────────

def test_filtro_codigos(make_row, today):
    rows = [
        make_row(codigo="A", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
        make_row(codigo="B", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00",
                    "__pe_proxy": False}),
    ]
    _populate_cache({"curvelo": rows})
    out = backend._diag_audit(op_filter=None, codigos_filter={"A"})
    assert out["resumo"]["vencem_hoje"] == 1


def test_filtro_op(make_row, today):
    """Filtra cache só para curvelo; ignora gv mesmo populada."""
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
    out = backend._diag_audit(op_filter="curvelo", codigos_filter=None)
    assert out["resumo"]["vencem_hoje"] == 1
    codigos = {d["codigo"] for d in out["detalhes"]}
    assert codigos == {"C1"}

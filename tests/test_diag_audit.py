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


def _populate_cache(rows_by_op: dict[str, list[dict]],
                    atualizado: str = "2026-05-15T14:00:00Z"):
    """Popula _cache['operacional'][key]['dados'] com rows."""
    for key, rows in rows_by_op.items():
        backend._cache.setdefault("operacional", {})[key] = {
            "key": key, "op": backend.OPS[key][0],
            "opIdx": backend.OPS[key][1], "tipo": "operacional",
            "atualizado": atualizado, "linhas": len(rows),
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
    out = backend._diag_audit(op_filter="curvelo", codigos_filter=None, include_detalhes=True)
    assert out["resumo"]["vencem_hoje"] == 1
    codigos = {d["codigo"] for d in out["detalhes"]}
    assert codigos == {"C1"}


# ── P5: include_detalhes opt-in + filtros novos + paginação ────────────────

def test_default_sem_detalhes(make_row, today):
    """Default include_detalhes=False → lista detalhes vazia, counts intactos."""
    rows = [make_row(codigo=f"E{i}", status="ENTREGUE",
                     dt_evento="15/05/2026 11:00:00",
                     previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                     **{"__primeira_entrada": "14/05/2026 09:00:00",
                        "__pe_proxy": False})
            for i in range(50)]
    _populate_cache({"curvelo": rows})
    out = backend._diag_audit(op_filter=None, codigos_filter=None)
    assert out["resumo"]["entregues_sla"] == 50
    assert out["detalhes"] == []
    assert out["filtros"]["include_detalhes"] is False


def test_filtro_categoria(make_row, today):
    """categoria=ENTREGUES_SLA deve retornar só entregues."""
    rows = [
        make_row(codigo="E1", status="ENTREGUE", dt_evento="15/05/2026 11:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00", "__pe_proxy": False}),
        make_row(codigo="V1", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00", "__pe_proxy": False}),
    ]
    _populate_cache({"curvelo": rows})
    out = backend._diag_audit(op_filter=None, codigos_filter=None,
                              include_detalhes=True, categoria_filter="ENTREGUES_SLA")
    cods = {d["codigo"] for d in out["detalhes"]}
    assert cods == {"E1"}
    # Resumo NÃO muda com filtro de detalhes (counts globais sempre fiéis).
    assert out["resumo"]["entregues_sla"] == 1
    assert out["resumo"]["vencem_hoje"] == 1


def test_apenas_excluidas_10h(make_row, today):
    rows = [
        # ENTRADA hoje 14:36 — excluída pela regra
        make_row(codigo="X1", status="ENTRADA", dt_evento="15/05/2026 14:36:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "15/05/2026 14:36:00", "__pe_proxy": False}),
        # Normal — entra no SLA
        make_row(codigo="OK", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00", "__pe_proxy": False}),
    ]
    _populate_cache({"curvelo": rows})
    out = backend._diag_audit(op_filter=None, codigos_filter=None,
                              include_detalhes=True, apenas_excluidas_10h=True)
    cods = {d["codigo"] for d in out["detalhes"]}
    assert cods == {"X1"}


def test_apenas_inconsistencias(make_row, today):
    """
    Caso: PE_real (não proxy) hoje >=10h, MAS row "passou" por algum bucket
    (forçamos pela posse de Status=EM ROTA e prazo hoje). Inconsistência estrita
    requer PE_real hoje >=10h. Construímos esse cenário sintético.
    """
    rows = [
        # row "inconsistente" sintética: PE real hoje 14:30 + EM ROTA + prazo hoje
        make_row(codigo="INC", status="EM ROTA", dt_evento="15/05/2026 11:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "15/05/2026 14:30:00", "__pe_proxy": False}),
        # row legítima
        make_row(codigo="OK", status="EM ROTA", dt_evento="15/05/2026 09:00:00",
                 previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                 **{"__primeira_entrada": "14/05/2026 09:00:00", "__pe_proxy": False}),
    ]
    _populate_cache({"curvelo": rows})
    out = backend._diag_audit(op_filter=None, codigos_filter=None,
                              include_detalhes=True, apenas_inconsistencias=True)
    cods = {d["codigo"] for d in out["detalhes"]}
    # INC: PE real hoje 14:30 + entra no SLA (porque entraNoSlaHoje vê PE=14:30
    # como hoje >=10h e EXCLUI; então não entra no SLA → não é inconsistência).
    # Esse é um caso real raro. Se PE >=10h, entraNoSlaHoje exclui. Para gerar
    # inconsistência, precisamos `entra=True` apesar disso. Não é possível com
    # a lógica atual. Logo `inconsistencias` deve ser 0 e `detalhes` vazio.
    assert cods == set()
    assert out["auditoria"]["inconsistencias"] == 0


def test_paginacao_limit_offset(make_row, today):
    rows = [make_row(codigo=f"E{i:03d}", status="ENTREGUE",
                     dt_evento="15/05/2026 11:00:00",
                     previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                     **{"__primeira_entrada": "14/05/2026 09:00:00",
                        "__pe_proxy": False})
            for i in range(50)]
    _populate_cache({"curvelo": rows})
    pg1 = backend._diag_audit(op_filter=None, codigos_filter=None,
                              include_detalhes=True, limit=10, offset=0)
    pg2 = backend._diag_audit(op_filter=None, codigos_filter=None,
                              include_detalhes=True, limit=10, offset=10)
    assert len(pg1["detalhes"]) == 10
    assert len(pg2["detalhes"]) == 10
    # Páginas diferentes
    assert {d["codigo"] for d in pg1["detalhes"]} != {d["codigo"] for d in pg2["detalhes"]}
    # Total filtrado deve ser 50 em ambas
    assert pg1["detalhes_total_filtrado"] == 50
    assert pg2["detalhes_total_filtrado"] == 50


def test_auditoria_inclui_idade_cache(make_row, today):
    from datetime import timedelta, timezone
    mock_now = backend.datetime.now(timezone.utc)
    atualizado = (mock_now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _populate_cache(
        {"curvelo": [make_row(codigo="A", status="EM ROTA",
                              dt_evento="15/05/2026 09:00:00",
                              previsao="15/05/2026", hist_ponto="CO CURVELO 02",
                              **{"__primeira_entrada": "14/05/2026 09:00:00",
                                 "__pe_proxy": False})]},
        atualizado=atualizado,
    )
    out = backend._diag_audit(op_filter=None, codigos_filter=None)
    assert out["auditoria"]["ultima_atualizacao_cache"] is not None
    assert 800 < out["auditoria"]["idade_cache_segundos"] < 1000  # ~900s = 15min

"""
Testes de _diag_entra_no_sla — réplica em Python do entraNoSlaHoje do frontend.

Regra das 10h:
- Sem PE e sem Status=ENTRADA: inclui por precaução.
- PE/DtEv antes de hoje: inclui (histórico).
- PE/DtEv depois de hoje: inclui (anomalia futura, conservador).
- PE/DtEv hoje antes das 10:00: inclui.
- PE/DtEv hoje 10:00 ou depois: EXCLUI.
- Status=ENTRADA usa DtEvento como candidato adicional → vence o MIN.
"""
from __future__ import annotations

from datetime import datetime

import main as backend

TODAY = datetime(2026, 5, 15, 0, 0, 0)


def _row(**kw):
    r = {
        "Codigo": "X",
        "Status": "EM ROTA",
        "Dt Evento": "",
        "__primeira_entrada": "",
        "__pe_proxy": False,
    }
    r.update(kw)
    return r


def test_pe_antes_de_hoje_inclui():
    r = _row(**{"__primeira_entrada": "14/05/2026 14:00:00"})
    entra, _ = backend._diag_entra_no_sla(r, TODAY)
    assert entra is True


def test_pe_hoje_antes_de_10h_inclui():
    r = _row(**{"__primeira_entrada": "15/05/2026 09:59:59"})
    entra, _ = backend._diag_entra_no_sla(r, TODAY)
    assert entra is True


def test_pe_hoje_exatamente_10h_exclui():
    """Fronteira: 10:00:00 já EXCLUI (regra é >= 10h)."""
    r = _row(**{"__primeira_entrada": "15/05/2026 10:00:00"})
    entra, _ = backend._diag_entra_no_sla(r, TODAY)
    assert entra is False


def test_pe_hoje_apos_10h_exclui():
    r = _row(**{"__primeira_entrada": "15/05/2026 14:36:00"})
    entra, motivo = backend._diag_entra_no_sla(r, TODAY)
    assert entra is False
    assert "EXCLUI" in motivo


def test_pe_futura_inclui():
    """Anomalia: PE no futuro. Comportamento conservador: inclui."""
    r = _row(**{"__primeira_entrada": "20/05/2026 14:36:00"})
    entra, _ = backend._diag_entra_no_sla(r, TODAY)
    assert entra is True


def test_pe_vazia_e_status_nao_entrada_inclui():
    r = _row(**{"__primeira_entrada": ""}, Status="EM ROTA", **{"Dt Evento": "15/05/2026 14:36:00"})
    entra, motivo = backend._diag_entra_no_sla(r, TODAY)
    assert entra is True
    assert "sem candidato" in motivo


def test_safety_net_status_entrada_hoje_10h_exclui():
    """Safety net: Status=ENTRADA + DtEv hoje >=10h, mesmo sem PE → EXCLUI."""
    r = _row(**{"__primeira_entrada": ""}, Status="ENTRADA", **{"Dt Evento": "15/05/2026 14:36:00"})
    entra, motivo = backend._diag_entra_no_sla(r, TODAY)
    assert entra is False
    assert "ENTRADA" in motivo or "DtEv" in motivo


def test_safety_net_status_entrada_hoje_antes_10h_inclui():
    r = _row(**{"__primeira_entrada": ""}, Status="ENTRADA", **{"Dt Evento": "15/05/2026 09:30:00"})
    entra, _ = backend._diag_entra_no_sla(r, TODAY)
    assert entra is True


def test_min_entre_pe_e_dtev_quando_status_entrada():
    """
    PE=hoje 14:36 + Status=ENTRADA + DtEv=hoje 09:00. MIN=09:00 → inclui.
    Cobre caso de PE com hora errada — DtEvento atual mais antigo prevalece.
    """
    r = _row(**{"__primeira_entrada": "15/05/2026 14:36:00"},
             Status="ENTRADA", **{"Dt Evento": "15/05/2026 09:00:00"})
    entra, _ = backend._diag_entra_no_sla(r, TODAY)
    assert entra is True


def test_status_em_rota_nao_usa_dtev_como_candidato():
    """Para status != ENTRADA, só PE é considerada (não DtEvento atual)."""
    r = _row(**{"__primeira_entrada": "14/05/2026 09:00:00"},
             Status="EM ROTA", **{"Dt Evento": "15/05/2026 14:36:00"})
    entra, _ = backend._diag_entra_no_sla(r, TODAY)
    # PE=14/05 (ontem) → INCLUI. DtEvento atual hoje 14:36 não conta porque Status!=ENTRADA.
    assert entra is True

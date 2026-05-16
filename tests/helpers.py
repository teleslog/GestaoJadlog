"""
Helpers para gerar linhas e xlsx sintéticos compatíveis com _read_xlsx.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

# Operações conhecidas (espelham main.OPS).
OP_NAMES = {
    "gv":      "CO GOV VALADARES 01",
    "itabira": "CO ITABIRA 01",
    "jm":      "CO JOAO MONLEVADE 01",
    "curvelo": "CO CURVELO 02",
}


def make_row(
    *,
    codigo: str,
    status: str,
    dt_evento: str,
    previsao: str = "",
    hist_ponto: str = "CO CURVELO 02",
    rota: str = "",
    operador: str = "",
    descricao: str = "",
    cidade: str = "Curvelo",
    bairro: str = "Centro",
    destinatario: str = "FULANO ***",
    **extra: Any,
) -> dict:
    """
    Cria uma linha 'crua' do Performance.xlsx. Os nomes das chaves espelham
    as colunas usadas em produção. `extra` permite adicionar/sobrescrever.

    `dt_evento`/`previsao` aceitam:
      - 'dd/mm/yyyy'
      - 'dd/mm/yyyy HH:MM:SS'
    """
    row = {
        "Codigo": codigo,
        "Cidade": cidade,
        "Bairro": bairro,
        "Destinatario": destinatario,
        "Status": status,
        "Dt Evento": dt_evento,
        "Previsao": previsao,
        "Hist. ultimo ponto": hist_ponto,
        "Hist. ultimo operador": operador,
        "Hist. ultima descricao": descricao or hist_ponto,
        "Rota": rota,
    }
    row.update(extra)
    return row


# Colunas exatas (e na ordem) que o Performance.xlsx real tem.
# Replicamos o suficiente para _find_header_idx + _read_xlsx funcionarem.
XLSX_COLUMNS = [
    "Codigo", "Nota Fiscal", "Pedido", "Cliente",
    "CNPJ/CPF Remetente", "Cep Remetente", "Destino",
    "Cidade", "Bairro", "UF",
    "Destinatario", "CNPJ/CPF Destinatario", "Cep Destinatario", "TP",
    "Status", "Dt Emissao", "Dt Evento", "Previsao",
    "Descricao", "Recebedor", "Doc. Recebedor", "Tratativa",
    "CTe Inicial", "Valor Declarado", "Codigo do Operador",
    "Peso", "Volume", "Modalidade", "Dt Entrega",
    "Hist. ultimo status", "Hist. ultima data", "Hist. ultimo ponto",
    "Hist. ultima uf", "Hist. ultimo operador", "Hist. ultimo oper matric",
    "Hist. ultima descricao", "Código sorter", "Tem reversa",
    "Cte gerado reversa", "Tipo Entrega", "ShipmentId",
    "Rota", "Pudo Destino", "IdEmbarcador", "Circuit",
]


def write_xlsx(path: Path, rows: list[dict]) -> Path:
    """
    Escreve `rows` em path como xlsx no mesmo formato do Performance.xlsx real:
      linha 0: título "Performance Entrega"
      linha 1: cabeçalho (XLSX_COLUMNS)
      linha 2+: dados
    Colunas ausentes em `rows` ficam vazias.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Monta DataFrame sem header (vamos escrever manualmente as 2 primeiras linhas).
    title = ["Performance Entrega"] + [""] * (len(XLSX_COLUMNS) - 1)
    header = list(XLSX_COLUMNS)
    body = [[r.get(c, "") for c in XLSX_COLUMNS] for r in rows]

    df = pd.DataFrame([title, header] + body)
    df.to_excel(path, index=False, header=False, engine="openpyxl")
    return path

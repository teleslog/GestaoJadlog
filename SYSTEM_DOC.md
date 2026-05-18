# SYSTEM_DOC — GestãoEntregas Jadlog

> **Fonte da verdade do sistema.** Este documento descreve a arquitetura, regras de negócio, lógica crítica e convenções do GestãoEntregas. Deve ser atualizado a cada mudança estrutural relevante.

**Versão do documento:** 2026-05-15 (rev4)  
**Repositório:** https://github.com/teleslog/GestaoJadlog.git  
&nbsp;&nbsp;&nbsp;&nbsp;branch `main` — produção  
&nbsp;&nbsp;&nbsp;&nbsp;branch `dev/profissionalizacao-core` — base técnica (testes, CI, health, logs, backup)  
**Deploy produção:** https://web-production-1614e.up.railway.app  
**Diretório local:** `C:\Users\André\Downloads\cloud_api\`

---

## Índice

1. [Visão Geral](#1-visão-geral)
2. [Arquitetura](#2-arquitetura)
3. [Regras Operacionais (SLA)](#3-regras-operacionais-sla)
4. [Aba Entregador](#4-aba-entregador)
5. [Estrutura de Dados](#5-estrutura-de-dados)
6. [Autenticação e Perfis](#6-autenticação-e-perfis)
7. [Convenções de Código](#7-convenções-de-código)
8. [Regras de Manutenção](#8-regras-de-manutenção)
9. [Fluxos Críticos](#9-fluxos-críticos)
10. [Troubleshooting](#10-troubleshooting)
11. [Roadmap](#11-roadmap)
12. [Backup e Restore](#12-backup-e-restore)
13. [Endpoints de Saúde e Diagnóstico](#13-endpoints-de-saúde-e-diagnóstico)
14. [Testes e CI](#14-testes-e-ci)
15. [Observabilidade (logs estruturados e request_id)](#15-observabilidade-logs-estruturados-e-request_id)
16. [Runbook Operacional](#16-runbook-operacional)
17. [Fluxo Dev → Produção](#17-fluxo-dev--produção)

---

## 1. Visão Geral

### 1.1 Objetivo

Dashboard web para monitoramento operacional de entregas da Jadlog. Centraliza dados de remessas (status, SLA, entregadores, finanças) em tempo real a partir de arquivos `.xlsx` exportados do sistema Jadlog.

### 1.2 Operações Atendidas

| Key interna | Nome oficial             | Observação                          |
|-------------|--------------------------|-------------------------------------|
| `gv`        | CO GOV VALADARES 01      | Maior operação; Performance.xlsx ~18k linhas |
| `itabira`   | CO ITABIRA 01            | —                                   |
| `jm`        | CO JOAO MONLEVADE 01     | —                                   |
| `curvelo`   | CO CURVELO 02            | —                                   |

### 1.3 Stack

| Camada      | Tecnologia                                      |
|-------------|--------------------------------------------------|
| Backend     | Python 3.14, FastAPI, Uvicorn                    |
| Frontend    | HTML5 + CSS3 + JavaScript vanilla (single file) |
| Banco       | SQLite (`users.db`) — somente usuários           |
| Deploy      | Railway — Volume `/data` para persistência       |
| Auth        | JWT HS256 (python-jose), bcrypt direto           |
| Excel       | pandas + openpyxl (leitura), xlsx.js (export)   |

### 1.4 Conceito de Torre Operacional

Cada "operação" é uma torre de distribuição física. O arquivo `.xlsx` exportado do sistema Jadlog contém todas as remessas que passaram por aquela torre. A coluna `Hist. ultimo ponto` identifica a qual torre o pacote pertence no momento — somente remessas com `Hist. ultimo ponto === NomeDaTorre` são contabilizadas na operação correspondente.

---

## 2. Arquitetura

### 2.1 Visão Geral

```
┌─────────────────────────────────────────────────────────┐
│                      RAILWAY                            │
│                                                         │
│  ┌──────────────┐     ┌──────────────────────────────┐  │
│  │  Volume /data │     │        FastAPI (main.py)      │  │
│  │               │     │                              │  │
│  │  /dados/      │────▶│  _cache[tipo][key]           │  │
│  │    financeiro/│     │  (in-memory, atualizado por  │  │
│  │    operacional│     │   upload ou a cada 300s)     │  │
│  │  users.db     │     │                              │  │
│  └──────────────┘     └──────────┬───────────────────┘  │
│                                  │ HTTP                  │
│                        ┌─────────▼───────────┐          │
│                        │  GestãoEntregas.html │          │
│                        │  (single-file SPA)   │          │
│                        └─────────────────────┘          │
└─────────────────────────────────────────────────────────┘
```

### 2.2 Backend (`main.py`)

#### Cache em memória

```python
_cache[tipo][key] = {
    "key":        str,        # ex: "gv"
    "op":         str,        # ex: "CO GOV VALADARES 01"
    "opIdx":      int,        # 0–3, mapeamento para OPS no frontend
    "tipo":       str,        # "operacional" | "financeiro"
    "atualizado": str,        # ISO timestamp UTC
    "linhas":     int,        # contagem de remessas
    "dados":      list[dict], # lista de todas as linhas do xlsx
    "arquivo":    str,        # nome do arquivo mais recente
    "erro":       str | None  # mensagem de erro ou None
}
```

`tipo` pode ser `"operacional"` ou `"financeiro"`.  
`key` pode ser `"gv"`, `"itabira"`, `"jm"` ou `"curvelo"`.

#### Persistência no Railway

```
DATA_DIR=/data  (variável de ambiente no Railway)

/data/
  dados/
    financeiro/   ← arquivos xlsx/zip financeiros
    operacional/  ← arquivos xlsx/zip operacionais
  users.db        ← SQLite com usuários
```

Localmente, sem `DATA_DIR`, o sistema usa o diretório do `main.py`.

#### Hierarquia de refresh

| Função              | Quando é chamada                          | Escopo                                |
|---------------------|-------------------------------------------|---------------------------------------|
| `refresh_all()`     | Startup + a cada 300s (background)        | Relê todos arquivos das 2 pastas      |
| `refresh_tipo()`    | Fallback se chave não detectada no upload | Relê todos arquivos de 1 tipo (4 ops) |
| `refresh_one()`     | Utilitário pós-upload quando necessário   | Relê arquivos de 1 tipo + 1 key       |
| `merge_incremental()` | **Caminho normal do upload**            | Lê SÓ o novo arquivo, mescla no cache |

O `merge_incremental` é o caminho crítico de desempenho: ~3-5s independente do histórico.

#### Detecção automática de operação

1. **Por conteúdo** (primário): lê as primeiras 330 linhas do arquivo e procura keywords nas células (`GOV VALADARES`, `ITABIRA`, `MONLEVADE`, `CURVELO`).
2. **Por nome de arquivo** (fallback): presença de `GV`, `ITA`, `JM`, `CUR` no nome.

#### Lógica de dedup no merge_incremental

- Chave de dedup: coluna `Codigo`
- Em conflito, vence o registro com `Dt Evento` mais recente
- Se o novo não tem `Dt Evento`, mantém o existente (conservador)
- `__primeira_entrada` é preservada: nunca sobrescrita com uma data posterior
- **Proxy PE** (2026-05-13): novos códigos sem PE após `nova_pe` recebem `Dt Evento` como proxy — ver seção 3.3

### 2.3 Frontend (`GestãoEntregas.html`)

Single-file SPA (~3200 linhas). Toda a interface, lógica de negócio e estilos estão neste único arquivo.

#### Stores principais

```javascript
const OPS = ['CO GOV VALADARES 01','CO ITABIRA 01','CO JOAO MONLEVADE 01','CO CURVELO 02'];
const opMaps = [new Map(), new Map(), new Map(), new Map()]; // por opIdx, key=Codigo
const finMaps = [new Map(), new Map(), new Map(), new Map()]; // financeiro por NCTE
```

#### Ciclo de refresh automático

```
startAutoRefresh()
  → fetchAll() a cada REFRESH_SECONDS (padrão: 300s)
  → Cada fetchAll() chama GET /dados/operacional/{key} para todas as operações
  → mergeOp(newRows, opIdx) — nunca diminui o store
  → renderDashTable() → filterDash()
```

O refresh automático preserva o modo ativo (ex.: aba Entregador permanece ativa durante o refresh — `dashFilter === '__entr__'` é detectado e o reset é ignorado).

#### Versionamento e cache busting

- `{{APP_VERSION}}` é substituído no startup pelo `main.py`
- Formato: `vAAAA.MM.DD.<git-short-hash>` (ou `vAAAA.MM.DD.HHmm` sem git)
- Headers `no-cache` na rota `GET /` evitam cache de proxy/CDN
- Versão exibida no rodapé da sidebar

### 2.4 Fluxo de Upload → Dashboard

```
1. Usuário seleciona arquivo(s) .xlsx ou .zip
2. POST /upload/{tipo}
3. Arquivo salvo em dados/{tipo}/
4. _detect_key_from_path(dest) detecta operação (~0.4s, lê 330 linhas)
5. merge_incremental(tipo, key, dest)
   a. Extrai xlsx se .zip
   b. _read_xlsx() lê o novo arquivo (~2.5-3s)
   c. Dedup por Codigo (mantém Dt Evento mais recente)
   d. _compute_primeira_entrada() atualiza __primeira_entrada
   e. _cache[tipo][key] atualizado em memória
6. Dashboard atualizado na próxima chamada de fetchAll() (ou imediata)
```

**Performance (validado em produção):**

| Etapa                          | Tempo  |
|--------------------------------|--------|
| Rede (upload zip para Railway) | ~27s   |
| `_read_xlsx` (novo arquivo)    | ~3s    |
| Dedup em memória               | ~0.08s |
| `merge_incremental` TOTAL      | ~3.1s  |
| Total percebido pelo usuário   | ~30s   |

---

## 3. Regras Operacionais (SLA)

### 3.1 Status Possíveis

| Status              | Significado                              | Cor na UI |
|---------------------|------------------------------------------|-----------|
| `ENTREGUE`          | Entregue ao destinatário                 | Verde     |
| `ENTREGUE NO PICKUP`| Entregue em ponto de retirada            | Verde     |
| `EM ROTA`           | Em rota de entrega                       | Âmbar     |
| `EM ROTA PICKUP`    | Em rota para ponto de retirada           | Âmbar     |
| `ENTRADA`           | Recebido na torre, aguardando saída      | Azul      |
| `CUSTODIA`          | Ocorrência — sob custódia da torre       | Roxo      |
| `TRAVADO`           | Impedido de ser entregado                | Vermelho  |

### 3.2 SLA do Dia — Componentes

O "SLA do Dia" é calculado exclusivamente sobre remessas com **prazo = hoje** (`dDiff(Previsao) === 0`), sujeitas à **Regra das 10h** (ver seção 3.3).

```
Previsto Hoje = Entregues + Ocorrências + Faltam
             = slaEntregue + slaCustodia + vencHoje + vencidas

SLA% = slaEntregue / (slaEntregue + vencidas + vencHoje) * 100
Meta = 98%
```

| Componente    | Definição                                                                                  |
|---------------|--------------------------------------------------------------------------------------------|
| **Previsto**  | Total de remessas com prazo ≤ hoje (vencidas + vencem hoje)                                |
| **Entregues** | ENTREGUE ou ENTREGUE NO PICKUP, dentro do SLA, com evento hoje ou prazo hoje               |
| **Ocorrências** | CUSTODIA com prazo hoje — não penaliza SLA                                               |
| **Vencidas**  | Pendentes com prazo **passado** (`dDiff(Previsao) > 0`) — **sem** regra das 10h            |
| **Vencem Hoje** | Pendentes com prazo **hoje** (`dDiff(Previsao) === 0`) — sujeitas à regra das 10h        |
| **Faltam**    | Vencidas + Vencem Hoje (total de pendentes no prazo)                                       |

> **Importante:** Vencidas (prazo passado) nunca são excluídas pela regra das 10h. A regra só se aplica ao coorte do dia (prazo = hoje).

### 3.3 Regra das 10h — CRÍTICO

#### Problema que resolve

Remessas que chegam à torre após as 10h não têm tempo hábil para entrega no mesmo dia. Incluí-las no SLA do dia inflaria artificialmente o denominador e prejudicaria o indicador.

#### Definição correta

> Uma remessa é **excluída do SLA do dia** se a sua **primeira entrada operacional** na torre ocorreu **hoje** em horário **≥ 10:00**.

#### Campo `__primeira_entrada` (backend) — invariantes pós-879a787

**IMPORTANTE:** o `Performance.xlsx` é um **SNAPSHOT** (1 linha por Código com o
estado atual), não um log de eventos. A PE precisa ser tracked ao longo de
múltiplos uploads — o backend mantém isso no cache em memória.

```python
def _compute_primeira_entrada(rows, op_name=None):
    """
    PE real = MIN(Dt Evento) entre linhas onde:
       Status == 'ENTRADA' AND Hist. ultimo ponto == op_name
    """
```

Invariantes (cobertas por `tests/test_pe_compute.py` e `test_regressions.py`):

1. **PE = MIN absoluto** (jamais MAX, jamais "ENTRADA mais recente de hoje").
   Pacote com ENTRADA registrada ontem 09:00 e re-scan hoje 14:36 mantém PE=ontem 09:00.
2. **Filtro `Hist=nossa op` é obrigatório.** Linhas com `Hist. ultimo ponto`
   diferente da operação atual (pacote em trânsito por outra torre) NÃO
   contam para PE. Antes de `879a787` esse filtro não existia e PE proxy
   herdava `Dt Evento` de outra torre, causando inclusão indevida no SLA.
3. **PE nunca é apagada por update.** `merge_incremental` preserva `pe_old`
   ao substituir uma linha existente.
4. **PE real sempre vence PE proxy.** Quando uma ENTRADA real aparece num
   upload posterior, ela substitui o proxy.
5. **Entre 2 PE reais, vence a MENOR.**

#### Proxy PE para remessas sem evento ENTRADA na nossa torre

Quando um código nunca aparece com `Status=ENTRADA` em snapshot algum
(chega já como `EM ROTA`, `CUSTODIA`, etc.):

- **`_read_and_merge`** (startup / `refresh_all`): proxy = MIN(`Dt Evento`)
  entre linhas com `Hist=nossa op` em qualquer arquivo. Linhas com
  `Hist=outra torre` são descartadas.
- **`merge_incremental`** (upload): mesma regra aplicada ao novo arquivo;
  códigos novos OU existentes sem PE recebem o proxy.
- Flag `__pe_proxy=True` marca esses casos. Um real ENTRADA futuro substitui.

#### Campo `PrimeiraEntrada` (frontend)

Mapeado de `r['__primeira_entrada']` em `parseOp()`. **O backend é a fonte
de verdade** — `mergeOp()` confia no valor recebido e só protege contra
ser sobrescrito por vazio (proteção mínima). Não há lógica de MIN no
frontend (commit `4403b6b` removeu o enforce MIN que causava cascateamento
do valor antigo errado).

#### Função centralizada `entraNoSlaHoje(row)`

```javascript
function entraNoSlaHoje(row) {
  // Candidatos para "primeira entrada":
  //   1. PrimeiraEntrada (PE) recebida do backend
  //   2. Para Status=ENTRADA atual, DtEvento também é candidato (safety net)
  // Vence o MIN entre os candidatos.
  //
  // Resultado:
  //   sem candidato       → true (inclui por precaução)
  //   MIN < hoje          → true (histórico)
  //   MIN hoje < 10h      → true
  //   MIN hoje >= 10h     → false (EXCLUI)
  //   MIN > hoje (futuro) → true (anomalia, conservador)
}
```

**Esta é a ÚNICA função que implementa a regra das 10h no frontend.
Não duplicar esta lógica. Coberta por 10 testes em `tests/test_entra_no_sla.py`.**

#### Exemplos

| Situação                            | PrimeiraEntrada         | entraNoSlaHoje | Motivo                         |
|-------------------------------------|-------------------------|----------------|--------------------------------|
| Entrada ontem, em rota hoje         | `12/05/2026 14:30:00`   | `true`         | Entrada foi ontem — inclui     |
| Entrada hoje às 09:45               | `13/05/2026 09:45:00`   | `true`         | Antes das 10h — inclui         |
| Entrada hoje às 10:00               | `13/05/2026 10:00:00`   | `false`        | Exatamente 10h — **exclui**    |
| Entrada hoje às 14:30               | `13/05/2026 14:30:00`   | `false`        | Após 10h — **exclui**          |
| Sem PrimeiraEntrada (dado ausente)  | `""`                    | `true`         | Sem dado — inclui por precaução|

#### Pontos de aplicação

A regra se aplica a **todos** os componentes do SLA do dia, incluindo vencidas:
- `vencidas` (prazo passado, pendentes) — desde `cfb73d3`
- `vencHoje` (vencem hoje, pendentes)
- `slaEntregue` (entregues no prazo)
- `slaCustodia` (custódia SLA do dia)
- `openSlaDrawer` cases `prev`, `venc`, `vhoje`, `falt` — desde `4403b6b`
  (todos alinhados com `calcM`)
- `calcEntregadorSummary` (resumo por entregador)

> Antes (até `cfb73d3`) vencidas ignoravam a regra. Hoje todas as classificações
> consultam `entraNoSlaHoje` antes de incluir uma remessa. Testes em
> `tests/test_diag_audit.py` confirmam o comportamento.

### 3.4 Custódia

- `CUSTODIA` = remessa com ocorrência, não penaliza o SLA percentual diretamente.
- Para o **SLA do dia**: custódias com prazo **hoje** entram como `slaCustodia` (componente "Ocorrências" — aparecem no numerador/denominador como tratadas, não como pendentes).
- Custódias com prazo passado entram em **Vencidas** normalmente.
- A distinção é: `r.Status === 'CUSTODIA' && dDiff(r.Previsao) === 0` → ocorrência SLA dia.

### 3.5 Travado

- `TRAVADO` = remessa impedida de prosseguir (problema documental, endereço, etc.)
- **Dias** é calculado com `dcTrav(r)` = `dDiff(r.DtEvento)` (dias desde o último evento), **não** desde a previsão.
- A justificativa: a previsão já passou (sempre em atraso); o que interessa operacionalmente é há quantos dias está travada.
- Travadas **não entram no SLA do dia** (não são pendentes elegíveis).

### 3.6 Tela ENTRADA — Filtro "Críticas" (vencidas + hoje)

Botão **⚠ Críticas** no painel-hdr da tela ENTRADA (`page-entrada`). Quando
ligado, filtra a tabela para mostrar apenas remessas com prazo vencido ou
vencendo hoje:

```
Crítica = r.Status === "ENTRADA" AND r.Dias !== null AND r.Dias >= 0
         (Dias = dDiff(Previsao); >0 = vencida, 0 = vence hoje, <0 = futura)
```

Características:

- **Cumulativo com os filtros existentes** da tela: busca, entregador, cidade,
  faixa de dias, ordenação, operação atual.
- **Não altera o SLA principal** — é puramente visual/operacional na tela ENTRADA.
- **Contador** prefixa `"Críticas: N registros"` em vermelho quando ativo.
- **Export Excel respeita o filtro** — o que está visível é o que sai no arquivo
  (efeito do refactor que centralizou a lógica em `_filteredOpRows(type)`).
- Toggle: clica de novo para limpar.

Funções relevantes em `GestãoEntregas.html`:

| Função | Papel |
|---|---|
| `_filteredOpRows(type)` | Fonte única de remessas para tabela + export (rota/cust/entrada/trav) |
| `renderOp('entrada')` | Renderiza a tabela usando `_filteredOpRows` |
| `exportOp('entrada')` | Exporta xlsx usando `_filteredOpRows` (= o que está visível) |
| `toggleEntradaCriticas()` | Inverte o estado `_entradaCriticas`, atualiza classe do botão, re-render |

> **Bug menor corrigido junto:** antes desta feature, `exportOp` exportava
> todas as remessas do status ignorando os filtros visíveis. Agora as 4
> telas (rota/cust/entrada/trav) têm comportamento "exporta o que vê".

Adicionado em: PR #2 (commit `5e73725`, mergeado em 2026-05-18).

---

## 4. Aba Entregador

### 4.1 Resumo por Entregador (`calcEntregadorSummary`)

Exibido na pill **Entregador** da tabela Remessas do Dashboard.

**Base de cálculo:** `baseHoje` (d=0, entraNoSlaHoje) + `baseVencPend` (d>0, pendentes vencidas).

```
PREVISTO = vencidas reais + vencendo hoje reais
         = baseVencPend.length + baseHoje.length
```

| Coluna        | Definição                                                                      |
|---------------|--------------------------------------------------------------------------------|
| Previsto      | Total `baseHoje + baseVencPend` — inclui vencidas pendentes no denominador     |
| Entregues     | ENTREGUE ou ENTREGUE NO PICKUP dentro de `baseHoje`                            |
| Faltam        | Pendentes em `baseHoje` + todos de `baseVencPend`                              |
| Custódia      | CUSTODIA dentro de `baseHoje`                                                  |
| SLA%          | `round(entregues / previsto * 100)` — 0 se previsto = 0                        |

Ordenação: menor SLA primeiro, depois mais faltam.

> **Por que incluir vencidas no previsto:** remessas com prazo passado que ainda estão pendentes são responsabilidade do entregador e devem compor o denominador do SLA. Antes desta correção (2026-05-13), `Previsto` mostrava apenas as do dia (d=0), ignorando as atrasadas.

O nome do entregador é resolvido por `_entrNome(r)` = `(r.Rota || r.Entr || '').trim() || 'Sem entregador'`.

> **Por que Rota tem prioridade:** `Hist. ultimo operador` (`r.Entr`) foi encontrado vazio em 100% das linhas do GV (diagnóstico 2026-05-13). A coluna `Rota` (índice 11) é o campo operacional confiável para identificação do entregador/rota.

Remessas sem Rota e sem Hist. ultimo operador exibem **"Sem entregador"** — casos legítimos incluem status TRANSFERENCIA, EMISSAO e ENTRADA (sem rota atribuída ainda).

### 4.2 RISCO Operacional (`calcRisco`)

Badge visual que combina SLA atual com urgência horária.

```javascript
function calcRisco(s) {
  const h = new Date().getHours(); // hora local atual

  if (s.sla < 85 || (h >= 15 && s.faltam >= 3) || (h >= 17 && s.faltam > 0))
    return 'crit';  // 🔴 Crítico

  if (s.sla < 95 || (h >= 15 && s.faltam > 0))
    return 'aten';  // 🟡 Atenção

  return 'ok';      // 🟢 OK
}
```

| Nível         | Cor  | Condição                                                      |
|---------------|------|---------------------------------------------------------------|
| 🔴 **Crítico** | Vermelho | SLA < 85% **OU** (hora ≥ 15h E faltam ≥ 3) **OU** (hora ≥ 17h E faltam > 0) |
| 🟡 **Atenção** | Âmbar    | SLA < 95% **OU** (hora ≥ 15h E faltam > 0)                   |
| 🟢 **OK**      | Verde    | Demais casos                                                  |

**Lógica do fator horário:** à medida que o dia avança, tolerância diminui. Às 15h, 1 pacote faltando já é Atenção. Às 17h, qualquer pacote faltando é Crítico.

### 4.3 Drawer do Entregador (`openEntrDrawer`)

Modal fullscreen (96vw × 94vh) ativado ao clicar em uma linha da tabela Entregador. Centrado na tela com animação fade+slide e border-radius.

> **Visão operacional de ação imediata:** o painel lista APENAS remessas que afetam o SLA. Remessas futuras (d<0) não aparecem.

#### Estrutura do modal

```
┌──────────────────────────────────────────────────────────┐
│ [Nome do Entregador]   [🔴 Crítico]  [⬇ Excel] [✕ Fechar]│ ← header vermelho
├──────────────────────────────────────────────────────────┤
│ PREVISTO │ ENTREGUES │ FALTAM │ CUSTÓDIA │ SLA%           │ ← stats grid (5 cols)
├──────────────────────────────────────────────────────────┤
│ PROGRESSO SLA  ████░░░░░░  X de Y entregues              │ ← barra de progresso
├──────────────────────────────────────────────────────────┤
│ VENCENDO HOJE (N remessas)                               │ ← baseHoje, pendentes
│  Código | Cidade | Bairro | Dest. | Status | ...         │
├──────────────────────────────────────────────────────────┤
│ VENCIDAS (N remessas)                                    │ ← baseVencPend (d>0)
├──────────────────────────────────────────────────────────┤
│ ENTREGUES (N remessas)                                   │ ← baseHoje, entregues
├──────────────────────────────────────────────────────────┤
│ CUSTÓDIA (N remessas)                                    │ ← baseHoje, custódia
├──────────────────────────────────────────────────────────┤
│ TRAVADAS (N remessas)                                    │ ← baseTrav (d>0, TRAVADO)
└──────────────────────────────────────────────────────────┘
```

#### Bases de dados por grupo de seções

O drawer usa **três bases separadas** para evitar contaminação entre coortes:

```javascript
// Coorte do dia: vence hoje + regra 10h (Entregues, Custódia, Vencendo Hoje)
const baseHoje = dashRows.filter(r =>
    _entrNome(r) === entr && entraNoSlaHoje(r) && dDiff(r.Previsao) === 0
);

// Vencidas pendentes: prazo passado, ainda não entregues (sem regra 10h)
const baseVencPend = dashRows.filter(r =>
    _entrNome(r) === entr && dDiff(r.Previsao) > 0 && isPend(r)
);

// Travadas vencidas
const baseTrav = dashRows.filter(r =>
    _entrNome(r) === entr && dDiff(r.Previsao) > 0 && isTrav(r)
);
```

> **Por que bases separadas:** usar `slaRows` unificada (d<=0) incluía remessas entregues com prazo semanas atrás na seção "Entregues", inflando a contagem. A separação garante que Entregues e Custódia mostram apenas o coorte do dia, enquanto Vencidas lista exclusivamente pendentes atrasadas.

#### Fonte de dados por seção

| Seção          | Base           | Filtro adicional              | Função Dias            |
|----------------|----------------|-------------------------------|------------------------|
| Vencendo Hoje  | `baseHoje`     | `isPend(r)`                   | `dc(r.Dias, r.Status)` |
| Vencidas       | `baseVencPend` | (já filtrado)                 | `dc(r.Dias, r.Status)` |
| Entregues      | `baseHoje`     | `isEnt(r)`                    | `dc(r.Dias, r.Status)` |
| Custódia       | `baseHoje`     | `isCust(r)`                   | `dc(r.Dias, r.Status)` |
| Travadas       | `baseTrav`     | (já filtrado)                 | `dcTrav(r)` (DtEvento) |

#### Export Excel do drawer (`exportEntrDrawerCurrent`)

Botão **⬇ Excel** no cabeçalho do modal. Exporta somente remessas SLA do entregador aberto, usando as mesmas três bases do drawer.

- Aba **Remessas SLA**: `baseHoje` + `baseVencPend` + `baseTrav` com colunas:
  `Tipo SLA | Código | Cidade | Bairro | Destinatário | Status | Dt Evento | Previsão | Entregador/Rota | Dias`
- Coluna **Tipo SLA**: `"Vence Hoje"` (baseHoje), `"Vencida"` (baseVencPend), `"Travada"` (baseTrav)
- Aba **Resumo**: uma linha com `Entregador | SLA% | Previsto | Entregues | Faltam | Custódia | Risco`
- Arquivo: `Entregador_{nome}_{data}.xlsx`

#### Campos exibidos nas tabelas do drawer

`Código | Cidade | Bairro | Destinatário | Status | Dt Evento | Previsão | Dias`

- Código é clicável → abre histórico da remessa (`openCodeHistory`)
- Tabelas têm scroll horizontal (notebook-safe)
- Seções vazias são ocultadas automaticamente

---

## 5. Estrutura de Dados

### 5.1 Arquivo Operacional (.xlsx)

Colunas raw do arquivo exportado do sistema Jadlog:

| Coluna raw                | Mapeamento frontend | Descrição                                      |
|---------------------------|---------------------|------------------------------------------------|
| `Codigo`                  | `r.Codigo`          | Código da remessa (chave primária)             |
| `Cidade`                  | `r.Cidade`          | Cidade de destino (normalizada com `cap()`)    |
| `Bairro`                  | `r.Bairro`          | Bairro de destino                              |
| `Destinatario` (variante) | `r.Dest`            | Nome do destinatário (busca flexível de coluna)|
| `Status`                  | `r.Status`          | Status atual (uppercased)                      |
| `Dt Evento`               | `r.DtEvento`        | Data/hora do último evento (`DD/MM/YYYY HH:MM:SS`) |
| `Previsao`                | `r.Previsao`        | Prazo de entrega (`DD/MM/YYYY`)                |
| `Rota`                    | `r.Rota`            | Rota/entregador — **campo primário** para `_entrNome()` |
| `Hist. ultimo operador`   | `r.Entr`            | Nome do operador — fallback em `_entrNome()` (frequentemente vazio) |
| `Hist. ultimo ponto`      | (filtro parseOp)    | Torre operacional — usado para filtrar remessas|
| `Hist. ultima descricao`  | `r.Desc`            | Descrição do último evento                     |
| `__primeira_entrada`      | `r.PrimeiraEntrada` | Injetado pelo backend — ver seção 3.3          |

**Campo virtual calculado no frontend:**

| Campo    | Cálculo                                 | Descrição                                    |
|----------|-----------------------------------------|----------------------------------------------|
| `r.Dias` | `dDiff(r.Previsao)` = dias desde prazo  | ≥ 0 = atrasado, 0 = vence hoje, < 0 = futuro |
| `r.opIdx`| índice 0-3 de `OPS[]`                   | Identifica a operação no store `opMaps`       |

### 5.2 Arquivo Financeiro (.xlsx)

| Coluna raw         | Mapeamento  | Descrição                    |
|--------------------|-------------|------------------------------|
| `NCTE`             | `r.ncte`    | Número do conhecimento        |
| `CidadeDestino`    | `r.cidade`  | Cidade de destino             |
| `Rota`             | `r.rota`    | Código de rota                |
| `Data`             | `r.data`    | Data da remessa               |
| `TaxaEntrega`      | `r.txE`     | Taxa de entrega local         |
| `TaxaInterior`     | `r.txI`     | Taxa de entrega interior      |
| (calculado)        | `r.total`   | `txE + txI`                   |
| `Hist. ultimo operador` | `r.entr` | Entregador                  |

### 5.3 Dedup e Merge

**No backend (`merge_incremental` e `_read_and_merge`):**
- Chave: `Codigo`
- Conflito: vence o registro com `Dt Evento` mais recente
- `__primeira_entrada`: nunca sobrescrita com data posterior (sempre preserva a mais antiga)

**No frontend (`mergeOp`):**
- `Object.assign(ex, row)` — novo evento sobrescreve o existente
- `PrimeiraEntrada` é explicitamente protegida: `if(!ex.PrimeiraEntrada && prevPE) ex.PrimeiraEntrada = prevPE`
- O store `opMaps` nunca diminui (remessas que saem do relatório mantêm o último status)

### 5.4 Filtro de Operação em `parseOp`

```javascript
// Regra estrita: só inclui remessa se Hist. ultimo ponto === opName exato
// Fallback: se nenhuma linha do arquivo tiver operação conhecida,
//           inclui todas com Codigo (formato single-op antigo)
```

Isso evita inflar custódia e outros cards com remessas em trânsito por outras torres (`TC TECA`, `FL BH`, `PA PICKUP`, etc.).

---

## 6. Autenticação e Perfis

### 6.1 JWT

- Algoritmo: HS256
- Expiração: 12 horas
- Secret: env `JWT_SECRET` (obrigatório em produção — nunca usar o default)
- Payload inclui: `sub` (login), `role`, `nome`, `must_change_password`

### 6.2 Perfis e Permissões

| Perfil         | Ver Operacional | Ver Financeiro | Upload | Admin |
|----------------|:-:|:-:|:-:|:-:|
| `DIRETOR`      | ✓ | ✓ | ✓ | ✓ |
| `SUPERVISAO`   | ✓ | ✓ | ✓ | — |
| `OPERACIONAL`  | ✓ | — | ✓ | — |
| `FINANCEIRO`   | — | ✓ | ✓ | — |
| `VISUALIZACAO` | ✓ | ✓ | — | — |

### 6.3 Usuário Admin Padrão

- Login: `admin`
- Senha inicial: env `DEFAULT_ADMIN_PASSWORD` (default: `Jadlog2026`)
- `must_change_password = true` no primeiro login

### 6.4 Hash de Senhas

bcrypt direto (sem passlib). Historicamente, passlib causou corrupção de hashes — **não reverter para passlib**.

---

## 7. Convenções de Código

### 7.1 Princípio fundamental

> **Regras de negócio são implementadas UMA vez, em um helper centralizado, e reutilizadas.**

Nunca duplicar:
- `entraNoSlaHoje(row)` — regra das 10h
- `_entrNome(r)` — nome do entregador operacional (`r.Rota||r.Entr||'Sem entregador'`)
- `dDiff(s)` — cálculo de dias
- `parseDate(s)` — parse de datas
- `bc(status)` — classe CSS do badge de status
- `dc(dias, status)` — texto de dias com cor
- `dcTrav(r)` — dias travado (usa DtEvento)
- `calcM(rows)` — métricas SLA
- `_compute_primeira_entrada(rows)` — backend, primeira entrada por Codigo

### 7.2 Nomes de Variáveis e Funções

| Padrão    | Uso                                          |
|-----------|----------------------------------------------|
| `camelCase` | Funções e variáveis JS                     |
| `snake_case` | Funções Python                            |
| `_cache`  | Prefixo `_` = variável global de estado (Python) |
| `r.`      | Acesso a campos de remessa (frontend)         |
| `tipo`    | `"operacional"` ou `"financeiro"`             |
| `key`     | `"gv"`, `"itabira"`, `"jm"`, `"curvelo"`     |
| `opIdx`   | Índice inteiro 0-3 da operação                |

### 7.3 Commits

Formato: `tipo: descrição concisa em português`

Tipos usados: `feat`, `fix`, `perf`, `refactor`, `diag`, `docs`

Exemplos do histórico:
```
feat: drawer lateral por entregador com coluna RISCO
perf: merge incremental no upload (~4s constante)
fix: regra das 10h baseada na primeira entrada operacional
```

### 7.4 APP_VERSION

```python
def _get_version() -> str:
    # Formato: vAAAA.MM.DD.<git-short-hash>
    # Fallback: vAAAA.MM.DD.HHmm (sem git no ambiente)
```

O placeholder `{{APP_VERSION}}` no HTML é substituído no startup e exibido no rodapé da sidebar. Permite identificar a versão em produção sem abrir o código.

### 7.5 Responsividade

Resolução mínima testada: **1280×768** (notebook).

- **Drawer SLA** (`#drawer`): painel lateral, max 760px. Tabelas internas com `overflow-x: auto`.
- **Modal Entregador** (`#entr-drawer`): modal fullscreen centrado, `min(96vw, 1360px) × 94vh`. Funciona bem de notebook a widescreen.

---

## 8. Regras de Manutenção

### 8.1 O que NUNCA quebrar

| Sistema               | Localização                              | Risco se quebrado              |
|-----------------------|------------------------------------------|--------------------------------|
| Regra das 10h         | `entraNoSlaHoje()` + `_compute_primeira_entrada()` | SLA inflado/deflado artificialmente |
| Auto-refresh          | `startAutoRefresh()` + `fetchAll()`      | Dashboard para de atualizar    |
| Merge incremental     | `merge_incremental()` em main.py         | Upload volta a 49s+            |
| Cache never-shrinks   | `mergeOp()` no frontend                  | Remessas "somem" do dashboard  |
| Export Excel          | `exportDash()`, `exportOp()`, `exportDrawer()`, `exportEntrDrawerCurrent()` | Equipe perde ferramenta de trabalho |
| Drawer SLA (`#drawer`)| `openSlaDrawer()`, `openDrawerWithRows()` | Cards do gauge param de funcionar |
| Aba Entregador        | `calcEntregadorSummary()`, `filterDash()`, `_entrNome()` | Visão por entregador some |
| `PrimeiraEntrada` protect | `mergeOp()` preserva prevPE         | Regra das 10h falha no frontend |

### 8.2 Antes de qualquer alteração em SLA

Verificar todos os pontos de uso de `entraNoSlaHoje`:
```
calcM()               → vencHoje, slaEntregue, slaCustodia
openSlaDrawer()       → prev, entr, ocorr, vhoje, falt
calcEntregadorSummary() → baseHoje (d=0 + entraNoSlaHoje)
openEntrDrawer()      → baseHoje (d=0 + entraNoSlaHoje)
```

Usar `/diag/sla?codigos=...` para validar casos suspeitos em produção sem alterar código.

### 8.3 Ao adicionar nova coluna no xlsx

1. Adicionar o mapeamento em `parseOp()` no frontend
2. Se a coluna precisa de tratamento especial no backend, adicionar em `_read_and_merge()` / `merge_incremental()`
3. Atualizar seção 5.1 deste documento

### 8.4 Performance

- `_read_xlsx` usa `openpyxl` read-only + pandas vetorizado. Não usar `iterrows()`.
- `merge_incremental` deve permanecer ~3-5s. Se ultrapassar 10s, investigar.
- `refresh_all` no startup pode levar 60-120s com muitos arquivos Performance.xlsx (GV ~13-16s cada).
- `REFRESH_SECONDS = 300` é configurável via env. Não reduzir abaixo de 120s em produção.

### 8.5 Sempre validar após mudança

- [ ] Sem `console.error` na aba Entregador e no modal
- [ ] Auto-refresh preserva o modo ativo da pill
- [ ] Overlay do drawer SLA e overlay do modal entregador fecham corretamente
- [ ] Export Excel funciona em todos os modos (TODOS, filtros, Aba Entregador, Modal Entregador)
- [ ] Drawer SLA original (`#drawer`) ainda abre e fecha
- [ ] Modal Entregador mostra somente remessas SLA (sem remessas futuras)
- [ ] Layout sem quebras em 1280×768

---

## 9. Fluxos Críticos

### 9.1 Upload Operacional

```
1. Usuário clica Enviar em /importar
2. POST /upload/operacional  (multipart, múltiplos arquivos)
3. Para cada arquivo:
   a. Salvo em OP_DIR (/data/dados/operacional/)
   b. _detect_key_from_path(dest)  → key ou None
   c. Se key detectada:
        merge_incremental(tipo, key, dest)  → ~3s
      Se key não detectada:
        refresh_tipo("operacional")  → relê os 4 arquivos
4. Response JSON: {operacao, tipo, linhas, arquivo}
5. Frontend mostra resultado + sugere Atualizar
```

### 9.2 Upload Financeiro

Mesmo fluxo, substituindo `OP_DIR` por `FIN_DIR` e `tipo = "financeiro"`.

### 9.3 Merge Incremental (núcleo)

```python
merge_incremental(tipo, key, src_path):
    1. Extrai xlsx do zip se necessário (temp dir)
    2. _read_xlsx(xlsx_path)              # ~2.5-3s
    3. Lock _cache
    4. Para cada row existente:
           merged[codigo] = row           # base = cache atual
    5. Para cada row novo (rastreia new_codes):
           if codigo em merged:
               if dt_evento_novo >= dt_evento_existente:
                   merged[codigo] = row_novo  # novo vence
                   preservar __primeira_entrada se novo for mais tardio
           else:
               merged[codigo] = row_novo      # novo código
               new_codes.add(codigo)
    6. Calcular nova_pe = _compute_primeira_entrada(new_rows)
       Atualizar __primeira_entrada se mais antiga que a existente
    7. Proxy PE: para codigo em new_codes sem PE após nova_pe,
       usar Dt Evento do row como __primeira_entrada
    8. Rebuild entry: linhas, dados, atualizado, arquivo
    9. _cache[tipo][key] = nova_entry
   10. Release lock
```

### 9.4a Diagnóstico SLA (`/diag/sla`)

Endpoint para investigar remessas suspeitas no SLA em produção.

```
GET /diag/sla?codigos=COD1,COD2,COD3
Requer: DIRETOR, SUPERVISAO, OPERACIONAL ou FINANCEIRO
```

Retorna para cada código encontrado no cache:

| Campo              | Descrição                                              |
|--------------------|--------------------------------------------------------|
| `codigo`           | Código da remessa                                      |
| `tipo` / `key`     | Localização no cache (ex: operacional/gv)              |
| `status`           | Status atual                                           |
| `previsao`         | Prazo de entrega                                       |
| `ddiff`            | Dias desde o prazo (0=hoje, >0=vencida, <0=futura)     |
| `dt_evento`        | Data do último evento                                  |
| `primeira_entrada` | Valor de `__primeira_entrada` no cache                 |
| `entraNoSlaHoje`   | `true`=inclusa no SLA / `false`=excluída               |
| `motivo`           | Explicação textual da decisão (`PE hoje HH:MM >= 10h → EXCLUI`) |

### 9.4 Cálculo SLA (`calcM`)

```javascript
calcM(rows):
  Filtra por operação atual (currentOp) ou todas
  Aplica filtro de período (periodFrom/periodTo) se ativo

  vencHoje    = rows onde dDiff(Previsao)===0 && isPending && entraNoSlaHoje
  slaEntregue = rows ENTREGUE(S) && inSLA && (evento hoje || vence hoje) && entraNoSlaHoje
  slaCustodia = rows CUSTODIA && vence hoje && entraNoSlaHoje
  vencidas    = rows pendentes && dDiff(Previsao)>0  [sem regra 10h]
  faltam      = vencidas + vencHoje
  previsto    = slaEntregue + slaCustodia + vencidas + vencHoje
  sla%        = slaEntregue / (slaEntregue + vencidas + vencHoje) * 100
```

### 9.5 Export Excel (frontend)

Biblioteca: `xlsx.js` (SheetJS CDN).

| Modo                   | Arquivo gerado                          | Conteúdo                                            |
|------------------------|-----------------------------------------|-----------------------------------------------------|
| Dashboard TODOS/filtro | `Dashboard_DD-MM-AAAA.xlsx`             | Remessas filtradas visíveis                         |
| Aba Entregador (geral) | `SLA_Entregador_DD-MM-AAAA.xlsx`        | `_entrDetailRows` + stats por entregador (todos)    |
| Modal Entregador       | `Entregador_{nome}_DD-MM-AAAA.xlsx`     | Apenas remessas SLA do entregador aberto + resumo   |
| Drawers SLA            | `SLA_{tipo}_DD-MM-AAAA.xlsx`            | Remessas do drawer com todos os campos              |
| Páginas Op             | `{Status}_DD-MM-AAAA.xlsx`              | Em Rota / Entrada / Custódia / Travadas             |

### 9.6 Refresh Automático (frontend)

```javascript
startAutoRefresh():
  setTimeout(loop, REFRESH_MS)  // padrão: 300.000ms = 5 min

loop():
  fetchAll(false)   // false = silent (sem loading overlay)
  setTimeout(loop, REFRESH_MS)

fetchAll(showLoading):
  Para cada op em OPS:
    GET /dados/operacional/{key}
    GET /dados/financeiro/{key}
  mergeOp(newRows, opIdx)     // nunca diminui store
  renderDashTable(rows)       // preserva dashFilter === '__entr__'
  renderOp('rota','cust','entrada','trav')
```

---

## 10. Troubleshooting

### 10.1 Dashboard não atualiza após upload

**Causa mais comum:** o frontend não chamou `fetchAll()` após o upload.  
**Solução:** clicar no botão "↻ Atualizar" ou aguardar o ciclo de 300s.  
**Verificar:** o retorno do POST `/upload/{tipo}` deve conter `linhas > 0`.

### 10.2 Cache do navegador servindo HTML antigo

**Sintoma:** versão no rodapé não bate com o commit atual.  
**Solução:** `Ctrl+Shift+R` (hard refresh) ou limpar cache do navegador.  
**Por que acontece:** alguns proxies ignoram os headers `no-cache` servidos pelo FastAPI.

### 10.3 Railway — deploy não reflete mudanças

**Verificação:** checar se o `APP_VERSION` no rodapé bate com o hash do commit no GitHub.  
**Solução:** aguardar 2-5 min após o push. Se não atualizar, forçar redeploy no painel Railway.  
**Causa comum:** Railway às vezes demora a detectar o webhook do GitHub.

### 10.4 Startup lento (> 60s)

**Causa:** arquivos `Performance.xlsx` do GV são grandes (~18k linhas, ~5MB comprimido) e levam ~13-16s cada para processar com openpyxl.  
**Comportamento normal:** startup com múltiplos arquivos Performance.xlsx pode levar 60-120s.  
**Mitigação futura:** investigar engine `calamine` para leitura de xlsx (potencial ganho de 5-10×).  
**Impacto:** o servidor só responde a requisições após o `startup()` completar.

### 10.5 Upload lento (> 60s percebidos)

**Diagnóstico:** verificar se o tempo é de rede (~27s) ou processamento (>30s).  
**Normal:** ~30s total (27s rede + 3s processamento).  
**Problema real:** se processamento > 10s, o `merge_incremental` pode ter caído para `refresh_tipo` por falha de detecção de chave.  
**Verificar:** logs do Railway em `[merge_incremental]` e `[refresh_tipo]`.

### 10.6 Conflito de arquivo — "OneDrive"

**Sintoma:** arquivo com "(1)" ou " - Cópia" no nome enviado por engano.  
**Causa:** OneDrive cria cópias conflitantes de arquivos.  
**Solução:** renomear o arquivo antes de enviar, ou aceitar que o sistema detecta a operação pelo conteúdo, não pelo nome.

### 10.7 Remessa aparece indevidamente no SLA (entrou hoje após 10h)

**Diagnóstico:** acessar `/diag/sla?codigos=CODIGO` e verificar o campo `primeira_entrada` e `motivo`.

**Causas possíveis:**

| Causa | Sintoma no `/diag/sla` | Solução |
|-------|------------------------|---------|
| `__primeira_entrada` vazio | `"primeira_entrada": "(vazio)"`, `motivo: "PE vazia → fallback true"` | Proxy não foi aplicado — verificar se o código entrou via `refresh_all` antes do fix de 2026-05-13. Aguardar próximo `refresh_all` (300s) ou upload. |
| PE com data anterior a hoje | `motivo: "PE DD/MM < hoje → inclui"` | Correto — remessa entrou antes de hoje e deve compor o SLA. |
| PE hoje < 10h | `motivo: "PE hoje HH:MM < 10h → inclui"` | Correto — entrou antes das 10h. |
| PE hoje ≥ 10h | `motivo: "PE hoje HH:MM >= 10h → EXCLUI"` | Correto — deve estar excluída. Se ainda aparece no painel, verificar se `entraNoSlaHoje` está sendo chamado naquele ponto. |

### 10.8 Remessa não aparece no SLA do dia

**Verificar em ordem:**
1. `dDiff(r.Previsao)` = 0? (prazo = hoje)
2. `entraNoSlaHoje(r)` = true? (PrimeiraEntrada antes das 10h ou de dia anterior)
3. Status é elegível? (ENTREGUE, EM ROTA, ENTRADA, CUSTODIA)
4. Operação correta? (`Hist. ultimo ponto` bate com a operação selecionada)

### 10.9 Drawer do Entregador vazio

**Causa provável:** `_entrDetailRows` está vazio porque `calcEntregadorSummary` ainda não foi chamado (pill Entregador nunca foi clicada nessa sessão).  
**Solução:** o drawer deve ser aberto apenas através da tabela da pill Entregador (nunca diretamente via URL ou programaticamente sem passar pelo `filterDash`).

---

## 11. Roadmap

### 11.1 Curto Prazo (próximas semanas)

- [ ] **Fix D — Performance.xlsx:** Investigar engine `calamine` para reduzir ~13s → ~2s por arquivo no startup/refresh
- [ ] **Deletar projeto vigilant-commitment (cf510)** após confirmar estabilidade em produção
- [ ] **Branches main/dev:** separar ciclo de desenvolvimento de produção
- [ ] **Dedup cross-op no refresh_all:** `_read_and_merge` não protege contra upload de arquivo mais antigo (Caso C — regressão de status). Risco baixo em uso normal.

### 11.2 Médio Prazo

- [ ] **Alertas automáticos:** notificação quando SLA cair abaixo de 90%, ou quando entregador ficar Crítico por > 30 min
- [ ] **Histórico SLA por dia:** gráfico de tendência de SLA ao longo do mês (hoje só tem SLA acumulado snapshot manual)
- [ ] **Exportação agendada:** gerar relatório Excel automaticamente ao fim do dia
- [ ] **Filtros avançados no drawer:** busca por código dentro do drawer do entregador
- [ ] **Notificações push:** alertar supervisão sobre remessas travadas ou vencidas via Web Push API

### 11.3 Longo Prazo

- [ ] **Mapa operacional:** visualização geográfica das entregas em rota (Leaflet.js / Google Maps)
- [ ] **PostgreSQL:** migrar de SQLite para Postgres (Railway nativo) para suporte multi-instância e consultas históricas
- [ ] **Mobile:** responsividade completa para tablet/smartphone (gestores em campo)
- [ ] **API de integração:** endpoint para exportar dados SLA para BI externo (Power BI, Google Sheets)
- [ ] **Multi-empresa:** suporte a múltiplas filiais Jadlog com login separado por empresa
- [ ] **OCR/Upload automático:** integração com e-mail ou pasta compartilhada para upload automático dos relatórios Jadlog

---

## 12. Backup e Restore

> Atalho operacional: `python tools/backup_data.py` gera um tarball
> `.tar.gz` em `backups/` com tudo do `/data` + um manifest com sha256.

### 12.1 O que entra no backup

| Conteúdo | Origem (em produção) |
|---|---|
| `users.db` | `/data/users.db` (SQLite com usuários) |
| `dados/operacional/*` | `/data/dados/operacional/*.xlsx,*.zip` |
| `dados/financeiro/*` | `/data/dados/financeiro/*.xlsx,*.zip` |
| `manifest.json` | gerado: `version`, `created_at`, lista de arquivos com `size` + `sha256` |

Arquivos temporários do Excel (`~$*`) são ignorados.

### 12.2 Como gerar backup local

```
python tools/backup_data.py                      # usa DATA_DIR ou raiz do projeto
python tools/backup_data.py --data-dir /caminho  # origem custom
python tools/backup_data.py --out /alvo/bkp.tar.gz
```

Saída padrão: `backups/backup-YYYYMMDD-HHMMSS.tar.gz`. A pasta `backups/`
está no `.gitignore` — nunca vai para o repo.

### 12.3 Como validar um backup (sem extrair)

```
python tools/restore_data.py <arquivo.tar.gz> --dry-run --data-dir /tmp/x
```

O `--dry-run`:
1. Lê o manifest.
2. Confere o sha256 de cada arquivo dentro do tarball.
3. Imprime versão + total de arquivos + bytes.
4. **Não escreve nada.**

Se algum sha256 não bater, o script termina com erro.

### 12.4 Como restaurar localmente

```
python tools/restore_data.py <arquivo.tar.gz> --data-dir /caminho/destino
```

Fluxo seguro:
1. Lê manifest + valida sha256 de todos os arquivos.
2. Pergunta `Confirma sobrescrever ...? [y/N]` (use `--yes` para pular).
3. **Cria pré-backup automático** do estado atual do destino em
   `backups/prebackup-YYYYMMDD-HHMMSS.tar.gz`.
4. Extrai os arquivos do tarball mantendo a estrutura.

Se algo der errado depois, o pré-backup permite reverter manualmente
rodando o restore com ele como input.

### 12.5 Cuidados em produção

**Por default o restore RECUSA rodar em produção.** Ele detecta o ambiente
pelas envs `RAILWAY_ENVIRONMENT`, `RENDER` ou `PRODUCTION` e aborta.

Se for absolutamente necessário restaurar em produção:

```
RAILWAY_ENVIRONMENT=production \
  python tools/restore_data.py <arquivo.tar.gz> \
    --data-dir /data \
    --allow-production
```

Antes de fazer isso:
- Confirme com a equipe.
- Tenha o backup ANTIGO em mãos (pré-backup é criado, mas redundância nunca é demais).
- Faça em janela de baixa atividade.
- Depois do restore, chame `POST /admin/rebuild-cache` para o `_cache` em
  memória refletir os novos arquivos.

### 12.6 Armazenamento externo (não automatizado ainda)

Hoje os backups ficam apenas em `backups/` no host local. Para
guardar histórico fora do servidor (S3, R2, Backblaze) basta `scp`/`rclone`
para o destino após o `backup_data.py`. Automação via cron / Railway job
não está incluída nesta etapa — fica para a próxima rodada (se decidirmos).

---

## 13. Endpoints de Saúde e Diagnóstico

Tabela completa dos endpoints introduzidos nas etapas P4-P5 (branch
`dev/profissionalizacao-core`).

### 13.1 Health

| Endpoint | Auth | Custo | Função |
|---|---|---|---|
| `GET /healthz` | nenhuma | <1ms | Liveness: responde se o processo está vivo. Útil para `healthcheckPath` no Railway. |
| `GET /readyz` | nenhuma | <50ms | Readiness: cache populado, `users.db` acessível, `/data` acessível. **Retorna 503 se algum check falha.** |
| `GET /health/sla` | gestor | 1-3s | Saúde operacional do SLA: counts, SLA%, inconsistências, idade do cache, status `ok/warning/critical`. |

**Status do `/health/sla`** (regra em `_classify_sla_status`):
- `critical`: inconsistências>0, total_sla=0, idade_cache>1h, ou sla<85%
- `warning`: idade_cache>30min ou sla<95%
- `ok`: caso contrário

### 13.2 Auditoria SLA

| Endpoint | Auth | Função |
|---|---|---|
| `GET /diag/sla?codigos=A,B,C` | gestor | **Modo legado:** diagnóstico detalhado por código (inclui `motivo` da inclusão/exclusão). Use para investigar uma remessa específica. |
| `GET /diag/sla?mode=audit` | gestor | **Auditoria do painel.** Resumo + auditoria (idade cache, inconsistências). Default sem detalhes (resposta ~2KB). |

Parâmetros do `mode=audit`:

| Param | Default | Efeito |
|---|---|---|
| `op` | — | filtra operação: `gv\|itabira\|jm\|curvelo` |
| `codigos` | — | filtra códigos específicos (CSV) |
| `details=1` | 0 | inclui lista `detalhes` por remessa |
| `categoria` | — | `VENCIDAS\|VENCEM_HOJE\|ENTREGUES_SLA\|CUSTODIA_SLA\|FORA_SLA` |
| `apenas_inconsistencias=true` | false | só inconsistências estritas |
| `apenas_excluidas_10h=true` | false | só remessas excluídas pela regra |
| `limit` | 0 | trunca `detalhes` |
| `offset` | 0 | paginação |
| `export=csv` | — | força `details=1` e devolve CSV |

A auditoria roda em thread executor (não bloqueia event loop) e tem log
de timing: `[diag/audit] op=X dash=N build=Ys classify=Ys total=Ys ...`.

### 13.3 Operacional

| Endpoint | Auth | Função |
|---|---|---|
| `POST /admin/rebuild-cache` | DIRETOR | Força `refresh_all()` do zero. Use após mudança de regra ou suspeita de cache inconsistente. |
| `GET /admin/rebuild-cache` | DIRETOR | Alias para chamar via browser. |
| `GET /status` | qualquer auth | Resumo por operação (linhas, arquivo, atualizado, erro). |

### 13.4 Como usar via console do navegador

Para chamar qualquer endpoint autenticado sem ferramentas externas:

```javascript
(async () => {
  const t = localStorage.getItem('auth_token');
  const r = await fetch('/diag/sla?mode=audit', {
    headers: { Authorization: 'Bearer ' + t }
  });
  console.log(await r.json());
})();
```

---

## 14. Testes e CI

### 14.1 Estrutura

```
tests/
├── conftest.py                  # fixtures: today fixo (15/05/2026), cache reset
├── helpers.py                   # make_row + write_xlsx sintético
├── test_pe_compute.py           # 9 — _compute_primeira_entrada
├── test_read_and_merge.py       # 7 — pipeline completo + filtro Hist
├── test_merge_incremental.py    # 7 — upload incremental, PE preservada
├── test_entra_no_sla.py         # 10 — regra das 10h, fronteiras
├── test_diag_audit.py           # 10 — buckets, filtros, paginação, idade cache
├── test_regressions.py          # 4 — bugs já corrigidos
├── test_health.py               # 15 — /healthz, /readyz, /health/sla
├── test_logging.py              # 9 — request_id, formatters, middleware
└── test_backup_restore.py       # 12 — backup/restore + manifest sha256
```

**Total: 89 testes em ~3s.**

Cada bug resolvido vira teste permanente (`@pytest.mark.regression`):

| Commit | Teste de regressão |
|---|---|
| `879a787` | `test_regression_879a787_proxy_outra_torre_nao_vira_pe` |
| `3e5ef2f` | `test_3e5ef2f_pe_min_sem_priorizar_hoje` |
| `4403b6b` | `test_4403b6b_mergeop_nao_cascateia_pe_antiga` |
| `879a787` (proxy) | `test_879a787_merge_incremental_filtra_hist_no_proxy` |

### 14.2 Como rodar localmente

```bash
pip install -r requirements-dev.txt   # inclui pytest, ruff, httpx

pytest -q                # 89 testes, ~3s
pytest -v                # com detalhes
pytest -m regression     # só testes de regressão
pytest tests/test_pe_compute.py::test_min_entrada_quando_so_uma_linha  # 1 só

ruff check main.py tests/                     # lint
python -m compileall -q main.py tests/        # syntax check
```

### 14.3 CI (GitHub Actions)

Workflow `.github/workflows/ci.yml`, dispara em:
- `push` em qualquer branch
- `pull_request` para `main`

Matriz **Python 3.11 + 3.12**, sem deploy, sem segredos:
1. compileall (syntax)
2. ruff check
3. pytest

`concurrency` cancela run anterior se houver novo push no mesmo branch.
`permissions: contents: read` — workflow não pode commitar nem push.

### 14.4 Branch protection (config manual no GitHub UI)

Recomendado em `Settings → Branches → Add rule` para `main`:
- ✅ Require status checks: `test (python 3.11)`, `test (python 3.12)`
- ✅ Require branches up to date before merging
- ✅ Require pull request before merging

Bloqueia merge direto para `main` sem CI verde.

---

## 15. Observabilidade (logs estruturados e request_id)

### 15.1 Request ID por requisição

Cada requisição recebe um `request_id` único (uuid4 8 bytes):

- **Entrada**: middleware lê `X-Request-ID` do header (se cliente forneceu)
  ou gera um novo de 12 chars hex.
- **Durante**: ContextVars (`_request_id_ctx`, `_user_login_ctx`,
  `_user_role_ctx`) carregam o ID + usuário pela execução inteira da
  requisição, mesmo em chamadas assíncronas paralelas.
- **Saída**: middleware devolve `X-Request-ID` no header da response.
- **Logs**: filter (`_ContextFilter`) injeta os 3 ctxvars em **todo**
  `LogRecord` automaticamente — não precisou tocar em nenhuma das ~80
  chamadas `log.info()` existentes.

### 15.2 Formatos

Default (`LOG_JSON=0`):

```
2026-05-15T18:00:00Z INFO  req=ab12cd34efgh user=admin  [admin] rebuild-cache concluído em 82.13s
```

JSON (`LOG_JSON=1` no Railway / k8s):

```json
{"ts":"2026-05-15T21:20:03.005Z","level":"INFO","logger":"api","msg":"...","request_id":"ab12cd34efgh","user":"admin","role":"DIRETOR","event":"rebuild_cache_done","duration_ms":82130.0}
```

Pronto para Datadog/Loki/Grafana Cloud sem mexer no app.

### 15.3 Eventos marcados (chave `event`)

| Evento | Quando |
|---|---|
| `http_request` | toda request (exceto `/healthz` e `/readyz`) |
| `login_success` / `login_failed` | /auth/login (sem senha) |
| `upload_started` / `upload_done` | /upload/{tipo} |
| `rebuild_cache_started` / `rebuild_cache_done` | /admin/rebuild-cache |
| `refresh_all_done` | ciclo de 300s ou rebuild |
| `merge_incremental_done` | upload incremental |

### 15.4 Dados sensíveis

- **Middleware não loga body** de request.
- **Login falho** loga só o login tentado, nunca a senha.
- **JWT** nunca aparece em log.
- Teste defensivo `test_login_falho_nao_loga_senha` varre todos os
  LogRecords e atributos extras confirmando ausência da senha.

---

## 16. Runbook Operacional

### 16.1 "Painel SLA com remessa indevida"

1. Pegue o(s) código(s) suspeito(s).
2. No console do browser (logado):
   ```javascript
   const t = localStorage.getItem('auth_token');
   fetch('/diag/sla?codigos=COD1,COD2,COD3',
         {headers:{Authorization:'Bearer '+t}})
     .then(r=>r.json()).then(j=>console.table(j.diagnostico));
   ```
3. Olhe `entraNoSlaHoje` + `motivo` para cada código.
4. Se `motivo` indica que entraNoSlaHoje retornou correto (false) mas
   a remessa ainda aparece no painel: **hard refresh** (Ctrl+Shift+R)
   para descartar opMap stale do navegador.
5. Se PE está claramente errada no backend: rode
   `POST /admin/rebuild-cache` para forçar `refresh_all`.

### 16.2 "Cache muito velho"

- `GET /health/sla` mostra `idade_cache_segundos`.
- Refresh automático roda a cada 300s.
- Para forçar agora: `POST /admin/rebuild-cache` (Diretor).
- Causa típica: o `_background_refresh` morreu — restart do processo
  no Railway resolve.

### 16.3 "Login bloqueando"

- Sem rate-limit ainda. Tentativas falhas viram log
  `[auth] login falhou para 'X'` com `event=login_failed`.
- Senha esquecida: Diretor pode resetar via `POST /usuarios/{uid}/reset-senha`.

### 16.4 "Quero entender o que aconteceu numa request específica"

- Pegue `X-Request-ID` da response (header).
- Grep no log do Railway por `req=<id>` (formato text) ou
  `request_id":"<id>"` (formato JSON).
- Todos os logs daquela request aparecem em sequência, com `event` tags.

### 16.5 "Preciso reverter para versão anterior"

- `git checkout <commit-anterior>` localmente.
- Não tem CI bloqueando reverter (mas se tiver branch protection ativa,
  reverter requer PR).
- **Dados** (xlsx, users.db) NÃO são versionados — usar `tools/restore_data.py`
  com um backup recente (ver Seção 12).

### 16.6 "Suspeita de dados de produção corrompidos"

- Pegue um backup recente: `python tools/backup_data.py` (rodando contra `/data` da prod).
- Valide com `--dry-run`: confere sha256 sem extrair.
- Se precisar restaurar, **sempre** use `--allow-production` consciente
  e tenha pré-backup recente. Ver Seção 12.5.

---

## 17. Fluxo Dev → Produção

```
Trabalho                         Branch                          Deploy
═══════════════════════════════════════════════════════════════════════
Implementação local              dev/profissionalizacao-core     —
  ↓ pytest verde + ruff verde
PR para main                     PR aberto                       CI roda
  ↓ revisão + CI verde
Merge para main                  main                            Railway redeploya
  ↓ startup roda refresh_all
Produção atualizada              —                               novos endpoints ativos
```

### 17.1 Regras invioláveis

1. **Nada vai pra `main` sem CI verde.**
2. **Toda regra de negócio nova vem com teste.**
3. **Bug corrigido vira teste de regressão** com referência ao commit.
4. **Frontend não duplica regra do backend.** Se uma lógica vive nos dois,
   o backend é a fonte de verdade; frontend só consome.
5. **Logs estruturados são padrão.** Eventos importantes ganham `event=...`.
6. **`POST /admin/rebuild-cache` é a maneira certa de forçar reset do cache.**
   Não há outro caminho seguro em produção.

### 17.2 Configuração do Railway (não automatizado ainda)

Para ativar healthcheck quando estabilizar:

```toml
# railway.toml
[deploy]
healthcheckPath = "/readyz"
healthcheckTimeout = 120
```

Para logs JSON (recomendado quando integrar com observabilidade externa):

```
LOG_JSON=1
```

---

*Documento mantido por: André Teles / equipe de desenvolvimento*  
*Última atualização: 2026-05-18 (rev5) — adiciona seção 3.6 (filtro Críticas na ENTRADA, PR #2)*

# SYSTEM_DOC — GestãoEntregas Jadlog

> **Fonte da verdade do sistema.** Este documento descreve a arquitetura, regras de negócio, lógica crítica e convenções do GestãoEntregas. Deve ser atualizado a cada mudança estrutural relevante.

**Versão do documento:** 2026-05-13  
**Repositório:** https://github.com/teleslog/GestaoJadlog.git (branch `main`)  
**Deploy:** https://web-production-1614e.up.railway.app  
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

#### Campo `__primeira_entrada` (backend)

O backend extrai, para cada `Codigo`, o menor `Dt Evento` onde `Status = 'ENTRADA'` — **antes da deduplicação**. Isso é necessário porque o arquivo xlsx é um log de eventos; após dedup, o registro de ENTRADA pode ter sido sobrescrito por um evento posterior (ex.: EM ROTA).

```python
def _compute_primeira_entrada(rows: list[dict]) -> dict[str, str]:
    # Para cada Codigo, encontra o menor DtEvento com Status=ENTRADA
    # Retorna dict {Codigo: "DD/MM/YYYY HH:MM:SS"}
```

O resultado é injetado como campo `__primeira_entrada` em cada linha antes de servir ao frontend.

#### Campo `PrimeiraEntrada` (frontend)

Mapeado de `r['__primeira_entrada']` em `parseOp()`. O campo é protegido em `mergeOp()` para não ser sobrescrito por uma atualização que não contenha o evento de ENTRADA.

#### Função centralizada `entraNoSlaHoje(row)`

```javascript
function entraNoSlaHoje(row) {
  // Retorna true  → incluir no SLA de hoje
  // Retorna false → excluir (entrada hoje após 10h)

  const pe = row.PrimeiraEntrada;
  // Sem data: inclui por precaução
  // Entrada antes de hoje: inclui (remessa antiga)
  // Entrada depois de hoje: inclui (caso improvável — inclui por precaução)
  // Entrada hoje antes das 10h: inclui
  // Entrada hoje às 10h ou depois: EXCLUI
}
```

**Esta é a ÚNICA função que implementa a regra das 10h. Nunca duplicar esta lógica.**

#### Exemplos

| Situação                            | PrimeiraEntrada         | entraNoSlaHoje | Motivo                         |
|-------------------------------------|-------------------------|----------------|--------------------------------|
| Entrada ontem, em rota hoje         | `12/05/2026 14:30:00`   | `true`         | Entrada foi ontem — inclui     |
| Entrada hoje às 09:45               | `13/05/2026 09:45:00`   | `true`         | Antes das 10h — inclui         |
| Entrada hoje às 10:00               | `13/05/2026 10:00:00`   | `false`        | Exatamente 10h — **exclui**    |
| Entrada hoje às 14:30               | `13/05/2026 14:30:00`   | `false`        | Após 10h — **exclui**          |
| Sem PrimeiraEntrada (dado ausente)  | `""`                    | `true`         | Sem dado — inclui por precaução|

#### Pontos de aplicação

A regra se aplica a todos os componentes do SLA do dia:
- `vencHoje` (vencem hoje)
- `slaEntregue` (entregues no prazo)
- `slaCustodia` (custódia SLA do dia)
- `openSlaDrawer` (drawers de detalhe dos cards)
- `calcEntregadorSummary` (resumo por entregador)

**Não se aplica a `vencidas`** (prazo passado) — essas já ultrapassaram o prazo independente do horário de entrada.

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

---

## 4. Aba Entregador

### 4.1 Resumo por Entregador (`calcEntregadorSummary`)

Exibido na pill **Entregador** da tabela Remessas do Dashboard.

**Base de cálculo:** remessas do coorte SLA do dia — `entraNoSlaHoje(r) && dDiff(r.Previsao) === 0`.

| Coluna        | Definição                                                |
|---------------|----------------------------------------------------------|
| Previsto Hoje | Total de remessas do entregador no coorte SLA do dia     |
| Entregues     | ENTREGUE ou ENTREGUE NO PICKUP                           |
| Faltam        | EM ROTA, EM ROTA PICKUP ou ENTRADA (pendentes)           |
| Custódia      | CUSTODIA                                                 |
| SLA%          | `round(entregues / previsto * 100)` — 0 se previsto = 0  |

Ordenação: menor SLA primeiro, depois mais faltam.

Entregador sem nome no arquivo exibe **"Sem entregador"**.

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

Painel lateral (760px) ativado ao clicar em uma linha da tabela Entregador.

#### Estrutura do drawer

```
┌─────────────────────────────────────────────────┐
│ [Nome do Entregador]               [🔴 Crítico]  │ ← header vermelho
│                                    [✕ Fechar]   │
├─────────────────────────────────────────────────┤
│ PREVISTO │ ENTREGUES │ FALTAM │ CUSTÓDIA │ SLA%  │ ← stats grid (5 cols)
├─────────────────────────────────────────────────┤
│ PROGRESSO SLA  ████░░░░░░░░  141 de 456         │ ← barra de progresso
├─────────────────────────────────────────────────┤
│ VENCENDO HOJE (N remessas)                      │ ← seção: pendentes coorte SLA
│  Código | Cidade | Bairro | Dest. | Status | ... │
├─────────────────────────────────────────────────┤
│ EM ROTA (N remessas)                            │ ← seção: todos em rota
├─────────────────────────────────────────────────┤
│ ENTREGUES (N remessas)                          │
├─────────────────────────────────────────────────┤
│ CUSTÓDIA (N remessas)                           │
├─────────────────────────────────────────────────┤
│ TRAVADAS (N remessas)                           │
└─────────────────────────────────────────────────┘
```

#### Fonte de dados por seção

| Seção          | Fonte              | Filtro                                      |
|----------------|--------------------|---------------------------------------------|
| Vencendo Hoje  | `_entrDetailRows`  | Status pendente (coorte SLA do dia)          |
| Em Rota        | `dashRows`         | Status = EM ROTA ou EM ROTA PICKUP           |
| Entregues      | `dashRows`         | Status = ENTREGUE ou ENTREGUE NO PICKUP      |
| Custódia       | `dashRows`         | Status = CUSTODIA                            |
| Travadas       | `dashRows`         | Status = TRAVADO (usa `dcTrav` para Dias)    |

> **Nota:** "Vencendo Hoje" e "Em Rota" podem ter overlap intencional — uma remessa EM ROTA com prazo hoje aparece em ambas, pois servem propósitos distintos (urgência do dia vs. visão geral da rota).

#### Campos exibidos nas tabelas do drawer

`Código | Cidade | Bairro | Destinatário | Status | Dt Evento | Previsão | Dias`

- Código é clicável → abre histórico da remessa (`openCodeHistory`)
- Tabelas têm scroll horizontal para resolução menor (notebook-safe)
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
| `Rota`                    | `r.Rota`            | Código de rota                                 |
| `Hist. ultimo operador`   | `r.Entr`            | Nome do entregador (normalizado com `cap()`)   |
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

Drawers laterais (SLA e Entregador) devem ter no máximo 760px de largura. Tabelas internas de drawers usam `overflow-x: auto` para evitar conteúdo cortado.

---

## 8. Regras de Manutenção

### 8.1 O que NUNCA quebrar

| Sistema               | Localização                              | Risco se quebrado              |
|-----------------------|------------------------------------------|--------------------------------|
| Regra das 10h         | `entraNoSlaHoje()` + `_compute_primeira_entrada()` | SLA inflado/deflado artificialmente |
| Auto-refresh          | `startAutoRefresh()` + `fetchAll()`      | Dashboard para de atualizar    |
| Merge incremental     | `merge_incremental()` em main.py         | Upload volta a 49s+            |
| Cache never-shrinks   | `mergeOp()` no frontend                  | Remessas "somem" do dashboard  |
| Export Excel          | `exportDash()`, `exportOp()`, `exportDrawer()` | Equipe perde ferramenta de trabalho |
| Drawer SLA (`#drawer`)| `openSlaDrawer()`, `openDrawerWithRows()` | Cards do gauge param de funcionar |
| Aba Entregador        | `calcEntregadorSummary()`, `filterDash()` | Visão por entregador some      |
| `PrimeiraEntrada` protect | `mergeOp()` preserva prevPE         | Regra das 10h falha no frontend |

### 8.2 Antes de qualquer alteração em SLA

Verificar todos os pontos de uso de `entraNoSlaHoje`:
```
calcM()               → vencHoje, slaEntregue, slaCustodia
openSlaDrawer()       → prev, entr, ocorr, vhoje, falt
calcEntregadorSummary() → base de cálculo do resumo
```

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

- [ ] Sem `console.error` na aba Entregador e no drawer
- [ ] Auto-refresh preserva o modo ativo da pill
- [ ] Overlay dos drawers fecha corretamente
- [ ] Export Excel funciona em todos os modos (TODOS, filtros, Entregador)
- [ ] Drawer SLA original ainda abre e fecha
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
    5. Para cada row novo:
           if codigo em merged:
               if dt_evento_novo >= dt_evento_existente:
                   merged[codigo] = row_novo  # novo vence
                   preservar __primeira_entrada se novo for mais tardio
           else:
               merged[codigo] = row_novo      # novo código
    6. Calcular nova_pe = _compute_primeira_entrada(new_rows)
       Atualizar __primeira_entrada se mais antiga que a existente
    7. Rebuild entry: linhas, dados, atualizado, arquivo
    8. _cache[tipo][key] = nova_entry
    9. Release lock
```

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

| Modo         | Arquivo gerado                    | Conteúdo                                    |
|--------------|-----------------------------------|---------------------------------------------|
| Dashboard TODOS/filtro | `Dashboard_DD-MM-AAAA.xlsx` | Remessas filtradas visíveis                |
| Entregador   | `SLA_Entregador_DD-MM-AAAA.xlsx`  | `_entrDetailRows` + stats por entregador    |
| Drawers SLA  | `SLA_{tipo}_DD-MM-AAAA.xlsx`      | Remessas do drawer com todos os campos      |
| Páginas Op   | `{Status}_DD-MM-AAAA.xlsx`        | Em Rota / Entrada / Custódia / Travadas     |

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

### 10.7 Remessa não aparece no SLA do dia

**Verificar em ordem:**
1. `dDiff(r.Previsao)` = 0? (prazo = hoje)
2. `entraNoSlaHoje(r)` = true? (PrimeiraEntrada antes das 10h ou de dia anterior)
3. Status é elegível? (ENTREGUE, EM ROTA, ENTRADA, CUSTODIA)
4. Operação correta? (`Hist. ultimo ponto` bate com a operação selecionada)

### 10.8 Drawer do Entregador vazio

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

*Documento mantido por: André Teles / equipe de desenvolvimento*  
*Última atualização: 2026-05-13 — commit `ba18aa3`*

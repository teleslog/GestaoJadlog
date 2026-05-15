"""
main.py — GestãoEntregas Backend (FastAPI)
Deploy: Railway / Render | PORT: auto (via $PORT env)

Estrutura de dados:
  dados/
    financeiro/   ← arquivos .xlsx ou .zip financeiros (qualquer nome)
    operacional/  ← arquivos .xlsx ou .zip operacionais (qualquer nome)

O sistema identifica a operação automaticamente pelo CONTEÚDO do arquivo.
Arquivos .zip são extraídos automaticamente; temporários são apagados após leitura.
Não é necessário renomear arquivos nem criar subpastas.

Endpoints públicos:
  GET  /              → GestãoEntregas.html
  POST /auth/login    → JWT token

Endpoints autenticados:
  PUT  /auth/change-password
  GET  /status
  GET  /dados/financeiro/{key}    key: gv | itabira | jm | curvelo
  GET  /dados/operacional/{key}

Endpoints ADMIN/GESTOR:
  POST /upload/{tipo}   → salva .xlsx ou .zip na pasta correta

Endpoints ADMIN:
  GET    /usuarios
  POST   /usuarios
  PUT    /usuarios/{id}
  DELETE /usuarios/{id}
"""

import asyncio
import io
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import bcrypt as _bcrypt
import pandas as pd
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

# ── CONFIG ─────────────────────────────────────────────────────────────────────

REFRESH_SECONDS        = int(os.environ.get("REFRESH_SECONDS", "300"))
JWT_SECRET             = os.environ.get("JWT_SECRET", "mude-em-producao-use-openssl-rand-hex-32")
JWT_ALGORITHM          = "HS256"
JWT_EXPIRE_HOURS       = 12
DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "Jadlog2026")[:72]

BASE_DIR  = Path(__file__).parent
DASHBOARD = BASE_DIR / "GestãoEntregas.html"

# Em produção: DATA_DIR=/data (Railway Volume). Em local: omitir → usa BASE_DIR.
_DATA_ROOT = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
FIN_DIR    = _DATA_ROOT / "dados" / "financeiro"
OP_DIR     = _DATA_ROOT / "dados" / "operacional"
DB_PATH    = Path(os.environ.get("DB_PATH", str(_DATA_ROOT / "users.db")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("api")

# ── VERSÃO ─────────────────────────────────────────────────────────────────────

def _get_version() -> str:
    """Gera string de versão: vAAAA.MM.DD.<git-short-hash> ou vAAAA.MM.DD.HHmm."""
    date = datetime.now(timezone.utc).strftime("%Y.%m.%d")
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(BASE_DIR), stderr=subprocess.DEVNULL, timeout=3,
        ).decode().strip()
        if commit:
            return f"v{date}.{commit}"
    except Exception:
        pass
    return datetime.now(timezone.utc).strftime("v%Y.%m.%d.%H%M")


APP_VERSION:    str = ""
_dashboard_html: str = ""

# ── OPERAÇÕES ──────────────────────────────────────────────────────────────────

OPS: dict[str, tuple[str, int]] = {
    "gv":      ("CO GOV VALADARES 01",  0),
    "itabira": ("CO ITABIRA 01",         1),
    "jm":      ("CO JOAO MONLEVADE 01", 2),
    "curvelo": ("CO CURVELO 02",         3),
}

TIPOS = ("financeiro", "operacional")

# Palavras-chave para identificar a operação pelo CONTEÚDO do arquivo
OP_KEYWORDS: dict[str, list[str]] = {
    "gv":      ["GOV VALADARES", "GOVERNADOR VALADARES", "CO GOV"],
    "itabira": ["ITABIRA"],
    "jm":      ["JOAO MONLEVADE", "JOÃO MONLEVADE", "MONLEVADE"],
    "curvelo": ["CURVELO"],
}

# Palavras-chave para identificar a operação pelo NOME DO ARQUIVO (fallback)
OP_FILENAME_HINTS: dict[str, list[str]] = {
    "gv":      ["GV", "GOV", "VALADARES"],
    "itabira": ["ITABIRA", "ITA"],
    "jm":      ["JM", "JOAO", "MONLEVADE"],
    "curvelo": ["CURVELO", "CUR"],
}

# ── AUTH ───────────────────────────────────────────────────────────────────────

ROLES = {"DIRETOR", "SUPERVISAO", "OPERACIONAL", "FINANCEIRO", "VISUALIZACAO"}

# Controle de acesso por perfil
CAN_VIEW_OP  = {"DIRETOR", "SUPERVISAO", "OPERACIONAL", "VISUALIZACAO"}
CAN_VIEW_FIN = {"DIRETOR", "SUPERVISAO", "FINANCEIRO",  "VISUALIZACAO"}
CAN_UPLOAD   = {"DIRETOR", "SUPERVISAO", "OPERACIONAL", "FINANCEIRO"}
CAN_ADMIN    = {"DIRETOR"}

ROLE_LABELS = {
    "DIRETOR":     "Diretor",
    "SUPERVISAO":  "Supervisão",
    "OPERACIONAL": "Operacional",
    "FINANCEIRO":  "Financeiro",
    "VISUALIZACAO":"Visualização",
}

bearer  = HTTPBearer()


@dataclass
class User:
    id: str
    login: str
    nome: str
    password_hash: str
    role: str
    ativo: bool = True
    must_change_password: bool = False


users_db:   dict[str, User] = {}
users_lock: asyncio.Lock
_cache_lock: asyncio.Lock


# ── SQLITE ─────────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id                   TEXT PRIMARY KEY,
            login                TEXT UNIQUE NOT NULL,
            nome                 TEXT NOT NULL DEFAULT '',
            password_hash        TEXT NOT NULL,
            role                 TEXT NOT NULL,
            ativo                INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 1
        )""")
        conn.commit()


def _upsert_user(u: User):
    with _db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO users
               (id, login, nome, password_hash, role, ativo, must_change_password)
               VALUES (?,?,?,?,?,?,?)""",
            (u.id, u.login, u.nome, u.password_hash, u.role,
             int(u.ativo), int(u.must_change_password)),
        )
        conn.commit()


def _delete_user_db(uid: str):
    with _db() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()


def _load_users_db() -> list[User]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM users").fetchall()
    return [
        User(
            id=r["id"], login=r["login"], nome=r["nome"] or "",
            password_hash=r["password_hash"], role=r["role"],
            ativo=bool(r["ativo"]),
            must_change_password=bool(r["must_change_password"]),
        )
        for r in rows
    ]


def _hash(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt()).decode()


def _verify(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def _create_token(user: User) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": user.id, "login": user.login, "role": user.role,
         "must_change_password": user.must_change_password, "exp": exp},
        JWT_SECRET, algorithm=JWT_ALGORITHM,
    )


async def _get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
) -> User:
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(401, "Token inválido ou expirado")
    uid = payload.get("sub")
    async with users_lock:
        user = users_db.get(uid)
    if not user:
        raise HTTPException(401, "Usuário não encontrado")
    if not user.ativo:
        raise HTTPException(401, "Usuário desativado. Contate o administrador.")
    return user


def _require_admin(user: User = Depends(_get_current_user)) -> User:
    if user.role not in CAN_ADMIN:
        raise HTTPException(403, "Acesso restrito a Diretores")
    return user


def _require_gestor(user: User = Depends(_get_current_user)) -> User:
    if user.role not in CAN_UPLOAD:
        raise HTTPException(403, "Acesso restrito a Diretores, Supervisão, Operacional e Financeiro")
    return user

# ── MODELOS ────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    login: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    login: str
    nome: str
    password: str
    role: str


class UpdateUserRequest(BaseModel):
    login: Optional[str] = None
    nome: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    ativo: Optional[bool] = None
    must_change_password: Optional[bool] = None


class ResetSenhaRequest(BaseModel):
    nova_senha: str

# ── DETECÇÃO DE OPERAÇÃO ───────────────────────────────────────────────────────

def _find_xlsx_files(folder: Path) -> list[Path]:
    """Retorna todos os .xlsx da pasta ordenados do mais recente ao mais antigo.
    Ignora arquivos temporários do Excel (~$)."""
    if not folder.exists():
        return []
    return sorted(
        [f for f in folder.glob("*.xlsx")
         if f.is_file() and not f.name.startswith("~$")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )


def _extract_zips_to_temp(folder: Path) -> tuple[list[Path], str | None]:
    """
    Extrai todos os .xlsx encontrados dentro dos .zip da pasta para um
    diretório temporário. Cada zip recebe um subdiretório próprio para evitar
    colisões de nomes entre arquivos de zips diferentes.

    Retorna (lista_de_paths_extraídos, caminho_do_tempdir).
    Se não houver zips, retorna ([], None).
    """
    if not folder.exists():
        return [], None
    zip_files = sorted(
        [f for f in folder.glob("*.zip") if f.is_file() and not f.name.startswith("~$")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not zip_files:
        return [], None

    temp_dir = tempfile.mkdtemp(prefix="gestao_")
    extracted: list[Path] = []
    _t_unzip_total = time.perf_counter()

    for zf_path in zip_files:
        zip_mtime = zf_path.stat().st_mtime
        sub_dir = Path(temp_dir) / zf_path.stem
        sub_dir.mkdir(exist_ok=True)
        _t_zip = time.perf_counter()
        try:
            with zipfile.ZipFile(zf_path, "r") as zf:
                for info in zf.infolist():
                    basename = Path(info.filename).name
                    if not basename.lower().endswith(".xlsx"):
                        continue
                    if basename.startswith("~$") or "__MACOSX" in info.filename:
                        continue
                    dest = sub_dir / basename
                    dest.write_bytes(zf.read(info.filename))
                    os.utime(dest, (zip_mtime, zip_mtime))
                    extracted.append(dest)
                    log.info(f"[zip] {zf_path.name} → {basename}")
            log.info(f"[perf] unzip  {zf_path.name} ({zf_path.stat().st_size // 1024} KB): {time.perf_counter()-_t_zip:.2f}s")
        except zipfile.BadZipFile:
            log.error(f"[zip] {zf_path.name}: ZIP inválido ou corrompido")
        except Exception as exc:
            log.error(f"[zip] {zf_path.name}: {exc}")

    log.info(f"[perf] unzip  TOTAL {len(zip_files)} arquivo(s): {time.perf_counter()-_t_unzip_total:.2f}s")
    return extracted, temp_dir


def _extract_one_zip(zip_path: Path) -> tuple[Path | None, str | None]:
    """Extrai o primeiro .xlsx de um único zip para dir temporário. Caller limpa tmp_dir."""
    tmp_dir = tempfile.mkdtemp(prefix="gestao_inc_")
    try:
        zip_mtime = zip_path.stat().st_mtime
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                basename = Path(info.filename).name
                if (basename.lower().endswith(".xlsx")
                        and not basename.startswith("~$")
                        and "__MACOSX" not in info.filename):
                    dest = Path(tmp_dir) / basename
                    dest.write_bytes(zf.read(info.filename))
                    os.utime(dest, (zip_mtime, zip_mtime))
                    return dest, tmp_dir
    except Exception as exc:
        log.error(f"[merge_incremental] extract {zip_path.name}: {exc}")
    return None, tmp_dir


def _detect_op_from_content(df: pd.DataFrame, header_idx: int) -> str | None:
    """
    Identifica a operação dominante analisando os VALORES das células.
    Amostra até 300 linhas de dados e conta ocorrências de palavras-chave.
    A operação com maior contagem de matches vence.
    """
    scores: dict[str, int] = {k: 0 for k in OP_KEYWORDS}

    for _, row in df.iloc[header_idx + 1: header_idx + 301].iterrows():
        # Concatena todos os valores da linha em uma string para busca
        row_text = " ".join(
            str(v).upper().strip()
            for v in row
            if pd.notna(v) and str(v).strip() not in ("", "NAN", "NONE")
        )
        for key, keywords in OP_KEYWORDS.items():
            if any(kw in row_text for kw in keywords):
                scores[key] += 1

    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else None


def _detect_op_from_filename(filename: str) -> str | None:
    """Fallback: tenta identificar a operação pelo nome do arquivo."""
    stem = Path(filename).stem.upper()
    for key, hints in OP_FILENAME_HINTS.items():
        if any(hint in stem for hint in hints):
            return key
    return None


def _find_header_idx(df: pd.DataFrame) -> int:
    """Encontra a linha de cabeçalho procurando pela coluna 'Codigo'."""
    for i in range(min(30, len(df))):
        if any(str(v).strip() == "Codigo" for v in df.iloc[i] if pd.notna(v)):
            return i
    return 0


def _detect_key_from_path(path: Path) -> str | None:
    """Detecta a operação de um único arquivo (xlsx ou zip) sem escanear a pasta toda."""
    xlsx_to_scan: Path | None = None
    tmp_dir: str | None = None
    try:
        if path.suffix.lower() == ".zip":
            tmp_dir = tempfile.mkdtemp(prefix="gestao_key_")
            with zipfile.ZipFile(path, "r") as zf:
                for info in zf.infolist():
                    basename = Path(info.filename).name
                    if (basename.lower().endswith(".xlsx")
                            and not basename.startswith("~$")
                            and "__MACOSX" not in info.filename):
                        dest = Path(tmp_dir) / basename
                        dest.write_bytes(zf.read(info.filename))
                        xlsx_to_scan = dest
                        break
        else:
            xlsx_to_scan = path

        if xlsx_to_scan is None:
            return _detect_op_from_filename(path.name)

        df = pd.read_excel(xlsx_to_scan, header=None, engine="openpyxl", nrows=330)
        header_idx = _find_header_idx(df)
        key = _detect_op_from_content(df, header_idx)
        if key is None:
            key = _detect_op_from_filename(path.name)
        return key

    except Exception as exc:
        log.warning(f"[upload] detect_key {path.name}: {exc}; tentando pelo nome")
        return _detect_op_from_filename(path.name)
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _scan_folder(folder: Path) -> tuple[dict[str, list[Path]], str | None]:
    """
    Escaneia a pasta (xlsx diretos + xlsx dentro de zips), detecta a operação
    de cada arquivo e retorna ({key: [paths do mais recente ao mais antigo]}, temp_dir_ou_None).

    Todos os arquivos de cada operação são coletados para permitir mesclagem
    acumulativa por Codigo (total de remessas nunca diminui).
    O chamador é responsável por limpar o temp_dir após consumir os paths.
    """
    direct_files = _find_xlsx_files(folder)
    extracted_files, temp_dir = _extract_zips_to_temp(folder)

    # Combina e ordena por mtime (mais recente primeiro)
    all_files: list[Path] = sorted(
        direct_files + extracted_files,
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    result: dict[str, list[Path]] = {}
    for path in all_files:
        try:
            _t = time.perf_counter()
            df = pd.read_excel(path, header=None, engine="openpyxl", nrows=330)
            log.info(f"[perf][scan] read_excel 330 rows {path.name}: {time.perf_counter()-_t:.2f}s")
            header_idx = _find_header_idx(df)

            key = _detect_op_from_content(df, header_idx)
            method = "conteúdo"

            if key is None:
                key = _detect_op_from_filename(path.name)
                method = "nome do arquivo"

            if key is None:
                log.warning(f"[scan] {folder.name}/{path.name} → operação não identificada, ignorado")
                continue

            if key not in result:
                result[key] = []
            result[key].append(path)
            log.info(f"[scan] {folder.name}/{path.name} → '{key}' detectado por {method}")

        except Exception as exc:
            log.error(f"[scan] Erro ao ler {path.name}: {exc}")

    return result, temp_dir

# ── LEITURA COMPLETA DO XLSX ───────────────────────────────────────────────────

def _fmt(val) -> str:
    """Converte valor pandas para string legível."""
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(val, pd.Timestamp):
        return (val.strftime("%d/%m/%Y")
                if val.hour == val.minute == val.second == 0
                else val.strftime("%d/%m/%Y %H:%M:%S"))
    if isinstance(val, float):
        return str(int(val)) if val == int(val) else str(val)
    return str(val).strip()


def _parse_dt_evento(s: str) -> datetime | None:
    """Parses 'dd/mm/yyyy' ou 'dd/mm/yyyy HH:MM:SS' para datetime, ou None."""
    s = str(s).strip()
    try:
        if len(s) >= 19:
            return datetime.strptime(s[:19], "%d/%m/%Y %H:%M:%S")
        if len(s) >= 10:
            return datetime.strptime(s[:10], "%d/%m/%Y")
    except ValueError:
        pass
    return None


def _compute_primeira_entrada(rows: list[dict], op_name: str | None = None) -> dict[str, str]:
    """
    Retorna {codigo: 'dd/mm/yyyy HH:MM:SS'} com o MENOR Dt Evento onde Status='ENTRADA'.
    Filtra por Hist. ultimo ponto == op_name (se informado): impede que ENTRADAs em
    outra torre (snapshots de quando o pacote estava em trânsito) virem PE desta op.
    """
    op_upper = op_name.upper() if op_name else None
    primeira: dict[str, datetime] = {}
    for row in rows:
        codigo = str(row.get("Codigo", "")).strip()
        if not codigo:
            continue
        if op_upper is not None:
            if str(row.get("Hist. ultimo ponto", "")).strip().upper() != op_upper:
                continue
        if str(row.get("Status", "")).strip().upper() != "ENTRADA":
            continue
        dt = _parse_dt_evento(str(row.get("Dt Evento", "")))
        if dt is None:
            continue
        if codigo not in primeira or dt < primeira[codigo]:
            primeira[codigo] = dt
    return {c: dt.strftime("%d/%m/%Y %H:%M:%S") for c, dt in primeira.items()}


def _read_xlsx(path: Path) -> list[dict]:
    """Lê o xlsx completo e retorna lista de dicts — conversão vetorizada (sem iterrows)."""
    _t0 = time.perf_counter()
    _kb = path.stat().st_size // 1024
    log.info(f"[ctx]  read_xlsx  arquivo={path.name}  tamanho={_kb} KB")
    df = pd.read_excel(path, header=None, engine="openpyxl",
                       engine_kwargs={"read_only": True, "data_only": True})
    _t1 = time.perf_counter()
    log.info(f"[perf] read_xlsx  {path.name} ({len(df)} rows | {_kb} KB): {_t1-_t0:.2f}s")

    header_idx = _find_header_idx(df)
    headers = [str(v).strip() if pd.notna(v) else "" for v in df.iloc[header_idx]]
    col_idx   = [j for j, h in enumerate(headers) if h]
    col_names = [headers[j] for j in col_idx]

    _t2 = time.perf_counter()
    data = df.iloc[header_idx + 1:].reset_index(drop=True)
    data = data.dropna(how="all")                  # descarta linhas 100% vazias
    data = data.iloc[:, col_idx].copy()
    data.columns = col_names

    # Converte coluna por coluna de forma vetorizada
    for col in data.columns:
        if pd.api.types.is_datetime64_any_dtype(data[col]):
            has_time = (data[col].dt.hour.ne(0)
                        | data[col].dt.minute.ne(0)
                        | data[col].dt.second.ne(0))
            data[col] = data[col].dt.strftime("%d/%m/%Y").where(
                ~has_time, data[col].dt.strftime("%d/%m/%Y %H:%M:%S"))
        elif data[col].dtype.kind == "f":
            # floats inteiros → "123" em vez de "123.0"
            int_mask = data[col].notna() & (data[col] % 1 == 0)
            as_obj = data[col].astype(object)
            as_obj[int_mask] = data[col][int_mask].apply(lambda v: str(int(v)))
            as_obj[~int_mask & data[col].notna()] = (
                data[col][~int_mask & data[col].notna()].astype(str))
            as_obj[data[col].isna()] = ""
            data[col] = as_obj
        else:
            data[col] = (data[col].fillna("")
                                  .astype(str)
                                  .str.strip()
                                  .replace({"nan": "", "<NA>": "", "NaT": ""}))

    rows = [r for r in data.to_dict("records") if any(v for v in r.values())]
    _t3 = time.perf_counter()
    log.info(f"[perf] process_rows  {path.name} ({len(rows)} linhas): {_t3-_t2:.2f}s")
    log.info(f"[perf] read_xlsx+process_rows TOTAL  {path.name}: {_t3-_t0:.2f}s")
    return rows


def _read_and_merge(paths: list[Path], op_name: str | None = None) -> list[dict]:
    """
    Lê todos os arquivos e mescla por coluna 'Codigo'.
    Processa do mais antigo ao mais recente: arquivo mais recente vence em conflitos.
    Garante que o total de remessas nunca diminua entre atualizações — Codigos
    que saem do relatório novo ainda ficam com o último status conhecido.
    Injeta '__primeira_entrada': MIN(Dt Evento) onde Status=ENTRADA E Hist=op_name.
    Proxy: códigos sem ENTRADA na nossa torre → MIN(Dt Evento) entre linhas com
    Hist=op_name (jamais conta Dt Evento de outras torres como aproximação).
    """
    merged: dict[str, dict] = {}
    all_rows_for_pe: list[dict] = []
    _no_key = 0

    for path in reversed(paths):  # mais antigo primeiro → mais recente sobrescreve
        try:
            rows = _read_xlsx(path)
            all_rows_for_pe.extend(rows)
            for row in rows:
                codigo = row.get("Codigo", "").strip()
                if codigo:
                    merged[codigo] = row
                else:
                    _no_key += 1
                    merged[f"__nk_{_no_key}"] = row
            log.info(f"[merge] {path.name}: {len(rows)} linhas lidas")
        except Exception as exc:
            log.error(f"[merge] {path.name}: {exc}")

    # PE real: MIN Dt Evento onde Status=ENTRADA E Hist=nossa op.
    pe_map = _compute_primeira_entrada(all_rows_for_pe, op_name)
    for codigo, pe_str in pe_map.items():
        if codigo in merged:
            merged[codigo]["__primeira_entrada"] = pe_str
            merged[codigo]["__pe_proxy"] = False

    # Proxy PE: para códigos sem ENTRADA na nossa torre, usa MIN(Dt Evento) APENAS
    # entre linhas com Hist=nossa op. Linhas com Hist=outra torre (em trânsito)
    # são descartadas — não representam presença física na nossa operação.
    op_upper = op_name.upper() if op_name else None
    earliest_dt: dict[str, datetime] = {}
    for row in all_rows_for_pe:
        codigo = str(row.get("Codigo", "")).strip()
        if not codigo:
            continue
        if op_upper is not None:
            if str(row.get("Hist. ultimo ponto", "")).strip().upper() != op_upper:
                continue
        dt = _parse_dt_evento(str(row.get("Dt Evento", "")))
        if dt is None:
            continue
        if codigo not in earliest_dt or dt < earliest_dt[codigo]:
            earliest_dt[codigo] = dt

    for codigo, dt in earliest_dt.items():
        if codigo in merged and not merged[codigo].get("__primeira_entrada", ""):
            merged[codigo]["__primeira_entrada"] = dt.strftime("%d/%m/%Y %H:%M:%S")
            merged[codigo]["__pe_proxy"] = True

    return list(merged.values())

# ── CACHE ──────────────────────────────────────────────────────────────────────

# _cache[tipo][key] = {key, op, opIdx, tipo, atualizado, linhas, dados, arquivo, erro}
_cache: dict[str, dict[str, dict]] = {t: {} for t in TIPOS}


def _build_entry(tipo: str, key: str, path: Path | None, rows: list | None = None,
                 erro: str | None = None) -> dict:
    op_name, op_idx = OPS[key]
    base = {"key": key, "op": op_name, "opIdx": op_idx, "tipo": tipo}
    if erro or path is None:
        folder = "financeiro" if tipo == "financeiro" else "operacional"
        msg = erro or f"Nenhum arquivo .xlsx/.zip identificado como '{key}' em dados/{folder}/"
        return {**base, "atualizado": None, "linhas": 0, "dados": [],
                "arquivo": None, "erro": msg}
    return {
        **base,
        "atualizado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "linhas": len(rows), "dados": rows,
        "arquivo": path.name, "erro": None,
    }


async def _refresh_from_paths(tipo: str, key: str, paths: list[Path]) -> dict:
    if not paths:
        return _build_entry(tipo, key, None)
    try:
        # Filtro Hist só vale para operacional (financeiro não tem essa coluna).
        op_name = OPS[key][0] if tipo == "operacional" else None
        rows = _read_and_merge(paths, op_name)
        newest = paths[0]
        log.info(f"[load] {tipo}/{key} — {len(paths)} arquivo(s) → {len(rows)} linhas mescladas")
        return _build_entry(tipo, key, newest, rows)
    except Exception as exc:
        log.error(f"[load] {tipo}/{key}: {exc}")
        return _build_entry(tipo, key, paths[0], erro=str(exc))


async def refresh_all():
    """Escaneia as pastas (xlsx e zip), detecta operações e atualiza o cache."""
    _t_total = time.perf_counter()
    loop = asyncio.get_event_loop()

    _t = time.perf_counter()
    (fin_scan, fin_tmp), (op_scan, op_tmp) = await asyncio.gather(
        loop.run_in_executor(None, _scan_folder, FIN_DIR),
        loop.run_in_executor(None, _scan_folder, OP_DIR),
    )
    log.info(f"[perf][refresh] _scan_folder ambas pastas: {time.perf_counter()-_t:.2f}s")

    scans = {"financeiro": fin_scan, "operacional": op_scan}
    pairs = [(t, k) for t in TIPOS for k in OPS]

    _t = time.perf_counter()
    results = await asyncio.gather(
        *[_refresh_from_paths(t, k, scans[t].get(k, [])) for t, k in pairs],
        return_exceptions=True,
    )
    log.info(f"[perf][refresh] _refresh_from_paths todas ops: {time.perf_counter()-_t:.2f}s")

    new_cache: dict[str, dict[str, dict]] = {t: {} for t in TIPOS}
    for (tipo, key), result in zip(pairs, results):
        if isinstance(result, Exception):
            new_cache[tipo][key] = _build_entry(tipo, key, None, erro=str(result))
        else:
            new_cache[tipo][key] = result

    async with _cache_lock:
        global _cache
        _cache = new_cache

    # Limpa diretórios temporários após todas as leituras
    for tmp in filter(None, [fin_tmp, op_tmp]):
        shutil.rmtree(tmp, ignore_errors=True)

    log.info(f"[perf][refresh] TOTAL refresh_all: {time.perf_counter()-_t_total:.2f}s")


async def refresh_tipo(tipo: str):
    """Após upload: re-escaneia e recarrega apenas o tipo afetado (financeiro ou operacional)."""
    _t_total = time.perf_counter()
    loop = asyncio.get_event_loop()
    folder = FIN_DIR if tipo == "financeiro" else OP_DIR

    _t = time.perf_counter()
    scan, tmp = await loop.run_in_executor(None, _scan_folder, folder)
    log.info(f"[perf] refresh_{tipo}_scan: {time.perf_counter()-_t:.2f}s")

    _t = time.perf_counter()
    results = await asyncio.gather(
        *[_refresh_from_paths(tipo, k, scan.get(k, [])) for k in OPS],
        return_exceptions=True,
    )
    log.info(f"[perf] refresh_{tipo}_load: {time.perf_counter()-_t:.2f}s")

    async with _cache_lock:
        for key, result in zip(OPS, results):
            if isinstance(result, Exception):
                _cache[tipo][key] = _build_entry(tipo, key, None, erro=str(result))
            else:
                _cache[tipo][key] = result

    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)

    log.info(f"[perf] refresh_{tipo}_TOTAL: {time.perf_counter()-_t_total:.2f}s")


async def refresh_one(tipo: str, key: str):
    """
    Pós-upload cirúrgico: re-escaneia a pasta e recarrega APENAS _cache[tipo][key].
    Não toca em outras keys nem no outro tipo.
    Mais rápido que refresh_tipo porque não lê arquivos das outras 3 operações.
    """
    _t_total = time.perf_counter()
    loop = asyncio.get_event_loop()
    folder = FIN_DIR if tipo == "financeiro" else OP_DIR

    _t = time.perf_counter()
    scan, tmp = await loop.run_in_executor(None, _scan_folder, folder)
    log.info(f"[perf] refresh_one_scan  {tipo}: {time.perf_counter()-_t:.2f}s")

    paths = scan.get(key, [])

    _t = time.perf_counter()
    entry = await _refresh_from_paths(tipo, key, paths)
    log.info(f"[perf] refresh_one_load  {tipo}/{key}: {time.perf_counter()-_t:.2f}s")

    async with _cache_lock:
        _cache[tipo][key] = entry

    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)

    log.info(f"[perf] refresh_one  {tipo}/{key}: {time.perf_counter()-_t_total:.2f}s")


async def merge_incremental(tipo: str, key: str, src_path: Path):
    """
    Pós-upload incremental: lê APENAS o arquivo recém-enviado e mescla
    no cache[tipo][key] existente por Codigo.
    Dedup: mantém o registro com 'Dt Evento' mais recente.
    Tempo: ~3-5s constante, independente do histórico acumulado.
    """
    _t_total = time.perf_counter()
    loop = asyncio.get_event_loop()

    # Extrai xlsx do zip se necessário
    tmp_dir = None
    if src_path.suffix.lower() == ".zip":
        xlsx_path, tmp_dir = await loop.run_in_executor(None, _extract_one_zip, src_path)
    else:
        xlsx_path = src_path

    if xlsx_path is None:
        log.warning(f"[merge_incremental] {src_path.name}: xlsx não encontrado, fallback refresh_one")
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        await refresh_one(tipo, key)
        return

    # Lê apenas o novo arquivo
    _t = time.perf_counter()
    new_rows = await loop.run_in_executor(None, _read_xlsx, xlsx_path)
    log.info(f"[perf] merge_inc_read  {src_path.name} ({len(new_rows)} linhas): {time.perf_counter()-_t:.2f}s")

    if tmp_dir:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Merge incremental no cache existente
    _t = time.perf_counter()
    async with _cache_lock:
        existing_rows = (_cache.get(tipo, {}).get(key) or {}).get("dados") or []

        merged: dict[str, dict] = {}
        _nk = 0
        for row in existing_rows:
            codigo = str(row.get("Codigo", "")).strip()
            if codigo:
                merged[codigo] = row
            else:
                _nk += 1
                merged[f"__nk_{_nk}"] = row

        rows_novas = 0
        rows_atualizadas = 0
        new_codes: set[str] = set()
        for row in new_rows:
            codigo = str(row.get("Codigo", "")).strip()
            if not codigo:
                _nk += 1
                merged[f"__nk_{_nk}"] = row
                rows_novas += 1
                continue
            ex = merged.get(codigo)
            if ex is None:
                merged[codigo] = row
                new_codes.add(codigo)
                rows_novas += 1
            else:
                dt_new = _parse_dt_evento(row.get("Dt Evento", ""))
                dt_ex  = _parse_dt_evento(ex.get("Dt Evento", ""))
                # Preserva SEMPRE PE e flag de proxy antes de substituir a linha:
                # PE rastreia primeira entrada operacional, que é um fato passado e
                # nunca pode ser apagado por uma atualização posterior.
                pe_old = ex.get("__primeira_entrada", "")
                pe_old_is_proxy = ex.get("__pe_proxy", False)
                if dt_new is None or (dt_ex is not None and dt_ex > dt_new):
                    pass  # mantém existente — evento mais recente no cache
                else:
                    merged[codigo] = row
                    rows_atualizadas += 1
                # Restaura PE: real prevalece sobre vazio; proxy só preenche se PE estava vazia.
                # Nunca limpa PE existente.
                if pe_old:
                    cur_pe = merged[codigo].get("__primeira_entrada", "")
                    if not cur_pe:
                        merged[codigo]["__primeira_entrada"] = pe_old
                        merged[codigo]["__pe_proxy"] = pe_old_is_proxy

        # Filtro Hist só vale para operacional (financeiro não tem essa coluna).
        op_name = OPS[key][0] if tipo == "operacional" else None
        op_upper = op_name.upper() if op_name else None

        # Atualiza __primeira_entrada com evidência do novo arquivo.
        # PE = MIN das ENTRADAs reais NA NOSSA TORRE. Real sempre vence proxy.
        nova_pe = _compute_primeira_entrada(new_rows, op_name)
        for codigo, pe_str in nova_pe.items():
            if codigo not in merged:
                continue
            new_pe = _parse_dt_evento(pe_str)
            if not new_pe:
                continue
            existing_pe_str = merged[codigo].get("__primeira_entrada", "")
            existing_is_proxy = merged[codigo].get("__pe_proxy", False)
            existing_pe = _parse_dt_evento(existing_pe_str) if existing_pe_str else None
            if not existing_pe or existing_is_proxy:
                # Vazio ou proxy: PE real do novo vence.
                merged[codigo]["__primeira_entrada"] = pe_str
                merged[codigo]["__pe_proxy"] = False
            elif new_pe < existing_pe:
                # Ambos reais: mantém a MENOR (primeira entrada operacional verdadeira).
                merged[codigo]["__primeira_entrada"] = pe_str
                merged[codigo]["__pe_proxy"] = False

        # Proxy PE: MIN(Dt Evento) APENAS entre linhas com Hist=nossa op.
        # Linhas com Hist=outra torre (em trânsito) não contam — não estavam aqui.
        new_min_dt: dict[str, datetime] = {}
        for row in new_rows:
            codigo = str(row.get("Codigo", "")).strip()
            if not codigo:
                continue
            if op_upper is not None:
                if str(row.get("Hist. ultimo ponto", "")).strip().upper() != op_upper:
                    continue
            dt_ev = _parse_dt_evento(str(row.get("Dt Evento", "")))
            if dt_ev is None:
                continue
            if codigo not in new_min_dt or dt_ev < new_min_dt[codigo]:
                new_min_dt[codigo] = dt_ev
        for codigo, dt_ev in new_min_dt.items():
            if codigo not in merged:
                continue
            cur_pe_str = merged[codigo].get("__primeira_entrada", "")
            cur_is_proxy = merged[codigo].get("__pe_proxy", False)
            if not cur_pe_str:
                merged[codigo]["__primeira_entrada"] = dt_ev.strftime("%d/%m/%Y %H:%M:%S")
                merged[codigo]["__pe_proxy"] = True
            elif cur_is_proxy:
                cur_pe = _parse_dt_evento(cur_pe_str)
                if not cur_pe or dt_ev < cur_pe:
                    merged[codigo]["__primeira_entrada"] = dt_ev.strftime("%d/%m/%Y %H:%M:%S")
                    merged[codigo]["__pe_proxy"] = True

        rows_total = len(merged)
        _cache[tipo][key] = _build_entry(tipo, key, src_path, list(merged.values()))

    log.info(f"[perf] merge_incremental  {tipo}/{key} "
             f"(novas={rows_novas}, atualizadas={rows_atualizadas}, total_cache={rows_total}): "
             f"{time.perf_counter()-_t:.2f}s")
    log.info(f"[perf] merge_incremental TOTAL  {tipo}/{key}: {time.perf_counter()-_t_total:.2f}s")


async def _background_refresh():
    while True:
        await asyncio.sleep(REFRESH_SECONDS)
        log.info(f"[refresh] ciclo periódico (intervalo={REFRESH_SECONDS}s)")
        await refresh_all()

# ── APP ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="GestãoEntregas", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    global users_lock, _cache_lock, APP_VERSION, _dashboard_html
    users_lock  = asyncio.Lock()
    _cache_lock = asyncio.Lock()

    # Versão e HTML pré-processado (substituição única no startup)
    APP_VERSION = _get_version()
    log.info(f"[startup] Versão: {APP_VERSION}")
    if DASHBOARD.exists():
        _dashboard_html = DASHBOARD.read_text(encoding="utf-8").replace("{{APP_VERSION}}", APP_VERSION)
    else:
        _dashboard_html = ""

    # Cria apenas as duas pastas necessárias
    FIN_DIR.mkdir(parents=True, exist_ok=True)
    OP_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"[startup] Pastas: {FIN_DIR} | {OP_DIR}")

    # Inicializa banco SQLite e carrega usuários persistidos
    _init_db()
    async with users_lock:
        for u in _load_users_db():
            users_db[u.id] = u
        log.info(f"[startup] {len(users_db)} usuário(s) carregado(s) do banco")

        # Cria Diretor padrão apenas se não houver nenhum
        if not any(u.role == "DIRETOR" for u in users_db.values()):
            admin_id = str(uuid.uuid4())
            admin = User(
                id=admin_id, login="admin", nome="Administrador",
                password_hash=_hash(DEFAULT_ADMIN_PASSWORD),
                role="DIRETOR", ativo=True, must_change_password=True,
            )
            users_db[admin_id] = admin
            _upsert_user(admin)
            log.info("[startup] Usuário admin (Diretor) criado e salvo no banco")

    log.info("[startup] Escaneando pastas de dados...")
    await refresh_all()
    log.info("[startup] Concluído")
    asyncio.create_task(_background_refresh())

_NO_CACHE = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma":        "no-cache",
    "Expires":       "0",
}

# ── HEALTH ENDPOINTS ───────────────────────────────────────────────────────────
# Princípios:
#   - /healthz e /readyz são públicos (probes não autenticam).
#   - Nenhum endpoint dispara refresh_all (read-only sobre o cache).
#   - /health/sla é autenticado (números operacionais).
#   - Funções helper retornam dicts puros: facilita testes sem subir servidor.


def _healthz_payload() -> dict:
    """Liveness: processo está vivo e respondendo. Mínimo possível."""
    return {
        "status": "ok",
        "version": APP_VERSION or "unknown",
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _readyz_payload() -> tuple[dict, bool]:
    """Readiness: pronto para servir tráfego. Retorna (payload, ok).
    ok=False → caller deve responder 503.
    """
    checks: dict[str, object] = {}

    # 1) Startup completou? (refresh_all gravou _cache populado)
    cache_loaded = any(
        (_cache.get("operacional", {}).get(k) or {}).get("dados")
        for k in OPS
    )
    checks["startup_completo"] = bool(APP_VERSION)
    checks["cache_loaded"] = cache_loaded

    # 2) Quais ops têm dados de fato carregados
    ops_com_dados = [
        k for k in OPS
        if ((_cache.get("operacional", {}).get(k) or {}).get("linhas") or 0) > 0
    ]
    checks["operacoes_com_dados"] = ops_com_dados

    # 3) users.db acessível? Query trivial.
    try:
        with _db() as conn:
            conn.execute("SELECT 1").fetchone()
        checks["users_db_acessivel"] = True
    except Exception as exc:  # noqa: BLE001
        checks["users_db_acessivel"] = False
        checks["users_db_erro"] = str(exc)[:200]

    # 4) /data acessível?
    checks["data_dir"] = str(_DATA_ROOT)
    checks["data_dir_acessivel"] = _DATA_ROOT.exists() and OP_DIR.exists()

    ok = (
        checks["startup_completo"]
        and checks["cache_loaded"]
        and checks["users_db_acessivel"]
        and checks["data_dir_acessivel"]
    )

    return {
        "status": "ok" if ok else "not_ready",
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checks": checks,
    }, ok


def _cache_age_info() -> dict:
    """Mais recente `atualizado` entre as ops operacionais + idade em segundos."""
    latest: datetime | None = None
    ops_info = []
    for key in OPS:
        e = _cache.get("operacional", {}).get(key) or {}
        atualizado = e.get("atualizado")
        ops_info.append({
            "key": key,
            "linhas": e.get("linhas", 0),
            "arquivo": e.get("arquivo"),
            "atualizado": atualizado,
            "erro": e.get("erro"),
        })
        if atualizado:
            try:
                dt = datetime.strptime(atualizado, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if latest is None or dt > latest:
                    latest = dt
            except ValueError:
                pass
    if latest is None:
        return {"ultima_atualizacao": None, "idade_cache_segundos": None, "operacoes": ops_info}
    age = int((datetime.now(timezone.utc) - latest).total_seconds())
    return {
        "ultima_atualizacao": latest.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "idade_cache_segundos": age,
        "operacoes": ops_info,
    }


def _classify_sla_status(sla_pct: float, idade_segundos: int | None,
                         inconsistencias: int, total_sla: int) -> str:
    """Regras de severidade do /health/sla."""
    if total_sla == 0:
        return "critical"  # sem dados = não consegue auditar
    if inconsistencias > 0:
        return "critical"
    if idade_segundos is not None and idade_segundos > 3600:
        return "critical"
    if sla_pct < 85:
        return "critical"
    if idade_segundos is not None and idade_segundos > 1800:
        return "warning"
    if sla_pct < 95:
        return "warning"
    return "ok"


def _health_sla_payload() -> dict:
    """Resumo SLA + idade do cache + status (ok/warning/critical).
    Reaproveita _diag_audit (sem detalhes) — mesma regra do painel."""
    audit = _diag_audit(op_filter=None, codigos_filter=None)
    cache_info = _cache_age_info()
    status = _classify_sla_status(
        sla_pct=audit["resumo"]["sla_percentual"],
        idade_segundos=cache_info["idade_cache_segundos"],
        inconsistencias=audit["auditoria"]["inconsistencias"],
        total_sla=audit["resumo"]["total_sla"],
    )
    return {
        "status": status,
        "version": APP_VERSION or "unknown",
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sla": {
            **audit["resumo"],
            "inconsistencias": audit["auditoria"]["inconsistencias"],
        },
        "cache": cache_info,
    }


@app.get("/healthz")
async def healthz():
    """Liveness probe — sem auth."""
    return _healthz_payload()


@app.get("/readyz")
async def readyz():
    """Readiness probe — sem auth. Retorna 503 se não pronto."""
    payload, ok = _readyz_payload()
    if not ok:
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/health/sla")
async def health_sla(admin: User = Depends(_require_gestor)):
    """Saúde operacional do SLA (resumo + cache age + status)."""
    loop = asyncio.get_event_loop()
    async with _cache_lock:
        # Acessa _cache dentro do lock; computação fora.
        cache_snap = {
            "operacional": {
                k: {"dados": list((_cache.get("operacional", {}).get(k) or {}).get("dados") or []),
                    "linhas": (_cache.get("operacional", {}).get(k) or {}).get("linhas", 0),
                    "arquivo": (_cache.get("operacional", {}).get(k) or {}).get("arquivo"),
                    "atualizado": (_cache.get("operacional", {}).get(k) or {}).get("atualizado"),
                    "erro": (_cache.get("operacional", {}).get(k) or {}).get("erro")}
                for k in OPS
            },
            "financeiro": {},
        }

    def _run():
        global _cache
        saved = _cache
        try:
            _cache = cache_snap
            return _health_sla_payload()
        finally:
            _cache = saved

    return await loop.run_in_executor(None, _run)


# ── ROTAS PÚBLICAS ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    if _dashboard_html:
        return HTMLResponse(content=_dashboard_html, headers=_NO_CACHE)
    if DASHBOARD.exists():
        return HTMLResponse(
            content=DASHBOARD.read_text(encoding="utf-8").replace("{{APP_VERSION}}", APP_VERSION),
            headers=_NO_CACHE,
        )
    return HTMLResponse("<h2>GestãoEntregas.html não encontrado</h2>", status_code=503)

# ── AUTH ───────────────────────────────────────────────────────────────────────

@app.post("/auth/login")
async def login(req: LoginRequest):
    async with users_lock:
        user = next((u for u in users_db.values() if u.login.lower() == req.login.lower()), None)
    if not user or not _verify(req.password.strip(), user.password_hash):
        raise HTTPException(401, "Login ou senha inválidos")
    return {
        "access_token":        _create_token(user),
        "token_type":          "bearer",
        "role":                user.role,
        "must_change_password": user.must_change_password,
    }


@app.put("/auth/change-password")
async def change_password(
    req: ChangePasswordRequest,
    user: User = Depends(_get_current_user),
):
    if not _verify(req.current_password, user.password_hash):
        raise HTTPException(400, "Senha atual incorreta")
    if len(req.new_password) < 6:
        raise HTTPException(400, "Nova senha deve ter ao menos 6 caracteres")
    async with users_lock:
        users_db[user.id].password_hash = _hash(req.new_password)
        users_db[user.id].must_change_password = False
        updated = users_db[user.id]
        _upsert_user(updated)
    return {"access_token": _create_token(updated), "token_type": "bearer"}

# ── GESTÃO DE USUÁRIOS (ADMIN) ─────────────────────────────────────────────────

@app.get("/usuarios")
async def list_users(admin: User = Depends(_require_admin)):
    async with users_lock:
        return [
            {"id": u.id, "login": u.login, "nome": u.nome, "role": u.role,
             "ativo": u.ativo, "must_change_password": u.must_change_password}
            for u in users_db.values()
        ]


@app.post("/usuarios", status_code=201)
async def create_user(req: CreateUserRequest, admin: User = Depends(_require_admin)):
    if req.role not in ROLES:
        raise HTTPException(400, f"Role inválido. Use: {', '.join(sorted(ROLES))}")
    if len(req.password) < 6:
        raise HTTPException(400, "Senha deve ter ao menos 6 caracteres")
    async with users_lock:
        if any(u.login.lower() == req.login.lower() for u in users_db.values()):
            raise HTTPException(409, f"Login '{req.login}' já existe")
        uid = str(uuid.uuid4())
        new_user = User(
            id=uid, login=req.login.lower(), nome=req.nome,
            password_hash=_hash(req.password),
            role=req.role, ativo=True, must_change_password=True,
        )
        users_db[uid] = new_user
        _upsert_user(new_user)
    log.info(f"[usuario] criado: {req.login} ({req.role})")
    return {"id": uid, "login": new_user.login, "nome": new_user.nome,
            "role": req.role, "ativo": True, "must_change_password": True}


@app.put("/usuarios/{uid}")
async def update_user(uid: str, req: UpdateUserRequest, admin: User = Depends(_require_admin)):
    async with users_lock:
        user = users_db.get(uid)
        if not user:
            raise HTTPException(404, "Usuário não encontrado")
        if req.login is not None:
            if any(u.login.lower() == req.login.lower() and u.id != uid for u in users_db.values()):
                raise HTTPException(409, f"Login '{req.login}' já existe")
            user.login = req.login.lower()
        if req.nome is not None:
            user.nome = req.nome
        if req.password is not None:
            if len(req.password) < 6:
                raise HTTPException(400, "Senha deve ter ao menos 6 caracteres")
            user.password_hash = _hash(req.password)
        if req.role is not None:
            if req.role not in ROLES:
                raise HTTPException(400, f"Role inválido. Use: {', '.join(sorted(ROLES))}")
            user.role = req.role
        if req.ativo is not None:
            user.ativo = req.ativo
        if req.must_change_password is not None:
            user.must_change_password = req.must_change_password
        _upsert_user(user)
    return {"id": uid, "login": user.login, "nome": user.nome, "role": user.role,
            "ativo": user.ativo, "must_change_password": user.must_change_password}


@app.delete("/usuarios/{uid}", status_code=204)
async def delete_user(uid: str, admin: User = Depends(_require_admin)):
    async with users_lock:
        user = users_db.get(uid)
        if not user:
            raise HTTPException(404, "Usuário não encontrado")
        if uid == admin.id:
            raise HTTPException(400, "Você não pode remover sua própria conta")
        if user.role == "DIRETOR" and sum(1 for u in users_db.values() if u.role == "DIRETOR") == 1:
            raise HTTPException(400, "Não é possível remover o único Diretor")
        del users_db[uid]
        _delete_user_db(uid)
    log.info(f"[usuario] removido: {user.login}")


@app.post("/usuarios/{uid}/reset-senha", status_code=200)
async def reset_senha(uid: str, req: ResetSenhaRequest, admin: User = Depends(_require_admin)):
    if len(req.nova_senha) < 6:
        raise HTTPException(400, "Senha deve ter ao menos 6 caracteres")
    async with users_lock:
        user = users_db.get(uid)
        if not user:
            raise HTTPException(404, "Usuário não encontrado")
        user.password_hash = _hash(req.nova_senha)
        user.must_change_password = True
        _upsert_user(user)
    log.info(f"[usuario] senha redefinida: {user.login}")
    return {"ok": True, "login": user.login}


@app.patch("/usuarios/{uid}/ativo", status_code=200)
async def toggle_ativo(uid: str, admin: User = Depends(_require_admin)):
    async with users_lock:
        user = users_db.get(uid)
        if not user:
            raise HTTPException(404, "Usuário não encontrado")
        if uid == admin.id:
            raise HTTPException(400, "Você não pode desativar sua própria conta")
        user.ativo = not user.ativo
        _upsert_user(user)
    log.info(f"[usuario] {'ativado' if user.ativo else 'desativado'}: {user.login}")
    return {"ok": True, "login": user.login, "ativo": user.ativo}

# ── DADOS ──────────────────────────────────────────────────────────────────────

@app.get("/dados/{tipo}/{key}")
async def get_dados(
    tipo: str, key: str,
    user: User = Depends(_get_current_user),
):
    if tipo not in TIPOS:
        raise HTTPException(404, f"Tipo inválido: '{tipo}'. Use: financeiro, operacional")
    if key not in OPS:
        raise HTTPException(404, f"Operação inválida: '{key}'. Use: {', '.join(OPS)}")
    if tipo == "operacional" and user.role not in CAN_VIEW_OP:
        raise HTTPException(403, "Seu perfil não tem acesso aos dados operacionais")
    if tipo == "financeiro" and user.role not in CAN_VIEW_FIN:
        raise HTTPException(403, "Seu perfil não tem acesso aos dados financeiros")
    async with _cache_lock:
        entry = _cache.get(tipo, {}).get(key)
    if entry is None:
        raise HTTPException(503, f"Dados de {tipo}/{key} ainda não carregados")
    return JSONResponse(entry)


# ── DIAG SLA — helpers compartilhados ──────────────────────────────────────
# Replica fielmente entraNoSlaHoje (frontend, com safety net Status=ENTRADA+DtEvento)
# e a classificação calcM. Fonte única em Python, sem regra paralela.

_PEND_STATUS = {"EM ROTA", "EM ROTA PICKUP", "ENTRADA"}
_ENT_STATUS  = {"ENTREGUE", "ENTREGUE NO PICKUP"}


def _diag_ddiff(prev_str: str, today: datetime):
    d = _parse_dt_evento(prev_str[:10] if prev_str else "")
    if not d:
        return None
    d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return (today - d).days


def _diag_is_today(dt_str: str, today: datetime) -> bool:
    d = _parse_dt_evento(dt_str[:10] if dt_str else "")
    if not d:
        return False
    return d.replace(hour=0, minute=0, second=0, microsecond=0) >= today


def _diag_entra_no_sla(row: dict, today: datetime) -> tuple[bool, str]:
    """
    Replica frontend entraNoSlaHoje (linha ~1973 de GestãoEntregas.html):
      candidatos: PE (se houver) + DtEvento se Status=ENTRADA. Vence o MIN.
      Se MIN < hoje OU > hoje OU hoje<10h → inclui (true). Hoje >=10h → exclui (false).
    """
    pe_str = str(row.get("__primeira_entrada", "")).strip()
    dt_str = str(row.get("Dt Evento", "")).strip()
    status = str(row.get("Status", "")).strip().upper()
    cands = []
    if pe_str:
        cands.append(("PE", pe_str))
    if status == "ENTRADA" and dt_str:
        cands.append(("DtEv(ENTRADA)", dt_str))
    if not cands:
        return True, "sem candidato → inclui por precaução"
    parsed = []
    for lbl, s in cands:
        d = _parse_dt_evento(s)
        if d:
            parsed.append((lbl, s, d))
    if not parsed:
        return True, "candidatos não parsearam → inclui"
    parsed.sort(key=lambda x: x[2])
    lbl, s, d = parsed[0]
    dd = d.replace(hour=0, minute=0, second=0, microsecond=0)
    if dd < today:
        return True, f"{lbl}={s[:10]} < hoje → inclui (histórico)"
    if dd > today:
        return True, f"{lbl}={s[:10]} > hoje → inclui (futuro)"
    if d.hour < 10:
        return True, f"{lbl}={d.strftime('%H:%M')} < 10h → inclui"
    return False, f"{lbl}={d.strftime('%H:%M')} >= 10h → EXCLUI"


def _diag_build_dashrows(op_filter: str | None) -> tuple[dict[str, dict], dict[str, str]]:
    """
    Replica parseOp + dedup do frontend (getRows, currentOp='all'):
      - parseOp filtra Hist. ultimo ponto == op_name (estrito)
      - dedup por Codigo entre as 4 ops, vencendo o de DtEvento mais recente
    Retorna (dash_by_code, op_of_code) usando _cache em produção.
    """
    known_ops = {op_name.upper() for op_name, _ in OPS.values()}
    op_keys = [op_filter] if (op_filter and op_filter in OPS) else list(OPS.keys())

    dash_by_code: dict[str, dict] = {}
    op_of_code:   dict[str, str]  = {}

    for key in op_keys:
        op_name = OPS[key][0]
        entry = _cache.get("operacional", {}).get(key) or {}
        rows = entry.get("dados") or []
        if not rows:
            continue
        # parseOp do frontend
        has_known = any(
            str(r.get("Hist. ultimo ponto", "")).strip().upper() in known_ops
            for r in rows
        )
        op_upper = op_name.upper()
        for r in rows:
            cod = str(r.get("Codigo", "")).strip()
            if not cod:
                continue
            if has_known and str(r.get("Hist. ultimo ponto", "")).strip().upper() != op_upper:
                continue
            ex = dash_by_code.get(cod)
            if ex is None:
                dash_by_code[cod] = r
                op_of_code[cod] = key
                continue
            d_ex  = _parse_dt_evento(ex.get("Dt Evento", ""))
            d_new = _parse_dt_evento(r.get("Dt Evento", ""))
            if d_new and (not d_ex or d_new > d_ex):
                dash_by_code[cod] = r
                op_of_code[cod] = key
    return dash_by_code, op_of_code


def _diag_audit(op_filter: str | None, codigos_filter: set[str] | None) -> dict:
    """
    Auditoria completa do painel SLA + validação da regra das 10h.
    Reproduz: parseOp + dedup + calcM + entraNoSlaHoje.
    """
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    dash_by_code, op_of_code = _diag_build_dashrows(op_filter)

    if codigos_filter:
        dash_by_code = {c: r for c, r in dash_by_code.items() if c in codigos_filter}

    counts = {"vencidas": 0, "vencem_hoje": 0, "entregues_sla": 0, "custodia_sla": 0,
              "excluidas_10h": 0, "fora_sla": 0}
    detalhes: list[dict] = []
    inconsistencias: list[dict] = []
    suspeitas: list[dict] = []

    for cod, r in dash_by_code.items():
        status   = str(r.get("Status", "")).strip().upper()
        prev_str = str(r.get("Previsao", ""))
        dt_str   = str(r.get("Dt Evento", ""))
        ddiff    = _diag_ddiff(prev_str, today)
        entra, motivo = _diag_entra_no_sla(r, today)

        buckets: list[str] = []
        if ddiff is not None:
            if status in _PEND_STATUS and ddiff > 0 and entra:
                buckets.append("VENCIDAS"); counts["vencidas"] += 1
            if status in _PEND_STATUS and ddiff == 0 and entra:
                buckets.append("VENCEM_HOJE"); counts["vencem_hoje"] += 1
            if status in _ENT_STATUS and ddiff >= 0 and (_diag_is_today(dt_str, today) or ddiff == 0) and entra:
                buckets.append("ENTREGUES_SLA"); counts["entregues_sla"] += 1
            if status == "CUSTODIA" and ddiff == 0 and entra:
                buckets.append("CUSTODIA_SLA"); counts["custodia_sla"] += 1
        if not entra:
            counts["excluidas_10h"] += 1
        if not buckets:
            counts["fora_sla"] += 1

        # Validação ESTRITA: pacote no SLA cuja primeira entrada conhecida é hoje >=10h.
        # PE_real hoje >=10h ou Status=ENTRADA atual hoje >=10h → viola a regra (bug).
        tipo_inc = None
        suspeita = None
        if buckets:
            pe_str   = str(r.get("__primeira_entrada", "")).strip()
            pe_proxy = bool(r.get("__pe_proxy", False))
            pe_d     = _parse_dt_evento(pe_str)
            pe_day   = pe_d.replace(hour=0, minute=0, second=0, microsecond=0) if pe_d else None
            de       = _parse_dt_evento(dt_str)
            de_day   = de.replace(hour=0, minute=0, second=0, microsecond=0) if de else None

            # Estrita: PE real (não proxy) hoje >=10h, ou Status=ENTRADA hoje >=10h.
            if pe_d and not pe_proxy and pe_day == today and pe_d.hour >= 10:
                tipo_inc = f"PE_real hoje {pe_d:%H:%M} >=10h no SLA"
            elif status == "ENTRADA" and de and de_day == today and de.hour >= 10:
                tipo_inc = f"Status=ENTRADA hoje {de:%H:%M} >=10h no SLA"

            # Suspeita (auditoria visual): no SLA com DtEvento hoje>=10h E PE proxy
            # apontando antes de hoje. Pós-fix de Hist deve ser sempre legítimo
            # (pacote estava na torre antes), mas listamos para conferência manual.
            if not tipo_inc and pe_proxy and pe_d and pe_day < today \
                    and de and de_day == today and de.hour >= 10:
                suspeita = f"PE_proxy={pe_str[:10]} (estava na torre antes), DtEvento hoje {de:%H:%M}"

        if tipo_inc:
            inconsistencias.append({"codigo": cod, "operacao": op_of_code.get(cod, "?"), "tipo": tipo_inc,
                                    "status": status, "dt_evento": dt_str,
                                    "primeira_entrada": str(r.get("__primeira_entrada", "")),
                                    "categoria_sla": ",".join(buckets)})
        if suspeita:
            suspeitas.append({"codigo": cod, "operacao": op_of_code.get(cod, "?"), "tipo": suspeita,
                              "status": status, "dt_evento": dt_str,
                              "primeira_entrada": str(r.get("__primeira_entrada", "")),
                              "categoria_sla": ",".join(buckets)})

        rota = (str(r.get("Rota", "")).strip()
                or str(r.get("Hist. ultimo operador", "")).strip()
                or "—")

        detalhes.append({
            "codigo": cod,
            "operacao": op_of_code.get(cod, "?"),
            "status_atual": status,
            "dt_evento": dt_str,
            "previsao": prev_str,
            "ddiff": ddiff,
            "primeira_entrada_valida": str(r.get("__primeira_entrada", "")),
            "pe_proxy": bool(r.get("__pe_proxy", False)),
            "rota_entregador": rota,
            "entrou_no_sla": bool(buckets),
            "categoria_sla": ",".join(buckets) if buckets else "—",
            "motivo": motivo,
            "inconsistente": bool(tipo_inc),
            "tipo_inconsistencia": tipo_inc,
        })

    den = counts["entregues_sla"] + counts["vencidas"] + counts["vencem_hoje"]
    sla_pct = round(counts["entregues_sla"] / den * 100, 2) if den else 100.0

    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "today": today.strftime("%Y-%m-%d"),
        "filtros": {"operacao": op_filter, "codigos": sorted(codigos_filter) if codigos_filter else None},
        "resumo": {
            "dashboard_total": len(dash_by_code),
            "total_sla": counts["vencidas"] + counts["vencem_hoje"] + counts["entregues_sla"] + counts["custodia_sla"],
            "vencidas": counts["vencidas"],
            "vencem_hoje": counts["vencem_hoje"],
            "entregues_sla": counts["entregues_sla"],
            "custodia_sla": counts["custodia_sla"],
            "faltam": counts["vencidas"] + counts["vencem_hoje"],
            "sla_percentual": sla_pct,
        },
        "auditoria": {
            "total_excluidas_10h": counts["excluidas_10h"],
            "inconsistencias": len(inconsistencias),
            "inconsistencias_detalhe": inconsistencias[:100],
            "remessas_suspeitas": len(suspeitas),
            "remessas_suspeitas_detalhe": suspeitas[:100],
        },
        "detalhes": detalhes,
    }


def _diag_audit_to_csv(payload: dict) -> str:
    """Serializa detalhes da auditoria em CSV (delimitador ;)."""
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf, delimiter=";", quoting=_csv.QUOTE_MINIMAL)
    w.writerow(["Codigo","Operacao","Status","DtEvento","Previsao","dDiff",
                "PrimeiraEntrada","PE_proxy","Rota_Entregador",
                "EntrouNoSLA","CategoriaSLA","Motivo","Inconsistente","TipoInconsistencia"])
    for d in payload.get("detalhes", []):
        w.writerow([d["codigo"], d["operacao"], d["status_atual"], d["dt_evento"],
                    d["previsao"], d["ddiff"], d["primeira_entrada_valida"], d["pe_proxy"],
                    d["rota_entregador"], d["entrou_no_sla"], d["categoria_sla"],
                    d["motivo"], d["inconsistente"], d["tipo_inconsistencia"] or ""])
    return buf.getvalue()


@app.get("/diag/sla")
async def diag_sla(
    codigos: str | None = None,
    mode: str | None = None,
    op: str | None = None,
    export: str | None = None,
    admin: User = Depends(_require_gestor),
):
    """
    Diagnóstico SLA.

    Modos:
      mode=audit         → auditoria completa do painel + inconsistências da regra 10h
      (sem mode)         → legado: diagnóstico por código (codigos=A,B,C obrigatório)

    Filtros (modo audit):
      op=gv|itabira|jm|curvelo   → restringe a uma operação
      codigos=A,B,C              → restringe a códigos específicos
      export=csv                 → retorna CSV em vez de JSON
    """
    if mode == "audit":
        cset = {c.strip() for c in codigos.split(",") if c.strip()} if codigos else None
        op_key = op if (op and op in OPS) else None
        async with _cache_lock:
            payload = _diag_audit(op_filter=op_key, codigos_filter=cset)
        if export == "csv":
            from fastapi.responses import PlainTextResponse
            csv_text = _diag_audit_to_csv(payload)
            fname = f"diag_sla_audit_{payload['today']}.csv"
            return PlainTextResponse(
                content=csv_text, media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{fname}"'},
            )
        return payload

    # Modo legado — diagnóstico por código
    if not codigos:
        raise HTTPException(400, "Use codigos=A,B,C ou mode=audit")
    target = {c.strip() for c in codigos.split(",") if c.strip()}
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    async with _cache_lock:
        for tipo in TIPOS:
            for key, entry in _cache.get(tipo, {}).items():
                for row in (entry.get("dados") or []):
                    cod = str(row.get("Codigo", "")).strip()
                    if cod not in target:
                        continue
                    entra, motivo = _diag_entra_no_sla(row, today)
                    ddiff = _diag_ddiff(str(row.get("Previsao", "")), today)
                    result.append({
                        "codigo": cod, "tipo": tipo, "key": key,
                        "status": row.get("Status", ""),
                        "previsao": str(row.get("Previsao", "")),
                        "ddiff": ddiff,
                        "dt_evento": str(row.get("Dt Evento", "")),
                        "primeira_entrada": str(row.get("__primeira_entrada", "")) or "(vazio)",
                        "pe_proxy": bool(row.get("__pe_proxy", False)),
                        "entraNoSlaHoje": entra,
                        "motivo": motivo,
                    })
    not_found = target - {r["codigo"] for r in result}
    return {"ts": datetime.now().isoformat(timespec="seconds"), "diagnostico": result,
            "nao_encontrados": list(not_found)}


async def _do_rebuild_cache(admin_login: str) -> dict:
    """Rebuild completo do _cache lendo tudo do disco. Bloqueia uploads concorrentes."""
    log.info(f"[admin] {admin_login} → rebuild-cache iniciado")
    _t = time.perf_counter()
    await refresh_all()
    elapsed = time.perf_counter() - _t
    async with _cache_lock:
        ops_state = []
        for tipo in TIPOS:
            for key in OPS:
                e = _cache.get(tipo, {}).get(key, {})
                ops_state.append({
                    "tipo": tipo, "key": key,
                    "op": e.get("op"), "arquivo": e.get("arquivo"),
                    "linhas": e.get("linhas", 0),
                    "atualizado": e.get("atualizado"), "erro": e.get("erro"),
                })
    log.info(f"[admin] rebuild-cache concluído em {elapsed:.2f}s")
    return {
        "ok": True,
        "elapsed_seconds": round(elapsed, 2),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "operacoes": ops_state,
    }


@app.post("/admin/rebuild-cache")
async def admin_rebuild_cache_post(admin: User = Depends(_require_admin)):
    """Força refresh_all do zero: relê todos arquivos de /data e recalcula PE.
    Útil após deploy, mudança de regra, ou suspeita de inconsistência no cache."""
    return await _do_rebuild_cache(admin.login)


@app.get("/admin/rebuild-cache")
async def admin_rebuild_cache_get(admin: User = Depends(_require_admin)):
    """Alias GET de POST /admin/rebuild-cache (facilita chamada manual via browser)."""
    return await _do_rebuild_cache(admin.login)


@app.get("/status")
async def status(user: User = Depends(_get_current_user)):
    async with _cache_lock:
        snap = {t: dict(v) for t, v in _cache.items()}
    result = []
    for tipo in TIPOS:
        for key in OPS:
            e = snap.get(tipo, {}).get(key, {})
            result.append({
                "tipo":      tipo,
                "key":       key,
                "op":        e.get("op", OPS.get(key, ("?",))[0]),
                "arquivo":   e.get("arquivo"),
                "linhas":    e.get("linhas", 0),
                "atualizado": e.get("atualizado"),
                "erro":      e.get("erro"),
            })
    return {"ts": datetime.now().isoformat(timespec="seconds"), "operacoes": result}

# ── UPLOAD (ADMIN / GESTOR) ────────────────────────────────────────────────────

@app.post("/upload/{tipo}")
async def upload_file(
    tipo: str,
    file: UploadFile = File(...),
    user: User = Depends(_require_gestor),
):
    """
    Salva o arquivo na pasta dados/{tipo}/ e re-escaneia toda a pasta.
    O sistema detecta automaticamente a qual operação o arquivo pertence.
    """
    if tipo not in TIPOS:
        raise HTTPException(404, f"Tipo inválido: '{tipo}'. Use: financeiro, operacional")
    fname_lower = file.filename.lower()
    if not (fname_lower.endswith(".xlsx") or fname_lower.endswith(".zip")):
        raise HTTPException(400, "Apenas arquivos .xlsx ou .zip são aceitos")
    if file.filename.startswith("~$"):
        raise HTTPException(400, "Arquivo temporário do Excel não aceito")

    _t_upload = time.perf_counter()

    _t = time.perf_counter()
    content = await file.read()
    log.info(f"[perf] http_receive  {file.filename} ({len(content)//1024} KB): {time.perf_counter()-_t:.3f}s")

    if fname_lower.endswith(".zip"):
        try:
            _t = time.perf_counter()
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                xlsx_inside = [
                    n for n in zf.namelist()
                    if n.lower().endswith(".xlsx") and not Path(n).name.startswith("~$")
                ]
            log.info(f"[perf] inspect_zip  {file.filename}: {time.perf_counter()-_t:.3f}s")
            if not xlsx_inside:
                raise HTTPException(400, "O ZIP não contém nenhum arquivo .xlsx")
            log.info(f"[upload] zip contém {len(xlsx_inside)} xlsx: {xlsx_inside}")
        except zipfile.BadZipFile:
            raise HTTPException(400, "Arquivo ZIP inválido ou corrompido")

    folder = FIN_DIR if tipo == "financeiro" else OP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / file.filename
    _t = time.perf_counter()
    dest.write_bytes(content)
    log.info(f"[perf] save_to_disk  {file.filename} ({len(content)//1024} KB | tipo={tipo}): {time.perf_counter()-_t:.3f}s")
    log.info(f"[upload] {user.login} → dados/{tipo}/{file.filename} ({len(content)} bytes)")

    # Detecta a key do arquivo recém-salvo e atualiza só esse cache
    loop = asyncio.get_event_loop()
    detected_key = await loop.run_in_executor(None, _detect_key_from_path, dest)
    log.info(f"[upload] {file.filename} → key detectada: {detected_key}")

    _t = time.perf_counter()
    if detected_key:
        await merge_incremental(tipo, detected_key, dest)
    else:
        log.warning(f"[upload] {file.filename}: key não detectada, fallback refresh_tipo completo")
        await refresh_tipo(tipo)
    log.info(f"[perf] refresh_all_TOTAL  tipo={tipo}: {time.perf_counter()-_t:.2f}s")
    log.info(f"[perf] TOTAL_UPLOAD  {file.filename}: {time.perf_counter()-_t_upload:.2f}s")

    # Retorna quais operações foram identificadas na pasta
    async with _cache_lock:
        ops_status = [
            {"key": k, "op": v.get("op"), "arquivo": v.get("arquivo"), "linhas": v.get("linhas", 0)}
            for k, v in _cache.get(tipo, {}).items()
            if v.get("arquivo")
        ]

    return {
        "ok":          True,
        "arquivo":     file.filename,
        "tipo":        tipo,
        "operacoes":   ops_status,
    }

# ── ENTRY POINT ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

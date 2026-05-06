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
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

# ── CONFIG ─────────────────────────────────────────────────────────────────────

REFRESH_SECONDS        = int(os.environ.get("REFRESH_SECONDS", "300"))
JWT_SECRET             = os.environ.get("JWT_SECRET", "mude-em-producao-use-openssl-rand-hex-32")
JWT_ALGORITHM          = "HS256"
JWT_EXPIRE_HOURS       = 12
DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "Admin@123")

BASE_DIR  = Path(__file__).parent
DADOS_DIR = BASE_DIR / "dados"
FIN_DIR   = DADOS_DIR / "financeiro"
OP_DIR    = DADOS_DIR / "operacional"
DASHBOARD = BASE_DIR / "GestãoEntregas.html"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("api")

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

ROLES = {"ADMIN", "GESTOR", "OPERACIONAL"}

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer  = HTTPBearer()


@dataclass
class User:
    id: str
    login: str
    password_hash: str
    role: str
    must_change_password: bool = False


users_db:   dict[str, User] = {}
users_lock: asyncio.Lock
_cache_lock: asyncio.Lock


def _hash(plain: str) -> str:
    return pwd_ctx.hash(plain)


def _verify(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


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
    return user


def _require_admin(user: User = Depends(_get_current_user)) -> User:
    if user.role != "ADMIN":
        raise HTTPException(403, "Acesso restrito a ADMIN")
    return user


def _require_gestor(user: User = Depends(_get_current_user)) -> User:
    if user.role not in {"ADMIN", "GESTOR"}:
        raise HTTPException(403, "Acesso restrito a ADMIN e GESTOR")
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
    password: str
    role: str


class UpdateUserRequest(BaseModel):
    login: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    must_change_password: Optional[bool] = None

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

    for zf_path in zip_files:
        zip_mtime = zf_path.stat().st_mtime
        sub_dir = Path(temp_dir) / zf_path.stem
        sub_dir.mkdir(exist_ok=True)
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
        except zipfile.BadZipFile:
            log.error(f"[zip] {zf_path.name}: ZIP inválido ou corrompido")
        except Exception as exc:
            log.error(f"[zip] {zf_path.name}: {exc}")

    return extracted, temp_dir


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
            df = pd.read_excel(path, header=None, engine="openpyxl", nrows=330)
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


def _read_xlsx(path: Path) -> list[dict]:
    """Lê o xlsx completo (somente leitura) e retorna lista de dicts."""
    df = pd.read_excel(path, header=None, engine="openpyxl")
    header_idx = _find_header_idx(df)
    headers = [str(v).strip() if pd.notna(v) else "" for v in df.iloc[header_idx]]

    rows = []
    for _, row in df.iloc[header_idx + 1:].iterrows():
        if row.isna().all():
            continue
        obj = {headers[j]: _fmt(row.iloc[j])
               for j in range(len(headers)) if headers[j]}
        if any(obj.values()):
            rows.append(obj)
    return rows


def _read_and_merge(paths: list[Path]) -> list[dict]:
    """
    Lê todos os arquivos e mescla por coluna 'Codigo'.
    Processa do mais antigo ao mais recente: arquivo mais recente vence em conflitos.
    Garante que o total de remessas nunca diminua entre atualizações — Codigos
    que saem do relatório novo ainda ficam com o último status conhecido.
    """
    merged: dict[str, dict] = {}
    _no_key = 0

    for path in reversed(paths):  # mais antigo primeiro → mais recente sobrescreve
        try:
            rows = _read_xlsx(path)
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
        rows = _read_and_merge(paths)
        newest = paths[0]
        log.info(f"[load] {tipo}/{key} — {len(paths)} arquivo(s) → {len(rows)} linhas mescladas")
        return _build_entry(tipo, key, newest, rows)
    except Exception as exc:
        log.error(f"[load] {tipo}/{key}: {exc}")
        return _build_entry(tipo, key, paths[0], erro=str(exc))


async def refresh_all():
    """Escaneia as pastas (xlsx e zip), detecta operações e atualiza o cache."""
    loop = asyncio.get_event_loop()

    (fin_scan, fin_tmp), (op_scan, op_tmp) = await asyncio.gather(
        loop.run_in_executor(None, _scan_folder, FIN_DIR),
        loop.run_in_executor(None, _scan_folder, OP_DIR),
    )

    scans = {"financeiro": fin_scan, "operacional": op_scan}
    pairs = [(t, k) for t in TIPOS for k in OPS]

    results = await asyncio.gather(
        *[_refresh_from_paths(t, k, scans[t].get(k, [])) for t, k in pairs],
        return_exceptions=True,
    )

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


async def _background_refresh():
    while True:
        await asyncio.sleep(REFRESH_SECONDS)
        log.info(f"[refresh] ciclo periódico (intervalo={REFRESH_SECONDS}s)")
        await refresh_all()

# ── APP ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="GestãoEntregas", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def startup():
    global users_lock, _cache_lock
    users_lock  = asyncio.Lock()
    _cache_lock = asyncio.Lock()

    # Cria apenas as duas pastas necessárias
    FIN_DIR.mkdir(parents=True, exist_ok=True)
    OP_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"[startup] Pastas: {FIN_DIR} | {OP_DIR}")

    # Usuário admin padrão (must_change_password=True no primeiro acesso)
    admin_id = str(uuid.uuid4())
    users_db[admin_id] = User(
        id=admin_id, login="admin",
        password_hash=_hash(DEFAULT_ADMIN_PASSWORD),
        role="ADMIN", must_change_password=True,
    )
    log.info("[startup] Usuário admin criado (login=admin)")

    log.info("[startup] Escaneando pastas de dados...")
    await refresh_all()
    log.info("[startup] Concluído")
    asyncio.create_task(_background_refresh())

# ── ROTAS PÚBLICAS ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    if DASHBOARD.exists():
        return DASHBOARD.read_text(encoding="utf-8")
    return HTMLResponse("<h2>GestãoEntregas.html não encontrado</h2>", status_code=503)

# ── AUTH ───────────────────────────────────────────────────────────────────────

@app.post("/auth/login")
async def login(req: LoginRequest):
    async with users_lock:
        user = next((u for u in users_db.values() if u.login == req.login), None)
    if not user or not _verify(req.password, user.password_hash):
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
    return {"access_token": _create_token(updated), "token_type": "bearer"}

# ── GESTÃO DE USUÁRIOS (ADMIN) ─────────────────────────────────────────────────

@app.get("/usuarios")
async def list_users(admin: User = Depends(_require_admin)):
    async with users_lock:
        return [{"id": u.id, "login": u.login, "role": u.role,
                 "must_change_password": u.must_change_password}
                for u in users_db.values()]


@app.post("/usuarios", status_code=201)
async def create_user(req: CreateUserRequest, admin: User = Depends(_require_admin)):
    if req.role not in ROLES:
        raise HTTPException(400, f"Role inválido. Use: {', '.join(sorted(ROLES))}")
    async with users_lock:
        if any(u.login == req.login for u in users_db.values()):
            raise HTTPException(409, f"Login '{req.login}' já existe")
        uid = str(uuid.uuid4())
        users_db[uid] = User(
            id=uid, login=req.login,
            password_hash=_hash(req.password),
            role=req.role, must_change_password=True,
        )
    log.info(f"[usuario] criado: {req.login} ({req.role})")
    return {"id": uid, "login": req.login, "role": req.role, "must_change_password": True}


@app.put("/usuarios/{uid}")
async def update_user(uid: str, req: UpdateUserRequest, admin: User = Depends(_require_admin)):
    async with users_lock:
        user = users_db.get(uid)
        if not user:
            raise HTTPException(404, "Usuário não encontrado")
        if req.login is not None:
            if any(u.login == req.login and u.id != uid for u in users_db.values()):
                raise HTTPException(409, f"Login '{req.login}' já existe")
            user.login = req.login
        if req.password is not None:
            if len(req.password) < 6:
                raise HTTPException(400, "Senha deve ter ao menos 6 caracteres")
            user.password_hash = _hash(req.password)
        if req.role is not None:
            if req.role not in ROLES:
                raise HTTPException(400, f"Role inválido. Use: {', '.join(sorted(ROLES))}")
            user.role = req.role
        if req.must_change_password is not None:
            user.must_change_password = req.must_change_password
    return {"id": uid, "login": user.login, "role": user.role,
            "must_change_password": user.must_change_password}


@app.delete("/usuarios/{uid}", status_code=204)
async def delete_user(uid: str, admin: User = Depends(_require_admin)):
    async with users_lock:
        user = users_db.get(uid)
        if not user:
            raise HTTPException(404, "Usuário não encontrado")
        if user.role == "ADMIN" and sum(1 for u in users_db.values() if u.role == "ADMIN") == 1:
            raise HTTPException(400, "Não é possível remover o único administrador")
        del users_db[uid]
    log.info(f"[usuario] removido: {user.login}")

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
    async with _cache_lock:
        entry = _cache.get(tipo, {}).get(key)
    if entry is None:
        raise HTTPException(503, f"Dados de {tipo}/{key} ainda não carregados")
    return JSONResponse(entry)


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

    content = await file.read()

    if fname_lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                xlsx_inside = [
                    n for n in zf.namelist()
                    if n.lower().endswith(".xlsx") and not Path(n).name.startswith("~$")
                ]
            if not xlsx_inside:
                raise HTTPException(400, "O ZIP não contém nenhum arquivo .xlsx")
            log.info(f"[upload] zip contém {len(xlsx_inside)} xlsx: {xlsx_inside}")
        except zipfile.BadZipFile:
            raise HTTPException(400, "Arquivo ZIP inválido ou corrompido")

    folder = FIN_DIR if tipo == "financeiro" else OP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / file.filename
    dest.write_bytes(content)
    log.info(f"[upload] {user.login} → dados/{tipo}/{file.filename} ({len(content)} bytes)")

    # Re-escaneia a pasta inteira para atualizar todas as operações
    await refresh_all()

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

"""
Bearlz CMS — Plataforma de carrosseis para @gabriel.bearlz
===========================================================
Dashboard web para criar, revisar e aprovar carrosseis do Instagram.

Rodar localmente:
  pip install -r requirements.txt
  python app.py
  → http://localhost:5000

Expor para Gabriel (sem deploy):
  cloudflared tunnel --url http://localhost:5000

Deploy permanente: ver DEPLOY.md
"""

import os
import re
import json
import sqlite3
import urllib.parse
from datetime import datetime
from pathlib import Path

try:
    import anthropic as _anthropic_lib
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

from flask import (
    Flask, render_template, jsonify, request,
    send_from_directory, abort, redirect, url_for
)

# ── Config ────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "bearlz-dev-2026")

BASE_DIR      = Path(__file__).parent
# Dados persistentes (DB + edits compartilhadas + carrosseis gerados em prod)
# ficam em /data, que é onde o disco persistente do Render é montado.
# Os 9 HTMLs fixos (carga-tributaria, bitcoin-2026, etc) ficam no repo
# (carrosseis/) e são atualizados a cada deploy.
DATA_DIR       = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH        = DATA_DIR / "bearlz.db"
CARROSSEIS_DIR = BASE_DIR / "carrosseis"
# Onde vão os carrosseis gerados via /api/gerar (disco persistente, nao se
# perdem entre deploys).
GENERATED_DIR  = DATA_DIR / "generated"
GENERATED_DIR.mkdir(exist_ok=True)

def _find_carrossel_file(nome: str):
    """Procura o arquivo de carrossel em ambos os diretorios (repo + persistente).
    Carrosseis gerados em prod vao pra GENERATED_DIR; carrosseis fixos do repo
    ficam em CARROSSEIS_DIR. Retorna o Path existente ou None."""
    p1 = CARROSSEIS_DIR / nome
    if p1.exists():
        return p1
    p2 = GENERATED_DIR / nome
    if p2.exists():
        return p2
    return None

# NOTA: a migracao antiga (copiar bearlz.db da raiz pro data/) foi removida.
# Ela estava sobrescrevendo o DB persistente com uma versao stale do repo a
# cada deploy, fazendo os posts gerados em prod sumirem.

# Chave para a API interna (usada por gerar-lote.py para registrar carrosseis)
CMS_API_KEY = os.environ.get("CMS_API_KEY", "bearlz-local-key")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ── Persistencia via GitHub API ───────────────────────────────────────────────
#
# O disco "persistente" do Render no plano free eh resetado a cada deploy.
# Como workaround, a gente commita os arquivos gerados (HTMLs + edits JSON) em
# uma branch separada do repo (`data-generated`) via API do GitHub. No boot,
# a gente puxa essa branch de volta pra dentro de data/.
#
# Branch separada = nao dispara auto-deploy (que so observa `main`).
#
# Requer: env var GITHUB_TOKEN (Personal Access Token com escopo `repo`).
# Opcional: GITHUB_REPO (default `adremerc/bearlz-cms`).
import base64 as _b64
import threading as _threading
try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO   = os.environ.get("GITHUB_REPO", "adremerc/bearlz-cms")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "data-generated")

def _gh_enabled():
    return bool(GITHUB_TOKEN and _REQUESTS_OK)

def _gh_api(method, path, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    headers["Accept"] = "application/vnd.github+json"
    headers["X-GitHub-Api-Version"] = "2022-11-28"
    url = f"https://api.github.com/repos/{GITHUB_REPO}{path}"
    try:
        return _requests.request(method, url, headers=headers, timeout=15, **kwargs)
    except Exception:
        return None

def _gh_ensure_branch():
    """Cria a branch data-generated a partir de main se nao existir."""
    br = _gh_api("GET", f"/branches/{GITHUB_BRANCH}")
    if br and br.status_code == 200:
        return True
    main = _gh_api("GET", "/branches/main")
    if not main or main.status_code != 200:
        return False
    main_sha = main.json()["commit"]["sha"]
    r = _gh_api("POST", "/git/refs",
                json={"ref": f"refs/heads/{GITHUB_BRANCH}", "sha": main_sha})
    return bool(r and r.status_code in (200, 201))

def _gh_save(repo_path: str, content_bytes: bytes, message: str):
    """Faz commit de `content_bytes` em `repo_path` na branch data-generated.
    Se o arquivo ja existe, atualiza (precisa do sha). Roda de forma silenciosa:
    nunca levanta exception pra nao quebrar a request principal."""
    if not _gh_enabled():
        return False
    try:
        _gh_ensure_branch()
        # Pega sha atual (se existir) pra fazer update
        get_resp = _gh_api("GET", f"/contents/{repo_path}",
                           params={"ref": GITHUB_BRANCH})
        sha = None
        if get_resp and get_resp.status_code == 200:
            try:
                sha = get_resp.json().get("sha")
            except Exception:
                pass
        body = {
            "message": message,
            "content": _b64.b64encode(content_bytes).decode("ascii"),
            "branch":  GITHUB_BRANCH,
        }
        if sha:
            body["sha"] = sha
        put = _gh_api("PUT", f"/contents/{repo_path}", json=body)
        return bool(put and put.status_code in (200, 201))
    except Exception:
        return False

def _gh_save_async(repo_path: str, content_bytes: bytes, message: str):
    """Versao assincrona: nao bloqueia a request HTTP principal."""
    if not _gh_enabled():
        return
    t = _threading.Thread(
        target=_gh_save,
        args=(repo_path, content_bytes, message),
        daemon=True
    )
    t.start()

def _gh_delete(repo_path: str, message: str):
    if not _gh_enabled():
        return False
    try:
        get_resp = _gh_api("GET", f"/contents/{repo_path}",
                           params={"ref": GITHUB_BRANCH})
        if not get_resp or get_resp.status_code != 200:
            return True  # nada pra deletar
        sha = get_resp.json().get("sha")
        r = _gh_api("DELETE", f"/contents/{repo_path}",
                    json={"message": message, "sha": sha, "branch": GITHUB_BRANCH})
        return bool(r and r.status_code == 200)
    except Exception:
        return False

def _gh_hydrate():
    """No boot, puxa todos os arquivos da branch `data-generated` pra dentro
    de data/. Roda uma vez, silencioso se falhar."""
    if not _gh_enabled():
        return 0
    n = 0
    try:
        br = _gh_api("GET", f"/branches/{GITHUB_BRANCH}")
        if not br or br.status_code != 200:
            # branch nao existe ainda; cria e sai
            _gh_ensure_branch()
            return 0
        tree_sha = br.json()["commit"]["commit"]["tree"]["sha"]
        tree = _gh_api("GET", f"/git/trees/{tree_sha}",
                       params={"recursive": "1"})
        if not tree or tree.status_code != 200:
            return 0
        for item in tree.json().get("tree", []):
            if item.get("type") != "blob":
                continue
            repo_path = item.get("path", "")
            # So nos interessam arquivos dentro de data/generated/ e data/edits/
            if not (repo_path.startswith("data/generated/")
                    or repo_path.startswith("data/edits/")):
                continue
            blob = _gh_api("GET", f"/git/blobs/{item['sha']}")
            if not blob or blob.status_code != 200:
                continue
            try:
                content = _b64.b64decode(blob.json()["content"])
            except Exception:
                continue
            local = BASE_DIR / repo_path
            try:
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(content)
                n += 1
            except Exception:
                pass
    except Exception:
        pass
    return n


# ── Banco de dados (SQLite) ───────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS carrosseis (
                slug          TEXT PRIMARY KEY,
                titulo        TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'rascunho',
                prioridade    TEXT NOT NULL DEFAULT 'media',
                arquivo       TEXT,
                num_slides    INTEGER DEFAULT 0,
                tempo_revisao INTEGER DEFAULT 0,
                created_at    TEXT DEFAULT (datetime('now')),
                updated_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS notas (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                slug        TEXT NOT NULL,
                autor       TEXT NOT NULL DEFAULT 'Gabriel',
                texto       TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now'))
            );
        """)
        # Migração de dados — renomeia autor "Adrenaldo" → "Adre"
        conn.execute("UPDATE notas SET autor='Adre' WHERE autor='Adrenaldo'")

        # Migração para bases existentes — adiciona colunas novas se não existirem
        for col, definition in [
            ("prioridade",      "TEXT NOT NULL DEFAULT 'media'"),
            ("tempo_revisao",   "INTEGER DEFAULT 0"),
            ("data_publicacao", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE carrosseis ADD COLUMN {col} {definition}")
            except Exception:
                pass  # coluna já existe


def scan_carrosseis_dir():
    """Escaneia as pastas carrosseis/ (repo) e data/generated/ (disco persistente)
    e registra HTMLs novos no banco."""
    dirs_to_scan = [d for d in (CARROSSEIS_DIR, GENERATED_DIR) if d.exists()]
    if not dirs_to_scan:
        return
    with get_db() as conn:
        files = []
        for d in dirs_to_scan:
            files.extend(d.glob("carrossel-*.html"))
        for html in sorted(files, key=lambda f: f.stat().st_mtime, reverse=True):
            exists = conn.execute(
                "SELECT 1 FROM carrosseis WHERE arquivo = ?", (html.name,)
            ).fetchone()
            if exists:
                continue
            slug = html.stem
            try:
                content = html.read_text(encoding="utf-8")
                m = re.search(r"<title>Carrossel — (.*?) \|", content)
                titulo = m.group(1).strip() if m else slug
                # Conta os slides
                n = len(re.findall(r"\{id:\d+,", content))
            except Exception:
                titulo, n = slug, 0

            conn.execute(
                "INSERT OR IGNORE INTO carrosseis (slug, titulo, arquivo, num_slides) "
                "VALUES (?, ?, ?, ?)",
                (slug, titulo, html.name, n)
            )


PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

def load_anthropic_key():
    global ANTHROPIC_API_KEY, PEXELS_API_KEY
    have_anthropic = ANTHROPIC_API_KEY and ANTHROPIC_API_KEY.startswith("sk-ant-api")
    have_pexels    = PEXELS_API_KEY and len(PEXELS_API_KEY) > 20 and "COLE_SUA" not in PEXELS_API_KEY
    if have_anthropic and have_pexels:
        return
    try:
        config_path = Path(__file__).parent.parent / "idea-bot" / "config.py"
        if config_path.exists():
            ns = {}
            exec(config_path.read_text(encoding="utf-8"), ns)
            if not have_anthropic:
                key = ns.get("ANTHROPIC_API_KEY", "")
                if key.startswith("sk-ant-api"):
                    ANTHROPIC_API_KEY = key
            if not have_pexels:
                pkey = ns.get("PEXELS_API_KEY", "")
                if pkey and len(pkey) > 20 and "COLE_SUA" not in pkey:
                    PEXELS_API_KEY = pkey
    except:
        pass


# ── Wrapper com retry pra chamadas Claude ────────────────────────────────────
# Anthropic eventualmente retorna 529 'overloaded_error'. Backoff exponencial
# resolve a maioria dos casos sem o usuario perceber.
def claude_call_with_retry(client, max_retries=4, **params):
    import time as _time
    last_err = None
    for attempt in range(max_retries):
        try:
            return client.messages.create(**params)
        except Exception as e:
            msg = str(e)
            # Retry pra overloaded (529), rate limit (429) ou erros transitorios
            transient = (
                "overloaded" in msg.lower() or
                "529" in msg or
                "rate_limit" in msg.lower() or
                "429" in msg or
                "timeout" in msg.lower() or
                "connection" in msg.lower()
            )
            last_err = e
            if not transient or attempt >= max_retries - 1:
                raise
            # Backoff exponencial: 1s, 2s, 4s, 8s
            _time.sleep(2 ** attempt)
    raise last_err


# Inicializa DB e escaneia pasta na startup
init_db()
# Hidrata a partir do branch data-generated ANTES do scan, pra que os
# arquivos gerados em prod voltem pro filesystem antes do scan os registrar.
_gh_hydrate()
scan_carrosseis_dir()
load_anthropic_key()


@app.context_processor
def inject_gabriel_count():
    """Injeta contagens de pendências do Gabriel em todos os templates."""
    try:
        with get_db() as conn:
            para_gabriel = conn.execute(
                "SELECT COUNT(*) FROM carrosseis WHERE status='analise_gabriel'"
            ).fetchone()[0]
            aguardando_adre = conn.execute(
                "SELECT COUNT(*) FROM carrosseis WHERE status IN ('rascunho','analise_adre')"
            ).fetchone()[0]
        return {"gabriel_fila": para_gabriel, "adre_fila": aguardando_adre}
    except Exception:
        return {"gabriel_fila": 0, "adre_fila": 0}


# ── Helpers ───────────────────────────────────────────────────────────────────

STATUS_LABELS = {
    "rascunho":      ("Rascunho",        "gray"),
    "analise_adre":  ("Análise Adre",    "orange"),
    "analise_gabriel": ("Análise Gabriel", "purple"),
    "aprovado":      ("Aprovado",        "green"),
    "publicado":     ("Publicado",       "blue"),
}


# ── Admin: historico do estado salvo (via branch GitHub data-generated) ───────

def _admin_check_key():
    return request.headers.get("X-Admin-Key", "") == CMS_API_KEY

@app.route("/api/_admin/bump-asset-version", methods=["POST"])
def api_admin_bump_assets():
    """Faz patch em todos os HTMLs em data/generated/ trocando
    ?v=N pra ?v=N+1 nos links de carousel-shared.css/js. Forca os
    browsers a baixarem a versao nova. Requer X-Admin-Key."""
    if not _admin_check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    new_v = str(data.get("version", "2"))
    fixados = 0
    if GENERATED_DIR.exists():
        for html in GENERATED_DIR.glob("carrossel-*.html"):
            try:
                content = html.read_text(encoding="utf-8")
                novo = re.sub(
                    r'(carousel-shared\.(?:css|js))\?v=\d+',
                    rf'\1?v={new_v}',
                    content
                )
                if novo != content:
                    html.write_text(novo, encoding="utf-8")
                    fixados += 1
            except Exception:
                pass
    return jsonify({"ok": True, "fixados": fixados, "version": new_v})


@app.route("/api/_admin/state-history/<slug>", methods=["GET"])
def api_admin_state_history(slug):
    """Lista versoes de data/edits/<slug>.json no branch data-generated.
    Cada item: {sha, date, slides_count, message}. Requer X-Admin-Key."""
    if not _admin_check_key():
        return jsonify({"error": "unauthorized"}), 401
    if not _gh_enabled():
        return jsonify({"error": "GITHUB_TOKEN nao configurado"}), 400
    safe_slug = re.sub(r"[^a-zA-Z0-9_\-]", "", slug)
    repo_path = f"data/edits/{safe_slug}.json"
    # Lista commits no branch data-generated que tocam esse arquivo
    r = _gh_api("GET", "/commits",
                params={"path": repo_path, "sha": GITHUB_BRANCH, "per_page": 30})
    if not r or r.status_code != 200:
        return jsonify({"error": f"GitHub API: {r.status_code if r else 'no response'}"}), 500
    commits = r.json()
    versions = []
    for c in commits:
        sha = c["sha"]
        date = c["commit"]["committer"]["date"]
        msg = c["commit"]["message"][:80]
        # Pega o conteudo desse commit pra contar slides
        blob = _gh_api("GET", f"/contents/{repo_path}",
                       params={"ref": sha})
        slides_count = None
        if blob and blob.status_code == 200:
            try:
                content_b64 = blob.json().get("content", "")
                content = _b64.b64decode(content_b64).decode("utf-8")
                payload = json.loads(content)
                slides = (payload.get("state") or {}).get("slides") or []
                slides_count = len(slides)
            except Exception:
                pass
        versions.append({
            "sha": sha[:8],
            "sha_full": sha,
            "date": date,
            "message": msg,
            "slides": slides_count,
        })
    return jsonify({"ok": True, "versions": versions})

@app.route("/api/_admin/state-restore/<slug>", methods=["POST"])
def api_admin_state_restore(slug):
    """Restaura state de um commit especifico do branch data-generated.
    Body: {sha: '<full_sha>'}. Requer X-Admin-Key."""
    if not _admin_check_key():
        return jsonify({"error": "unauthorized"}), 401
    if not _gh_enabled():
        return jsonify({"error": "GITHUB_TOKEN nao configurado"}), 400
    data = request.get_json() or {}
    sha = data.get("sha", "").strip()
    if not sha:
        return jsonify({"error": "sha obrigatorio"}), 400
    safe_slug = re.sub(r"[^a-zA-Z0-9_\-]", "", slug)
    repo_path = f"data/edits/{safe_slug}.json"
    blob = _gh_api("GET", f"/contents/{repo_path}", params={"ref": sha})
    if not blob or blob.status_code != 200:
        return jsonify({"error": f"versao nao encontrada (status {blob.status_code if blob else 'no response'})"}), 404
    content = _b64.b64decode(blob.json()["content"]).decode("utf-8")
    # Sobrescreve o arquivo local em data/edits/
    p = _edits_path(slug)
    p.write_text(content, encoding="utf-8")
    # Re-commita no branch (atualizado)
    _gh_save_async(repo_path, content.encode("utf-8"),
                   f"Restore: {slug} para {sha[:8]}")
    payload = json.loads(content)
    slides = (payload.get("state") or {}).get("slides") or []
    return jsonify({"ok": True, "slides_count": len(slides), "from_sha": sha[:8]})

PRIO_LABELS = {
    "alta":  ("Alta",  "red"),
    "media": ("Média", "yellow"),
    "baixa": ("Baixa", "gray"),
}

def fmt_tempo(segundos: int) -> str:
    """Formata segundos em hh:mm:ss ou mm:ss."""
    if not segundos:
        return ""
    h = segundos // 3600
    m = (segundos % 3600) // 60
    s = segundos % 60
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"

try:
    from zoneinfo import ZoneInfo
    _TZ_BR = ZoneInfo("America/Sao_Paulo")
    _TZ_UTC = ZoneInfo("UTC")
except Exception:
    # Python <3.9 fallback: deslocamento fixo de -3h (BR nao tem mais horario
    # de verao desde 2019)
    from datetime import timezone, timedelta
    _TZ_BR = timezone(timedelta(hours=-3))
    _TZ_UTC = timezone.utc

def fmt_data(iso: str) -> str:
    """Formata ISO timestamp pra dd/mm/yyyy hh:mm em America/Sao_Paulo.
    Aceita strings com 'Z', com offset, ou naive (assume UTC nesse caso)."""
    try:
        s = (iso or "").strip()
        # SQLite datetime('now') retorna 'YYYY-MM-DD HH:MM:SS' (sem T, sem Z)
        # Substitui espaco por T pra fromisoformat aceitar
        if " " in s and "T" not in s:
            s = s.replace(" ", "T", 1)
        # 'Z' nao e aceito por fromisoformat antes de Python 3.11
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ_UTC)  # assume UTC pra dados antigos
        dt = dt.astimezone(_TZ_BR)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return iso or ""


# ── Rotas principais ──────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    status_filter = request.args.get("status", "todos")
    prio_filter   = request.args.get("prio",   "todos")
    busca         = request.args.get("q", "").strip().lower()

    with get_db() as conn:
        # Build query with optional filters
        where, params = [], []
        if status_filter != "todos":
            where.append("status = ?"); params.append(status_filter)
        if prio_filter != "todos":
            where.append("prioridade = ?"); params.append(prio_filter)
        sql = "SELECT * FROM carrosseis"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()

        counts = {
            "todos":           conn.execute("SELECT COUNT(*) FROM carrosseis").fetchone()[0],
            "rascunho":        conn.execute("SELECT COUNT(*) FROM carrosseis WHERE status='rascunho'").fetchone()[0],
            "analise_adre":    conn.execute("SELECT COUNT(*) FROM carrosseis WHERE status='analise_adre'").fetchone()[0],
            "analise_gabriel": conn.execute("SELECT COUNT(*) FROM carrosseis WHERE status='analise_gabriel'").fetchone()[0],
            "aprovado":        conn.execute("SELECT COUNT(*) FROM carrosseis WHERE status='aprovado'").fetchone()[0],
            "publicado":       conn.execute("SELECT COUNT(*) FROM carrosseis WHERE status='publicado'").fetchone()[0],
        }

    carrosseis = [dict(r) for r in rows]
    if busca:
        carrosseis = [c for c in carrosseis if busca in c["titulo"].lower()]

    for c in carrosseis:
        c["status_label"], c["status_color"] = STATUS_LABELS.get(c["status"], ("?", "gray"))
        c["prio_label"],   c["prio_color"]   = PRIO_LABELS.get(c.get("prioridade","media"), ("Média","yellow"))
        c["created_fmt"]  = fmt_data(c["created_at"])
        c["tempo_fmt"]    = fmt_tempo(c.get("tempo_revisao") or 0)

    return render_template("index.html",
                           carrosseis=carrosseis,
                           status_filter=status_filter,
                           prio_filter=prio_filter,
                           counts=counts,
                           busca=busca)


@app.route("/c/<slug>")
def ver_carrossel(slug):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM carrosseis WHERE slug = ?", (slug,)
        ).fetchone()
        if not row:
            abort(404)
        notas = conn.execute(
            "SELECT * FROM notas WHERE slug = ? ORDER BY created_at DESC", (slug,)
        ).fetchall()

    carrossel = dict(row)
    carrossel["status_label"], carrossel["status_color"] = STATUS_LABELS.get(carrossel["status"], ("?", "gray"))
    carrossel["prio_label"],   carrossel["prio_color"]   = PRIO_LABELS.get(carrossel.get("prioridade","media"), ("Média","yellow"))
    carrossel["created_fmt"] = fmt_data(carrossel["created_at"])
    carrossel["tempo_fmt"]   = fmt_tempo(carrossel.get("tempo_revisao") or 0)

    notas_list = [dict(n) for n in notas]
    for n in notas_list:
        n["created_fmt"] = fmt_data(n["created_at"])

    return render_template("viewer.html",
                           c=carrossel,
                           notas=notas_list)


@app.route("/c/<slug>/arquivo")
def arquivo_carrossel(slug):
    """Serve o arquivo HTML do carrossel para o iframe."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT arquivo FROM carrosseis WHERE slug = ?", (slug,)
        ).fetchone()
    if not row or not row["arquivo"]:
        abort(404)
    # Carrosseis fixos do repo em CARROSSEIS_DIR; carrosseis gerados em prod
    # em GENERATED_DIR (disco persistente).
    path = _find_carrossel_file(row["arquivo"])
    if not path:
        abort(404)
    return send_from_directory(path.parent, path.name)


# ── Edits compartilhadas (ambos editam, ambos veem) ───────────────────────────

EDITS_DIR = DATA_DIR / "edits"
EDITS_DIR.mkdir(exist_ok=True)

# Migração: se existir carrosseis/edits/ (lugar antigo), move pro novo
_old_edits = CARROSSEIS_DIR / "edits"
if _old_edits.exists() and _old_edits.is_dir():
    import shutil
    for _f in _old_edits.glob("*.json"):
        _target = EDITS_DIR / _f.name
        if not _target.exists():
            shutil.copy2(_f, _target)


def _edits_path(slug: str) -> Path:
    # Sanitize slug para nao permitir path traversal
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "", slug)
    return EDITS_DIR / f"{safe}.json"


@app.route("/api/carrossel/<slug>/state", methods=["GET"])
def api_carrossel_state(slug):
    """Retorna o estado salvo (slides editados, perfil, estilo). None se nunca editado."""
    p = _edits_path(slug)
    if not p.exists():
        return jsonify({"state": None, "updated_at": None, "autor": None})
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return jsonify(data)
    except Exception:
        return jsonify({"state": None, "updated_at": None, "autor": None})


@app.route("/api/carrossel/<slug>/save", methods=["POST"])
def api_carrossel_save(slug):
    """Salva o estado atual do editor. Body JSON: {state:{slides,profile,style}, autor:'Adre'|'Gabriel'}"""
    data = request.get_json(silent=True) or {}
    state = data.get("state")
    autor = data.get("autor") or "Anônimo"
    if not state or not isinstance(state, dict):
        return jsonify({"error": "state obrigatorio"}), 400
    p = _edits_path(slug)
    payload = {
        "state": state,
        "autor": autor,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        body = json.dumps(payload, ensure_ascii=False)
        p.write_text(body, encoding="utf-8")
        with get_db() as conn:
            conn.execute(
                "UPDATE carrosseis SET updated_at=datetime('now') WHERE slug=?", (slug,)
            )
        # Commita os edits no branch data-generated (sobrevive deploys).
        _gh_save_async(
            f"data/edits/{p.name}",
            body.encode("utf-8"),
            f"Edit: {slug} por {autor}"
        )
        return jsonify({"ok": True, "updated_at": payload["updated_at"], "autor": autor})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API JSON ──────────────────────────────────────────────────────────────────

@app.route("/api/status", methods=["POST"])
def api_status():
    data = request.get_json() or {}
    slug   = data.get("slug")
    status = data.get("status")
    if status not in STATUS_LABELS:
        return jsonify({"error": "Status inválido"}), 400
    with get_db() as conn:
        conn.execute(
            "UPDATE carrosseis SET status=?, updated_at=datetime('now') WHERE slug=?",
            (status, slug)
        )
    return jsonify({"ok": True, "status": status,
                    "label": STATUS_LABELS[status][0],
                    "color": STATUS_LABELS[status][1]})


@app.route("/api/nota", methods=["POST"])
def api_nota_add():
    data  = request.get_json() or {}
    slug  = data.get("slug", "")
    autor = data.get("autor", "Gabriel").strip() or "Gabriel"
    texto = data.get("texto", "").strip()
    if not texto:
        return jsonify({"error": "Nota vazia"}), 400
    with get_db() as conn:
        conn.execute(
            "INSERT INTO notas (slug, autor, texto) VALUES (?, ?, ?)",
            (slug, autor, texto)
        )
        nota_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        ts = conn.execute(
            "SELECT created_at FROM notas WHERE id=?", (nota_id,)
        ).fetchone()["created_at"]
    return jsonify({"ok": True, "id": nota_id,
                    "created_fmt": fmt_data(ts), "autor": autor, "texto": texto})


@app.route("/api/nota/<int:nota_id>", methods=["DELETE"])
def api_nota_delete(nota_id):
    with get_db() as conn:
        conn.execute("DELETE FROM notas WHERE id = ?", (nota_id,))
    return jsonify({"ok": True})


@app.route("/api/prioridade", methods=["POST"])
def api_prioridade():
    data = request.get_json() or {}
    slug      = data.get("slug")
    prioridade = data.get("prioridade")
    if prioridade not in ("alta", "media", "baixa"):
        return jsonify({"error": "Prioridade inválida"}), 400
    with get_db() as conn:
        conn.execute(
            "UPDATE carrosseis SET prioridade=?, updated_at=datetime('now') WHERE slug=?",
            (prioridade, slug)
        )
    PRIO_LABELS = {"alta": ("Alta", "red"), "media": ("Média", "yellow"), "baixa": ("Baixa", "gray")}
    label, color = PRIO_LABELS[prioridade]
    return jsonify({"ok": True, "label": label, "color": color})


@app.route("/api/tempo/zerar", methods=["POST"])
def api_tempo_zerar():
    """Zera o tempo acumulado de revisão de um carrossel."""
    data = request.get_json() or {}
    slug = data.get("slug", "")
    if not slug:
        return jsonify({"error": "slug obrigatório"}), 400
    with get_db() as conn:
        conn.execute(
            "UPDATE carrosseis SET tempo_revisao = 0, updated_at=datetime('now') WHERE slug=?",
            (slug,)
        )
    return jsonify({"ok": True})


@app.route("/api/tempo", methods=["POST"])
def api_tempo():
    """Acumula segundos de revisão ao total do carrossel."""
    data    = request.get_json() or {}
    slug    = data.get("slug", "")
    segundos = max(0, int(data.get("segundos", 0)))
    if not slug or segundos == 0:
        return jsonify({"ok": True})
    with get_db() as conn:
        conn.execute(
            "UPDATE carrosseis SET tempo_revisao = COALESCE(tempo_revisao,0) + ?, "
            "updated_at=datetime('now') WHERE slug=?",
            (segundos, slug)
        )
        row = conn.execute(
            "SELECT tempo_revisao FROM carrosseis WHERE slug=?", (slug,)
        ).fetchone()
    total = row["tempo_revisao"] if row else segundos
    return jsonify({"ok": True, "total": total})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Re-escaneia a pasta carrosseis/ para pegar arquivos novos."""
    scan_carrosseis_dir()
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM carrosseis").fetchone()[0]
    return jsonify({"ok": True, "total": total})


@app.route("/api/registrar", methods=["POST"])
def api_registrar():
    """
    Endpoint chamado por gerar-lote.py após gerar cada carrossel.
    Registra ou atualiza o carrossel no banco de dados.
    """
    key = request.headers.get("X-API-Key", "")
    if key != CMS_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data      = request.get_json() or {}
    slug      = data.get("slug", "")
    titulo    = data.get("titulo", slug)
    arquivo   = data.get("arquivo", "")
    n_slides  = int(data.get("num_slides", 0))

    if not slug:
        return jsonify({"error": "slug obrigatório"}), 400

    with get_db() as conn:
        conn.execute("""
            INSERT INTO carrosseis (slug, titulo, arquivo, num_slides, status)
            VALUES (?, ?, ?, ?, 'rascunho')
            ON CONFLICT(slug) DO UPDATE SET
                titulo     = excluded.titulo,
                arquivo    = excluded.arquivo,
                num_slides = excluded.num_slides,
                updated_at = datetime('now')
        """, (slug, titulo, arquivo, n_slides))

    return jsonify({"ok": True})


# ── Calendar ──────────────────────────────────────────────────────────────────

@app.route("/calendario")
def calendario():
    from datetime import date, timedelta
    semana_str = request.args.get("semana", "")
    try:
        start = date.fromisoformat(semana_str)
        start = start - timedelta(days=start.weekday())
    except Exception:
        today = date.today()
        start = today - timedelta(days=today.weekday())
    days = [start + timedelta(days=i) for i in range(7)]
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM carrosseis WHERE data_publicacao BETWEEN ? AND ? ORDER BY data_publicacao",
            (days[0].isoformat(), days[6].isoformat())
        ).fetchall()
        all_rows = conn.execute(
            "SELECT slug, titulo, status FROM carrosseis ORDER BY created_at DESC"
        ).fetchall()
    cal = {d.isoformat(): [] for d in days}
    for row in rows:
        d = row["data_publicacao"]
        if d in cal:
            c = dict(row)
            c["status_label"], c["status_color"] = STATUS_LABELS.get(c["status"], ("?", "gray"))
            cal[d].append(c)
    from datetime import date as _date
    return render_template("calendar.html",
        days=days, cal=cal,
        all_carrosseis=[dict(r) for r in all_rows],
        prev_week=(days[0] - timedelta(days=7)).isoformat(),
        next_week=(days[0] + timedelta(days=7)).isoformat(),
        today=_date.today().isoformat())


@app.route("/api/agendar", methods=["POST"])
def api_agendar():
    data = request.get_json() or {}
    with get_db() as conn:
        conn.execute("UPDATE carrosseis SET data_publicacao=?, updated_at=datetime('now') WHERE slug=?",
                     (data.get("data"), data.get("slug")))
    return jsonify({"ok": True})


@app.route("/api/carrossel/<slug>", methods=["DELETE"])
def api_deletar_carrossel(slug):
    with get_db() as conn:
        row = conn.execute("SELECT arquivo FROM carrosseis WHERE slug=?", (slug,)).fetchone()
        if row and row["arquivo"]:
            html_path = _find_carrossel_file(row["arquivo"])
            try:
                if html_path and html_path.exists():
                    html_path.unlink()
            except Exception:
                pass
            # Propaga delete pro branch data-generated (gerados + edits).
            _gh_delete(f"data/generated/{row['arquivo']}", f"Delete: {slug}")
            _gh_delete(f"data/edits/{slug}.json",          f"Delete edits: {slug}")
        conn.execute("DELETE FROM notas WHERE slug=?", (slug,))
        conn.execute("DELETE FROM carrosseis WHERE slug=?", (slug,))
    return jsonify({"ok": True})


# ── Gabriel inbox ─────────────────────────────────────────────────────────────

@app.route("/gabriel")
def gabriel_inbox():
    with get_db() as conn:
        para_revisar = conn.execute(
            "SELECT * FROM carrosseis WHERE status='analise_gabriel' ORDER BY updated_at DESC"
        ).fetchall()
        aguardando = conn.execute(
            "SELECT * FROM carrosseis WHERE status IN ('rascunho','analise_adre') ORDER BY updated_at DESC"
        ).fetchall()

    def enrich(rows):
        result = []
        for r in rows:
            c = dict(r)
            c["status_label"], c["status_color"] = STATUS_LABELS.get(c["status"], ("?", "gray"))
            c["prio_label"],   c["prio_color"]   = PRIO_LABELS.get(c.get("prioridade","media"), ("Média","yellow"))
            c["updated_fmt"] = fmt_data(c["updated_at"])
            result.append(c)
        return result

    return render_template("gabriel.html",
                           para_revisar=enrich(para_revisar),
                           aguardando=enrich(aguardando))


# ── Revisar com Claude ────────────────────────────────────────────────────────

SYSTEM_REVISAO = """Você é editor sênior de carrosseis @gabriel.bearlz. Recebe slides atuais + instruções de revisão. Aplica correções mantendo o estilo.

VOZ — ESCRITA HUMANIZADA:
- Escreve como analista CONVERSANDO, não como IA
- PROIBIDOS clichês de IA:
  * "Na prática," — não usar
  * "O que acontece é que," — não usar
  * "Com isso," — só 1x no carrossel inteiro
  * "Vale destacar", "é importante ressaltar", "cabe destacar" — proibido
  * Frases picotadas: "Queda. Alta. Oportunidade." — proibido
- NUNCA use travessão (—)
- Sem emoji, sem hashtag
- ASPAS DUPLAS " sempre (não ')

NEGRITOS — FRASES INTEIRAS (4-12 palavras), não palavras isoladas:
- BOM: **a maior alta em 10 anos**
- RUIM: **9%**, **Selic**

ARREDONDAMENTO:
- "R$ 14 bilhões" não "R$ 14,247 bilhões"

CONEXÃO ENTRE SLIDES:
- Cada slide se conecta com o anterior, conta uma história

PARÁGRAFOS:
- Máximo 3 linhas. 2-3 parágrafos por slide separados por \\n\\n

TAMANHO: 180-420 chars, intercalado. MAX 420.

RETORNE SOMENTE JSON VÁLIDO:
{"slides": ["texto do slide 1", "texto do slide 2", ...]}

Pra dividir um slide, adicione texto como elemento extra no array."""


@app.route("/api/revisar/<slug>", methods=["POST"])
def api_revisar(slug):
    if not ANTHROPIC_AVAILABLE:
        return jsonify({"error": "Biblioteca anthropic não instalada"}), 400
    if not ANTHROPIC_API_KEY or not ANTHROPIC_API_KEY.startswith("sk-ant-api"):
        return jsonify({"error": "ANTHROPIC_API_KEY não configurada"}), 400

    data      = request.get_json() or {}
    instrucoes = data.get("instrucoes", "").strip()
    if not instrucoes:
        return jsonify({"error": "Instruções obrigatórias"}), 400

    with get_db() as conn:
        row = conn.execute(
            "SELECT arquivo FROM carrosseis WHERE slug=?", (slug,)
        ).fetchone()
    if not row or not row["arquivo"]:
        return jsonify({"error": "Carrossel não encontrado"}), 404

    html_path = _find_carrossel_file(row["arquivo"])
    if not html_path:
        return jsonify({"error": "Arquivo HTML não encontrado"}), 404

    html = html_path.read_text(encoding="utf-8")

    # Localiza o bloco const slides=[...]; de forma robusta
    # Usa a posição do marcador de início e procura o fechamento correto
    arr_start = html.find('const slides=[')
    if arr_start == -1:
        return jsonify({"error": "Não foi possível ler os slides do arquivo"}), 400

    # Avança até o '[' e conta colchetes para achar o ']' correto
    bracket_pos = html.index('[', arr_start)
    depth, i = 0, bracket_pos
    while i < len(html):
        if html[i] == '[':
            depth += 1
        elif html[i] == ']':
            depth -= 1
            if depth == 0:
                break
        i += 1
    slides_raw = html[arr_start: i + 1]  # "const slides=[...]"

    # Extrai cada slide: captura text (entre backticks) e o restante dos campos
    # image pode ser qualquer expressão JS (string, concatenação com variáveis, etc.)
    slide_objs = re.findall(
        r'\{id:(\d+),text:`(.*?)`,\s*(image:.*?),\s*zoom:([\d.]+),\s*ox:([\d.]+),\s*oy:([\d.]+)\}',
        slides_raw, re.DOTALL
    )
    if not slide_objs:
        return jsonify({"error": "Não foi possível interpretar os slides"}), 400

    # Desfaz escapes JS para o Claude ler o texto limpo
    def unescape_js(t):
        return t.replace("\\n", "\n").replace("\\`", "`").replace("\\\\", "\\")

    # Reescape para inserir de volta no template literal JS
    def escape_js(t):
        return (t.replace("\\", "\\\\")
                 .replace("`",  "\\`")
                 .replace("${", "\\${")
                 .replace("\n", "\\n"))

    # Monta numeração dos slides para o Claude
    slides_numerados = "\n\n".join(
        f"[SLIDE {i+1}]\n{unescape_js(text)}"
        for i, (sid, text, *_) in enumerate(slide_objs)
    )

    # Pede APENAS os números dos slides que precisam mudar e o novo texto
    prompt = (
        f"Carrossel com {len(slide_objs)} slides:\n\n"
        f"{slides_numerados}\n\n"
        f"INSTRUÇÕES: {instrucoes}\n\n"
        f"Altere SOMENTE os slides que as instruções mencionam. "
        f"Slides não mencionados devem ser retornados EXATAMENTE iguais ao original, palavra por palavra.\n"
        f"Retorne SOMENTE JSON onde a chave é o número do slide (string) e o valor é o texto:\n"
        f'{{ "1": "texto do slide 1 se mudou", "3": "texto do slide 3 se mudou" }}\n'
        f"Inclua na resposta TODOS os slides, mesmo os que não mudaram."
    )

    try:
        client = _anthropic_lib.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp   = claude_call_with_retry(client,
            model="claude-sonnet-4-5", max_tokens=5000,
            system=SYSTEM_REVISAO,
            messages=[{"role": "user", "content": prompt}]
        )
        texto = resp.content[0].text.strip()
        if texto.startswith("```"):
            texto = re.sub(r"^```[a-z]*\n?", "", texto)
            texto = re.sub(r"\n?```$", "", texto).strip()

        alteracoes = _parse_claude_json(texto)  # robusto a aspas mal escapadas
        if alteracoes is None:
            return jsonify({
                "error": "Resposta do Claude veio malformada. Tente reformular as instruções.",
                "raw": texto[:500]
            }), 500
        if not alteracoes:
            return jsonify({"error": "Claude não retornou alterações"}), 500

        # Aplica cada alteração de forma CIRÚRGICA: troca só o campo text:`...`
        # sem tocar em mais nada do HTML
        html_novo = html
        alterados = 0
        for i, (sid, old_text_raw, *_) in enumerate(slide_objs):
            chave = str(i + 1)
            if chave not in alteracoes:
                continue
            # Sanitiza: remove travessoes que o Claude insiste em colocar e
            # garante quebras de paragrafo
            novo_texto = _sanitizar_slide(alteracoes[chave])
            novo_raw   = escape_js(novo_texto)

            # Só substitui se realmente mudou
            if novo_raw == old_text_raw:
                continue

            old_js = f"text:`{old_text_raw}`"
            new_js = f"text:`{novo_raw}`"
            if old_js in html_novo:
                html_novo = html_novo.replace(old_js, new_js, 1)
                alterados += 1

        if alterados == 0:
            return jsonify({"ok": True, "num_slides": len(slide_objs),
                            "msg": "Nenhuma alteração necessária"})

        html_path.write_text(html_novo, encoding="utf-8")

        with get_db() as conn:
            conn.execute(
                "UPDATE carrosseis SET updated_at=datetime('now') WHERE slug=?", (slug,)
            )

        return jsonify({"ok": True, "num_slides": len(slide_objs), "alterados": alterados})

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Resposta inválida do Claude: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Revisar: preview (gera alteracoes propostas SEM escrever) + apply ─────────
# Permite usuario ver as mudancas antes de aplicar (igual ao fluxo de hooks).

def _revisar_carrega_html(slug):
    """Helper compartilhado: pega o HTML do carrossel e parseia os slides.
    Retorna (html_path, html, slide_objs, escape_js, unescape_js) ou
    (None, None, None, None, None) com error_response setado."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT arquivo FROM carrosseis WHERE slug=?", (slug,)
        ).fetchone()
    if not row or not row["arquivo"]:
        return None, jsonify({"error": "Carrossel não encontrado"}), 404
    html_path = _find_carrossel_file(row["arquivo"])
    if not html_path:
        return None, jsonify({"error": "Arquivo HTML não encontrado"}), 404
    html = html_path.read_text(encoding="utf-8")
    arr_start = html.find('const slides=[')
    if arr_start == -1:
        return None, jsonify({"error": "Não foi possível ler os slides"}), 400
    bracket_pos = html.index('[', arr_start)
    depth, i = 0, bracket_pos
    while i < len(html):
        if html[i] == '[':
            depth += 1
        elif html[i] == ']':
            depth -= 1
            if depth == 0:
                break
        i += 1
    slides_raw = html[arr_start: i + 1]
    slide_objs = re.findall(
        r'\{id:(\d+),text:`(.*?)`,\s*(image:.*?),\s*zoom:([\d.]+),\s*ox:([\d.]+),\s*oy:([\d.]+)\}',
        slides_raw, re.DOTALL
    )
    if not slide_objs:
        return None, jsonify({"error": "Não foi possível interpretar os slides"}), 400
    return (html_path, html, slide_objs), None, 0

def _parse_claude_json(texto: str):
    """Tenta parsear JSON do Claude com varios fallbacks pra lidar com:
    - control chars (json.loads strict=True nao aceita)
    - aspas duplas dentro de valores nao escapadas
    - newlines literais dentro de strings
    - texto antes/depois do JSON
    Retorna dict ou None."""
    if not texto:
        return None
    # 1. Tentativa direta com strict=False (aceita control chars)
    try:
        return json.loads(texto, strict=False)
    except Exception:
        pass
    # 2. Extrai apenas o primeiro { ... } balanceado
    try:
        start = texto.find('{')
        if start == -1:
            return None
        depth = 0
        for i in range(start, len(texto)):
            ch = texto[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    candidate = texto[start:i+1]
                    return json.loads(candidate, strict=False)
    except Exception:
        pass
    # 3. Tenta extrair pares "chave": "valor" via regex tolerante a aspas
    try:
        out = {}
        for m in re.finditer(
            r'"(\d+)"\s*:\s*"((?:[^"\\]|\\.)*)"',
            texto, re.DOTALL
        ):
            chave = m.group(1)
            # Desescape JSON
            valor = m.group(2)
            valor = (valor.replace('\\"', '"')
                          .replace('\\n', '\n')
                          .replace('\\t', '\t')
                          .replace('\\\\', '\\'))
            out[chave] = valor
        return out if out else None
    except Exception:
        return None

def _revisar_unescape_js(t):
    return t.replace("\\n", "\n").replace("\\`", "`").replace("\\\\", "\\")

def _revisar_escape_js(t):
    return (t.replace("\\", "\\\\")
             .replace("`",  "\\`")
             .replace("${", "\\${")
             .replace("\n", "\\n"))


SYSTEM_REVISAO_PREVIEW = """Você é editor sênior de carrosseis @gabriel.bearlz. Recebe slides numerados + instruções de revisão.

VOZ — ESCRITA HUMANIZADA (foco #1):
- Escreve como analista conversando, NÃO como IA
- PROIBIDOS clichês de IA:
  * "Na prática," — não usar
  * "O que acontece é que," — não usar
  * "Com isso," — só 1x no carrossel inteiro
  * "Vale destacar", "é importante ressaltar", "cabe destacar", "é fundamental" — proibido
  * Frases picotadas: "Queda. Alta. Oportunidade." — proibido
- NUNCA use travessão (—)
- Sem emoji, sem hashtag
- ASPAS DUPLAS " sempre (não ')

NEGRITOS — FRASES INTEIRAS:
- Negrite TRECHOS de 4-12 palavras com sentido completo
- BOM: **a maior alta em 10 anos**
- RUIM (palavra isolada): **Selic**, **9%**
- 2-3 negritos por slide

ARREDONDAMENTO:
- "R$ 14 bilhões" não "R$ 14,247 bilhões"
- "9%" não "9,3%"

CONEXÃO ENTRE SLIDES:
- Cada slide se conecta ao anterior. É HISTÓRIA, não info jogada.
- Slide N+1 referência o slide N implicitamente

PARÁGRAFOS:
- Máximo 3 linhas. Cada slide tem 2-3 parágrafos separados por \\n\\n

TAMANHO: 180-420 chars, intercalado. Curtos (220-260) e maiores (320-400). MAX 420.

FORMATO DA RESPOSTA — JSON ÚNICO COM TODOS OS SLIDES:
{"1": "Texto do slide 1.\\n\\nSegundo paragrafo.", "2": "Texto inalterado do slide 2."}

- Chave: numero como string
- Valor: novo texto (com \\n\\n entre paragrafos)
- INCLUA TODOS os slides
- ASPAS DUPLAS sempre. Escape interno com \\"
- NÃO retorne markdown, array, ou texto fora do JSON."""


@app.route("/api/revisar/<slug>/preview", methods=["POST"])
def api_revisar_preview(slug):
    """Pede sugestoes ao Claude e retorna SEM escrever. Frontend mostra
    diff e usuario aprova manualmente via /apply."""
    if not ANTHROPIC_AVAILABLE:
        return jsonify({"error": "Biblioteca anthropic não instalada"}), 400
    if not ANTHROPIC_API_KEY or not ANTHROPIC_API_KEY.startswith("sk-ant-api"):
        return jsonify({"error": "ANTHROPIC_API_KEY não configurada"}), 400

    data       = request.get_json() or {}
    instrucoes = data.get("instrucoes", "").strip()
    if not instrucoes:
        return jsonify({"error": "Instruções obrigatórias"}), 400

    loaded, err_resp, err_status = _revisar_carrega_html(slug)
    if not loaded:
        return err_resp, err_status
    html_path, html, slide_objs = loaded

    slides_numerados = "\n\n".join(
        f"[SLIDE {i+1}]\n{_revisar_unescape_js(text)}"
        for i, (sid, text, *_) in enumerate(slide_objs)
    )
    prompt = (
        f"Carrossel com {len(slide_objs)} slides:\n\n"
        f"{slides_numerados}\n\n"
        f"INSTRUÇÕES: {instrucoes}\n\n"
        f"Altere SOMENTE os slides que as instruções mencionam. "
        f"Slides não mencionados devem ser retornados EXATAMENTE iguais ao original.\n"
        f"Retorne SOMENTE JSON onde a chave é o número do slide (string) e o valor é o texto:\n"
        f'{{ "1": "texto do slide 1 se mudou", "3": "texto do slide 3 se mudou" }}\n'
        f"Inclua TODOS os slides na resposta, mesmo os que não mudaram."
    )

    try:
        client = _anthropic_lib.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = claude_call_with_retry(client,
            model="claude-sonnet-4-5", max_tokens=5000,
            system=SYSTEM_REVISAO_PREVIEW,
            messages=[{"role": "user", "content": prompt}]
        )
        texto = resp.content[0].text.strip()
        if texto.startswith("```"):
            texto = re.sub(r"^```[a-z]*\n?", "", texto)
            texto = re.sub(r"\n?```$", "", texto).strip()
        alteracoes = _parse_claude_json(texto)
        if alteracoes is None:
            return jsonify({
                "error": "Resposta do Claude veio malformada. Tente reformular as instruções.",
                "raw": texto[:500]
            }), 500

        # Monta lista de mudancas REAIS (so onde o texto difere)
        propostas = []
        for i, (sid, old_text_raw, *_) in enumerate(slide_objs):
            chave = str(i + 1)
            if chave not in alteracoes:
                continue
            texto_atual = _revisar_unescape_js(old_text_raw)
            texto_novo  = _sanitizar_slide(alteracoes[chave])
            if texto_novo.strip() == texto_atual.strip():
                continue  # sem mudanca real
            propostas.append({
                "slide": i + 1,
                "atual": texto_atual,
                "novo":  texto_novo,
            })

        return jsonify({
            "ok": True,
            "num_slides": len(slide_objs),
            "propostas": propostas,
        })
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Resposta inválida do Claude: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/revisar/<slug>/apply", methods=["POST"])
def api_revisar_apply(slug):
    """Aplica alteracoes ja aprovadas pelo usuario. Body: {alteracoes:
    [{slide:1, novo:'texto'}, {slide:3, novo:'texto'}]}"""
    data = request.get_json() or {}
    alteracoes = data.get("alteracoes", [])
    if not alteracoes or not isinstance(alteracoes, list):
        return jsonify({"error": "Nenhuma alteração para aplicar"}), 400

    loaded, err_resp, err_status = _revisar_carrega_html(slug)
    if not loaded:
        return err_resp, err_status
    html_path, html, slide_objs = loaded

    html_novo = html
    aplicados = 0
    for alt in alteracoes:
        idx = alt.get("slide")
        novo = alt.get("novo")
        if not isinstance(idx, int) or not isinstance(novo, str):
            continue
        if idx < 1 or idx > len(slide_objs):
            continue
        sid, old_text_raw, *_ = slide_objs[idx - 1]
        novo_san = _sanitizar_slide(novo)
        novo_raw = _revisar_escape_js(novo_san)
        if novo_raw == old_text_raw:
            continue
        old_js = f"text:`{old_text_raw}`"
        new_js = f"text:`{novo_raw}`"
        if old_js in html_novo:
            html_novo = html_novo.replace(old_js, new_js, 1)
            aplicados += 1

    if aplicados == 0:
        return jsonify({"ok": True, "aplicados": 0,
                        "msg": "Nenhuma alteração efetiva"})

    html_path.write_text(html_novo, encoding="utf-8")
    with get_db() as conn:
        conn.execute("UPDATE carrosseis SET updated_at=datetime('now') WHERE slug=?", (slug,))
    # Persiste no branch GitHub data-generated (se enabled)
    _gh_save_async(
        f"data/generated/{html_path.name}",
        html_novo.encode("utf-8"),
        f"Revisao aplicada: {slug}"
    )
    return jsonify({"ok": True, "aplicados": aplicados,
                    "num_slides": len(slide_objs)})


# ── Generator ─────────────────────────────────────────────────────────────────

# ── System prompt do gerador (extraido pra constante pra ser editavel via UI) ──
SYSTEM_GERAR_DEFAULT = (
    "Você é redator sênior de conteúdo financeiro para @gabriel.bearlz no Instagram.\n"
    "Estilo: análise aprofundada que cria autoridade. Público: investidores brasileiros 25-45.\n\n"

    "VOZ — ESCRITA HUMANIZADA (foco #1):\n"
    "- Escreva como um analista CONVERSANDO com o leitor, não como IA\n"
    "- Tom analítico, assertivo, levemente provocador — você vê o que outros não veem\n"
    "- PROIBIDO clichês de IA (faz parecer texto automatizado):\n"
    "  * 'Na prática,' — NÃO usar\n"
    "  * 'O que acontece é que,' — NÃO usar\n"
    "  * 'Com isso,' — usar com MUITA parcimônia (max 1 vez no carrossel inteiro)\n"
    "  * 'Vale destacar', 'é importante ressaltar', 'cabe destacar', 'é fundamental' — PROIBIDO\n"
    "  * Frases curtas e de efeito picotadas: 'Queda. Recuperação. Oportunidade.' — PROIBIDO\n"
    "- NUNCA use travessão (—) em hipótese alguma\n"
    "- Sem emoji, sem hashtag\n"
    "- Use ASPAS DUPLAS \" sempre (nunca aspas simples ')\n\n"

    "ARREDONDAMENTO DE NÚMEROS:\n"
    "- Sempre que possível, arredonde números\n"
    "- BOM: 'R$ 14 bilhões', '9%', 'mais de R$ 1 trilhão'\n"
    "- RUIM: 'R$ 14,247 bilhões', '9,3%', 'R$ 1.245.678.901'\n"
    "- Exceção: dado central da análise pode ter 1 casa decimal se for impactante\n"
    "- Formatação brasileira: vírgula decimal, % colado ao número\n\n"

    "USO DE FONTES E DADOS:\n"
    "- Se houver [CONTEÚDO DO LINK ...] no brief, USE APENAS dados que aparecem ali\n"
    "- NUNCA invente números, datas, citações ou estatísticas\n"
    "- Se o brief não tem dado suficiente, faça slide conceitual (sem número fabricado)\n\n"

    "NEGRITOS — FRASES INTEIRAS, não palavras isoladas:\n"
    "- Negrite TRECHOS de 4-12 palavras (frases ou parte da frase com sentido completo)\n"
    "- BOM: '**a maior alta em 10 anos**', '**um cenário estrutural frágil no Brasil**'\n"
    "- RUIM (palavra isolada): '**9%**', '**Brasil**', '**Selic**'\n"
    "- 2 a 3 trechos em negrito por slide é o ideal\n"
    "- Pode incluir números na frase em negrito: '**queda de 9% em 2026**'\n\n"

    "TAMANHO — VARIAR PRA CRIAR RITMO:\n"
    "- Range: 180-420 caracteres por slide\n"
    "- INTERCALE: hook curto (200-280), meio variado (mistura 220-280 e 320-400), final 280-380\n"
    "- Slides com dado específico podem ser mais curtos; slides com argumento mais longos\n"
    "- LIMITE ABSOLUTO: 420 chars (acima cai fora do post 1080x1350)\n\n"

    "PARÁGRAFOS:\n"
    "- MÁXIMO 3 linhas por parágrafo (fluida e respirada)\n"
    "- Cada slide tem 2 ou 3 parágrafos separados por \\n\\n (linha em branco)\n"
    "- NÃO escreva slide como bloco corrido\n\n"

    "CONECTANDO SLIDES — REGRA OBRIGATÓRIA:\n"
    "- Cada slide se conecta com o anterior. NÃO são informações jogadas, é uma HISTÓRIA\n"
    "- Use referência implícita ao slide anterior:\n"
    "  * Slide N fala de queda do dólar. Slide N+1 começa: 'Esse movimento é só uma parte da equação.'\n"
    "  * Slide N cita um dado. Slide N+1: 'Mas existe um problema escondido nesse número.'\n"
    "  * Slide N fala de um setor. Slide N+1: 'O mesmo padrão aparece em outro lugar.'\n"
    "- Evite começar slides com substantivo cru ('O Banco Central anunciou...') sem amarrar\n"
    "  com o que veio antes. Pense em FLUIDEZ NARRATIVA\n\n"

    "QUANTIDADE DE SLIDES — PROFUNDIDADE = AUTORIDADE:\n"
    "- MÍNIMO 10 slides + 1 CTA final = 11 total\n"
    "- Análise com menos é rasa, não gera autoridade\n"
    "- Se o brief for curto, EXPANDA: contexto histórico, implicações de 2ª ordem, comparações\n"
    "- Cada slide com função clara: hook, contexto, dado, comparação, implicação, etc\n\n"

    "ESTRUTURA NARRATIVA:\n"
    "- Slide 1: Hook forte, afirmação provocadora ou dado surpreendente\n"
    "- Slides 2-3: Contexto (situa o leitor no problema)\n"
    "- Slides 4-7: Análise (dados, causa-efeito, comparações)\n"
    "- Slides 8-9: Implicação de 2ª ordem (o que ninguém viu ainda)\n"
    "- Slide final (antes do CTA): Conclusão prática pro investidor brasileiro\n\n"

    "IMAGENS — FUNÇÃO CLARA, NUNCA GENÉRICAS:\n"
    "Toda imagem deve ter UMA das funções:\n"
    "  A) Facilitar entendimento (gráfico do dado citado)\n"
    "  B) Gerar curiosidade ou polêmica (pessoa famosa, lugar simbólico)\n"
    "  C) Relação direta com o que está sendo falado (ex: slide sobre BC -> foto BC Brasília)\n"
    "PROIBIDO: foto stock genérica de 'business', 'office', 'money', 'people working'\n\n"

    "Tipos de image_type:\n"
    "- 'chart': dados numéricos comparáveis. Inclua chart_data com labels, values, unit, highlight\n"
    "  chart_type: 'bar' | 'horizontal_bar' | 'line'\n"
    "- 'photo': contexto visual com FUNÇÃO. Use photo_topic CINEMATOGRÁFICO em inglês:\n"
    "  REGRAS:\n"
    "  * 3-6 palavras descritivas + 1 modificador VISUAL/EMOCIONAL\n"
    "  * Modificadores poderosos: 'dramatic', 'dark', 'intense', 'golden hour',\n"
    "    'close-up', 'cinematic', 'aerial', 'macro', 'silhouette', 'neon'\n"
    "  * EXCELENTE: 'dramatic Brazilian flag wind cinematic', 'dark Federal Reserve building Washington',\n"
    "    'intense stock trader screens panic', 'golden hour Sao Paulo skyline aerial',\n"
    "    'close-up Brazilian real currency notes', 'silhouette politician brasilia night'\n"
    "  * RUIM: 'money', 'business', 'office'\n"
    "  * NUNCA reutilize photo_topic entre slides\n"
    "  - photo_topic_alt: 2-3 palavras alternativas (fallback se a 1ª não retornar)\n\n"

    "IMAGENS DOS LINKS:\n"
    "- Se brief tem [IMAGENS DISPONÍVEIS DOS LINKS], pode usar image_from_link com índice 1-based\n"
    "- USE quando o artigo tem gráfico do dado que você ia mostrar (autêntico, sem erro de número)\n"
    "- NÃO use imagens com texto pequeno demais, em inglês ou ilegíveis\n\n"

    "RETORNE SOMENTE JSON VÁLIDO, sem markdown, sem texto fora do JSON:\n"
    '{"titulo":"...","slides":[{"texto":"...","tema":"bitcoin|economia|mercado|geopolitica|ia|tecnologia",'
    '"image_type":"chart|photo","chart_title":"...","chart_type":"bar|horizontal_bar|line",'
    '"chart_data":[{"label":"...","value":0,"unit":"%","highlight":false}],'
    '"photo_topic":"...","photo_topic_alt":"...",'
    '"image_from_link":null}]}'
)


# ── Sanitizers do texto dos slides ────────────────────────────────────────────
# O Claude as vezes ignora a regra "nao usar travessao" e tambem retorna o
# slide inteiro como bloco corrido sem quebrar paragrafos. A gente forca o
# resultado pra cumprir as regras.

def _strip_em_dash(text: str) -> str:
    """Remove travessoes (—) substituindo por virgula/espaco conforme contexto."""
    if not text or "—" not in text:
        return text
    # Variacoes ordenadas (maior pra menor pra evitar substituicao parcial)
    text = text.replace(" — ", ", ")
    text = text.replace(" —",  ",")
    text = text.replace("— ",  "")
    text = text.replace("—",   "")
    return text

def _ensure_paragraphs(text: str) -> str:
    """Se o slide for longo (>180 chars) e nao tiver \\n\\n, divide em paragrafos
    no fim de frases. Garante legibilidade mesmo se o Claude ignorar a regra."""
    if not text:
        return text
    if "\n\n" in text:
        return text  # ja tem paragrafos
    if len(text) < 180:
        return text  # texto curto nao precisa quebrar
    # Divide em frases (final de frase = . ! ? seguido de espaco)
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    if len(sentences) <= 1:
        return text
    # Agrupa em paragrafos de ~2 frases ou ~120 chars
    paragraphs = []
    current = []
    current_len = 0
    for s in sentences:
        current.append(s)
        current_len += len(s) + 1
        if current_len >= 110 or len(current) >= 2:
            paragraphs.append(" ".join(current))
            current = []
            current_len = 0
    if current:
        if paragraphs and current_len < 60:
            # frase final muito curta: anexa no ultimo paragrafo
            paragraphs[-1] += " " + " ".join(current)
        else:
            paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs) if len(paragraphs) > 1 else text

MAX_SLIDE_CHARS = 420

def _truncar_slide_se_grande(text: str, max_chars: int = MAX_SLIDE_CHARS) -> str:
    """Se passar do limite, trunca de forma inteligente: tenta cortar no
    final do ultimo paragrafo completo. Se nao der, corta no final da
    ultima frase. Ultimo recurso: corta no caractere."""
    if not text or len(text) <= max_chars:
        return text
    # 1. Tenta cortar no fim do ultimo paragrafo (\n\n) que cabe
    paras = text.split("\n\n")
    acumulado = ""
    for p in paras:
        candidato = (acumulado + "\n\n" + p).strip() if acumulado else p
        if len(candidato) > max_chars:
            break
        acumulado = candidato
    if acumulado and len(acumulado) >= max_chars // 2:
        return acumulado.strip()
    # 2. Corta no fim da ultima frase completa (. ! ?) que cabe
    sub = text[:max_chars]
    for sep in (". ", "! ", "? ", ".", "!", "?"):
        idx = sub.rfind(sep)
        if idx > max_chars // 2:
            return sub[:idx+1].strip()
    # 3. Ultimo recurso: corta na ultima palavra completa
    sub = text[:max_chars].rsplit(" ", 1)[0]
    return sub.strip()

def _aspas_simples_pra_duplas(text: str) -> str:
    """Converte aspas simples ' em duplas " quando usadas como pontuacao de
    citacao/destaque. Mantem apostrofo natural ("d'agua", "L'Oreal", etc).
    Aplica heuristica: se o ' eh seguido ou precedido de espaco/inicio/fim,
    eh aspa; se nao, eh apostrofo."""
    if not text or "'" not in text:
        return text
    # Padroes: '...' em volta de palavras (aspa de citacao)
    # Substitui pares de ' em " mantendo apostrofo no meio de palavras
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            # Verifica se eh apostrofo (cercado por letras de ambos os lados)
            prev_is_letter = i > 0 and text[i-1].isalpha()
            next_is_letter = i+1 < len(text) and text[i+1].isalpha()
            if prev_is_letter and next_is_letter:
                # Apostrofo (ex: "d'agua"), mantem
                out.append("'")
            else:
                # Aspa de citacao, converte
                out.append('"')
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _sanitizar_slide(text: str) -> str:
    """Pipeline completo: tira travessao, converte aspas, garante paragrafos,
    trunca se >420. Ordem importa: truncar POR ULTIMO pra que adicao de
    \\n\\n nao estoure o limite depois do corte."""
    text = _strip_em_dash(text or "")
    text = _aspas_simples_pra_duplas(text)
    text = _ensure_paragraphs(text)
    text = _truncar_slide_se_grande(text)
    return text


# ── Fetcher de URLs no brief ──────────────────────────────────────────────────
# Quando o usuario cola um link no brief, a gente baixa o texto da pagina e
# injeta no prompt como [CONTEÚDO DO LINK ...]. Assim o Claude le o artigo
# em vez de inventar dados.
URL_PATTERN = re.compile(r'https?://[^\s<>"\'\)]+', re.IGNORECASE)

# Headers que imitam Chrome desktop real, com Accept-Language PT-BR.
# Necessario pra passar por anti-bot basico de muitos sites.
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

def _wayback_fetch(url: str, max_chars: int = 4000):
    """Ultimo fallback: Wayback Machine. Util pra paginas de noticia
    especificas (URL estavel) que estao indexadas pelo Internet Archive."""
    try:
        import requests as _req
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
    except ImportError:
        return None
    try:
        # Pergunta qual snapshot mais recente existe
        api = _req.get(f"https://archive.org/wayback/available?url={url}",
                       timeout=10, headers={"User-Agent": _BROWSER_HEADERS["User-Agent"]})
        if api.status_code != 200:
            return None
        info = api.json()
        snap = info.get("archived_snapshots", {}).get("closest")
        if not snap or not snap.get("available") or not snap.get("url"):
            return None
        # Baixa o snapshot
        r = _req.get(snap["url"], timeout=15, headers=_BROWSER_HEADERS)
        if r.status_code != 200 or len(r.text) < 500:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        # Wayback adiciona barras de navegacao (#wm-ipp). Remove.
        for sel in ["#wm-ipp-base", "#wm-ipp", "#donato", "[id^=wm-]"]:
            for el in soup.select(sel):
                el.decompose()
        for tag in soup(["script", "style", "nav", "footer", "aside",
                         "form", "noscript", "iframe", "header"]):
            tag.decompose()
        main = soup.find("article") or soup.find("main") or soup.body
        if not main:
            return None
        text = main.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        if len(text) < 200:
            return None
        return {"text": text[:max_chars], "images": []}
    except Exception:
        return None

def _jina_fetch(url: str, max_chars: int = 4000):
    """Fallback robusto via Jina AI Reader (r.jina.ai). Retorna markdown
    limpo do conteudo. Funciona em sites com Cloudflare/anti-bot/JS-only.
    Free tier: ~200 req/min sem API key."""
    try:
        import requests as _req
    except ImportError:
        return None
    try:
        jr = _req.get(
            f"https://r.jina.ai/{url}",
            timeout=20,
            headers={
                "Accept": "text/plain",
                "X-Return-Format": "markdown",
                "User-Agent": _BROWSER_HEADERS["User-Agent"],
            }
        )
        if jr.status_code != 200 or len(jr.text) < 80:
            return None
        md = jr.text
        # Extrai imagens do markdown: ![alt](url)
        imgs = re.findall(r'!\[[^\]]*\]\(([^)]+)\)', md)
        imgs_clean, seen = [], set()
        for u in imgs:
            if u in seen: continue
            low = u.lower()
            if any(skip in low for skip in [
                "logo","avatar","icon","pixel","tracking","blank.","1x1","spacer",
                "/profile/","/ad/","/ads/","advert","banner","/social/","/share/",
                "facebook","twitter","linkedin","instagram","/emoji/","/sprite/","/ui/",
                "_small.","_thumb.","thumbnail-small"
            ]):
                continue
            if low.endswith((".gif",".svg",".ico")):
                continue
            seen.add(u); imgs_clean.append(u)
        # Markdown -> texto puro
        text = re.sub(r'!\[[^\]]*\]\([^)]+\)', '', md)
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
        text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'\*\*|__|`', '', text)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        if len(text) < 80:
            return None
        return {"text": text[:max_chars], "images": imgs_clean[:4]}
    except Exception:
        return None

def _fetch_url_text(url: str, max_chars: int = 4000):
    """Baixa o HTML da URL e extrai (texto principal, imagens candidatas).
    Tenta 3 estrategias em sequencia:
    1) Direto com headers de Chrome real (rapido, sem deps externos)
    2) Jina Reader (r.jina.ai) — bypassa anti-bot/JS-only/paywall fraco
    3) Wayback Machine — pra sites bloqueados pelo Jina (Investing, Reuters)
    Retorna dict com {text, images, source} no sucesso, ou None se nao
    conseguir extrair em nenhuma estrategia."""
    try:
        import requests as _req
        from bs4 import BeautifulSoup
    except ImportError:
        return _jina_fetch(url, max_chars) or _wayback_fetch(url, max_chars)

    html_text = None
    direct_status = 0
    try:
        r = _req.get(url, timeout=12, headers=_BROWSER_HEADERS, allow_redirects=True)
        direct_status = r.status_code
        if r.status_code == 200 and len(r.text) > 500:
            html_text = r.text
    except Exception:
        pass

    if not html_text:
        # Tenta Jina, depois Wayback como ultimo recurso
        result = _jina_fetch(url, max_chars)
        if result:
            result["source"] = "jina"
            return result
        result = _wayback_fetch(url, max_chars)
        if result:
            result["source"] = "wayback"
            return result
        return None

    # Caminho normal: html funcionou
    try:
        from urllib.parse import urljoin
        soup = BeautifulSoup(html_text, "html.parser")

        # ── Extrai imagens candidatas ANTES de remover scripts/header ───────
        # (precisa de meta tags do head)
        candidate_images = []
        # 1. og:image (geralmente a melhor)
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            candidate_images.append(urljoin(url, og["content"]))
        # 2. twitter:image (fallback se og nao existe)
        tw = soup.find("meta", attrs={"name": "twitter:image"})
        if tw and tw.get("content"):
            tw_url = urljoin(url, tw["content"])
            if tw_url not in candidate_images:
                candidate_images.append(tw_url)
        # 3. <img> dentro de article/main — com filtros rigorosos
        article_root = soup.find("article") or soup.find("main") or soup.body
        if article_root:
            for img in article_root.find_all("img", limit=30):
                src = img.get("src") or img.get("data-src") or img.get("data-original")
                if not src:
                    continue
                full = urljoin(url, src)
                low = full.lower()
                # Filtro 1: palavras-chave de ruido na URL
                if any(skip in low for skip in [
                    "logo", "avatar", "icon", "pixel", "tracking", "/profile/",
                    "blank.", "1x1", "spacer", "/ad/", "/ads/", "advert", "banner",
                    "thumbnail-small", "_small.", "_thumb.", "/social/", "/share/",
                    "facebook", "twitter", "linkedin", "instagram",
                    "/emoji/", "/sprite/", "/ui/",
                ]):
                    continue
                # Filtro 2: formato ruim
                if low.endswith((".gif", ".svg", ".ico")):
                    continue
                # Filtro 3: dimensoes pequenas (width/height attrs ou class)
                w = img.get("width") or "0"
                h = img.get("height") or "0"
                try:
                    w_n = int(str(w).replace("px","").split(".")[0])
                    h_n = int(str(h).replace("px","").split(".")[0])
                    if w_n and w_n < 200: continue  # pequena demais
                    if h_n and h_n < 200: continue
                except (ValueError, TypeError):
                    pass
                # Filtro 4: classes/alt suspeitos
                cls = " ".join(img.get("class", [])).lower()
                alt = (img.get("alt") or "").lower()
                if any(s in cls for s in ["avatar", "logo", "icon", "thumb", "thumbnail"]):
                    continue
                if any(s in alt for s in ["avatar", "logo do", "ícone", "compartilhar"]):
                    continue
                if full not in candidate_images:
                    candidate_images.append(full)
        # max 4 imagens por URL (era 6) — menos ruido, mais foco no que importa
        candidate_images = candidate_images[:4]

        # ── Texto principal ─────────────────────────────────────────────────
        for tag in soup(["script", "style", "nav", "footer", "aside",
                         "form", "noscript", "iframe", "header"]):
            tag.decompose()
        main = soup.find("article") or soup.find("main") or soup.body
        if not main:
            j = _jina_fetch(url, max_chars) or _wayback_fetch(url, max_chars)
            if j: j.setdefault("source", "jina")
            return j
        text = main.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        # Se extracao deu pouco texto (paywall, JS-only, layout incomum),
        # tenta Jina + Wayback. Pega o que tiver mais texto.
        if len(text) < 200:
            for fb_fn, src in ((_jina_fetch, "jina"), (_wayback_fetch, "wayback")):
                fb = fb_fn(url, max_chars)
                if fb and len(fb["text"]) > len(text):
                    fb["source"] = src
                    return fb
            if len(text) < 80:
                return None
        return {"text": text[:max_chars], "images": candidate_images, "source": "direct"}
    except Exception:
        j = _jina_fetch(url, max_chars) or _wayback_fetch(url, max_chars)
        if j: j.setdefault("source", "jina")
        return j

def _processar_brief_com_urls(brief: str):
    """Detecta URLs no brief, baixa o conteudo de cada uma e devolve:
    - brief com cada URL substituida por bloco [CONTEÚDO DO LINK ...]
    - lista de info por URL (sucesso/falha + chars + qtd de imagens)
    - lista global de imagens candidatas (com origem) que o Claude pode usar"""
    if not brief:
        return brief, [], []
    urls = URL_PATTERN.findall(brief)
    seen = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]
    info = []
    enriched = brief
    all_images = []  # cada item: {"url_imagem": ..., "origem": <url do artigo>}
    for url in urls:
        result = _fetch_url_text(url)
        if result:
            bloco = f"\n\n[CONTEÚDO DO LINK {url}]\n{result['text']}\n[/CONTEÚDO]\n"
            enriched = enriched.replace(url, bloco, 1)
            info.append({
                "url": url, "ok": True,
                "chars": len(result["text"]),
                "imgs": len(result["images"]),
                "source": result.get("source", "direct"),
            })
            for img_url in result["images"]:
                all_images.append({"url_imagem": img_url, "origem": url})
        else:
            info.append({"url": url, "ok": False, "chars": 0, "imgs": 0,
                         "reason": "blocked"})
    return enriched, info, all_images


# ── Pexels API search ─────────────────────────────────────────────────────────
# Usa o photo_topic gerado pelo Claude pra cada slide pra buscar foto unica
# em vez de rotacionar 3 IDs hardcoded por tema.

def _pexels_search(query: str, used_ids: set, orientation: str = "portrait", n: int = 30):
    """Busca foto no Pexels matching `query`, ESCOLHE ALEATORIAMENTE entre as
    fotos disponiveis pra evitar que multiplos posts sobre o mesmo tema peguem
    sempre a foto top 1. n maior = mais variedade."""
    if not PEXELS_API_KEY:
        return None
    try:
        import requests as _req
        import random as _rand
        # Pagina aleatoria (1-3) tambem ajuda a diversificar entre posts
        page = _rand.randint(1, 3)
        r = _req.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": n, "page": page, "orientation": orientation},
            headers={"Authorization": PEXELS_API_KEY},
            timeout=8
        )
        if r.status_code != 200:
            return None
        data = r.json()
        photos = [p for p in data.get("photos", []) if p.get("id") and p["id"] not in used_ids]
        if not photos:
            return None
        # Pega aleatoriamente entre as 15 primeiras disponiveis (qualidade ~ topo
        # mas com variedade pra evitar todos os posts pegarem a mesma foto)
        candidate = _rand.choice(photos[:15])
        used_ids.add(candidate["id"])
        src = candidate.get("src", {})
        return src.get("portrait") or src.get("large") or src.get("original")
    except Exception:
        return None


@app.route("/gerar")
def pagina_gerar():
    has_key = bool(ANTHROPIC_API_KEY and ANTHROPIC_API_KEY.startswith("sk-ant-api"))
    return render_template("gerar.html", has_key=has_key)


@app.route("/api/pexels/search", methods=["GET"])
def api_pexels_search():
    """Busca fotos no Pexels pra galeria do viewer. Retorna 12 fotos."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "query obrigatoria"}), 400
    if not PEXELS_API_KEY:
        return jsonify({"error": "PEXELS_API_KEY nao configurada"}), 400
    try:
        import requests as _req
        r = _req.get(
            "https://api.pexels.com/v1/search",
            params={"query": q, "per_page": 12, "orientation": "portrait"},
            headers={"Authorization": PEXELS_API_KEY},
            timeout=10
        )
        if r.status_code != 200:
            return jsonify({"error": f"Pexels {r.status_code}"}), r.status_code
        data = r.json()
        fotos = []
        for p in data.get("photos", []):
            src = p.get("src", {})
            url = src.get("portrait") or src.get("large") or src.get("original")
            if url:
                fotos.append({
                    "url": url,
                    "thumb": src.get("medium") or url,
                    "photographer": p.get("photographer", ""),
                })
        return jsonify({"ok": True, "fotos": fotos})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/gerar/system-prompt", methods=["GET"])
def api_gerar_system_prompt():
    """Retorna o system prompt padrao pra UI exibir/editar."""
    return jsonify({"system": SYSTEM_GERAR_DEFAULT})


@app.route("/api/gerar", methods=["POST"])
def api_gerar():
    if not ANTHROPIC_AVAILABLE:
        return jsonify({"error": "Biblioteca anthropic não instalada. Rode: pip install anthropic"}), 400
    if not ANTHROPIC_API_KEY or not ANTHROPIC_API_KEY.startswith("sk-ant-api"):
        return jsonify({"error": "ANTHROPIC_API_KEY não configurada"}), 400

    data       = request.get_json() or {}
    topico     = data.get("topico", "").strip()
    brief      = data.get("brief", "").strip()
    # Min 10 slides (analise rasa = sem autoridade). Max 20 (limite Instagram).
    num_slides = min(max(int(data.get("num_slides", 11)), 10), 20)
    # Override do system prompt vindo da UI (opcional). Se vazio, usa o default.
    system_override = (data.get("system_override") or "").strip()

    if not topico:
        return jsonify({"error": "Tópico obrigatório"}), 400

    SYSTEM = system_override if system_override else SYSTEM_GERAR_DEFAULT

    # Baixa conteudo dos links no brief antes de mandar pro Claude.
    # all_images: lista de {url_imagem, origem} pra Claude usar via image_from_link.
    brief_enriched, urls_info, all_images = _processar_brief_com_urls(brief)

    # Bloco com imagens dos links pro Claude saber que existem
    imagens_block = ""
    if all_images:
        linhas = ["\n[IMAGENS DISPONÍVEIS DOS LINKS]"]
        for i, img in enumerate(all_images, 1):
            linhas.append(f"{i}. {img['url_imagem']}  (do artigo: {img['origem']})")
        linhas.append("[/IMAGENS]\n")
        imagens_block = "\n".join(linhas)

    prompt = (
        f"Crie exatamente {num_slides} slides sobre:\n\n"
        f"TÓPICO: {topico}\n"
        f"CONTEÚDO/BRIEF: {brief_enriched or topico}\n"
        f"{imagens_block}\n"
        f"Slide 1: Hook. Slides 2-{num_slides-1}: desenvolvimento com dados. "
        f"Slide {num_slides}: implicação para o investidor.\n"
        "Retorne SOMENTE JSON válido."
    )

    try:
        client = _anthropic_lib.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp   = claude_call_with_retry(client,
            model="claude-sonnet-4-5", max_tokens=6000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        texto = resp.content[0].text.strip()
        if texto.startswith("```"):
            texto = re.sub(r"^```[a-z]*\n?", "", texto)
            texto = re.sub(r"\n?```$", "", texto).strip()

        dados = _parse_claude_json(texto)  # robusto a JSON malformado
        if dados is None:
            return jsonify({
                "error": "Resposta do Claude veio malformada. Tente novamente.",
                "raw": texto[:500]
            }), 500
        slides_raw   = dados.get("slides", [])
        titulo_gerado = dados.get("titulo", topico[:40])

        # Build image URLs
        PEXELS_FALLBACK = {
            "bitcoin": ["5980567","6770610","844124"],
            "economia": ["4386469","210607","5831251"],
            "mercado": ["6770610","844127","210607"],
            "geopolitica": ["259027","5831251","636190"],
            "ia": ["8386434","3861969","2599244"],
            "tecnologia": ["1181671","2599244","3861969"],
        }
        PX = "https://images.pexels.com/photos/"
        Q  = "?auto=compress&cs=tinysrgb&w=1080"

        slides_out = []
        # Estado de dedup pra evitar repeticao de imagens
        used_pexels_ids   = set()
        used_link_indices = set()
        for i, s in enumerate(slides_raw):
            itype = s.get("image_type", "photo").lower()
            img   = ""
            # ── PRIORIDADE 1: imagem de um link no brief (image_from_link) ──
            link_idx_raw = s.get("image_from_link")
            try:
                link_idx = int(link_idx_raw) if link_idx_raw not in (None, "", 0) else None
            except (ValueError, TypeError):
                link_idx = None
            if link_idx and 1 <= link_idx <= len(all_images) and link_idx not in used_link_indices:
                img = all_images[link_idx - 1]["url_imagem"]
                used_link_indices.add(link_idx)
            if not img and itype == "chart":
                ctype  = s.get("chart_type", "bar").lower()
                ctitle = s.get("chart_title", "")
                cdata  = s.get("chart_data") or []
                if cdata:
                    labels = [str(d["label"]) for d in cdata]
                    values = [float(d["value"]) for d in cdata]
                    unit   = cdata[0].get("unit", "") if cdata else ""
                    colors = ["rgba(239,68,68,0.85)" if d.get("highlight") else "rgba(29,155,240,0.85)" for d in cdata]
                    bcols  = ["rgba(239,68,68,1)"    if d.get("highlight") else "rgba(29,155,240,1)"    for d in cdata]
                    fmt_cb = f"function(v){{return v+'{unit}';}}"
                    if ctype == "horizontal_bar":
                        cjs = "horizontalBar"
                        scales = {"xAxes": [{"ticks": {"beginAtZero": True, "callback": fmt_cb}}]}
                    elif ctype == "line":
                        cjs    = "line"
                        scales = {"yAxes": [{"ticks": {"callback": fmt_cb}}]}
                    else:
                        cjs    = "bar"
                        scales = {"yAxes": [{"ticks": {"beginAtZero": False, "callback": fmt_cb}}]}
                    cfg = {
                        "type": cjs,
                        "data": {"labels": labels, "datasets": [{
                            "data": values, "backgroundColor": colors,
                            "borderColor": bcols, "borderWidth": 2, "fill": False,
                        }]},
                        "options": {
                            "title": {"display": True, "text": ctitle, "fontSize": 16, "fontStyle": "bold"},
                            "legend": {"display": False},
                            "scales": scales,
                            "plugins": {"datalabels": {
                                "anchor": "end", "align": "top",
                                "font": {"weight": "bold", "size": 12},
                                "color": "#111827", "formatter": fmt_cb,
                            }},
                        },
                    }
                    img = (
                        "https://quickchart.io/chart?c="
                        + urllib.parse.quote(json.dumps(cfg, separators=(",", ":")))
                        + "&width=1080&height=520&backgroundColor=white&version=2"
                    )
            # ── PRIORIDADE 2: Pexels API por photo_topic (foto unica por slide) ──
            # Tenta principal, depois alt, depois variacao com 1 palavra-chave do tema
            if not img:
                photo_topic = (s.get("photo_topic") or "").strip()
                photo_topic_alt = (s.get("photo_topic_alt") or "").strip()
                for query in [photo_topic, photo_topic_alt]:
                    if not query: continue
                    pexels_url = _pexels_search(query, used_pexels_ids)
                    if pexels_url:
                        img = pexels_url
                        break
            # ── PRIORIDADE 3: Pexels API por tema (fallback se topic vazio/falhou) ──
            if not img and PEXELS_API_KEY:
                tema_query = {
                    "bitcoin": "bitcoin cryptocurrency",
                    "economia": "economy finance market",
                    "mercado": "stock market trading",
                    "geopolitica": "geopolitics world map",
                    "ia": "artificial intelligence technology",
                    "tecnologia": "technology innovation",
                }.get(s.get("tema", "").lower(), "business finance")
                pexels_url = _pexels_search(tema_query, used_pexels_ids)
                if pexels_url:
                    img = pexels_url
            # ── PRIORIDADE 4: hardcoded por tema (com dedup) ──
            if not img:
                tema = s.get("tema", "default").lower()
                ids  = PEXELS_FALLBACK.get(tema, ["4386469", "6770610", "5831251"])
                # Tenta achar um id ainda nao usado nessa geracao
                disponiveis = [fid for fid in ids if fid not in used_pexels_ids]
                fid = (disponiveis[0] if disponiveis else ids[i % len(ids)])
                used_pexels_ids.add(fid)
                img  = f"{PX}{fid}/pexels-photo-{fid}.jpeg{Q}"
            # Sanitiza texto: remove travessoes e garante quebras de paragrafo
            texto_sanit = _sanitizar_slide(s.get("texto", ""))
            slides_out.append({"texto": texto_sanit, "image_url": img})

        # Build slug
        slug_base = re.sub(r"[^a-z0-9]+" , "-", titulo_gerado.lower())[:40].strip("-")
        from datetime import date as _date2
        hoje  = _date2.today().strftime("%Y%m%d")
        slug  = f"{slug_base}-{hoje}"
        nome  = f"carrossel-{slug}.html"

        # Inject into template HTML
        template_path = CARROSSEIS_DIR / "carrossel-carga-tributaria.html"
        if template_path.exists():
            html = template_path.read_text(encoding="utf-8")
            html = re.sub(r"<title>.*?</title>",
                          f"<title>Carrossel — {titulo_gerado} | Gabriel Bearlz</title>", html)
            html = re.sub(r"(<h1>).*?(</h1>)", rf"\g<1>{titulo_gerado}\g<2>", html)
            html = re.sub(r'(<p id="subtitle">).*?(</p>)',
                          rf"\g<1>{len(slides_out)+1} slides · Gabriel Bearlz\g<2>", html)
            # Slug JS para sync com servidor (identifica o carrossel no /api/carrossel/<slug>/...)
            html = re.sub(r"window\.CAROUSEL_SLUG='[^']*'",
                          f"window.CAROUSEL_SLUG='{slug}'", html)
            # Chave do localStorage (convertendo hifens para underscores)
            ls_key_slug = slug.replace("-", "_")
            html = re.sub(r"window\.CAROUSEL_LS_KEY='[^']*'",
                          f"window.CAROUSEL_LS_KEY='bearlz_{ls_key_slug}_v1'", html)
            # Compatibilidade com templates antigos que ainda tenham LS_KEY sem prefixo window.
            html = re.sub(r"(?<!window\.CAROUSEL_)LS_KEY='[^']*'",
                          f"LS_KEY='bearlz_{ls_key_slug}_v1'", html)

            # Injeta as imagens extraidas dos links do brief pra usuario poder
            # acessar via galeria no viewer, mesmo que Claude nao tenha usado.
            imgs_json = json.dumps(all_images, ensure_ascii=False)
            inject_imgs = f"window.EXTRACTED_IMAGES={imgs_json};"
            if "window.EXTRACTED_IMAGES=" in html:
                html = re.sub(r"window\.EXTRACTED_IMAGES=[^;]*;", inject_imgs, html)
            else:
                # Adiciona logo apos CAROUSEL_LS_KEY pra ficar no mesmo bloco
                html = html.replace(
                    f"window.CAROUSEL_LS_KEY='bearlz_{ls_key_slug}_v1'",
                    f"window.CAROUSEL_LS_KEY='bearlz_{ls_key_slug}_v1';\n{inject_imgs}",
                    1
                )

            # Garante que a foto do Gabriel está embutida (template refatorado ja tem base64,
            # mas se um template antigo ainda tiver avatarDataUrl=null, embutimos aqui)
            avatar_path = BASE_DIR / "static" / "gabriel.png"
            if avatar_path.exists() and 'avatarDataUrl=null' in html:
                import base64 as _b64
                b64 = _b64.b64encode(avatar_path.read_bytes()).decode()
                html = html.replace(
                    'let avatarDataUrl=null;',
                    f'let avatarDataUrl="data:image/png;base64,{b64}";'
                )

            def _esc(t):
                return t.replace("\\","\\\\").replace("`","\\`").replace("${","\\${").replace("\n","\\n")

            # Sem raw string: precisamos de newlines reais para o _esc() converter
            # em '\n' JS (que eh quebra de linha no template literal).
            CTA_TEXT = (
                "Dólar em baixa e Ibovespa em alta podem esconder um **cenário estrutural frágil** no Brasil.\n\n"
                "Existem boas oportunidades de investimento no país, mas estar com o patrimônio 100% exposto a esses riscos é uma decisão arriscada.\n\n"
                "Posso te ajudar a montar uma **Estratégia de Investimento Global**.\n\n"
                'Comenta **"Estratégia"** aqui embaixo.'
            )
            linhas = [
                f"  {{id:{i+1},text:`{_esc(s['texto'])}`,image:'{s['image_url']}',zoom:1,ox:50,oy:50}}"
                for i, s in enumerate(slides_out)
            ]
            n_cta = len(slides_out) + 1
            linhas.append(
                f"  {{id:{n_cta},text:`{_esc(CTA_TEXT)}`,"
                f"image:'https://images.pexels.com/photos/636190/pexels-photo-636190.jpeg?auto=compress&cs=tinysrgb&w=1080',"
                f"zoom:1,ox:50,oy:50}}"
            )
            novo_array = "const slides=[\n" + ",\n".join(linhas) + "\n];"
            html = re.sub(r"const slides=\[.*?\];", lambda _: novo_array, html, flags=re.DOTALL)
            # Grava em GENERATED_DIR. Como o disco do Render free tier eh
            # resetado a cada deploy, tambem commitamos no GitHub em branch
            # separada pra sobreviver.
            (GENERATED_DIR / nome).write_text(html, encoding="utf-8")
            _gh_save_async(
                f"data/generated/{nome}",
                html.encode("utf-8"),
                f"Carrossel gerado: {titulo_gerado}"
            )

        # Register in DB
        with get_db() as conn:
            conn.execute("""
                INSERT INTO carrosseis (slug, titulo, arquivo, num_slides, status)
                VALUES (?, ?, ?, ?, 'rascunho')
                ON CONFLICT(slug) DO UPDATE SET
                    titulo=excluded.titulo, arquivo=excluded.arquivo,
                    num_slides=excluded.num_slides, updated_at=datetime('now')
            """, (slug, titulo_gerado, nome, len(slides_out)+1))

        return jsonify({
            "ok": True, "slug": slug, "titulo": titulo_gerado, "url": f"/c/{slug}",
            "debug": {
                "system_used":      SYSTEM,
                "user_prompt":      prompt,
                "urls_fetched":     urls_info,
                "system_is_custom": bool(system_override),
                "model":            "claude-sonnet-4-5",
                "images_from_links": all_images,           # candidatas extraidas
                "images_used_from_links": sorted(used_link_indices),  # indices que viraram slide
                "pexels_api_active": bool(PEXELS_API_KEY),
            }
        })

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Resposta inválida do Claude: {e}",
                        "debug": {"system_used": SYSTEM, "user_prompt": prompt,
                                  "urls_fetched": urls_info}}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Gerador de 3 hooks (slide 1) por abordagem: Curiosidade / Dor / Promessa ───

def _extrair_contexto_carrossel(slug: str):
    """Le o HTML do carrossel e extrai titulo + texto do slide 1 atual + resumo
    dos proximos slides para dar contexto ao Claude na geracao dos hooks."""
    with get_db() as conn:
        row = conn.execute("SELECT arquivo, titulo FROM carrosseis WHERE slug=?", (slug,)).fetchone()
    if not row or not row["arquivo"]:
        return None, None, None
    html_path = _find_carrossel_file(row["arquivo"])
    if not html_path:
        return row["titulo"], None, None

    html = html_path.read_text(encoding="utf-8")
    arr_start = html.find("const slides=[")
    if arr_start == -1:
        return row["titulo"], None, None
    # Captura o array de slides (ingenuo: acha o primeiro "];" depois do inicio)
    arr_end = html.find("\n];", arr_start)
    if arr_end == -1:
        return row["titulo"], None, None
    bloco = html[arr_start:arr_end]

    # Extrai os textos dos slides (dentro de backticks `...`). Regex simples.
    textos = re.findall(r"text:`([^`]*)`", bloco, flags=re.DOTALL)
    slide1 = textos[0].strip() if textos else ""
    # Resumo dos proximos (primeiros 200 chars de cada)
    resto = [t.strip().replace("\n", " ")[:200] for t in textos[1:5]]
    return row["titulo"], slide1, resto


@app.route("/api/hooks/<slug>", methods=["POST"])
def api_hooks(slug):
    """Gera 3 variantes de slide 1 (hook) com abordagens Curiosidade, Dor e Promessa."""
    if not ANTHROPIC_AVAILABLE:
        return jsonify({"error": "Biblioteca anthropic não instalada"}), 400
    if not ANTHROPIC_API_KEY or not ANTHROPIC_API_KEY.startswith("sk-ant-api"):
        return jsonify({"error": "ANTHROPIC_API_KEY não configurada"}), 400

    titulo, slide1_atual, proximos = _extrair_contexto_carrossel(slug)
    if titulo is None:
        return jsonify({"error": "Carrossel não encontrado"}), 404

    # Permite override via body (caso usuario queira passar contexto manual)
    data = request.get_json(silent=True) or {}
    contexto_extra = data.get("contexto", "").strip()

    SYSTEM = (
        "Você é redator sênior de conteúdo financeiro para @gabriel.bearlz no Instagram.\n"
        "Estilo: thread do Twitter/X analítico. Público: investidores brasileiros 25-45 anos.\n\n"

        "TAREFA: gerar 3 VARIANTES do slide 1 (hook) usando 3 abordagens distintas de copywriting:\n\n"

        "1. CURIOSIDADE — desperta interesse com pergunta provocadora, revelação intrigante,\n"
        "   dado surpreendente ou paralelo histórico que faz o leitor parar pra entender.\n"
        "   Ex: 'Em 1995 quase ninguém tinha site. Em 2007 achavam iPhone caro. Agora existe\n"
        "   um sinal parecido, e quase ninguém está prestando atenção.'\n\n"

        "2. DOR — toca num medo concreto, prejuízo real ou frustração que o público já sente.\n"
        "   Use contraste forte (quem faz X vs quem não faz). Ex: 'Seu concorrente acabou de\n"
        "   contratar 30 funcionários que nunca dormem — e você ainda nem começou.'\n\n"

        "3. PROMESSA — vende transformação, ganho tangível ou janela de oportunidade clara,\n"
        "   com número/dado quando possível. Ex: 'Mercado que cresceu 822% em 6 anos, e a\n"
        "   janela de entrada ainda está aberta.'\n\n"

        "REGRAS INEGOCIÁVEIS — ESCRITA HUMANIZADA:\n"
        "- TAMANHO: 220-380 chars por hook. MAX 420.\n"
        "- NEGRITOS em FRASES inteiras (4-12 palavras com sentido completo)\n"
        "  BOM: **a maior alta em 10 anos**\n"
        "  RUIM (palavra isolada): **9%**, **Brasil**\n"
        "- ASPAS DUPLAS \" sempre (não ')\n"
        "- ARREDONDAR números: 'R$ 14 bilhões' não 'R$ 14,247 bilhões'\n"
        "- NUNCA use travessão (—)\n"
        "- PROIBIDOS clichês de IA:\n"
        "  * 'Na prática,' — não usar\n"
        "  * 'O que acontece é que,' — não usar\n"
        "  * 'Com isso,' — só 1 vez no max\n"
        "  * 'Vale destacar', 'é importante ressaltar' — proibido\n"
        "  * Frases picotadas: 'Queda. Alta. Oportunidade.' — proibido\n"
        "- Sem emoji, sem hashtag\n"
        "- Números em formato brasileiro (vírgula decimal, % colado)\n\n"

        "RETORNE SOMENTE JSON VÁLIDO (sem markdown, sem texto fora):\n"
        '{"variantes":[{"tipo":"Curiosidade","texto":"..."},'
        '{"tipo":"Dor","texto":"..."},{"tipo":"Promessa","texto":"..."}]}'
    )

    prompt_partes = [f"TÍTULO DO CARROSSEL: {titulo}"]
    if slide1_atual:
        prompt_partes.append(f"\nSLIDE 1 ATUAL (a ser SUBSTITUÍDO por algo mais forte):\n{slide1_atual}")
    if proximos:
        prompt_partes.append("\nPRÓXIMOS SLIDES (use para manter coerência temática):")
        for i, t in enumerate(proximos, start=2):
            prompt_partes.append(f"  Slide {i}: {t}")
    if contexto_extra:
        prompt_partes.append(f"\nCONTEXTO ADICIONAL DO USUÁRIO: {contexto_extra}")
    prompt_partes.append("\nGere 3 variantes do slide 1 com abordagens: Curiosidade, Dor, Promessa.")
    prompt = "\n".join(prompt_partes)

    try:
        client = _anthropic_lib.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = claude_call_with_retry(client,
            model="claude-sonnet-4-5", max_tokens=2500,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        texto = resp.content[0].text.strip()
        if texto.startswith("```"):
            texto = re.sub(r"^```[a-z]*\n?", "", texto)
            texto = re.sub(r"\n?```$", "", texto).strip()
        dados = _parse_claude_json(texto)  # robusto a JSON malformado
        if dados is None:
            return jsonify({
                "error": "Resposta do Claude veio malformada. Tente novamente.",
                "raw": texto[:500]
            }), 500
        variantes = dados.get("variantes", [])
        if not isinstance(variantes, list) or len(variantes) != 3:
            return jsonify({"error": "Claude não retornou 3 variantes"}), 500
        return jsonify({"ok": True, "variantes": variantes})
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Resposta inválida do Claude: {e}"}), 500
    except Exception as e:
        msg = str(e)
        if "overloaded" in msg.lower() or "529" in msg:
            return jsonify({"error": "A API do Claude está sobrecarregada agora (529). Tente em 1-2 min."}), 503
        if "rate_limit" in msg.lower() or "429" in msg:
            return jsonify({"error": "Limite de requests atingido. Aguarde um minuto."}), 429
        return jsonify({"error": str(e)}), 500


# ── Static (css/js se precisar de arquivos extras) ─────────────────────────────
@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(BASE_DIR / "static", filename)


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

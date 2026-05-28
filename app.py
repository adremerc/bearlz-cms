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
            ("artigo",          "TEXT"),  # texto corrido completo (fase 1)
            ("legenda",         "TEXT"),  # resumo executivo pro Instagram
            ("hashtags",        "TEXT"),  # "#a #b #c" pro Instagram
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

def _parse_polir_response(texto: str):
    """Parser tolerante pra resposta do /api/polir-slide. O Claude as vezes
    devolve JSON com aspas duplas INTERNAS nao escapadas no texto_novo
    (ex: o texto polido contem 'frase entre aspas') — isso quebra json.loads.

    Estrategia em camadas:
    1. Tenta JSON limpo via _parse_claude_json (caso bom)
    2. Regex que ancora em '"texto_novo": "' e procura a aspa de fim ANTES
       da chave 'mudancas_principais' — tolera aspas duplas no meio
    3. Fallback: pega tudo apos '"texto_novo": "', limpa caudas conhecidas
       do JSON (}, "mudancas...) e usa como texto polido sem mudancas
    Retorna dict {'texto_novo':..., 'mudancas_principais':[...]} ou None."""
    if not texto:
        return None
    # 1. JSON direto
    d = _parse_claude_json(texto)
    if d and isinstance(d, dict) and 'texto_novo' in d:
        # Garante mudancas como lista
        mp = d.get('mudancas_principais', [])
        if not isinstance(mp, list):
            mp = []
        d['mudancas_principais'] = mp
        return d
    # 2. Regex ancorada: texto_novo ... mudancas_principais
    m = re.search(
        r'"texto_novo"\s*:\s*"(.*?)"\s*,\s*"mudancas_principais"\s*:\s*\[(.*?)\]',
        texto, re.DOTALL
    )
    if m:
        texto_novo = m.group(1)
        # Desescape minimo
        texto_novo = (texto_novo
                      .replace('\\"', '"')
                      .replace('\\n', '\n')
                      .replace('\\t', '\t')
                      .replace('\\\\', '\\'))
        # Mudancas: extrai strings dentro do array
        mudancas = []
        for sm in re.finditer(r'"((?:[^"\\]|\\.)*)"', m.group(2)):
            v = (sm.group(1)
                 .replace('\\"', '"')
                 .replace('\\n', '\n')
                 .replace('\\\\', '\\'))
            mudancas.append(v)
        return {'texto_novo': texto_novo, 'mudancas_principais': mudancas}
    # 3. Fallback: pega depois de "texto_novo": " e limpa cauda
    m = re.search(r'"texto_novo"\s*:\s*"(.*)', texto, re.DOTALL)
    if m:
        texto_novo = m.group(1)
        # Remove cauda: ", "mudancas_principais"... ou "}\s*$
        texto_novo = re.sub(
            r'"\s*,\s*"mudancas_principais".*$', '', texto_novo, flags=re.DOTALL
        )
        texto_novo = re.sub(r'"\s*\}\s*$', '', texto_novo).rstrip('"\n ')
        texto_novo = (texto_novo
                      .replace('\\"', '"')
                      .replace('\\n', '\n')
                      .replace('\\t', '\t')
                      .replace('\\\\', '\\'))
        if texto_novo and len(texto_novo) > 30:
            return {'texto_novo': texto_novo, 'mudancas_principais': []}
    return None


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

    "🔴 CONTEXTO TEMPORAL — REGRA INVIOLÁVEL #0:\n"
    "- Hoje é MAIO DE 2026. Todo conteúdo é publicado em 2026.\n"
    "- Use SEMPRE tempos verbais e referencias compativeis com 2026:\n"
    "  * 'neste ano' / 'em 2026' / 'atualmente' = AGORA, dado de 2026\n"
    "  * 'ano passado' = 2025\n"
    "  * Citar 2024 ou anterior SEM marcar como historico = ERRO GRAVE\n"
    "- Se o brief tem fonte marcada com [FONTE ANTIGA] ou data <2026:\n"
    "  * USE o dado, MAS contextualize com a data explicita: 'em 2024, ...'\n"
    "  * NAO apresente como 'recente' ou 'atual'\n"
    "- Se o brief NAO tem dado recente de 2026 sobre o topico central:\n"
    "  * PREFIRA falar conceitualmente (causa/efeito, mecanica do problema)\n"
    "  * NUNCA fabrique numeros 'parecidos com 2026' a partir de dados antigos\n"
    "  * Mencione '2025 foi o ultimo dado disponivel' se for relevante\n"
    "- Numeros sem data explicita devem ser DE 2026 ou claramente atemporais\n"
    "  (ex: 'PIB do Brasil eh ~R$ 11 trilhoes' — atemporal-ish, OK; "
    "'Selic esta em 11%' — implica 2026, so use se o brief confirmar)\n"
    "- PROIBIDO: 'recentemente o BC anunciou X' sem o brief ter dado de 2026\n\n"

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
    "- 'photo': contexto visual com FUNÇÃO. Use photo_topic ESPECÍFICO:\n"
    "  PRIORIDADE 1 — NOMES PRÓPRIOS (busca Wikimedia, fotos REAIS):\n"
    "  * Quando slide cita pessoa, empresa, prédio ou lugar específico,\n"
    "    use o NOME PRÓPRIO em inglês/português no photo_topic\n"
    "  * EXEMPLOS BONS: 'Lula Brasília 2024', 'Petrobras headquarters Rio',\n"
    "    'Federal Reserve building Washington', 'Powell speech',\n"
    "    'Banco Central Brasil', 'Faria Lima São Paulo', 'JP Morgan logo',\n"
    "    'Roberto Campos Neto', 'Bovespa B3 trading floor'\n"
    "  * Esses retornam fotos REAIS da Wikimedia, não stock genérico\n\n"
    "  PRIORIDADE 2 — CINEMATOGRÁFICO (busca Pexels, stock visual):\n"
    "  * Quando o slide é contexto abstrato (mercado caindo, otimismo, crise),\n"
    "    use 3-6 palavras descritivas + modificador visual em inglês\n"
    "  * Modificadores: 'dramatic', 'dark', 'intense', 'golden hour',\n"
    "    'close-up', 'cinematic', 'aerial', 'silhouette'\n"
    "  * EXEMPLOS: 'dramatic Brazilian flag wind cinematic',\n"
    "    'intense stock trader screens panic',\n"
    "    'golden hour Sao Paulo skyline aerial'\n\n"
    "  REGRAS GERAIS:\n"
    "  * RUIM (sempre): 'money', 'business', 'office'\n"
    "  * NUNCA reutilize photo_topic entre slides\n"
    "  - photo_topic_alt: 2-3 palavras alternativas (fallback)\n\n"

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


# ══════════════════════════════════════════════════════════════════════════════
# GERACAO EM 2 FASES (estilo Leandro Varos)
# Insight: criadores de elite NAO escrevem slide por slide. Escrevem UM texto
# corrido, fluido, com inicio-meio-fim, e DEPOIS fatiam em pedacos. O resultado
# eh um carrossel que, lido em sequencia, parece um artigo unico sem quebras
# estranhas — cada slide comeca exatamente onde o anterior parou.
#
# FASE 1 (SYSTEM_ARTIGO): Claude escreve o ENSAIO. Nao pensa em slides.
# FASE 2 (SYSTEM_FATIAR): Claude corta o ensaio em N slides + escolhe imagens.
# ══════════════════════════════════════════════════════════════════════════════

_CONTEXTO_TEMPORAL_2026 = (
    "🔴 CONTEXTO TEMPORAL — REGRA INVIOLÁVEL #0:\n"
    "- Hoje é MAIO DE 2026. Todo conteúdo é publicado em 2026.\n"
    "- 'neste ano'/'atualmente'/'em 2026' = AGORA. 'ano passado' = 2025.\n"
    "- Citar 2024 ou anterior SEM marcar como histórico = ERRO GRAVE.\n"
    "- Brief com [FONTE ANTIGA] ou data <2026: USE o dado MAS com data\n"
    "  explícita ('em 2024, ...'). NUNCA apresente como 'recente'/'atual'.\n"
    "- Sem dado de 2026 sobre o tópico: PREFIRA falar conceitualmente\n"
    "  (causa/efeito, mecânica). NUNCA fabrique número 'parecido com 2026'.\n\n"
)

SYSTEM_ARTIGO = (
    "Você é redator sênior de análise de negócios e finanças para @gabriel.bearlz.\n"
    "TAREFA AGORA: escrever UM ARTIGO CORRIDO, fluido, do começo ao fim.\n"
    "NÃO pense em slides. NÃO numere nada. NÃO corte em blocos. Escreva um\n"
    "ensaio único e contínuo, como uma reportagem de negócios.\n\n"

    + _CONTEXTO_TEMPORAL_2026 +

    "REFERÊNCIA DE ESTILO (analista culto conversando, NÃO repórter factual):\n"
    "Um texto que começa com um fato surpreendente, constrói o contexto\n"
    "histórico, explica o mecanismo por dentro, apresenta números concretos,\n"
    "e termina com uma lição maior. Você RECONSTRÓI o raciocínio (por que tal\n"
    "empresa/pessoa decidiu X), não apenas relata o evento. Coloca o leitor\n"
    "DENTRO da tomada de decisão. Isso cria intimidade intelectual.\n\n"

    "ARQUITETURA NARRATIVA (siga a ordem):\n"
    "1. ABERTURA COM IMPACTO — A PARTE MAIS IMPORTANTE DO TEXTO TODO.\n"
    "   A primeira frase decide se a pessoa para o dedo ou rola pra próxima.\n"
    "   Se o início não fisga em 2 segundos, ninguém lê o resto.\n"
    "   - O MELHOR gancho tem PARADOXO ou DETALHE HUMANO CONCRETO E INESPERADO.\n"
    "   - Técnica: comece pelo detalhe mais ESTRANHO/CONTRAINTUITIVO da\n"
    "     história, NÃO pelo resumo. Crie um 'espera, como assim?' na cabeça.\n"
    "   - FRACO (não faça): dado seco ('Os juros subiram mais que em 2008.')\n"
    "     ou pergunta genérica ('Bitcoin vai subir?', 'Você sabia que...?').\n"
    "   - FORTE (faça assim):\n"
    "     * 'Uma empresa de US$ 1 trilhão começou no porão de um consultório\n"
    "       odontológico, com dinheiro de um fazendeiro de batatas.'\n"
    "     * 'O remédio que move bilhões hoje foi engavetado por uma\n"
    "       farmacêutica que achou que ninguém aceitaria outra injeção.'\n"
    "     * 'O Brasil criou o sistema de pagamento mais avançado do mundo e,\n"
    "       no processo, destruiu a maior fonte de lucro dos próprios bancos.'\n"
    "   - A abertura deve prometer uma HISTÓRIA, não anunciar um tópico.\n\n"
    "   DEPOIS DO GANCHO, percorra estes blocos NA ORDEM (nem todo tema tem\n"
    "   todos, mas use os que fizerem sentido):\n"
    "   GANCHO → ORIGEM → PRODUTO/MECANISMO → ESCASSEZ/TENSÃO → NÚMEROS →\n"
    "   MARCO → CONTEXTO (geopolítico/setorial) → ANÁLISE → FECHAMENTO\n"
    "   - ORIGEM: de onde isso veio, a história por trás (humaniza antes de\n"
    "     explicar o técnico). Quase-fracassos e reviravoltas criam interesse.\n"
    "   - PRODUTO/MECANISMO: explique COMO a coisa funciona, do simples ao\n"
    "     específico. É aqui que o leitor aprende o conceito.\n"
    "   - ESCASSEZ/TENSÃO: o conflito, o gargalo, o que está em jogo agora.\n"
    "   - NÚMEROS: os dados financeiros brutais, sem adjetivo. Deixe os\n"
    "     números falarem (não diga 'impressionante', mostre o +756%).\n"
    "   - MARCO: o momento-clímax (a empresa cruzou X, o recorde, a virada).\n"
    "   - CONTEXTO: amplie o horizonte (disputa global, posição no setor).\n"
    "     Transforma 'notícia' em 'análise estratégica'.\n"
    "   - ANÁLISE: a leitura de investimento. O que isso significa, o que olhar.\n"
    "   - FECHAMENTO: uma SENTENÇA FILOSÓFICA — máxima generalizável que\n"
    "     transcende o caso e vira insight universal. A frase que a pessoa\n"
    "     copia e manda no WhatsApp. Ex: 'Nem toda revolução parece óbvia\n"
    "     quando ela nasce.' Idealmente fecha o loop com o gancho inicial.\n\n"

    "PRESSUPOSTO FUNDAMENTAL — O LEITOR NÃO SABE DE NADA:\n"
    "- Escreva partindo do zero. Não assuma que a pessoa conhece a empresa,\n"
    "  o termo técnico, o contexto histórico ou o porquê de aquilo importar.\n"
    "- Toda sigla/termo técnico é explicado na primeira vez, dentro da frase\n"
    "  ('a HBM, memória de altíssima largura de banda que alimenta as GPUs').\n"
    "- Construa o raciocínio passo a passo: contexto antes do dado, causa\n"
    "  antes da consequência. Quem nunca ouviu falar do assunto entende tudo.\n"
    "- Mas SEM ser condescendente: explique com a naturalidade de quem sabe\n"
    "  muito e conversa de igual pra igual, não de quem dá aula.\n\n"

    "PROFUNDIDADE = AUTORIDADE:\n"
    "- Cada afirmação ancorada em dado concreto, exemplo ou mecanismo.\n"
    "- Vá à segunda ordem: não só 'o que aconteceu', mas 'por que' e 'o que\n"
    "  isso desencadeia'. É o que separa análise de notícia.\n"
    "- Reconstrua decisões (por que a empresa/pessoa escolheu X), mostrando\n"
    "  o trade-off que ela enfrentava.\n\n"

    "RITMO (marca registrada — siga à risca):\n"
    "- ALTERNE frases curtas (1 linha, funcionam como respiração) com médias.\n"
    "- Curtas de impacto: 'O projeto foi encerrado em 1991.' 'É matemática.'\n"
    "- A alternância cria leitura dinâmica, como gente inteligente fala.\n\n"

    "PERGUNTAS RETÓRICAS: use, curtas, com resposta IMEDIATA na frase seguinte.\n"
    "  Ex: 'O motivo? A empresa achou que ninguém aceitaria outra injeção.'\n\n"

    "CONTRASTE: use 'de um lado X, do outro Y' ou 'enquanto X, Y' pra mostrar\n"
    "  que você entende os dois lados (dá credibilidade analítica).\n\n"

    "DADOS — SEMPRE específicos, nunca vagos:\n"
    "- BOM: 'subiu mais de 8x em 12 meses', 'R$ 3,8 bilhões', 'alta de 21%'\n"
    "- RUIM: 'subiu muito', 'bilhões de reais', 'cresceu bastante'\n"
    "- Nunca um parágrafo longo sem um dado concreto ancorando.\n"
    "- Arredonde: 'R$ 14 bilhões' não 'R$ 14,247 bilhões'.\n\n"

    "VOCABULÁRIO: técnico mas SEMPRE ancorado em linguagem simples ao redor.\n"
    "  Pode usar termo técnico (HBM, P/L, FGC, Selic) explicando implícito no\n"
    "  contexto. SEM gíria, SEM palavra de influencer ('top', 'incrível').\n\n"

    "════ PROIBIDO — VÍCIOS QUE DENUNCIAM TEXTO DE IA (regra crítica) ════\n"
    "PRINCÍPIO MESTRE: textos humanos AFIRMAM. Textos de IA ANUNCIAM. Corte\n"
    "todos os anúncios e mantenha as afirmações diretas.\n\n"

    "1) CONECTORES BUROCRÁTICOS — nunca use:\n"
    "   'Na prática,', 'O que acontece é que,', 'Vale destacar (que)',\n"
    "   'É importante ressaltar', 'Cabe destacar', 'É fundamental',\n"
    "   'Dessa forma,', 'Nesse sentido,', 'Em suma,', 'Por outro lado,'\n"
    "   'Com isso,' (no máximo 1× no artigo inteiro; prefira 'O resultado',\n"
    "   'Aliás', 'No fim', ou conectores naturais)\n"
    "2) GANCHOS DRAMÁTICOS VAZIOS — nunca use (são a cara da IA):\n"
    "   'isso muda tudo' / 'é isso que muda tudo' / 'muda o jogo' / 'vira o\n"
    "   jogo' — em vez disso EXPLIQUE concretamente o que muda.\n"
    "   'só que ninguém está falando' / 'ninguém te conta' / 'o que poucos\n"
    "   sabem' / 'pouca gente sabe' — vá direto ao fato, sem anunciar que é raro.\n"
    "   'a verdade é que' / 'a realidade é que' / 'o problema é que' (como\n"
    "   abertura) — apenas afirme o ponto sem o preâmbulo.\n"
    "   'eis o problema' / 'aqui está o ponto' / 'o pulo do gato' — corte.\n"
    "   'isso não é coincidência' / 'isso não é à toa' — mostre a causa, não\n"
    "   anuncie que ela existe.\n"
    "3) LINGUAGEM REBUSCADA — troque por natural:\n"
    "   'concomitantemente'→'ao mesmo tempo'; 'outrossim'→'além disso';\n"
    "   'por conseguinte'→'por isso'. Jargão financeiro técnico (Selic, P/L,\n"
    "   FGC, HBM) está OK — só evite formalismo desnecessário.\n"
    "4) TRAVESSÃO (—): NUNCA, em hipótese alguma. Use vírgula, ponto ou ( ).\n"
    "5) FRASES PICOTADAS em sequência: 'Queda. Alta. Recuperação.' — reescreva\n"
    "   como ideia fluida (exceto a frase curta de impacto pontual do ritmo).\n"
    "6) Chamada de ação ('salva esse post', 'comenta', 'me segue') — proibido.\n"
    "7) Emoji, hashtag no corpo do texto, exclamação excessiva — proibido.\n"
    "8) Aspas simples: use SEMPRE aspas duplas (\").\n"
    "9) DOIS-PONTOS COMO ANÚNCIO — vício pesado de IA. EVITE estruturas tipo\n"
    "   'A verdade: tal coisa', 'O resultado: assim', 'Pergunta: resposta'.\n"
    "   Esse padrão 'X: Y' faz parecer slide de PowerPoint, não texto humano.\n"
    "   Em vez disso, reescreva fluindo: 'A verdade é tal coisa', 'O resultado\n"
    "   foi assim'. Dois-pontos legítimos só pra listar (raro) ou em hora\n"
    "   (15:30). No corpo do texto, prefira ponto, vírgula ou conector natural.\n"
    "O texto final tem que estar 100% LIVRE desses vícios. Reler e limpar\n"
    "antes de entregar.\n\n"

    "USO DE FONTES: se houver [CONTEÚDO DO LINK] no brief, use SÓ dados de lá.\n"
    "  NUNCA invente número, data, citação. Sem dado, escreva conceitual.\n\n"

    "IMPORTANTE: NÃO escreva chamada de venda/CTA. O CTA é adicionado depois.\n"
    "  Termine no fechamento filosófico, no auge do conteúdo.\n\n"

    "ESTRUTURA EM PARÁGRAFOS (1 parágrafo = 1 slide):\n"
    "- Escreva o artigo em EXATAMENTE {num_slides} parágrafos, separados por\n"
    "  \\n\\n. Cada parágrafo vira UM slide.\n"
    "- Cada parágrafo é AUTO-CONTIDO (uma ideia central) mas CONECTADO ao\n"
    "  anterior: começa de onde o outro parou, sem repetir, sem 'como vimos'.\n"
    "- O último parágrafo de vários blocos pode terminar numa frase que puxa\n"
    "  o próximo (cliffhanger natural), porque o corte será exatamente ali.\n\n"

    "TAMANHO (CRÍTICO — não entregue artigo curto, é o erro mais comum):\n"
    "- Cada parágrafo/slide tem ~300-330 caracteres, TODOS de tamanho parecido.\n"
    "- O artigo inteiro PRECISA ter entre {min_chars} e {max_chars} caracteres.\n"
    "- Artigo curto = slides rasos e ruins. Você precisa de material pra\n"
    "  {num_slides} blocos densos. NÃO resuma os pontos — DESENVOLVA cada um\n"
    "  com contexto histórico, dados, mecanismo, comparações e implicações.\n"
    "- Antes de finalizar, confira: o texto tem pelo menos {min_chars} chars\n"
    "  e {num_slides} parágrafos? Se não, está raso — adicione profundidade\n"
    "  (mais contexto, mais dados, mais nuance), NUNCA enrolação ou repetição.\n\n"

    "LEGENDA E HASHTAGS (pro post do Instagram):\n"
    "- legenda: resumo executivo de 3-5 frases que entrega a TESE do post.\n"
    "  Quem ler só a legenda já entende o essencial. Mesmo tom do artigo,\n"
    "  sem clichê. Pode terminar com a sentença filosófica. SEM hashtag aqui.\n"
    "- hashtags: 6-10 hashtags relevantes ao tema, misturando amplas\n"
    "  (#investimentos, #economia) e específicas (#micron, #semicondutores).\n"
    "  Em português quando fizer sentido. Cada uma começa com #.\n\n"

    "RETORNE SOMENTE JSON VÁLIDO, sem markdown:\n"
    '{"titulo":"título curto e forte",'
    '"artigo":"texto corrido completo, parágrafos separados por \\n\\n",'
    '"legenda":"resumo executivo de 3-5 frases",'
    '"hashtags":["#tag1","#tag2","#tag3"]}'
)

SYSTEM_FATIAR = (
    "Você recebe um ARTIGO pronto e o fatia em slides para um carrossel do\n"
    "Instagram. Você é um EDITOR que corta, não um redator que reescreve.\n\n"

    "REGRA DE OURO: você NÃO reescreve o artigo. Você CORTA ele em pedaços.\n"
    "O ARTIGO É O CORAÇÃO — RESPEITE INTEGRALMENTE:\n"
    "Você NÃO escreve conteúdo novo. NÃO reformula em outras palavras. NÃO\n"
    "cria slide do zero. NÃO 'melhora' o texto. NÃO adiciona informação que\n"
    "não está lá. Você é um EDITOR que apenas COLOCA TESOURAS no artigo.\n"
    "Se concatenar os slides, tem que dar o artigo original (palavra por\n"
    "palavra, salvo micro-ajuste de coesão tipo trocar 'ela' pelo nome ou\n"
    "ajustar uma conjunção no início). NUNCA muda dados, conteúdo ou ordem.\n\n"

    "ONDE CORTAR:\n"
    "- Corte em pontos de CLIFFHANGER NATURAL: onde uma frase termina\n"
    "  deixando curiosidade pro próximo ('Mas o produto que mudou tudo tem\n"
    "  outro nome.'). Esses cortes já existem no texto, você só os encontra.\n"
    "- Cada slide tem UMA ideia central. Quando o assunto vira, corte.\n"
    "- Cada slide começa EXATAMENTE de onde o anterior parou. Sem reset,\n"
    "  sem repetir o que já foi dito, sem 'como vimos', sem 'recapitulando'.\n\n"

    "SLIDE 1 — O HOOK É O TUDO (atenção máxima aqui):\n"
    "O slide 1 decide se a pessoa continua passando os slides ou rola pra\n"
    "o próximo post. Se ele não fisga em 1 segundo, todo o resto não importa.\n"
    "- Use a abertura do artigo (paradoxo/detalhe humano inesperado) — JÁ\n"
    "  estará lá se o redator fez certo. Não suavize, não 'contextualize'.\n"
    "- Pode ser MAIS CURTO que os outros slides (150-280 chars): o hook tem\n"
    "  espaço pra respirar, não precisa de densidade — precisa de IMPACTO.\n"
    "- Negrite a FRASE DO IMPACTO (a manchete-paradoxo) pra puxar o olho.\n"
    "- Termine com uma frase que CRIE PERGUNTA na cabeça do leitor ('e tudo\n"
    "  começou num porão'), nunca com explicação completa do tópico.\n"
    "- Se o slide 1 estiver morno, REORDENE: pegue a frase mais surpreendente\n"
    "  do artigo e use ela como abertura.\n\n"

    "TAMANHO UNIFORME (regra importante — é assim que o Varos faz):\n"
    "- Cada slide tem entre 220 e 340 caracteres.\n"
    "- O tamanho deve ser CONSISTENTE entre todos os slides. Todos com\n"
    "  comprimento parecido (~280). NÃO faça um slide com 150 e outro com\n"
    "  400. Equilibre o corte pra que fiquem uniformes.\n"
    "- Exceção: o slide 1 (hook) pode ser um pouco mais curto.\n"
    "- NUNCA acima de 420.\n\n"

    "FORMATO VISUAL — PARÁGRAFOS CURTOS PRA RESPIRAR (igual ao Varos):\n"
    "- Cada slide é quebrado em 2 a 4 parágrafos CURTOS, separados por uma\n"
    "  linha em branco (\\n\\n). Cada parágrafo tem 1 ou 2 frases (~80-140\n"
    "  caracteres). NUNCA um bloco corrido único de texto.\n"
    "- Exemplo do visual desejado (1 slide):\n"
    "    Hoje, mais uma empresa atingiu a marca de US$ 1 trilhão.\n\n"
    "    Essa é a Micron, que fabrica os chips de memória que alimentam\n"
    "    toda a infraestrutura de inteligência artificial.\n\n"
    "    Mas tudo começou no porão de um consultório odontológico, com\n"
    "    dinheiro de um fazendeiro de batatas.\n"
    "- Esse respiro visual é parte do estilo. Bloco corrido cansa o olho.\n\n"

    "NÚMERO DE SLIDES: corte em EXATAMENTE {num_slides} slides.\n"
    "  Distribua o artigo de forma equilibrada entre eles.\n\n"

    "NEGRITO — FRASES INTEIRAS ESTRATÉGICAS (como o Varos):\n"
    "- Negrite FRASES INTEIRAS importantes (4-12 palavras), não palavras soltas.\n"
    "- 1 a 2 trechos por slide: a frase do gancho, o dado-chave OU a virada.\n"
    "- BOM: **a maior alta em 10 anos**, **sem os chips dela nada funciona**\n"
    "- RUIM (palavra solta): **9%**, **Nvidia**, **Selic**\n"
    "- RUIM (exagero): slide inteiro em negrito. Destaque 1-2 frases, não tudo.\n"
    "- Use **markdown** de negrito.\n\n"

    "IMAGENS — cada slide ganha UMA imagem com FUNÇÃO clara:\n"
    "- 'chart': quando o slide tem dados numéricos comparáveis. Inclua\n"
    "  chart_title, chart_type (bar|horizontal_bar|line) e chart_data\n"
    "  [{label,value,unit,highlight}].\n"
    "- 'photo': contexto visual. Você DECIDE a fonte via photo_source:\n"
    "  * photo_source='real' — SÓ pra PESSOA, EMPRESA, PRÉDIO ou LUGAR\n"
    "    ESPECÍFICO E FAMOSO que tem foto real (busca no Wikimedia). O\n"
    "    photo_topic deve ser o NOME PRÓPRIO exato em ingles ou portugues:\n"
    "    'Javier Milei', 'Banco Central Argentina', 'Jerome Powell',\n"
    "    'Casa Rosada Buenos Aires', 'Nvidia headquarters'.\n"
    "  * photo_source='stock' — pra QUALQUER conceito abstrato (crise,\n"
    "    mercado, otimismo, indústria, dinheiro, recessão). Busca no Pexels.\n"
    "    photo_topic = 3-6 palavras descritivas EM INGLÊS + modificador\n"
    "    visual: 'dramatic stock market crash screens', 'argentine peso\n"
    "    banknotes close up', 'empty factory industrial decline'.\n"
    "  REGRA DE OURO: se você não tem CERTEZA de que existe foto real e\n"
    "    famosa daquele nome, use 'stock'. Conceito abstrato com Wikimedia\n"
    "    devolve imagem aleatória e errada (rio, locomotiva, etc). Na dúvida,\n"
    "    SEMPRE 'stock'. Nomes próprios de pessoa/lugar famoso = 'real'.\n"
    "  * NUNCA photo_topic generico: 'money', 'business', 'office', 'people'.\n"
    "  * NUNCA repita o mesmo photo_topic entre slides.\n"
    "  - photo_topic_alt: 2-3 palavras de fallback (mesma linha do source).\n"
    "- Se o brief tinha [IMAGENS DISPONÍVEIS DOS LINKS], pode referenciar via\n"
    "  image_from_link (índice 1-based) quando a imagem do artigo casa com\n"
    "  o dado do slide. Senão, image_from_link: null.\n\n"

    "RETORNE SOMENTE JSON VÁLIDO, sem markdown fora do JSON:\n"
    '{"slides":[{"texto":"...","tema":"bitcoin|economia|mercado|geopolitica|ia|tecnologia",'
    '"image_type":"chart|photo","chart_title":"...","chart_type":"bar|horizontal_bar|line",'
    '"chart_data":[{"label":"...","value":0,"unit":"%","highlight":false}],'
    '"photo_source":"real|stock","photo_topic":"...","photo_topic_alt":"...","image_from_link":null}]}'
)


# ── Sanitizers do texto dos slides ────────────────────────────────────────────
# O Claude as vezes ignora a regra "nao usar travessao" e tambem retorna o
# slide inteiro como bloco corrido sem quebrar paragrafos. A gente forca o
# resultado pra cumprir as regras.

def _remover_dois_pontos_anuncio(text: str) -> str:
    """Remove o vicio de DOIS-PONTOS de anuncio/lista ('X: Y'), que faz o
    texto parecer slide de PowerPoint. Troca por '. ' e capitaliza a proxima
    letra. Preserva horas (15:30), proporcoes (1:2) e URLs (:// ) — esses
    nao tem LETRA antes ou espaco depois do ':'.
    Ex: 'E matematica: sem emissao' -> 'E matematica. Sem emissao'
        'loop vicioso: emissao, inflacao' -> 'loop vicioso. Emissao, inflacao'"""
    if not text or ":" not in text:
        return text
    def _repl(m):
        return m.group(1) + ". " + m.group(2).upper()
    # ':' precedido de LETRA, seguido de espaco(s) + LETRA
    return re.sub(
        r"([A-Za-zÁÉÍÓÚÂÊÔÃÕÇÀáéíóúâêôãõçà])\s*:\s+"
        r"([A-Za-zÁÉÍÓÚÂÊÔÃÕÇÀáéíóúâêôãõçà])",
        _repl, text,
    )


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
    """Rede de seguranca: SO quebra em paragrafos se o slide for muito grande
    (>400 chars) e sem \\n\\n. No estilo Varos cada slide eh UM paragrafo
    corrido de ~280 chars, entao a maioria passa intacta. So divide blocos
    realmente grandes (perto do limite de 420) pra nao virar muralha de texto."""
    if not text:
        return text
    if "\n\n" in text:
        return text  # ja tem paragrafos (respeita o que veio)
    if len(text) < 400:
        return text  # ate 400 chars fica como 1 paragrafo (estilo Varos)
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


def _limpar_cliches_abertura(text: str) -> str:
    """Remove cliches de abertura vazios do INICIO de um bloco de texto
    (preambulos de IA que nao agregam). Seguro: so corta no inicio e
    recapitaliza. Cliches no meio sao deixados pro Polir/revisao humana."""
    if not text:
        return text
    cliches = [
        r'^Na pr[aá]tica,\s*',
        r'^O que acontece [eé] que,?\s*',
        r'^A verdade [eé] que,?\s*',
        r'^A realidade [eé] que,?\s*',
        r'^O (?:grande )?problema [eé] que,?\s*',
        r'^Vale (?:destacar|ressaltar) que,?\s*',
        r'^[EÉ] importante (?:destacar|ressaltar) que,?\s*',
        r'^Cabe (?:destacar|ressaltar) que,?\s*',
        r'^Dessa forma,\s*',
        r'^Nesse sentido,\s*',
        r'^Em suma,\s*',
    ]
    for pat in cliches:
        novo = re.sub(pat, '', text, flags=re.IGNORECASE)
        if novo != text and novo:
            # Recapitaliza a primeira letra do que sobrou
            text = novo[0].upper() + novo[1:]
            break
    return text


def _sanitizar_legenda(text: str) -> str:
    """Sanitiza a legenda do Instagram: tira travessao, aspas simples,
    cliche de abertura. Mantem os paragrafos (legenda pode ter varios)."""
    text = _strip_em_dash(text or "")
    text = _remover_dois_pontos_anuncio(text)
    text = _aspas_simples_pra_duplas(text)
    text = _limpar_cliches_abertura(text)
    return text.strip()


def _formatar_paragrafos_varos(text: str) -> str:
    """Formata o slide no VISUAL do Varos: quebra em paragrafos curtos
    (1-2 frases, ~80-140 chars cada) separados por \\n\\n, pra respirar.
    NAO eh 1 bloco corrido — sao varios blocos curtos como na imagem.
    Preserva listas de bullet (linhas com •/-/* ) intactas."""
    if not text:
        return text
    # Bullets: estrutura de linhas eh intencional, nao mexe
    if re.search(r'(?m)^\s*[•\-\*]\s+', text):
        return text
    # Junta tudo num texto plano primeiro (remove quebras existentes)
    flat = re.sub(r'\s*\n+\s*', ' ', text).strip()
    # Divide em frases (fim de frase = . ! ? seguido de espaco)
    sentences = re.split(r'(?<=[.!?])\s+', flat)
    if len(sentences) <= 1:
        return flat
    # Agrupa em paragrafos curtos mirando ~130 chars. Fecha o bloco ANTES de
    # adicionar uma frase que o faria estourar 135 chars — assim frases longas
    # viram paragrafos proprios em vez de blocos gigantes de 200 chars.
    ALVO = 130
    paragraphs = []
    current = []
    current_len = 0
    for s in sentences:
        s_len = len(s) + 1
        if current and (current_len + s_len) > ALVO + 5:
            paragraphs.append(" ".join(current))
            current = []
            current_len = 0
        current.append(s)
        current_len += s_len
    if current:
        # Bloco final muito curto: funde no anterior pra nao ficar orfao
        if paragraphs and current_len < 45:
            paragraphs[-1] += " " + " ".join(current)
        else:
            paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def _sanitizar_slide_varos(text: str) -> str:
    """Sanitizacao pro fluxo de geracao em 2 fases. Remove travessao/aspas,
    limpa cliche de abertura e FORMATA em paragrafos curtos (visual Varos:
    varios blocos de 1-2 frases separados por linha em branco)."""
    text = _strip_em_dash(text or "")
    text = _remover_dois_pontos_anuncio(text)
    text = _aspas_simples_pra_duplas(text)
    text = _limpar_cliches_abertura(text)
    text = _formatar_paragrafos_varos(text)
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

_INSTAGRAM_DOMAINS = ("instagram.com", "instagr.am")
def _is_instagram_url(url: str) -> bool:
    """Detecta URLs de Instagram (post, reel, perfil). Instagram requer
    login pra ver conteudo via web — sem credenciais, scraping pega so
    o HTML publico (meta tags). Util pra avisar o user."""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in _INSTAGRAM_DOMAINS)
    except Exception:
        return False


def _extract_publish_date(html_or_soup, url: str = ""):
    """Tenta extrair a data de publicacao do HTML do artigo via:
    1. <meta property="article:published_time">
    2. <meta property="og:updated_time">, <meta itemprop="datePublished">
    3. JSON-LD: {"@type":"NewsArticle","datePublished":"..."}
    4. <time datetime="..."> dentro de article/header
    Retorna 'YYYY-MM-DD' (string) ou None se nao achar."""
    try:
        if hasattr(html_or_soup, "find"):
            soup = html_or_soup
        else:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_or_soup, "html.parser")
    except Exception:
        return None

    # 1. Meta tags padrao OpenGraph/Schema
    META_KEYS = [
        ("property", "article:published_time"),
        ("name",     "article:published_time"),
        ("property", "og:published_time"),
        ("property", "og:updated_time"),
        ("itemprop", "datePublished"),
        ("name",     "datePublished"),
        ("name",     "pubdate"),
        ("name",     "publication-date"),
        ("name",     "date"),
        ("name",     "DC.date.issued"),
    ]
    for attr, val in META_KEYS:
        tag = soup.find("meta", attrs={attr: val})
        if tag and tag.get("content"):
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", tag["content"])
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # 2. JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            txt = script.string or script.get_text() or ""
            for m in re.finditer(r'"datePublished"\s*:\s*"([^"]+)"', txt):
                d = re.search(r"(\d{4})-(\d{2})-(\d{2})", m.group(1))
                if d:
                    return f"{d.group(1)}-{d.group(2)}-{d.group(3)}"
        except Exception:
            continue
    # 3. <time datetime="...">
    t = soup.find("time")
    if t and t.get("datetime"):
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t["datetime"])
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _is_date_recent(date_str: str, min_year: int = 2026) -> bool:
    """True se a data eh do ano corrente (>= min_year). False caso contrario.
    Date format: YYYY-MM-DD"""
    if not date_str:
        return False
    m = re.match(r"^(\d{4})-", date_str)
    if not m:
        return False
    return int(m.group(1)) >= min_year


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
        BANNER = [
            "logo","avatar","icon","pixel","tracking","blank.","1x1","spacer",
            "/profile/","/ad/","/ads/","advert","banner","/social/","/share/",
            "facebook","twitter","linkedin","instagram","youtube","/emoji/",
            "/sprite/","/ui/","_small.","_thumb.","thumbnail-small",
            # Produtos/cursos/promo
            "ebook","e-book","curso","course","simulador","simulator",
            "viver-de-renda","carteira-recomendada","guia-","manual-",
            "/cta/","/produto/","/promo/","/promotional/","/promocional/",
            "newsletter-","lead-","lead_","captura","acquisition","acquisiti",
            "patrocinad","sponsored","publicidade","publi",
            "lead-magnet","leadmagnet","popup","inline-ad",
            # Assets do tema do site
            "/themes/","/wp-content/themes/","/v2/assets/","/assets/img/",
            "/static/img/","/dist/img/","/build/img/",
            "badge","selo","stamp","ribbon","/mascot/","mascote",
            "/author/","/colunista/","/by/","/autor/",
        ]
        for u in imgs:
            if u in seen: continue
            low = u.lower()
            if any(skip in low for skip in BANNER):
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
        return {"text": text[:max_chars], "images": imgs_clean[:3]}
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

        # ── Extrai imagens candidatas com PRIORIZACAO editorial ───────────
        # Estrategia: pega apenas conteudo curado, descarta banners/ebooks/promo
        candidate_images = []

        # Palavras-chave de RUIDO COMERCIAL/PROMOCIONAL (expandida)
        BANNER_KEYWORDS = [
            # Anuncios/banners genericos
            "logo", "avatar", "icon", "pixel", "tracking", "/profile/",
            "blank.", "1x1", "spacer", "/ad/", "/ads/", "advert", "banner",
            "thumbnail-small", "_small.", "_thumb.", "/social/", "/share/",
            "facebook", "twitter", "linkedin", "instagram", "youtube",
            "/emoji/", "/sprite/", "/ui/",
            # Produtos/cursos (capas de ebook, simuladores, etc)
            "ebook", "e-book", "curso", "course", "simulador", "simulator",
            "viver-de-renda", "carteira-recomendada", "guia-", "manual-",
            "/cta/", "/produto/", "/promo/", "/promotional/", "/promocional/",
            "newsletter-", "lead-", "lead_", "captura", "acquisition", "acquisiti",
            "patrocinad", "sponsored", "publicidade", "publi",
            "lead-magnet", "leadmagnet", "popup", "inline-ad",
            # Assets do tema do site (quase sempre promo/UI, nao conteudo)
            "/themes/", "/wp-content/themes/", "/v2/assets/", "/assets/img/",
            "/static/img/", "/dist/img/", "/build/img/",
            # Selos/badges
            "badge", "selo", "stamp", "ribbon", "tag-",
            # Mascots/mascotes que sites usam (XP, BTG, etc geralmente tem mascote)
            "/mascot/", "mascote",
            # Author thumbnails (geralmente <60px)
            "/author/", "/colunista/", "/by/", "/autor/",
        ]

        # Helper pra filtrar uma URL/elemento
        def _img_is_garbage(url_low, alt, cls, parent_ctx):
            if any(s in url_low for s in BANNER_KEYWORDS): return True
            if url_low.endswith((".gif", ".svg", ".ico", ".webp.gif")): return True
            # Classe/alt promocional
            promo_terms = ["avatar", "logo", "icon", "thumb", "thumbnail",
                           "ad-", "ads-", "advert", "banner", "promo",
                           "lead", "newsletter", "cta", "sponsor",
                           "ebook", "course", "curso", "simulador"]
            if any(s in cls for s in promo_terms): return True
            if any(s in alt for s in ["avatar", "logo do", "ícone", "compartilhar",
                                       "ebook", "e-book", "curso", "simulador",
                                       "patrocin", "publicidade", "anúncio"]):
                return True
            # Context: dentro de aside/sidebar/related/promo containers
            if parent_ctx and any(s in parent_ctx for s in [
                "sidebar", "aside", "related", "newsletter", "promo",
                "advertisement", "footer", "header-promo", "lead-form",
                "recommended", "cta-", "popup"
            ]):
                return True
            return False

        def _check_dimensions(img):
            """Retorna True se imagem tem dimensoes razoaveis pra ser editorial.
            Min 400x300, max aspect 3:1 (banners ultra-wide sao ruido)."""
            w = img.get("width") or "0"
            h = img.get("height") or "0"
            try:
                w_n = int(str(w).replace("px","").split(".")[0])
                h_n = int(str(h).replace("px","").split(".")[0])
                if w_n and w_n < 400: return False
                if h_n and h_n < 300: return False
                if w_n and h_n and (w_n/h_n > 3 or h_n/w_n > 3): return False
            except (ValueError, TypeError):
                pass
            return True

        # 1. og:image (curada pelo editor, geralmente a hero foto)
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            og_url = urljoin(url, og["content"])
            if not any(s in og_url.lower() for s in BANNER_KEYWORDS):
                candidate_images.append(og_url)

        # 2. PRIORIDADE: <figure> com <img> + <figcaption> — sao conteudo
        #    editorial real, geralmente fotos/graficos/screenshots do artigo
        article_root = soup.find("article") or soup.find("main") or soup.body
        if article_root:
            for fig in article_root.find_all("figure", limit=10):
                img = fig.find("img")
                if not img: continue
                # figure dentro de aside/sidebar/promo? pula
                parent_ctx = ""
                p = fig.parent
                for _ in range(3):
                    if p is None: break
                    parent_ctx += " " + " ".join(p.get("class", [])).lower()
                    parent_ctx += " " + (p.get("id", "") or "").lower()
                    p = p.parent
                src = img.get("src") or img.get("data-src") or img.get("data-original")
                if not src: continue
                full = urljoin(url, src)
                low = full.lower()
                cls = " ".join(img.get("class", [])).lower()
                alt = (img.get("alt") or "").lower()
                if _img_is_garbage(low, alt, cls, parent_ctx): continue
                if not _check_dimensions(img): continue
                if full not in candidate_images:
                    candidate_images.append(full)

        # 3. <img> normais dentro do article (sem figure) — so se tiver poucos
        if article_root and len(candidate_images) < 3:
            for img in article_root.find_all("img", limit=20):
                if len(candidate_images) >= 3: break
                # Pula se ja tem figure pai (ja processado)
                if img.find_parent("figure"): continue
                src = img.get("src") or img.get("data-src") or img.get("data-original")
                if not src: continue
                full = urljoin(url, src)
                low = full.lower()
                cls = " ".join(img.get("class", [])).lower()
                alt = (img.get("alt") or "").lower()
                # Pega contexto dos ancestrais
                parent_ctx = ""
                p = img.parent
                for _ in range(3):
                    if p is None: break
                    parent_ctx += " " + " ".join(p.get("class", [])).lower()
                    parent_ctx += " " + (p.get("id", "") or "").lower()
                    p = p.parent
                if _img_is_garbage(low, alt, cls, parent_ctx): continue
                if not _check_dimensions(img): continue
                if full not in candidate_images:
                    candidate_images.append(full)

        # Max 3 imagens por URL — qualidade > quantidade
        candidate_images = candidate_images[:3]

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
        # Extrai data de publicacao do HTML (meta tags + JSON-LD)
        pub_date = _extract_publish_date(soup, url)
        return {
            "text": text[:max_chars],
            "images": candidate_images,
            "source": "direct",
            "published_date": pub_date,
        }
    except Exception:
        j = _jina_fetch(url, max_chars) or _wayback_fetch(url, max_chars)
        if j: j.setdefault("source", "jina")
        return j

def _processar_brief_com_urls(brief: str, min_year: int = 2026):
    """Detecta URLs no brief, baixa o conteudo de cada uma e devolve:
    - brief com cada URL substituida por bloco [CONTEÚDO DO LINK ...]
    - lista de info por URL (sucesso/falha + chars + qtd de imagens
      + data de publicacao + flags is_instagram, is_stale)
    - lista global de imagens candidatas (com origem) que o Claude pode usar

    Tambem INJETA no brief enriquecido avisos sobre:
    - URLs do Instagram: bloqueado pelo login wall, conteudo limitado
    - URLs com data anterior a min_year: marca como [FONTE ANTIGA] pro Claude
      saber que NÃO eh dado atual"""
    if not brief:
        return brief, [], []
    urls = URL_PATTERN.findall(brief)
    seen = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]
    info = []
    enriched = brief
    all_images = []
    for url in urls:
        is_ig = _is_instagram_url(url)
        result = _fetch_url_text(url)
        if result:
            pub_date = result.get("published_date")
            is_stale = bool(pub_date) and not _is_date_recent(pub_date, min_year)
            # Header do bloco com avisos relevantes pro Claude
            header_parts = [f"[CONTEÚDO DO LINK {url}]"]
            if is_ig:
                header_parts.append(
                    "⚠ FONTE INSTAGRAM: conteudo pode estar incompleto. "
                    "Instagram bloqueia scraping sem login. "
                    "Use APENAS os fragmentos abaixo, NAO complete com suposicoes."
                )
            if pub_date:
                if is_stale:
                    header_parts.append(
                        f"⚠ FONTE ANTIGA — publicada em {pub_date}. NAO use como "
                        f"dado atual. Mencione data explicita ('em {pub_date[:4]}, ...') "
                        f"ou peca por fonte mais recente."
                    )
                else:
                    header_parts.append(f"Data de publicacao: {pub_date}")
            bloco = "\n\n" + "\n".join(header_parts) + "\n" + result["text"] + "\n[/CONTEÚDO]\n"
            enriched = enriched.replace(url, bloco, 1)
            info.append({
                "url": url, "ok": True,
                "chars": len(result["text"]),
                "imgs": len(result["images"]),
                "source": result.get("source", "direct"),
                "published_date": pub_date,
                "is_instagram": is_ig,
                "is_stale": is_stale,
            })
            for img_url in result["images"]:
                all_images.append({"url_imagem": img_url, "origem": url})
        else:
            # Mesmo se falhou fetch: avisa Claude que era Instagram (provavel bloqueio)
            if is_ig:
                enriched = enriched.replace(
                    url,
                    f"[INSTAGRAM BLOQUEADO {url} — nao consegui ler. "
                    f"NAO INVENTE conteudo desse post. Peca pro user colar o texto.]",
                    1
                )
            info.append({
                "url": url, "ok": False, "chars": 0, "imgs": 0,
                "reason": "instagram_blocked" if is_ig else "blocked",
                "is_instagram": is_ig,
            })
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


def _montar_chart_url(ctype: str, ctitle: str, cdata: list) -> str:
    """Monta a URL de um grafico do quickchart LEGIVEL e CORRETO.
    Retorna '' (sem grafico, slide cai pra foto) se os dados nao formam
    um grafico bom. Regras aprendidas na marra:
    - quickchart REJEITA (HTTP 400) config com funcao JS (callback/formatter).
      Entao a unidade vai no TITULO, datalabel mostra o numero puro.
    - Escalas dispares no mesmo grafico (ex: 10 milhoes + -6%) ficam
      ilegiveis. Se max/min dos valores > 50x, NAO faz grafico.
    - Labels longos espremem o eixo. Encurta os > 16 chars.
    - Fontes GRANDES (titulo 30, ticks 22, datalabel 26) pra ler no mobile.
    - 2 a 6 pontos. Menos que isso nao vale grafico; mais polui."""
    if not cdata:
        return ""
    try:
        pts = [(str(d.get("label", "")).strip(),
                float(d.get("value")),
                bool(d.get("highlight")))
               for d in cdata if d.get("value") is not None]
    except (ValueError, TypeError):
        return ""
    if not (2 <= len(pts) <= 6):
        return ""
    valores = [v for _, v, _ in pts]
    nonzero = [abs(v) for v in valores if v != 0]
    # Escalas muito diferentes = grafico sem sentido visual -> melhor foto
    if nonzero and (max(nonzero) / min(nonzero)) > 50:
        return ""
    unit = (cdata[0].get("unit") or "").strip()

    def _short(lbl):
        return lbl if len(lbl) <= 16 else lbl[:15].rstrip() + "."
    labels = [_short(l) for l, _, _ in pts]
    colors = ["rgba(239,68,68,0.9)" if hl else "rgba(29,155,240,0.9)" for _, _, hl in pts]
    cjs = {"horizontal_bar": "horizontalBar", "line": "line"}.get(ctype, "bar")
    titulo = ctitle or ""
    if unit and unit.lower() not in titulo.lower():
        titulo = f"{titulo} (em {unit})" if titulo else f"Valores em {unit}"
    cfg = {
        "type": cjs,
        "data": {"labels": labels, "datasets": [{
            "data": valores, "backgroundColor": colors,
            "borderColor": colors, "borderWidth": 3, "fill": False,
            "pointRadius": 6, "lineTension": 0.2,
        }]},
        "options": {
            "title": {"display": True, "text": titulo,
                      "fontSize": 30, "fontStyle": "bold", "fontColor": "#0f1419", "padding": 18},
            "legend": {"display": False},
            "layout": {"padding": {"top": 28, "bottom": 14, "left": 14, "right": 28}},
            "scales": {
                "xAxes": [{"ticks": {"fontSize": 22, "fontStyle": "bold", "fontColor": "#0f1419"},
                           "gridLines": {"display": False}}],
                "yAxes": [{"ticks": {"fontSize": 20, "fontColor": "#6b7280"}}],
            },
            "plugins": {"datalabels": {
                "anchor": "end", "align": "end", "offset": 2,
                "font": {"weight": "bold", "size": 28},
                "color": "#0f1419",
            }},
        },
    }
    return ("https://quickchart.io/chart?c="
            + urllib.parse.quote(json.dumps(cfg, separators=(",", ":")))
            + "&width=1080&height=760&backgroundColor=white&version=2&devicePixelRatio=2")


@app.route("/gerar")
def pagina_gerar():
    has_key = bool(ANTHROPIC_API_KEY and ANTHROPIC_API_KEY.startswith("sk-ant-api"))
    return render_template("gerar.html", has_key=has_key)


def _wikimedia_search(query: str, n: int = 12):
    """Busca imagens no Wikimedia Commons. Otimo pra fotos REAIS de
    politicos, edificios, eventos, logos — diferente do stock generico
    do Pexels. Sem key necessaria.
    Retorna lista de {url, thumb, title}."""
    try:
        import requests as _req
        # API do Commons: gera lista de imagens via search
        r = _req.get("https://commons.wikimedia.org/w/api.php", params={
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrnamespace": 6,           # File namespace
            "gsrsearch": query,
            "gsrlimit": n,
            "prop": "imageinfo",
            "iiprop": "url|size|mime",
            "iiurlwidth": 800,           # thumbnail 800px
        }, headers={"User-Agent": "BearlzCMS/1.0"}, timeout=10)
        if r.status_code != 200:
            return []
        pages = (r.json().get("query") or {}).get("pages") or {}
        out = []
        # SO formatos que renderizam direto no browser/canvas. djvu, tiff,
        # pdf, gif e svg passariam pelo 'image/' mas quebram (o caso do
        # '.djvu' que apareceu nos slides). Whitelist explicita:
        MIMES_OK = ("image/jpeg", "image/png", "image/webp")
        for pid, page in pages.items():
            ii = (page.get("imageinfo") or [{}])[0]
            mime = ii.get("mime", "")
            if mime not in MIMES_OK:
                continue
            url = ii.get("url", "") or ""
            # Reforco por extensao (alguns mimes vem errados)
            if not url.lower().rsplit("?", 1)[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            # Filtra imagens muito pequenas
            w = ii.get("width", 0)
            h = ii.get("height", 0)
            if w < 400 or h < 400:
                continue
            # Aceitar fotos com aspect razoavel (nao banners ultra-wide)
            ratio = (w / h) if h else 1
            if ratio > 2.5 or ratio < 0.4:
                continue
            # Usa o thumb de 800px (mais leve e confiavel que o original, que
            # pode ter varios MB e 4000px+ e carregar lento ou falhar).
            thumb = ii.get("thumburl") or url
            out.append({
                "url": thumb,
                "thumb": thumb,
                "title": page.get("title", "").replace("File:", "").rsplit(".", 1)[0],
                "source": "wikimedia",
            })
        return out
    except Exception:
        return []


@app.route("/api/img/search", methods=["GET"])
def api_img_search():
    """Busca imagens combinando Wikimedia Commons + Pexels.
    Wikimedia eh otimo pra fotos REAIS de politicos, predios, marcas;
    Pexels pra contexto visual generico. Retorna mix priorizando
    Wikimedia (mais relevante pra conteudo jornalistico)."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "query obrigatoria"}), 400
    fotos = []
    # 1. Wikimedia primeiro (relevancia editorial)
    wiki = _wikimedia_search(q, n=8)
    for w in wiki:
        fotos.append({
            "url": w["url"],
            "thumb": w["thumb"],
            "source": "wikimedia",
            "credit": w.get("title", ""),
        })
    # 2. Pexels (preenche o resto)
    if PEXELS_API_KEY:
        try:
            import requests as _req
            r = _req.get(
                "https://api.pexels.com/v1/search",
                params={"query": q, "per_page": 12, "orientation": "portrait"},
                headers={"Authorization": PEXELS_API_KEY},
                timeout=8
            )
            if r.status_code == 200:
                data = r.json()
                for p in data.get("photos", []):
                    src = p.get("src", {})
                    url = src.get("portrait") or src.get("large") or src.get("original")
                    thumb = src.get("medium") or url
                    if url:
                        fotos.append({
                            "url": url,
                            "thumb": thumb,
                            "source": "pexels",
                            "credit": p.get("photographer", ""),
                        })
        except Exception:
            pass
    return jsonify({"ok": True, "fotos": fotos})


def _detect_vicios_ia(texto: str):
    """Detecta padroes tipicos de escrita de IA — NAO sao erros gramaticais,
    mas sao clichês que tornam o texto artificial e que o usuario quer evitar."""
    matches = []
    if not texto:
        return matches

    # 1. Cliches/mulet. O '(?:^|(?<=[.!?]\\s)|(?<=\\n))' restringe ao INICIO
    # de frase, pra nao pegar uso legitimo no meio ('testar na pratica').
    _ini = r"(?:^|(?<=[.!?]\s)|(?<=\n))"
    cliches = [
        # Conectores burocráticos (so como ABERTURA de frase)
        (_ini + r"Na pr[áa]tica,?", "'Na prática' como abertura é clichê de IA. Va direto ao ponto."),
        (r"\bO que acontece [eé] que,?", "'O que acontece é que' é clichê de IA."),
        (r"\bVale destacar(?:\s+que)?,?", "'Vale destacar' soa burocrático. Apenas destaque o ponto."),
        (r"\bÉ importante ressaltar(?:\s+que)?,?", "'É importante ressaltar' soa burocrático."),
        (r"\bCabe destacar(?:\s+que)?,?", "'Cabe destacar' soa burocrático."),
        (r"\bÉ fundamental(?:\s+que)?", "'É fundamental' soa formal. Use 'precisa', 'tem que'."),
        (r"\bDessa forma,?", "'Dessa forma' é clichê. Use transição mais natural."),
        (r"\bNesse sentido,?", "'Nesse sentido' é clichê. Use algo mais direto."),
        (r"\bEm suma,?", "'Em suma' soa formal demais. Reescreva o fechamento."),
        (r"\bPor outro lado,?", "'Por outro lado' é muleta. Use 'mas', 'só que'."),
        # Ganchos dramáticos tipicos de IA — pseudo-suspense pra "criar engajamento"
        (r"\b(?:E\s+)?isso muda tudo\b\.?", "'Isso muda tudo' é fechamento dramático cara de IA. Reescreva mostrando O QUE muda concretamente."),
        (r"\b(?:Só|So) que ningu[eé]m (?:est[aá]|ta|esta) (?:falando|comentando|olhando|notando|prestando|reparando)\b", "'Só que ninguém está falando' é gancho dramático de IA. Afirme o ponto direto."),
        (r"\bningu[eé]m (?:te\s+)?(?:fala|comenta|conta|diz)(?:\s+(?:isso|sobre|disso|nisso))?\b", "'Ninguém fala/conta isso' é gancho dramático de IA. Va direto pro fato."),
        (r"\bo que ningu[eé]m (?:te\s+)?(?:fala|conta|diz|comenta)\b", "'O que ninguém te conta' é gancho dramático de IA."),
        (r"\b(?:Mas\s+)?a verdade [eé] que,?", "'A verdade é que' é gancho de IA. Apenas afirme o fato."),
        (r"\bA realidade [eé] que,?", "'A realidade é que' é gancho de IA. Apenas afirme o fato."),
        (r"\bO que poucos sabem,?", "'O que poucos sabem' é gancho dramático de IA."),
        (r"\bPouca gente sabe\b", "'Pouca gente sabe' é gancho dramático de IA."),
        (r"\b(?:Eis|Aqui est[aá]) o (?:problema|ponto|detalhe|pulo do gato)\b", "Esse tipo de 'eis o problema' é gancho cara de IA."),
        (r"\b(?:vira|virou) o jogo\b", "'Vira o jogo' é metáfora desgastada de IA."),
        (r"\bjoga tudo (?:pelos ares|por terra)\b", "'Joga tudo pelos ares' é metáfora desgastada."),
        (r"\bmuda o jogo\b", "'Muda o jogo' é metáfora desgastada de IA."),
        (r"\b(?:E )?[eé] (?:isso|aqui) que muda tudo\b", "'É isso que muda tudo' é fechamento dramático de IA."),
        (r"\bo (?:grande )?problema [eé] que,?", "'O problema é que' como abertura é gancho de IA. Apenas afirme."),
        (r"\bisso (?:n[ãa]o [eé] coincid[êe]ncia|n[ãa]o [eé] [aà]\s*toa)\b", "'Não é coincidência/à toa' é dramatização cara de IA."),
        (r"\b(?:e )?[eé] (?:exatamente )?(?:a[ií]|nesse momento) que\b", "'É aí que' como gancho repetido vira tique de IA."),
    ]
    for pattern, msg in cliches:
        for m in re.finditer(pattern, texto, re.IGNORECASE):
            matches.append({
                "offset": m.start(),
                "length": m.end() - m.start(),
                "message": msg,
                "short": "Clichê de IA",
                "suggestions": [],
                "category": "Vício de IA",
                "type": "AI_CLICHE",
                "context": texto[max(0,m.start()-20):min(len(texto), m.end()+20)]
            })

    # 2. Travessão (—) é proibido absoluto
    for m in re.finditer(r"—", texto):
        matches.append({
            "offset": m.start(),
            "length": 1,
            "message": "Travessão (—) é proibido. Substitua por vírgula ou parênteses.",
            "short": "Travessão",
            "suggestions": [",", " ("],
            "category": "Vício de IA",
            "type": "AI_DASH",
            "context": texto[max(0,m.start()-20):min(len(texto), m.end()+20)]
        })

    # 3. "Com isso," — só permitido 1x. Marca a partir da 2a aparição
    com_isso = list(re.finditer(r"\bCom isso,?", texto, re.IGNORECASE))
    if len(com_isso) > 1:
        for m in com_isso[1:]:
            matches.append({
                "offset": m.start(),
                "length": m.end() - m.start(),
                "message": f"'Com isso' já foi usado antes. Varie: 'Aliás,', 'Tudo isso', 'O resultado'.",
                "short": "Repetição de muleta",
                "suggestions": ["Aliás,", "Tudo isso", "O resultado"],
                "category": "Vício de IA",
                "type": "AI_REPEAT",
                "context": texto[max(0,m.start()-20):min(len(texto), m.end()+20)]
            })

    # 4. Frases picotadas: 3+ frases curtas em sequência tipo "Queda. Alta. Recuperação."
    picotada = re.compile(
        r"\b([A-ZÁÊÍÔÚÃÕÇÀÉ][a-záêíôúãõçàé]{2,15})\.\s+"
        r"([A-ZÁÊÍÔÚÃÕÇÀÉ][a-záêíôúãõçàé]{2,15})\.\s+"
        r"([A-ZÁÊÍÔÚÃÕÇÀÉ][a-záêíôúãõçàé]{2,15})\."
    )
    for m in picotada.finditer(texto):
        # As 3 palavras juntas são curtas (picotada típica)
        total = sum(len(g) for g in m.groups())
        if total < 50:
            matches.append({
                "offset": m.start(),
                "length": m.end() - m.start(),
                "message": "Frases picotadas estilo IA. Reescreva como uma frase fluida.",
                "short": "Frase picotada",
                "suggestions": [],
                "category": "Vício de IA",
                "type": "AI_CHOPPY",
                "context": texto[max(0,m.start()-10):min(len(texto), m.end()+10)]
            })

    # 5. Negrito em palavra isolada (**X** com 1-2 palavras curtas)
    # Mau uso: **Brasil**, **Selic**, **9%** — palavra solta sem sentido proprio.
    # EXCECAO (estilo Varos): sentenca curta de IMPACTO terminada em . ! ? eh
    # negrito intencional e bom ("**E matematica.**", "**Ninguem viu isso.**").
    # So marcamos como mau uso se for palavra solta SEM pontuacao de fechamento.
    for m in re.finditer(r"\*\*([^*\n]{1,40})\*\*", texto):
        content = m.group(1).strip()
        word_count = len(content.split())
        termina_em_impacto = content.endswith((".", "!", "?"))
        # 1-3 palavras sem pontuacao final = palavra isolada (mau uso).
        # Com pontuacao = sentenca de impacto proposital (permitido).
        if word_count <= 3 and len(content) < 25 and not termina_em_impacto:
            matches.append({
                "offset": m.start(),
                "length": m.end() - m.start(),
                "message": f"Negrito em '{content}' é palavra isolada. Negrite FRASES inteiras (4-12 palavras) ou frase curta de impacto terminada em ponto.",
                "short": "Negrito mal usado",
                "suggestions": [],
                "category": "Vício de IA",
                "type": "AI_BOLD",
                "context": texto[max(0,m.start()-15):min(len(texto), m.end()+15)]
            })

    # 6. Aspas simples (regra: usar sempre duplas)
    # Detecta padrão 'palavra' ou 'frase' (não apóstrofo natural d'agua)
    for m in re.finditer(r"(?<!\w)'([^'\n]{2,80})'(?!\w)", texto):
        matches.append({
            "offset": m.start(),
            "length": m.end() - m.start(),
            "message": "Use aspas duplas (\") em vez de simples (').",
            "short": "Aspas erradas",
            "suggestions": [f'"{m.group(1)}"'],
            "category": "Vício de IA",
            "type": "AI_QUOTES",
            "context": texto[max(0,m.start()-15):min(len(texto), m.end()+15)]
        })

    # 7. Dois-pontos como anuncio ('A verdade:', 'O resultado:', etc).
    # Tique de IA que faz o texto parecer slide de PowerPoint.
    # Pega: palavra capitalizada (com ate 2 palavras seguintes) + ":" + texto.
    # Ignora 15:30 (sem letras antes), https:// (sem letras antes), 1:2.
    # Pega ':' precedido de LETRA (maiuscula OU minuscula) e seguido de
    # espaco + letra. Cobre 'A verdade: X', 'loop vicioso: X', 'austeridade: X'.
    # Ignora hora (15:30, digito antes), proporcao (1:2) e URL (:// sem espaco).
    cdois_pat = re.compile(
        r"([A-Za-zÁÉÍÓÚÂÊÔÃÕÇÀáéíóúâêôãõçà]{2,})"
        r":\s+[A-Za-zÁÉÍÓÚÂÊÔÃÕÇÀ]"
    )
    for m in cdois_pat.finditer(texto):
        anuncio = m.group(1)
        if anuncio.lower() in ("hoje", "ontem", "amanha", "agora", "obs", "http", "https"):
            continue
        # Posicao do ":" no texto
        colon_offset = m.start() + len(anuncio)
        matches.append({
            "offset": colon_offset,
            "length": 1,
            "message": f"'{anuncio}:' soa como anúncio de slide PowerPoint. Reescreva fluindo (vírgula ou conector natural).",
            "short": "Dois-pontos de anúncio",
            "suggestions": [],
            "category": "Vício de IA",
            "type": "AI_COLON",
            "context": texto[max(0,m.start()-15):min(len(texto), m.end()+15)]
        })

    return matches


SYSTEM_POLIR = """Você é editor sênior do @gabriel.bearlz. Recebe UM slide e reescreve com voz HUMANIZADA, MANTENDO profundidade e densidade.

FILOSOFIA DO REWRITE:
- O OBJETIVO eh tirar os vicios de IA, NAO encurtar
- Mantenha frases longas, complexas, com nuance — eh o que cria autoridade
- Texto humano de qualidade NAO eh texto curto — eh texto que flui
- Se o texto original tinha 380 chars, a versao polida deve ter ~ 380 chars tambem
- NUNCA reduza pra menos de 80% do tamanho original
- Adicione contexto/desenvolvimento se ficou raso; nunca enxugue raciocinio

VOZ HUMANIZADA — remova clichês SUBSTITUINDO por linguagem natural:
- "Na prática," → REMOVA mas mantém o resto da frase intacto, ou
  conecte com transição natural se necessário ("A consequência prática")
- "O que acontece é que," → REMOVA, vá direto ao ponto, mas mantém
  o conteúdo todo
- "Vale destacar que X" → simplesmente afirme X de forma direta, MAS
  com toda a nuance que tinha
- "É importante ressaltar," "Cabe destacar," "É fundamental" → idem
- "Com isso," — max 1× no slide. Se aparecer 2+, varie ("Aliás",
  "Tudo isso", "O resultado", ou conectores naturais)
- "Dessa forma," "Nesse sentido," "Em suma," → use transições humanas
  ("E aí", "O resultado", "No fim")
- NUNCA travessão (—) — use vírgula ou parênteses
- Frases picotadas ("Queda. Alta.") → reescreva como ideia fluida

GANCHOS DRAMÁTICOS DE IA — REMOVA SEMPRE (são a cara da IA):
- "Isso muda tudo" / "E isso muda tudo" / "é isso que muda tudo" /
  "muda o jogo" / "vira o jogo" → REMOVA o fechamento dramático e
  EXPLIQUE concretamente o que muda (ex.: "isso muda tudo pra renda
  fixa" → "investidor de renda fixa vai sentir no rendimento mensal")
- "Só que ninguém está falando" / "Ninguém te conta" / "O que poucos
  sabem" / "Pouca gente sabe" → CORTA o gancho de suspense. Vá direto
  pro fato. Não precisa anunciar que é informação rara.
- "A verdade é que" / "A realidade é que" / "O problema é que" como
  abertura → REMOVA. Apenas afirme o ponto sem o preâmbulo.
- "Eis o problema" / "Aqui está o ponto" / "Esse é o pulo do gato" →
  REMOVA. Esse tipo de anúncio é narrativa de IA.
- "Isso não é coincidência" / "Isso não é à toa" → REMOVA. Se há
  causa, mostre a causa em vez de anunciar que existe.

Princípio: textos humanos AFIRMAM. Textos de IA ANUNCIAM. Corte os
anúncios e mantenha as afirmações.

LINGUAGEM — humanize sem perder densidade:
- "Concomitantemente" → "ao mesmo tempo"
- "Outrossim" → "além disso"
- "Por conseguinte" → "por isso"
- Jargão financeiro técnico está OK (público é investidor)
- NÃO simplifique conceitos — só vocabulário desnecessariamente formal
- MANTENHA subordinações que carregam nuance ("o que mostra que",
  "embora", "ainda que", "mesmo com")

ESTRUTURA QUE MANTÉM PROFUNDIDADE:
- Tamanho: mantenha aproximadamente igual ao original (idealmente
  320-400 chars se o original era assim)
- Parágrafos separados por \\n\\n (2-3 por slide normalmente)
- Negritos em FRASES inteiras (4-12 palavras). Se tinha **palavra
  isolada**, ENVOLVA numa frase: **a queda de 9% no trimestre**
- Aspas duplas " (não ')
- Números arredondados quando possível

EXEMPLO DO RIGHT WAY:
ORIG: "Na prática, o que acontece é que o Banco Central foi forçado
a subir os juros porque a inflação não cedia. Vale destacar que isso
tem consequências severas pro consumidor de classe média que depende
de crédito pra fechar o mês."
POLIDO BOM: "O Banco Central foi forçado a subir os juros porque a
inflação não cedia, e a consequência aparece exatamente no consumidor
de classe média que depende de crédito pra fechar o mês."
POLIDO RUIM (encolheu demais): "O BC subiu os juros. Isso prejudica
a classe média." — NÃO faça assim.

RESPOSTA: SOMENTE JSON valido, sem markdown:
{"texto_novo": "texto reescrito completo com densidade", "mudancas_principais": ["lista breve dos ajustes"]}

REGRAS DE ESCAPE DO JSON (criticas — parsing quebra se voce errar):
- Aspas duplas DENTRO do texto_novo devem ser ESCAPADAS com \\"
  Exemplo: "texto_novo": "O CEO disse \\"foi um sucesso\\" no relatorio"
  NUNCA: "texto_novo": "O CEO disse "foi um sucesso" no relatorio"
- Prefira aspas SIMPLES (') ao citar fala. Eh mais seguro.
- Quebras de linha viram \\n explicito"""


@app.route("/api/polir-slide", methods=["POST"])
def api_polir_slide():
    """Recebe texto de UM slide e devolve versao polida (sem vicios IA,
    linguagem mais simples). Frontend mostra antes/depois lado a lado."""
    if not ANTHROPIC_AVAILABLE:
        return jsonify({"error": "Biblioteca anthropic não instalada"}), 400
    if not ANTHROPIC_API_KEY or not ANTHROPIC_API_KEY.startswith("sk-ant-api"):
        return jsonify({"error": "ANTHROPIC_API_KEY não configurada"}), 400
    data = request.get_json() or {}
    texto = (data.get("text") or "").strip()
    if not texto:
        return jsonify({"error": "texto vazio"}), 400
    if len(texto) > 2000:
        return jsonify({"error": "texto muito grande (max 2000 chars)"}), 400

    try:
        client = _anthropic_lib.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = claude_call_with_retry(client,
            model="claude-sonnet-4-5", max_tokens=1500,
            system=SYSTEM_POLIR,
            messages=[{"role": "user", "content": f"SLIDE ATUAL:\n{texto}\n\nReescreva removendo vícios e simplificando."}]
        )
        out = resp.content[0].text.strip()
        if out.startswith("```"):
            out = re.sub(r"^```[a-z]*\n?", "", out)
            out = re.sub(r"\n?```$", "", out).strip()
        dados = _parse_polir_response(out)
        if not dados or "texto_novo" not in dados:
            # Loga no stderr pra debug nos logs do Fly
            import sys
            print(f"[POLIR-PARSE-FAIL] {len(out)} chars: {out[:800]!r}", file=sys.stderr, flush=True)
            return jsonify({"error": "Resposta inválida do Claude", "raw": out[:600]}), 500
        # Sanitiza o resultado pelos mesmos filtros que o gerador
        texto_novo = _sanitizar_slide(dados.get("texto_novo", ""))
        # Guardrail: se o polido encolheu mais de 25% do original, ALERTA
        # mas devolve mesmo assim pra usuario decidir
        encolheu = False
        ratio = len(texto_novo) / max(1, len(texto))
        if ratio < 0.75:
            encolheu = True
        return jsonify({
            "ok": True,
            "texto_original": texto,
            "texto_novo": texto_novo,
            "mudancas": dados.get("mudancas_principais", []),
            "encolheu": encolheu,
            "chars_antes": len(texto),
            "chars_depois": len(texto_novo),
        })
    except Exception as e:
        msg = str(e)
        if "overloaded" in msg.lower() or "529" in msg:
            return jsonify({"error": "Claude sobrecarregado. Tente em 1-2 min."}), 503
        return jsonify({"error": str(e)}), 500


def _numeros_normalizados(texto: str):
    """Extrai numeros do texto como conjunto de strings de digitos puros
    (sem separador). '47,9' e '47.9' viram '479'. Usado pra comparar os
    dados do conteudo gerado com o material de origem."""
    out = set()
    for m in re.finditer(r"\d[\d.,]*\d|\d", texto or ""):
        d = re.sub(r"[.,]", "", m.group(0))
        if d:
            out.add(d)
            out.add(d.lstrip("0") or "0")  # tambem sem zeros a esquerda
    return out


def _verificar_dados_inventados(slides_raw, fonte_texto):
    """CHECK-IN ANTI-INVENCAO: compara os numeros que aparecem nos slides
    com os que existem no material de origem (brief + conteudo dos links).
    Retorna lista de {slide, numero, trecho} pros numeros do conteudo que
    NAO tem respaldo na fonte — candidatos a dado inventado/alucinado.

    Heuristica conservadora pra reduzir falso positivo:
    - So checa numeros COM peso (>=2 digitos, ou com unidade $/%/bi/mi/mil).
    - Ignora anos (19xx/20xx) — esses sao cobertos pelo _validar_datas_2026.
    - Aceita o numero se a sequencia de digitos aparece em QUALQUER lugar
      da fonte (tolera formatacao diferente: 47,9 ~ 47.9 ~ 479)."""
    if not slides_raw or not fonte_texto:
        return []
    fonte = _numeros_normalizados(fonte_texto)
    # Padrao: numero opcionalmente precedido de R$/US$ e seguido de unidade
    pat = re.compile(
        r"(R\$|US\$|US|€)?\s?(\d[\d.,]*\d|\d)\s?"
        r"(%|mil|milh[õo]es|milh[ãa]o|bilh[õo]es|bilh[ãa]o|bi|tri|trilh[õo]es|"
        r"pontos|ponto|p\.?p\.?|x|vezes)?",
        re.IGNORECASE,
    )
    avisos = []
    vistos = set()
    for i, s in enumerate(slides_raw):
        texto = s.get("texto", "") if isinstance(s, dict) else str(s)
        for m in pat.finditer(texto):
            simbolo, num, unidade = m.group(1), m.group(2), m.group(3)
            digits = re.sub(r"[.,]", "", num)
            if len(digits) < 2:
                # numero de 1 digito so conta se tiver unidade/simbolo forte
                if not (simbolo or unidade):
                    continue
            # ignora anos
            if len(digits) == 4 and digits.startswith(("19", "20")):
                continue
            # tem respaldo na fonte?
            if digits in fonte or (digits.lstrip("0") or "0") in fonte:
                continue
            chave = (i, digits)
            if chave in vistos:
                continue
            vistos.add(chave)
            trecho = texto[max(0, m.start()-25):min(len(texto), m.end()+25)].replace("\n", " ")
            avisos.append({
                "slide": i + 1,
                "numero": m.group(0).strip(),
                "trecho": trecho.strip(),
            })
    return avisos


def _validar_datas_2026(slides_raw, ano_atual: int = 2026):
    """Varre o texto dos slides procurando datas suspeitas (anos antigos
    sem contexto historico). Retorna lista de avisos pra UI mostrar
    no painel de gerar pro user revisar manualmente.

    Heuristica: ano 20XX < ano_atual eh suspeito A NAO SER QUE apareca
    apos uma palavra-gatilho de contexto historico ('em', 'desde',
    'durante', 'na crise de', 'fundado em', etc)."""
    if not slides_raw:
        return []
    avisos = []
    # Palavras antes do ano que indicam contexto historico legitimo
    HIST_TRIGGER = re.compile(
        r"\b(em|desde|antes\s+de|durante|durante\s+a|durante\s+o|"
        r"na\s+crise\s+de|crise\s+de|do\s+ano\s+de|fundad[oa]\s+em|"
        r"criad[oa]\s+em|nascid[oa]\s+em|recorde\s+de|historic[oa]|"
        r"de\s+\d{4}\s+a|entre\s+\d{4}\s+e|relatorio\s+de|safra\s+de|"
        r"eleicao\s+de|primeiro\s+semestre\s+de|segundo\s+semestre\s+de|"
        r"trimestre\s+de|exercicio\s+de|balanco\s+de|resultado\s+de|"
        r"ate)\s+$",
        re.IGNORECASE
    )
    for i, s in enumerate(slides_raw):
        texto = s.get("text", "")
        if not texto:
            continue
        for m in re.finditer(r"\b(20\d{2})\b", texto):
            ano = int(m.group(1))
            if ano >= ano_atual:
                continue  # ano atual ou futuro: OK
            if ano < 1990:
                continue  # nao vamos olhar antiguidade
            ctx_antes = texto[max(0, m.start() - 60):m.start()]
            # Se tem palavra-gatilho historica ANTES do ano, eh OK
            if HIST_TRIGGER.search(ctx_antes + " "):
                continue
            # Se o ano tem barra/hifen depois (ex: 2024/2025, 2024-2026), pode ser range
            ctx_depois = texto[m.end():m.end() + 5]
            if re.match(r"\s*[/\-–—]\s*\d{4}", ctx_depois):
                continue
            avisos.append({
                "slide": i + 1,
                "ano_detectado": ano,
                "trecho": texto[max(0, m.start() - 35):min(len(texto), m.end() + 35)],
                "msg": f"Slide {i+1}: ano {ano} sem contexto historico explicito. "
                       f"Confira se eh dado atual ou se precisa marcar com 'em {ano},...'",
            })
    return avisos


def _shift_lt_offsets_to_original(lt_matches, original):
    """LanguageTool recebe texto SEM ** (markdown removido) e devolve
    offsets relativos a esse texto limpo. O frontend trabalha com o texto
    ORIGINAL (com ** intactos). Sem ajuste, slice(offset, length) pega
    trecho errado e a sugestao fica colada/duplicada na palavra.

    Esta funcao constroi um mapping char-por-char do texto_limpo pro
    texto original e traduz cada {offset, length} dos matches do LT.

    Se nao ha ** no original, retorna os matches inalterados."""
    if not lt_matches or "**" not in original:
        return lt_matches
    # clean_to_orig[k] = posicao no texto original equivalente ao char k
    # do texto limpo (sem **). Cada par ** consome 2 indices do original
    # sem avancar o limpo.
    clean_to_orig = []
    i = 0
    n = len(original)
    while i < n:
        if i + 1 < n and original[i] == "*" and original[i+1] == "*":
            i += 2
            continue
        clean_to_orig.append(i)
        i += 1
    n_clean = len(clean_to_orig)
    for m in lt_matches:
        off = m.get("offset", 0)
        length = m.get("length", 0)
        # Inicio (inclusivo) no texto original
        if off < n_clean:
            new_off = clean_to_orig[off]
        elif n_clean > 0:
            new_off = clean_to_orig[-1] + 1
        else:
            new_off = 0
        # Fim (exclusivo) no texto original — pega o char limpo no
        # offset+length-1 e soma 1 pra ter o fim exclusivo
        end = off + length
        if length == 0:
            new_end = new_off
        elif end - 1 < n_clean:
            new_end = clean_to_orig[end - 1] + 1
        elif n_clean > 0:
            new_end = clean_to_orig[-1] + 1
        else:
            new_end = new_off
        m["offset"] = new_off
        m["length"] = new_end - new_off
    return lt_matches


@app.route("/api/check-pt", methods=["POST"])
def api_check_pt():
    """Verifica erros ortograficos/gramaticais via LanguageTool (free API).
    Body: {text: "...", remove_md: bool}. Retorna matches do LanguageTool.
    Free tier: ~20 req/min, sem key."""
    data = request.get_json() or {}
    texto = (data.get("text") or "").strip()
    if not texto:
        return jsonify({"error": "texto vazio"}), 400
    # Remove markdown bold pra nao confundir o checker. Os offsets do LT
    # voltam relativos ao texto_limpo — sao traduzidos pro texto original
    # via _shift_lt_offsets_to_original antes de virem pra UI.
    texto_limpo = re.sub(r"\*\*([^*]+)\*\*", r"\1", texto)
    lt_matches = []

    # ── PARTE 1: LanguageTool (ortografia/gramatica/pontuacao) ──
    try:
        import requests as _req
        r = _req.post(
            "https://api.languagetool.org/v2/check",
            data={
                "text": texto_limpo,
                "language": "pt-BR",
                "enabledOnly": "false",
            },
            timeout=20,
            headers={"User-Agent": "BearlzCMS/1.0"}
        )
        if r.status_code == 200:
            data_lt = r.json()
            for m in data_lt.get("matches", []):
                sug = [r.get("value", "") for r in m.get("replacements", [])][:5]
                lt_matches.append({
                    "offset": m.get("offset", 0),
                    "length": m.get("length", 0),
                    "message": m.get("message", ""),
                    "short": m.get("shortMessage", ""),
                    "suggestions": sug,
                    "category": (m.get("rule", {}) or {}).get("category", {}).get("name", ""),
                    "type": (m.get("rule", {}) or {}).get("issueType", ""),
                    "context": (m.get("context", {}) or {}).get("text", ""),
                })
    except Exception:
        pass  # Falha no LT nao bloqueia os vicios IA

    # Ajusta offsets dos matches do LT pro espaco do texto original (com **)
    lt_matches = _shift_lt_offsets_to_original(lt_matches, texto)

    # ── PARTE 2: Vicios de IA (cliches, travessao, negrito, etc) ──
    # Roda no texto ORIGINAL (com **) — offsets ja estao no espaco certo
    vicios = _detect_vicios_ia(texto)

    # Combina, ordena por offset pra UI mostrar em ordem
    matches = lt_matches + vicios
    matches.sort(key=lambda m: m.get("offset", 0))
    return jsonify({"ok": True, "matches": matches, "total": len(matches)})


@app.route("/api/img-proxy", methods=["GET"])
def api_img_proxy():
    """Proxy de imagem: baixa a URL externa e devolve com CORS aberto.
    Util pra imagens cujo servidor nao manda Access-Control-Allow-Origin,
    o que faz html2canvas tainted e o export pular a imagem.
    Uso: <img crossorigin="anonymous" src="/api/img-proxy?url=https://...">
    """
    url = (request.args.get("url") or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "url invalida"}), 400
    try:
        import requests as _req
        r = _req.get(url, timeout=15, stream=False,
                     headers={"User-Agent": _BROWSER_HEADERS["User-Agent"]})
        if r.status_code != 200:
            return jsonify({"error": f"upstream {r.status_code}"}), 502
        # Detecta content-type
        ct = r.headers.get("Content-Type", "image/jpeg")
        if not ct.startswith("image/"):
            return jsonify({"error": "nao eh imagem"}), 400
        # Resposta com CORS aberto pra html2canvas conseguir usar
        from flask import Response
        resp = Response(r.content, mimetype=ct)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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


def _gerar_conteudo_2fases(client, topico, brief_enriched, imagens_block,
                            num_slides, system_artigo):
    """Geracao em 2 fases (estilo Varos):
      FASE 1: escreve o ARTIGO corrido fluido (system_artigo / SYSTEM_ARTIGO)
      FASE 2: fatia o artigo em num_slides slides + escolhe imagens (SYSTEM_FATIAR)

    Retorna (titulo, slides_raw, artigo, legenda, hashtags, fase_debug).
    Levanta ValueError se alguma fase falhar de forma irrecuperavel."""
    # Tamanho-alvo do artigo: ~290-360 chars por slide (estilo Varos, denso).
    # Piso de seguranca pra nao gerar raso mesmo com poucos slides.
    min_chars = max(num_slides * 290, 900)
    max_chars = num_slides * 360
    sys_artigo = (system_artigo
                  .replace("{min_chars}", str(min_chars))
                  .replace("{max_chars}", str(max_chars))
                  .replace("{num_slides}", str(num_slides)))

    # ── FASE 1: ARTIGO CORRIDO ──
    prompt_artigo = (
        f"TÓPICO: {topico}\n\n"
        f"BRIEF/CONTEÚDO:\n{brief_enriched or topico}\n\n"
        f"Escreva o artigo completo ({min_chars}-{max_chars} caracteres) "
        "seguindo a arquitetura narrativa: abertura com paradoxo/detalhe "
        "humano inesperado, desenvolvimento que reconstrói o raciocínio com "
        "dados, fechamento com sentença filosófica. Texto corrido, fluido, "
        "SEM CTA. Inclua também legenda e hashtags. Retorne SOMENTE JSON."
    )
    # max_tokens generoso: artigo grande + legenda + hashtags
    resp1 = claude_call_with_retry(client,
        model="claude-sonnet-4-5", max_tokens=6000,
        system=sys_artigo,
        messages=[{"role": "user", "content": prompt_artigo}]
    )
    out1 = resp1.content[0].text.strip()
    if out1.startswith("```"):
        out1 = re.sub(r"^```[a-z]*\n?", "", out1)
        out1 = re.sub(r"\n?```$", "", out1).strip()
    dados1 = _parse_claude_json(out1)
    legenda = ""
    hashtags = []
    if dados1 and isinstance(dados1, dict) and dados1.get("artigo"):
        artigo = str(dados1.get("artigo", "")).strip()
        titulo = str(dados1.get("titulo") or topico[:40]).strip()
        legenda = str(dados1.get("legenda") or "").strip()
        ht = dados1.get("hashtags") or []
        if isinstance(ht, list):
            hashtags = [str(h).strip() for h in ht if str(h).strip()]
        elif isinstance(ht, str):
            # As vezes vem como string "#a #b #c"
            hashtags = [w for w in ht.split() if w.startswith("#")]
    else:
        # Fallback: Claude pode ter devolvido texto cru sem JSON. Usa como artigo.
        artigo = out1
        titulo = topico[:40]
    if not artigo or len(artigo) < 100:
        raise ValueError("Fase 1 (artigo) não gerou texto suficiente")

    # Limpa vicios no ARTIGO antes de fatiar e salvar — assim o artigo (que
    # vira a base de tudo) e os slides ficam consistentes e sem dois-pontos
    # de anuncio / travessao.
    artigo = _remover_dois_pontos_anuncio(_strip_em_dash(artigo))
    legenda = _remover_dois_pontos_anuncio(_strip_em_dash(legenda)) if legenda else legenda

    # ── FASE 2: FATIAR EM SLIDES ──
    sys_fatiar = SYSTEM_FATIAR.replace("{num_slides}", str(num_slides))
    prompt_fatiar = (
        f"ARTIGO PARA FATIAR (título: {titulo}):\n\n{artigo}\n"
        f"{imagens_block}\n"
        f"Corte em EXATAMENTE {num_slides} slides nos pontos de cliffhanger "
        "natural. NÃO reescreva, apenas corte e ilustre. Negrito cirúrgico "
        "(máx 1 por slide). Retorne SOMENTE JSON."
    )
    # max_tokens escala com o numero de slides: cada slide gera ~400-500
    # tokens de JSON (texto + chart_data + photo_topic). Pra 15-20 slides
    # 6000 estourava e truncava o JSON -> parse falhava. Damos folga.
    max_tok_fatiar = min(16000, 2500 + num_slides * 650)
    resp2 = claude_call_with_retry(client,
        model="claude-sonnet-4-5", max_tokens=max_tok_fatiar,
        system=sys_fatiar,
        messages=[{"role": "user", "content": prompt_fatiar}]
    )
    out2 = resp2.content[0].text.strip()
    if out2.startswith("```"):
        out2 = re.sub(r"^```[a-z]*\n?", "", out2)
        out2 = re.sub(r"\n?```$", "", out2).strip()
    dados2 = _parse_claude_json(out2)
    if not dados2 or not isinstance(dados2, dict) or not dados2.get("slides"):
        import sys as _sys
        print(f"[FATIAR-FAIL] num_slides={num_slides} max_tok={max_tok_fatiar} "
              f"out_len={len(out2)} tail={out2[-200:]!r}", file=_sys.stderr, flush=True)
        raise ValueError("Fase 2 (fatiar) não gerou slides válidos")
    slides_raw = dados2.get("slides", [])

    fase_debug = {
        "artigo": artigo,
        "artigo_chars": len(artigo),
        "legenda": legenda,
        "hashtags": hashtags,
        "prompt_artigo": prompt_artigo,
        "prompt_fatiar": prompt_fatiar,
        "min_chars": min_chars,
        "max_chars": max_chars,
    }
    return titulo, slides_raw, artigo, legenda, hashtags, fase_debug


@app.route("/api/gerar/system-prompt", methods=["GET"])
def api_gerar_system_prompt():
    """Retorna o system prompt da FASE 1 (escrita do artigo) pra UI editar.
    Eh o prompt que define o CONTEUDO/ESTILO. A fase 2 (fatiar) usa
    SYSTEM_FATIAR internamente e nao eh editavel pela UI.
    Substitui os placeholders {min_chars}/{max_chars} por valores default
    (base 11 slides) pra UI mostrar numeros reais em vez de '{min_chars}'."""
    preview = (SYSTEM_ARTIGO
               .replace("{min_chars}", str(11 * 290))
               .replace("{max_chars}", str(11 * 360))
               .replace("{num_slides}", "11"))
    return jsonify({"system": preview})


@app.route("/api/gerar", methods=["POST"])
def api_gerar():
    if not ANTHROPIC_AVAILABLE:
        return jsonify({"error": "Biblioteca anthropic não instalada. Rode: pip install anthropic"}), 400
    if not ANTHROPIC_API_KEY or not ANTHROPIC_API_KEY.startswith("sk-ant-api"):
        return jsonify({"error": "ANTHROPIC_API_KEY não configurada"}), 400

    data       = request.get_json() or {}
    topico     = data.get("topico", "").strip()
    brief      = data.get("brief", "").strip()
    # Usuario escolhe 1-20 slides (Instagram aceita ate 20). Default 11.
    num_slides = min(max(int(data.get("num_slides", 11)), 1), 20)
    # Override do system prompt vindo da UI (opcional). Se vazio, usa o default.
    system_override = (data.get("system_override") or "").strip()

    if not topico:
        return jsonify({"error": "Tópico obrigatório"}), 400

    # FASE 1 do system prompt: override da UI (se houver) ou SYSTEM_ARTIGO.
    # A FASE 2 (fatiar em slides) sempre usa SYSTEM_FATIAR internamente.
    system_artigo = system_override if system_override else SYSTEM_ARTIGO

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

    # prompt_para_debug eh preenchido dentro do try (vem de fase_debug)
    prompt_para_debug = ""
    try:
        client = _anthropic_lib.Anthropic(api_key=ANTHROPIC_API_KEY)
        # ══ GERACAO EM 2 FASES (estilo Varos): artigo corrido -> fatiar ══
        (titulo_gerado, slides_raw, artigo_gerado, legenda_gerada,
         hashtags_geradas, fase_debug) = _gerar_conteudo_2fases(
            client, topico, brief_enriched, imagens_block, num_slides, system_artigo
        )
        prompt_para_debug = fase_debug.get("prompt_artigo", "")
        if not slides_raw:
            return jsonify({
                "error": "Geração não produziu slides. Tente novamente.",
                "raw": (artigo_gerado or "")[:500]
            }), 500

        # Validacao de datas: detecta anos < 2026 sem contexto historico
        # (heuristica). NAO bloqueia geracao, devolve avisos_data pra UI
        # mostrar pro user revisar manualmente.
        avisos_data = _validar_datas_2026(slides_raw, ano_atual=2026)
        # CHECK-IN ANTI-INVENCAO: numeros nos slides que NAO aparecem no
        # material de origem (brief + links). Candidatos a dado alucinado.
        avisos_dados = _verificar_dados_inventados(slides_raw, brief_enriched)

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
            # GRAFICOS: nao geramos mais grafico sintetico (dados podiam ser
            # inventados pelo Claude). Grafico SO vem de imagem real de um link
            # que o usuario colou (via image_from_link, tratado acima). Se o
            # slide eh 'chart' mas nao tem imagem de link, cai pra foto e o
            # numero permanece no TEXTO do slide (que passa pelo check-in).
            # ── PRIORIDADE 2: foto. A FONTE depende do photo_source que o
            # Claude decidiu: 'real' = Wikimedia primeiro (foto real de
            # pessoa/lugar famoso), 'stock' = Pexels primeiro (conceito
            # abstrato). Isso evita o Wikimedia devolver imagem aleatoria
            # (rio, locomotiva) pra termo abstrato. Default: stock (mais seguro).
            if not img:
                photo_topic = (s.get("photo_topic") or "").strip()
                photo_topic_alt = (s.get("photo_topic_alt") or "").strip()
                photo_source = (s.get("photo_source") or "stock").strip().lower()
                queries = [q for q in [photo_topic, photo_topic_alt] if q]

                def _try_wikimedia():
                    for q in queries:
                        for w in (_wikimedia_search(q, n=5) or []):
                            if w["url"] not in used_pexels_ids:
                                used_pexels_ids.add(w["url"])
                                return w["url"]
                    return ""
                def _try_pexels():
                    for q in queries:
                        u = _pexels_search(q, used_pexels_ids)
                        if u:
                            return u
                    return ""

                if photo_source == "real":
                    # Nome proprio: Wikimedia primeiro, Pexels como fallback
                    img = _try_wikimedia() or _try_pexels()
                else:
                    # Conceito abstrato: Pexels primeiro, Wikimedia como fallback
                    img = _try_pexels() or _try_wikimedia()
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
            # Sanitiza texto no modo Varos: 1 paragrafo unico por slide
            texto_sanit = _sanitizar_slide_varos(s.get("texto", ""))
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
                          rf"\g<1>{len(slides_out)} slides · Gabriel Bearlz\g<2>", html)
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

            # CTA fixo removido (usuario nao usa o copy padrao). Os slides
            # gerados sao APENAS os de conteudo. Se quiser um slide de venda,
            # adiciona manualmente no editor.
            linhas = [
                f"  {{id:{i+1},text:`{_esc(s['texto'])}`,image:'{s['image_url']}',zoom:1,ox:50,oy:50}}"
                for i, s in enumerate(slides_out)
            ]
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

        # Register in DB. Guarda artigo (texto corrido), legenda (resumo) e
        # hashtags — tudo acessivel no viewer e na pagina de gerar.
        # Sanitiza a legenda (tira travessao, aspas simples, cliche de abertura).
        legenda_gerada = _sanitizar_legenda(legenda_gerada)
        hashtags_str = " ".join(hashtags_geradas) if hashtags_geradas else ""
        with get_db() as conn:
            conn.execute("""
                INSERT INTO carrosseis (slug, titulo, arquivo, num_slides, status,
                                        artigo, legenda, hashtags)
                VALUES (?, ?, ?, ?, 'rascunho', ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    titulo=excluded.titulo, arquivo=excluded.arquivo,
                    num_slides=excluded.num_slides, artigo=excluded.artigo,
                    legenda=excluded.legenda, hashtags=excluded.hashtags,
                    updated_at=datetime('now')
            """, (slug, titulo_gerado, nome, len(slides_out),
                  artigo_gerado, legenda_gerada, hashtags_str))

        return jsonify({
            "ok": True, "slug": slug, "titulo": titulo_gerado, "url": f"/c/{slug}",
            # Conteudo pro Instagram (mostrado na pagina de gerar):
            "artigo": artigo_gerado,
            "legenda": legenda_gerada,
            "hashtags": hashtags_geradas,
            # Avisos pra revisao manual:
            # - avisos_data: anos antigos detectados nos slides sem contexto
            # - avisos_dados: numeros sem respaldo no material de origem
            # - urls_fetched: cada URL tem is_instagram + is_stale + published_date
            "avisos_data": avisos_data,
            "avisos_dados": avisos_dados,
            "debug": {
                "system_used":      system_artigo,
                "user_prompt":      prompt_para_debug,
                "fluxo":            "2-fases (artigo + fatiar)",
                "artigo_gerado":    fase_debug.get("artigo", ""),
                "artigo_chars":     fase_debug.get("artigo_chars", 0),
                "prompt_fatiar":    fase_debug.get("prompt_fatiar", ""),
                "urls_fetched":     urls_info,
                "system_is_custom": bool(system_override),
                "model":            "claude-sonnet-4-5",
                "images_from_links": all_images,           # candidatas extraidas
                "images_used_from_links": sorted(used_link_indices),  # indices que viraram slide
                "pexels_api_active": bool(PEXELS_API_KEY),
            }
        })

    except ValueError as e:
        # Falha controlada de uma das fases (artigo vazio, slides invalidos)
        return jsonify({"error": f"Geração falhou: {e}. Tente novamente.",
                        "debug": {"system_used": system_artigo,
                                  "user_prompt": prompt_para_debug,
                                  "urls_fetched": urls_info}}), 500
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Resposta inválida do Claude: {e}",
                        "debug": {"system_used": system_artigo,
                                  "user_prompt": prompt_para_debug,
                                  "urls_fetched": urls_info}}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Gerador de 3 hooks (slide 1) por abordagem: Curiosidade / Dor / Promessa ───

def _extrair_todos_slides(slug: str):
    """Le o HTML salvo do carrossel e retorna lista de {idx, text}.
    Usado pelos endpoints de verificacao pra rodar checagem de dados
    em carrosseis ja gerados. Retorna None se carrossel nao existe ou
    HTML nao pode ser lido."""
    with get_db() as conn:
        row = conn.execute("SELECT arquivo, titulo FROM carrosseis WHERE slug=?", (slug,)).fetchone()
    if not row or not row["arquivo"]:
        return None
    html_path = _find_carrossel_file(row["arquivo"])
    if not html_path:
        return None
    try:
        html = html_path.read_text(encoding="utf-8")
    except Exception:
        return None
    arr_start = html.find("const slides=[")
    if arr_start == -1:
        return None
    arr_end = html.find("\n];", arr_start)
    if arr_end == -1:
        return None
    bloco = html[arr_start:arr_end]
    textos = re.findall(r"text:`([^`]*)`", bloco, flags=re.DOTALL)
    return [{"idx": i, "text": t.strip()} for i, t in enumerate(textos)]


@app.route("/api/verificar-texto", methods=["POST"])
def api_verificar_texto():
    """Verifica um texto livre: detecta anos antigos sem contexto historico
    + vicios de IA. Usado pelo painel /verificar pra checar conteudo antes
    de virar carrossel."""
    data = request.get_json() or {}
    texto = (data.get("text") or "").strip()
    if not texto:
        return jsonify({"error": "texto vazio"}), 400
    # Empacota como pseudo-slide pra reutilizar _validar_datas_2026
    avisos_data = _validar_datas_2026([{"text": texto}], ano_atual=2026)
    vicios = _detect_vicios_ia(texto)
    return jsonify({
        "ok": True,
        "avisos_data": avisos_data,
        "vicios_ia": vicios,
        "chars": len(texto),
    })


@app.route("/api/verificar-carrossel/<slug>", methods=["GET"])
def api_verificar_carrossel(slug):
    """Roda validacao de datas + vicios IA em cada slide de um carrossel
    ja gerado. Devolve: {slides: [{idx, text, avisos_data, vicios_ia}],
    total_avisos, titulo}."""
    slides = _extrair_todos_slides(slug)
    if slides is None:
        return jsonify({"error": "carrossel nao encontrado ou HTML invalido"}), 404
    with get_db() as conn:
        row = conn.execute("SELECT titulo FROM carrosseis WHERE slug=?", (slug,)).fetchone()
    titulo = row["titulo"] if row else slug
    # Valida em batch usando a funcao existente (que ja indexa por slide)
    avisos_data_global = _validar_datas_2026(slides, ano_atual=2026)
    # Indexa avisos por slide pra UI mostrar agrupado
    avisos_por_slide = {}
    for a in avisos_data_global:
        avisos_por_slide.setdefault(a["slide"], []).append(a)
    # Roda vicios IA por slide
    out = []
    total_avisos = len(avisos_data_global)
    total_vicios = 0
    for s in slides:
        idx = s["idx"]
        vicios = _detect_vicios_ia(s["text"])
        total_vicios += len(vicios)
        out.append({
            "idx": idx,
            "text": s["text"],
            "avisos_data": avisos_por_slide.get(idx + 1, []),
            "vicios_ia": vicios,
        })
    return jsonify({
        "ok": True,
        "slug": slug,
        "titulo": titulo,
        "slides": out,
        "total_avisos_data": total_avisos,
        "total_vicios": total_vicios,
        "n_slides": len(slides),
    })


@app.route("/api/verificar-todos", methods=["GET"])
def api_verificar_todos():
    """Varre todos os carrosseis cadastrados e retorna resumo:
    {carrosseis: [{slug, titulo, status, n_avisos_data, n_vicios, n_slides}]}.
    Usado pelo dashboard /verificar pra mostrar overview rapido de
    quais carrosseis precisam de revisao."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT slug, titulo, status, created_at FROM carrosseis ORDER BY created_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        slug = r["slug"]
        slides = _extrair_todos_slides(slug)
        if slides is None:
            out.append({
                "slug": slug, "titulo": r["titulo"], "status": r["status"],
                "n_avisos_data": 0, "n_vicios": 0, "n_slides": 0,
                "indisponivel": True,
                "created_fmt": fmt_data(r["created_at"]),
            })
            continue
        avisos = _validar_datas_2026(slides, ano_atual=2026)
        n_vicios = 0
        for s in slides:
            n_vicios += len(_detect_vicios_ia(s["text"]))
        out.append({
            "slug": slug, "titulo": r["titulo"], "status": r["status"],
            "n_avisos_data": len(avisos),
            "n_vicios": n_vicios,
            "n_slides": len(slides),
            "created_fmt": fmt_data(r["created_at"]),
        })
    return jsonify({"ok": True, "carrosseis": out})


@app.route("/verificar")
def pagina_verificar():
    """Painel de verificacao de dados — checa carrosseis existentes
    + permite testar texto livre antes de gerar."""
    return render_template("verificar.html")


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

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


def load_anthropic_key():
    global ANTHROPIC_API_KEY
    if ANTHROPIC_API_KEY and ANTHROPIC_API_KEY.startswith("sk-ant-api"):
        return
    try:
        config_path = Path(__file__).parent.parent / "idea-bot" / "config.py"
        if config_path.exists():
            ns = {}
            exec(config_path.read_text(encoding="utf-8"), ns)
            key = ns.get("ANTHROPIC_API_KEY", "")
            if key.startswith("sk-ant-api"):
                ANTHROPIC_API_KEY = key
    except:
        pass


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

def fmt_data(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
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

SYSTEM_REVISAO = """Você é editor sênior de carrosseis para @gabriel.bearlz no Instagram.
Receberá os slides atuais de um carrossel e instruções de revisão. Sua tarefa é aplicar as correções mantendo o estilo e estrutura.

VOZ E LINGUAGEM — REGRAS ABSOLUTAS:
- Parágrafos de 2-3 linhas com estrutura de argumento: premissa → consequência
- Conectores naturais obrigatórios: "Com isso,", "Só que,", "O que acontece é que,", "Na prática,"
- Estruturas preferidas: "Se X, então Y" / "Enquanto todos olham para A, o verdadeiro risco é B"
- NUNCA use travessão (—) em hipótese alguma — regra absoluta inegociável
- NUNCA use frases picotadas estilo IA: "Queda. Recuperação. Oportunidade." — proibido
- NUNCA use palavras de enchimento: "é importante ressaltar", "vale destacar", "é fundamental", "cabe destacar"
- Tom: analítico, assertivo, levemente provocador — analista que vê o que outros não veem
- Sem emoji, sem hashtag
- Números e datas sempre com formatação brasileira (vírgula decimal, % colado ao número)

NEGRITOS — use de 2 a 4 por slide:
- Negrite palavras-chave, números importantes e expressões de impacto (2 a 6 palavras)
- Pode incluir dados numéricos em negrito: **9%**, **R$ 1,2 trilhão**, **maior alta em 10 anos**
- NUNCA negrite frases longas, períodos inteiros ou parágrafos completos

TAMANHO: 280 a 420 caracteres por slide. Se um ponto exige mais espaço, DIVIDA em 2 slides. NUNCA comprima — divida.

RETORNE SOMENTE JSON VÁLIDO:
{"slides": ["texto do slide 1", "texto do slide 2", ...]}

Se precisar dividir um slide, adicione o novo texto como elemento extra no array.
Mantenha o número de slides igual ao original, a menos que as instruções peçam para dividir ou remover."""


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
        resp   = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=5000,
            system=SYSTEM_REVISAO,
            messages=[{"role": "user", "content": prompt}]
        )
        texto = resp.content[0].text.strip()
        if texto.startswith("```"):
            texto = re.sub(r"^```[a-z]*\n?", "", texto)
            texto = re.sub(r"\n?```$", "", texto).strip()

        alteracoes = json.loads(texto)  # {"1": "novo texto", "2": "igual", ...}

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
            novo_texto = alteracoes[chave]
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


# ── Generator ─────────────────────────────────────────────────────────────────

@app.route("/gerar")
def pagina_gerar():
    has_key = bool(ANTHROPIC_API_KEY and ANTHROPIC_API_KEY.startswith("sk-ant-api"))
    return render_template("gerar.html", has_key=has_key)


@app.route("/api/gerar", methods=["POST"])
def api_gerar():
    if not ANTHROPIC_AVAILABLE:
        return jsonify({"error": "Biblioteca anthropic não instalada. Rode: pip install anthropic"}), 400
    if not ANTHROPIC_API_KEY or not ANTHROPIC_API_KEY.startswith("sk-ant-api"):
        return jsonify({"error": "ANTHROPIC_API_KEY não configurada"}), 400

    data       = request.get_json() or {}
    topico     = data.get("topico", "").strip()
    brief      = data.get("brief", "").strip()
    num_slides = min(max(int(data.get("num_slides", 8)), 4), 14)

    if not topico:
        return jsonify({"error": "Tópico obrigatório"}), 400

    SYSTEM = (
        "Você é redator sênior de conteúdo financeiro para @gabriel.bearlz no Instagram.\n"
        "Estilo: thread do Twitter/X analítico. Público: investidores brasileiros, 25-45 anos.\n\n"

        "VOZ E LINGUAGEM — REGRAS ABSOLUTAS:\n"
        "- Parágrafos de 2-3 linhas com estrutura de argumento: premissa → consequência\n"
        "- Conectores naturais obrigatórios: 'Com isso,', 'Só que,', 'O que acontece é que,', 'Na prática,'\n"
        "- Estruturas preferidas: 'Se X, então Y' / 'Enquanto todos olham para A, o verdadeiro risco é B'\n"
        "- NUNCA use travessão (—) em hipótese alguma — regra absoluta inegociável\n"
        "- NUNCA use frases picotadas estilo IA: 'Queda. Recuperação. Oportunidade.' — proibido\n"
        "- NUNCA use palavras de enchimento: 'é importante ressaltar', 'vale destacar', 'é fundamental', 'cabe destacar'\n"
        "- Tom: analítico, assertivo, levemente provocador — analista que vê o que outros não veem\n"
        "- Sem emoji, sem hashtag\n"
        "- Números e datas sempre com formatação brasileira (vírgula decimal, % colado ao número)\n\n"

        "NEGRITOS — use de 2 a 4 por slide:\n"
        "- Negrite palavras-chave, números importantes e expressões de impacto (2 a 6 palavras)\n"
        "- Pode incluir dados numéricos em negrito: **9%**, **R$ 1,2 trilhão**, **maior alta em 10 anos**\n"
        "- NUNCA negrite frases longas, períodos inteiros ou parágrafos completos\n\n"

        "TAMANHO: 280 a 420 caracteres por slide. "
        "Se um ponto exige mais espaço, DIVIDA em 2 slides. NUNCA comprima — divida.\n\n"

        "ESTRUTURA DO CARROSSEL:\n"
        "- Slide 1: Hook forte — afirmação provocadora ou dado surpreendente que prende atenção\n"
        "- Slides intermediários: desenvolvimento com dados concretos, causa-efeito, comparações\n"
        "- Slide final: implicação prática para o investidor brasileiro\n\n"

        "IMAGENS — para cada slide escolha image_type:\n"
        "- 'chart': slides com dados numéricos comparáveis (inclua chart_data com labels, values, unit, highlight)\n"
        "  chart_type: 'bar' (comparação entre categorias), 'horizontal_bar' (rankings), 'line' (evolução temporal)\n"
        "  highlight: true no ponto mais importante do gráfico\n"
        "- 'photo': slides de contexto, hook ou implicação (inclua photo_topic em inglês específico e visual)\n"
        "  photo_topic deve ser descritivo: 'US dollar bills close-up' NÃO 'money'\n\n"

        "RETORNE SOMENTE JSON VÁLIDO, sem markdown, sem texto fora do JSON:\n"
        '{"titulo":"...","slides":[{"texto":"...","tema":"bitcoin|economia|mercado|geopolitica|ia|tecnologia",'
        '"image_type":"chart|photo","chart_title":"...","chart_type":"bar|horizontal_bar|line",'
        '"chart_data":[{"label":"...","value":0,"unit":"%","highlight":false}],'
        '"photo_topic":"..."}]}'
    )

    prompt = (
        f"Crie exatamente {num_slides} slides sobre:\n\n"
        f"TÓPICO: {topico}\n"
        f"CONTEÚDO/BRIEF: {brief or topico}\n\n"
        f"Slide 1: Hook. Slides 2-{num_slides-1}: desenvolvimento com dados. "
        f"Slide {num_slides}: implicação para o investidor.\n"
        "Retorne SOMENTE JSON válido."
    )

    try:
        client = _anthropic_lib.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp   = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=6000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        texto = resp.content[0].text.strip()
        if texto.startswith("```"):
            texto = re.sub(r"^```[a-z]*\n?", "", texto)
            texto = re.sub(r"\n?```$", "", texto).strip()

        dados        = json.loads(texto)
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
        for i, s in enumerate(slides_raw):
            itype = s.get("image_type", "photo").lower()
            img   = ""
            if itype == "chart":
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
            if not img:
                tema = s.get("tema", "default").lower()
                ids  = PEXELS_FALLBACK.get(tema, ["4386469", "6770610", "5831251"])
                fid  = ids[i % len(ids)]
                img  = f"{PX}{fid}/pexels-photo-{fid}.jpeg{Q}"
            slides_out.append({"texto": s.get("texto",""), "image_url": img})

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

        return jsonify({"ok": True, "slug": slug, "titulo": titulo_gerado, "url": f"/c/{slug}"})

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Resposta inválida do Claude: {e}"}), 500
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

        "REGRAS INEGOCIÁVEIS:\n"
        "- 280 a 420 caracteres por hook\n"
        "- 2 a 4 negritos (**palavra**) com palavras-chave, números ou expressões de impacto\n"
        "- NUNCA use travessão (—) em hipótese alguma\n"
        "- Sem frases picotadas estilo IA: 'Queda. Alta. Oportunidade.' é proibido\n"
        "- Sem enchimento: 'vale destacar', 'é importante ressaltar', 'cabe destacar' proibidos\n"
        "- Sem emoji, sem hashtag\n"
        "- Conectores naturais: 'Com isso,', 'Só que,', 'O que acontece é que,', 'Na prática,'\n"
        "- Números em formatação brasileira (vírgula decimal, % colado)\n\n"

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
        resp = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=2500,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        texto = resp.content[0].text.strip()
        if texto.startswith("```"):
            texto = re.sub(r"^```[a-z]*\n?", "", texto)
            texto = re.sub(r"\n?```$", "", texto).strip()
        dados = json.loads(texto)
        variantes = dados.get("variantes", [])
        if not isinstance(variantes, list) or len(variantes) != 3:
            return jsonify({"error": "Claude não retornou 3 variantes"}), 500
        return jsonify({"ok": True, "variantes": variantes})
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Resposta inválida do Claude: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Static (css/js se precisar de arquivos extras) ─────────────────────────────
@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(BASE_DIR / "static", filename)


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

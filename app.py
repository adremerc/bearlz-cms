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
            ("api_usage",       "TEXT"),  # JSON: tokens/buscas/custo da geracao
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


# ── Modelo do gerador + custo por post ───────────────────────────────────────
# O gerador roda no Sonnet 4.5 (decisao do Adre, 02/07/2026: o resultado do
# Opus 4.8 a US$ 1,49/post nao compensou — os posts pra valer sao feitos a mao
# na sessao do Claude Code; o gerador e so rascunho barato). Ele mantem o web
# search server-side (verifica dados antes de escrever) e a fase 2.5 de
# correcao. Override via env GERAR_MODEL se quiser trocar.
GERAR_MODEL = os.environ.get("GERAR_MODEL", "claude-sonnet-4-5")

# USD por 1M tokens (input, output). Cache: read = 0.1x input, write = 1.25x.
# Web search: US$ 10 por 1.000 buscas.
MODEL_PRICES = {
    "claude-opus-4-8":   (5.00, 25.00),
    "claude-opus-4-7":   (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5":  (1.00, 5.00),
}

def _novo_usage():
    return {"input_tokens": 0, "output_tokens": 0, "cache_read": 0,
            "cache_write": 0, "web_searches": 0, "calls": 0}

def _acumular_usage(acc, resp):
    """Soma o usage de uma resposta da API no acumulador (custo por post)."""
    if acc is None or resp is None:
        return
    try:
        u = resp.usage
        acc["input_tokens"]  += getattr(u, "input_tokens", 0) or 0
        acc["output_tokens"] += getattr(u, "output_tokens", 0) or 0
        acc["cache_read"]    += getattr(u, "cache_read_input_tokens", 0) or 0
        acc["cache_write"]   += getattr(u, "cache_creation_input_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        if stu is not None:
            acc["web_searches"] += getattr(stu, "web_search_requests", 0) or 0
        acc["calls"] += 1
    except Exception:
        pass

def _custo_usd(acc, model=None):
    """Custo estimado (USD) do acumulador de usage."""
    pin, pout = MODEL_PRICES.get(model or GERAR_MODEL, (5.00, 25.00))
    custo = (acc["input_tokens"] * pin + acc["output_tokens"] * pout
             + acc["cache_read"] * pin * 0.1 + acc["cache_write"] * pin * 1.25) / 1e6
    custo += acc["web_searches"] * 0.01
    return round(custo, 4)

def _extrair_texto_resp(resp):
    """Concatena so os blocos de texto (ignora thinking/tool_use/resultados)."""
    return "".join(
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()

def _claude_fase1_com_busca(client, usage_acc, **params):
    """FASE 1 com web search server-side: o modelo pesquisa e confirma os
    dados ANTES de escrever. Trata stop_reason=pause_turn (o loop de busca do
    servidor pausa a cada ~10 iteracoes) re-enviando a conversa. Retorna o
    texto final da resposta."""
    model = params.get("model", GERAR_MODEL)
    max_searches = params.pop("max_searches", 6)
    # Modelos 4.6+ tem a variante nova (filtro dinamico); 4.5 so a basica.
    tool_type = "web_search_20250305" if "4-5" in model else "web_search_20260209"
    params["tools"] = [{"type": tool_type, "name": "web_search", "max_uses": max_searches}]
    if "4-5" not in model:
        # Adaptive thinking melhora planejamento/escrita (nao suportado no 4.5)
        params.setdefault("thinking", {"type": "adaptive"})
    msgs = list(params.pop("messages"))
    resp = None
    for _ in range(8):
        resp = claude_call_with_retry(client, messages=msgs, **params)
        _acumular_usage(usage_acc, resp)
        if getattr(resp, "stop_reason", "") != "pause_turn":
            break
        # Servidor pausou no meio das buscas: re-envia com o parcial e continua
        msgs = msgs + [{"role": "assistant", "content": resp.content}]
    return _extrair_texto_resp(resp)


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
    "nao_publicado": ("Não Publicado",   "red"),
}


# ── Admin: historico do estado salvo (via branch GitHub data-generated) ───────

def _admin_check_key():
    return request.headers.get("X-Admin-Key", "") == CMS_API_KEY

@app.route("/api/_admin/editar-textos/<slug>", methods=["POST"])
def api_admin_editar_textos(slug):
    """Substitui SO os textos dos slides no HTML do carrossel, preservando
    imagens, zoom, profile e tudo mais. Body: {textos:["t1","t2",...]} na
    ordem dos slides. Aplica a sanitizacao Varos (respiro + anti-vicio).
    Tambem apaga edits salvos pra a nova versao aparecer. Requer X-Admin-Key."""
    if not _admin_check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    textos = data.get("textos") or []
    if not isinstance(textos, list) or not textos:
        return jsonify({"error": "textos (lista) obrigatorio"}), 400
    with get_db() as conn:
        row = conn.execute("SELECT arquivo FROM carrosseis WHERE slug=?", (slug,)).fetchone()
    if not row or not row["arquivo"]:
        return jsonify({"error": "carrossel nao encontrado"}), 404
    html_path = _find_carrossel_file(row["arquivo"])
    if not html_path:
        return jsonify({"error": "HTML nao encontrado"}), 404
    html = html_path.read_text(encoding="utf-8")
    arr_start = html.find("const slides=[")
    arr_end = html.find("\n];", arr_start)
    if arr_start == -1 or arr_end == -1:
        return jsonify({"error": "array de slides nao encontrado"}), 500
    bloco = html[arr_start:arr_end]

    def _esc(t):
        return t.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${").replace("\n", "\\n")

    # Substitui o N-esimo text:`...` pelo texto[N] sanitizado, mantendo o resto
    contador = {"i": 0}
    def _repl(m):
        i = contador["i"]; contador["i"] += 1
        if i < len(textos):
            novo = _sanitizar_slide_varos(str(textos[i]))
            return "text:`" + _esc(novo) + "`"
        return m.group(0)
    bloco_novo = re.sub(r"text:`(?:[^`\\]|\\.)*`", _repl, bloco, flags=re.DOTALL)
    html = html[:arr_start] + bloco_novo + html[arr_end:]
    html_path.write_text(html, encoding="utf-8")
    _gh_save_async(f"data/generated/{html_path.name}", html.encode("utf-8"),
                   f"Edicao manual de textos: {slug}")
    # Remove edits salvos (state) pra a nova versao do HTML aparecer
    try:
        ep = _edits_path(slug)
        if ep.exists():
            ep.unlink()
    except Exception:
        pass
    with get_db() as conn:
        conn.execute("UPDATE carrosseis SET updated_at=datetime('now') WHERE slug=?", (slug,))
    return jsonify({"ok": True, "substituidos": min(contador["i"], len(textos))})


@app.route("/api/_admin/editar-meta/<slug>", methods=["POST"])
def api_admin_editar_meta(slug):
    """Atualiza titulo/artigo/legenda/hashtags de um carrossel no DB. Permite
    manter o artigo IDENTICO ao texto dos slides (fonte unica: artigo fatiado)
    e renomear o post (titulo aparece no card do dashboard).
    Body JSON: {titulo?, artigo?, legenda?, hashtags?} (str). Requer X-Admin-Key."""
    if not _admin_check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    campos = {k: data[k] for k in ("titulo", "artigo", "legenda", "hashtags")
              if k in data and isinstance(data[k], str)}
    if not campos:
        return jsonify({"error": "nada pra atualizar (titulo/artigo/legenda/hashtags)"}), 400
    sets = ", ".join(f"{k}=?" for k in campos)
    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE carrosseis SET {sets}, updated_at=datetime('now') WHERE slug=?",
            (*campos.values(), slug)
        )
        if cur.rowcount == 0:
            return jsonify({"error": "slug nao encontrado"}), 404
    return jsonify({"ok": True, "campos": sorted(campos)})


@app.route("/api/_admin/criar-shell", methods=["POST"])
def api_admin_criar_shell():
    """Cria um carrossel COMPLETO a mao, SEM chamar LLM. Injeta titulo, slug,
    subtitulo e os SLIDES direto no HTML do template (mesmas substituicoes que
    o /api/gerar faz), pra o post renderizar certo no viewer publico. Body:
    {titulo, slides:[{text, image?}], num_slides?, slug?}. Se 'slides' vier,
    vira o conteudo do post; senao cria shell vazio. Se 'slug' vier (de um
    post ja existente), sobrescreve o HTML desse slug no lugar, sem passar
    pelo sanitizador do editar-textos (preserva negrito/paragrafos exatos).
    Requer X-Admin-Key."""
    if not _admin_check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    titulo = (data.get("titulo") or "").strip()
    if not titulo:
        return jsonify({"error": "titulo obrigatorio"}), 400
    slides_in = data.get("slides") if isinstance(data.get("slides"), list) else []
    n = len(slides_in) if slides_in else min(max(int(data.get("num_slides", 15)), 1), 20)
    slug_override = re.sub(r"[^a-zA-Z0-9_\-]", "", (data.get("slug") or "").strip())
    if slug_override:
        slug = slug_override
    else:
        # slug igual ao do gerador (acentos viram '-'), com data de hoje
        slug_base = re.sub(r"[^a-z0-9]+", "-", titulo.lower())[:40].strip("-") or "post"
        from datetime import date as _d
        slug = f"{slug_base}-{_d.today().strftime('%Y%m%d')}"
    nome = f"carrossel-{slug}.html"
    template_path = CARROSSEIS_DIR / "carrossel-carga-tributaria.html"
    if not template_path.exists():
        return jsonify({"error": "template ausente"}), 500
    html = template_path.read_text(encoding="utf-8")
    # Mesmas substituicoes do /api/gerar (titulo, h1, subtitulo, slug JS, LS key)
    html = re.sub(r"<title>.*?</title>",
                  f"<title>Carrossel — {titulo} | Gabriel Bearlz</title>", html, flags=re.DOTALL)
    html = re.sub(r"(<h1>).*?(</h1>)", rf"\g<1>{titulo}\g<2>", html, flags=re.DOTALL)
    html = re.sub(r'(<p id="subtitle">).*?(</p>)',
                  rf"\g<1>{n} slides · Gabriel Bearlz\g<2>", html, flags=re.DOTALL)
    html = re.sub(r"window\.CAROUSEL_SLUG='[^']*'",
                  f"window.CAROUSEL_SLUG='{slug}'", html)
    ls_key_slug = slug.replace("-", "_")
    html = re.sub(r"window\.CAROUSEL_LS_KEY='[^']*'",
                  f"window.CAROUSEL_LS_KEY='bearlz_{ls_key_slug}_v1'", html)
    html = re.sub(r"(?<!window\.CAROUSEL_)LS_KEY='[^']*'",
                  f"LS_KEY='bearlz_{ls_key_slug}_v1'", html)
    # Injeta o array de slides no HTML (assim o viewer publico ja mostra o
    # conteudo certo, sem depender so do editor-state).
    if slides_in:
        def _esc(t):
            return (str(t).replace("\\", "\\\\").replace("`", "\\`")
                    .replace("${", "\\${").replace("\n", "\\n"))
        linhas = ["  {id:%d,text:`%s`,image:'%s',zoom:1,ox:50,oy:50}" % (
            i + 1, _esc(s.get("text", "")), (s.get("image") or "")) for i, s in enumerate(slides_in)]
        novo_array = "const slides=[\n" + ",\n".join(linhas) + "\n];"
        html = re.sub(r"const slides=\[.*?\];", lambda _: novo_array, html, flags=re.DOTALL)
    GENERATED_DIR.mkdir(exist_ok=True)
    (GENERATED_DIR / nome).write_text(html, encoding="utf-8")
    # Se ja existia um edit salvo desse slug (post antigo), apaga pra nao
    # sobrescrever o HTML novo com estado velho.
    ep = _edits_path(slug)
    if ep.exists() and not slides_in:
        try: ep.unlink()
        except Exception: pass
    _gh_save_async(f"data/generated/{nome}", html.encode("utf-8"), f"Post manual: {titulo}")
    with get_db() as conn:
        conn.execute("""
            INSERT INTO carrosseis (slug, titulo, arquivo, num_slides, status)
            VALUES (?, ?, ?, ?, 'rascunho')
            ON CONFLICT(slug) DO UPDATE SET
                titulo=excluded.titulo, arquivo=excluded.arquivo,
                num_slides=excluded.num_slides, updated_at=datetime('now')
        """, (slug, titulo, nome, n))
    return jsonify({"ok": True, "slug": slug, "url": f"/c/{slug}", "num_slides": n})


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
    busca         = request.args.get("q", "").strip()

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
            "nao_publicado":   conn.execute("SELECT COUNT(*) FROM carrosseis WHERE status='nao_publicado'").fetchone()[0],
        }

    carrosseis = [dict(r) for r in rows]
    if busca:
        b = busca.lower()
        # (titulo or "") evita 500 se alguma linha tiver titulo NULL
        carrosseis = [c for c in carrosseis if b in (c["titulo"] or "").lower()]

    for c in carrosseis:
        c["status_label"], c["status_color"] = STATUS_LABELS.get(c["status"], ("?", "gray"))
        c["prio_label"],   c["prio_color"]   = PRIO_LABELS.get(c.get("prioridade","media"), ("Média","yellow"))
        c["created_fmt"]  = fmt_data(c["created_at"])
        c["tempo_fmt"]    = fmt_tempo(c.get("tempo_revisao") or 0)
        # Custo da geracao via API (JSON gravado pelo /api/gerar)
        c["custo_fmt"], c["custo_title"] = "", ""
        try:
            au = json.loads(c.get("api_usage") or "null") or {}
            cu = au.get("custo_usd")
            if cu:
                c["custo_fmt"] = "US$ %.2f" % cu
                c["custo_title"] = (
                    "%s tokens in / %s out · %s buscas web · %s" % (
                        f"{au.get('input_tokens', 0):,}".replace(",", "."),
                        f"{au.get('output_tokens', 0):,}".replace(",", "."),
                        au.get("web_searches", 0),
                        au.get("model", ""),
                    ))
        except Exception:
            pass

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
        # Mantem num_slides em sincronia com o editor: sem isso o card do
        # dashboard mostra a contagem da geracao original pra sempre (ex.:
        # "13 slides" num post que foi editado pra 15).
        slides_novos = state.get("slides")
        with get_db() as conn:
            if isinstance(slides_novos, list) and slides_novos:
                conn.execute(
                    "UPDATE carrosseis SET updated_at=datetime('now'), num_slides=? WHERE slug=?",
                    (len(slides_novos), slug)
                )
            else:
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


# ── Blog (teste Mercurius): renderiza o post como análise textual ─────────────

_MESES_PT = ["janeiro", "fevereiro", "março", "abril", "maio", "junho",
             "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]


def _blog_data_fmt(iso):
    """'2026-07-09 13:17:48' -> '9 de julho de 2026'."""
    try:
        d = datetime.fromisoformat(str(iso)[:19])
        return f"{d.day} de {_MESES_PT[d.month - 1]} de {d.year}"
    except Exception:
        return str(iso or "")[:10]


def _blog_slide_html(texto):
    """Converte o texto de um slide (com **negrito**, • e →) em HTML de blog."""
    import html as _htmlmod

    def _fmt(s):
        s = _htmlmod.escape(s, quote=False)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)

    out = []
    for block in texto.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if any(re.match(r"^\s*[•→]", ln) for ln in lines):
            prosa, itens = [], []
            for ln in lines:
                if re.match(r"^\s*[•→]", ln):
                    itens.append(re.sub(r"^\s*[•→]\s*", "", ln).strip())
                else:
                    prosa.append(ln.strip())
            if prosa:
                out.append("<p>%s</p>" % _fmt(" ".join(prosa)))
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % _fmt(i) for i in itens))
        else:
            txt = " ".join(ln.strip() for ln in lines)
            m = re.fullmatch(r"\*\*(.+)\*\*", txt)
            if m and "**" not in m.group(1):
                # Parágrafo inteiro em negrito vira bloco de destaque (estilo Nord)
                out.append('<p class="destaque">%s</p>'
                           % _htmlmod.escape(m.group(1), quote=False))
            else:
                out.append("<p>%s</p>" % _fmt(txt))
    return "\n".join(out)


def _slides_do_arquivo(arquivo):
    """Extrai os slides (texto rico + imagem) do `const slides=[...]` do HTML
    do carrossel. Fallback pra post que nunca foi salvo no editor: assim o
    blog sai rico (negrito/bullets/capa) direto da geracao automatica."""
    if not arquivo:
        return None
    path = _find_carrossel_file(arquivo)
    if not path:
        return None
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r"const slides=\[(.*?)\];", html, re.DOTALL)
    if not m:
        return None
    slides = []
    # image vem como 'url' (gerados/criar-shell), PX+'id/foto.jpeg'+Q
    # (template antigo) ou uma variavel JS tipo chartJuros (grafico embutido
    # do template — vira imagem vazia, o corpo do blog so usa a capa).
    for tm in re.finditer(
            r"\{id:\d+\s*,\s*text:`((?:\\.|[^`\\])*)`\s*,\s*"
            r"image:\s*(?:(PX\+)?'([^']*)'|[A-Za-z_$][\w$]*)",
            m.group(1), re.DOTALL):
        # Desfaz o escape do gerador: \\ -> \, \` -> `, \$ -> $, \n -> quebra
        txt = re.sub(r"\\(.)", lambda e: "\n" if e.group(1) == "n" else e.group(1),
                     tm.group(1))
        img = tm.group(3) or ""
        if tm.group(2):
            img = ("https://images.pexels.com/photos/" + img
                   + "?auto=compress&cs=tinysrgb&w=1080")
        slides.append({"text": txt, "image": img})
    return slides or None


def _blog_carregar(slug):
    """Retorna (row, slides): slides do editor se houver, senao do HTML."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM carrosseis WHERE slug = ?", (slug,)).fetchone()
    if not row:
        return None, None
    slides = None
    p = _edits_path(slug)
    if p.exists():
        try:
            st = json.loads(p.read_text(encoding="utf-8")).get("state") or {}
            if isinstance(st.get("slides"), list) and st["slides"]:
                slides = st["slides"]
        except Exception:
            pass
    if not slides:
        slides = _slides_do_arquivo(dict(row).get("arquivo"))
    return dict(row), slides


def _blog_capa(arquivo, slides, permitir_data=False):
    """Capa do post: imagem http do slide 1; se o editor salvou base64,
    cai pra primeira imagem http do arquivo HTML (base64 de 50-100 KB por
    card deixaria o /blog com dezenas de MB)."""
    img = (slides[0].get("image") or "") if slides else ""
    if img.startswith("http"):
        return img
    if permitir_data and img.startswith("data:"):
        return img
    if arquivo:
        path = _find_carrossel_file(arquivo)
        if path:
            try:
                m = re.search(r"image:\s*'(https?://[^']+)'",
                              path.read_text(encoding="utf-8", errors="replace"))
                if m:
                    return m.group(1)
            except Exception:
                pass
    return ""


@app.route("/blog")
def blog_index():
    # Porteiro: so entra no blog o que o Gabriel ja aprovou (ou publicou).
    # /blog?todos=1 mostra tudo (preview interno antes da aprovacao).
    mostrar_todos = request.args.get("todos") == "1"
    with get_db() as conn:
        rows = conn.execute(
            "SELECT slug, titulo, legenda, artigo, arquivo, status, created_at "
            "FROM carrosseis ORDER BY created_at DESC"
        ).fetchall()
    posts = []
    for r in rows:
        r = dict(r)
        if not (r.get("artigo") or "").strip():
            continue  # so entra no blog quem tem analise completa
        if not mostrar_todos and r.get("status") not in ("aprovado", "publicado"):
            continue
        slides = None
        p = _edits_path(r["slug"])
        if p.exists():
            try:
                st = json.loads(p.read_text(encoding="utf-8")).get("state") or {}
                if isinstance(st.get("slides"), list) and st["slides"]:
                    slides = st["slides"]
            except Exception:
                pass
        posts.append({
            "slug": r["slug"], "titulo": r["titulo"],
            "capa": _blog_capa(r.get("arquivo"), slides),
            "resumo": (r.get("legenda") or "")[:220],
            "data_fmt": _blog_data_fmt(r["created_at"]),
        })
    return render_template("blog_index.html", posts=posts)


@app.route("/blog/<slug>")
def blog_post(slug):
    row, slides = _blog_carregar(slug)
    if not row:
        abort(404)
    if slides:
        corpo = "\n".join(_blog_slide_html(s.get("text") or "") for s in slides)
        capa = _blog_capa(row.get("arquivo"), slides, permitir_data=True)
    elif (row.get("artigo") or "").strip():
        corpo = "\n".join("<p>%s</p>" % par.strip()
                          for par in row["artigo"].split("\n\n") if par.strip())
        capa = ""
    else:
        abort(404)
    tags = [t for t in (row.get("hashtags") or "").split() if t.startswith("#")]
    categoria = tags[0].lstrip("#") if tags else ""
    palavras = len(re.sub(r"<[^>]+>", " ", corpo).split())
    leitura = max(3, round(palavras / 180))
    return render_template(
        "blog.html",
        titulo=row["titulo"],
        legenda=row.get("legenda") or "",
        corpo_html=corpo,
        capa=capa,
        tags=tags,
        categoria=categoria,
        data_fmt=_blog_data_fmt(row["created_at"]),
        leitura=leitura,
        foto_autor="",  # trocar pela foto real do Gabriel quando tivermos a URL
    )


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


def _extrair_campos_artigo(texto: str):
    """Fallback robusto pro JSON da FASE 1 quando _parse_claude_json falha
    (tipico: aspas duplas NAO escapadas no meio da prosa — slogans, falas —
    que quebram o json.loads). Extrai titulo/artigo/legenda/hashtags por
    DELIMITADOR de chave, tolerante a aspas internas. Os marcadores
    ('","legenda":', '","hashtags":') praticamente nao aparecem em prosa,
    entao o corte e confiavel. Retorna dict ou None se nem o artigo achar."""
    if not texto:
        return None

    def _campo(chave, prox):
        m = re.search(r'"' + chave + r'"\s*:\s*"(.*?)"\s*,\s*"' + prox + r'"',
                      texto, re.DOTALL)
        if not m:
            return None
        v = m.group(1)
        # desescapa o que o modelo escapou; mantem aspas internas como aspas
        return (v.replace('\\n', '\n').replace('\\t', '\t')
                 .replace('\\"', '"').replace('\\\\', '\\')).strip()

    artigo = _campo('artigo', 'legenda') or _campo('artigo', 'hashtags')
    if not artigo or len(artigo) < 100:
        return None
    titulo = _campo('titulo', 'artigo') or ""
    legenda = _campo('legenda', 'hashtags') or ""
    mh = re.search(r'"hashtags"\s*:\s*\[(.*?)\]', texto, re.DOTALL)
    hashtags = re.findall(r'"(#[^"]+)"', mh.group(1)) if mh else []
    return {"titulo": titulo, "artigo": artigo,
            "legenda": legenda, "hashtags": hashtags}


def _consolidar_paragrafos(paras, alvo):
    """Usado no FALLBACK de fatiar. Pega os paragrafos do artigo e os
    consolida pra caber em 'alvo' slides (e nunca estourar 20 do Instagram):
    1) um lead-in que termina em ':' cola no proximo (introduz a lista/frase),
       pra nao sobrar slide orfao tipo 'O tamanho do que esta em jogo:';
    2) enquanto houver paragrafos demais, funde o mais curto com o vizinho
       menor (mantendo o respiro \\n\\n dentro do slide)."""
    paras = [p.strip() for p in paras if p.strip()]
    # 1) cola lead-in (':') curto no proximo paragrafo
    merged, i = [], 0
    while i < len(paras):
        p = paras[i]
        if (p.rstrip().endswith(":") and len(p) < 110 and i + 1 < len(paras)):
            nxt = paras[i + 1]
            sep = "\n" if re.match(r'^\s*[•\-\*→]', nxt) else "\n\n"
            merged.append(p + sep + nxt)
            i += 2
        else:
            merged.append(p)
            i += 1
    paras = merged
    # 2) AUTO-DIMENSIONA pela densidade: em vez de fatiar fino pra bater o
    # numero pedido, funde ate cada slide ter ~360 chars (densidade do post
    # de referencia, CazeTV ~376). Assim a contagem flutua mas a profundidade
    # por slide fica sempre cheia. Nunca passa do numero pedido nem de 20.
    total = sum(len(p) for p in paras)
    ideal = max(6, round(total / 360))   # nº de slides que da ~360 chars cada
    teto = min(alvo if alvo else 20, 20, ideal)
    while len(paras) > teto and len(paras) > 1:
        idx = min(range(len(paras)), key=lambda k: len(paras[k]))
        if idx == 0:
            j = 1
        elif idx == len(paras) - 1:
            j = idx - 1
        else:
            j = idx - 1 if len(paras[idx - 1]) <= len(paras[idx + 1]) else idx + 1
        a, b = sorted((idx, j))
        sep = "\n" if paras[b].lstrip()[:1] in "•-*→" else "\n\n"
        paras[a] = paras[a] + sep + paras[b]
        del paras[b]
    return paras


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
    "  PRIORIDADE 2 — CONCEITUAL MINIMALISTA (busca Pexels):\n"
    "  * Quando o slide é contexto abstrato (mercado caindo, incerteza, ciclo),\n"
    "    busque imagem CONCEITUAL e SÓBRIA, não cena literal de banco de imagem.\n"
    "  * Estética: minimalista, tons sóbrios/neutros, textura abstrata,\n"
    "    arquitetura geométrica, sombra e luz, objeto único em fundo limpo.\n"
    "  * Modificadores (inglês): 'minimal', 'abstract', 'muted tones',\n"
    "    'monochrome', 'texture', 'geometric', 'shadow', 'negative space',\n"
    "    'concrete architecture', 'single object'\n"
    "  * EXEMPLOS BONS: 'minimal concrete stairs shadow' (queda/declínio),\n"
    "    'abstract cracked surface texture muted' (crise/ruptura),\n"
    "    'monochrome chess piece negative space' (estratégia),\n"
    "    'geometric glass building looking up minimal' (mercado/corporativo),\n"
    "    'single red thread tension abstract' (risco)\n"
    "  * A imagem é METÁFORA do conceito, nunca ilustração óbvia.\n\n"
    "  REGRAS GERAIS:\n"
    "  * RUIM (sempre): 'money', 'business', 'office', 'people working',\n"
    "    'handshake', aperto de mão, cofrinho, gráfico 3D genérico, ilustração\n"
    "    3D de robô/cérebro, moedas empilhadas, notas de dólar voando\n"
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
    "1. ABERTURA — A PARTE MAIS IMPORTANTE DO TEXTO TODO.\n"
    "   O primeiro parágrafo decide se a pessoa para o dedo ou rola pra próxima.\n"
    "   NÃO abra com pergunta nem com título-manchete. Abra CONTANDO A HISTÓRIA,\n"
    "   em 4 a 5 frases (280-330 caracteres), nesta sequência:\n"
    "   (1) A NOTÍCIA/FATO de agora, em ordem direta ('O Ibovespa acaba de\n"
    "       fechar sua oitava semana seguida de queda.');\n"
    "   (2) POR QUE IMPORTA, em 1 frase ('Isso nunca tinha acontecido, nem em\n"
    "       2008, nem na pandemia.');\n"
    "   (3) UMA ÂNCORA DE ESCALA que dá tamanho ao número. No estilo 'Para\n"
    "       comparar, [referência conhecida] fatura/custa [número]' ('Para\n"
    "       comparar, o Real Madrid, maior clube do mundo, fatura cerca de\n"
    "       US$ 1 bilhão por ano.'), ou um reframe de 1 linha que reembala o\n"
    "       dado ('A FIFA fatura 13x mais com um evento de 40 dias.');\n"
    "   (4) UM GANCHO que abre um loop a ser pago adiante. PODE ser uma\n"
    "       AFIRMAÇÃO com detalhe concreto e inesperado ('Mas tudo começou no\n"
    "       porão de um consultório odontológico em Idaho.') OU uma PERGUNTA\n"
    "       curta, concreta e em negrito, do jeito que o Varos fecha a capa\n"
    "       ('**Mas quem fica com essa montanha de dinheiro quando a Copa\n"
    "       acaba?**'). Escolha a que for mais forte pro tema.\n"
    "   O gancho precisa de ALGO ESPECÍFICO (detalhe absurdo, número, ou a\n"
    "   pergunta exata) — é ele que obriga a continuar lendo. PROIBIDO gancho\n"
    "   vago ('o motivo vai te surpreender', 'a resposta está adiante').\n"
    "   O parágrafo 2 começa a PAGAR o gancho (ou avança a história que leva\n"
    "   até ele), nunca muda de assunto.\n\n"
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
    "   - FECHAMENTO: uma conclusão DIRETA, CONCRETA e específica do caso.\n"
    "     O que o leitor leva pra casa, em linguagem clara. PODE fechar o\n"
    "     loop com o gancho inicial.\n"
    "     PROIBIDO no fechamento (e em qualquer slide): FRASE DE EFEITO\n"
    "     'poética'. Nada de aforismo, máxima de almanaque, rima, paralelismo\n"
    "     ou antítese bonitinha tentando soar profundo. Isso é vício de IA\n"
    "     fingindo ser poeta e fica genérico/vazio.\n"
    "     RUIM (NÃO faça): 'Nem todo remédio amargo cura, mas todo veneno\n"
    "     doce mata devagar.' / 'Não importa a cor da bandeira.' / 'O trade-off\n"
    "     está nu.' / 'Estabilização comprada com recessão, futuro pago com\n"
    "     presente.' — frases que rimam/equilibram pra impressionar.\n"
    "     BOM (faça): conclusão concreta. 'Se a recessão durar até a eleição\n"
    "     de 2027, o ajuste pode ser revertido antes de dar resultado, e a\n"
    "     Argentina volta à estaca zero.' — diz algo REAL, não um provérbio.\n\n"

    "PRESSUPOSTO FUNDAMENTAL — O LEITOR NÃO SABE DE NADA:\n"
    "- Escreva partindo do zero. Não assuma que a pessoa conhece a empresa,\n"
    "  o termo técnico, o contexto histórico ou o porquê de aquilo importar.\n"
    "- Toda sigla/termo técnico é explicado na primeira vez, dentro da frase\n"
    "  ('a HBM, memória de altíssima largura de banda que alimenta as GPUs').\n"
    "- Construa o raciocínio passo a passo: contexto antes do dado, causa\n"
    "  antes da consequência. Quem nunca ouviu falar do assunto entende tudo.\n"
    "- Mas SEM ser condescendente: explique com a naturalidade de quem sabe\n"
    "  muito e conversa de igual pra igual, não de quem dá aula.\n\n"

    "PROFUNDIDADE = AUTORIDADE (não seja superficial):\n"
    "- Cada afirmação ancorada em dado concreto, exemplo ou mecanismo.\n"
    "- EXPLIQUE o MECANISMO econômico por dentro: não diga só 'a inflação\n"
    "  caiu', mostre POR QUE caiu (o que o governo fez, como isso afeta os\n"
    "  preços). O leitor tem que ENTENDER a engrenagem, não só ver o número.\n"
    "- Vá à segunda ordem: não só 'o que aconteceu', mas 'por que' e 'o que\n"
    "  isso desencadeia'. É o que separa análise de notícia.\n"
    "- Reconstrua decisões (por que a pessoa/governo escolheu X), mostrando\n"
    "  o trade-off que enfrentava. Traga contexto histórico de verdade.\n"
    "- Evite o raso: se um slide só repete um número sem explicar a causa e\n"
    "  a consequência, ele está incompleto.\n\n"

    "IMPARCIALIDADE (regra inegociável quando o tema é político/econômico):\n"
    "- Analista NEUTRO, não militante. NUNCA pareça campanha a favor ou\n"
    "  contra um governo, político ou ideologia.\n"
    "- Para CADA ponto positivo, traga o custo/risco correspondente, e\n"
    "  vice-versa. Mostre os DOIS lados com o mesmo peso e os mesmos dados.\n"
    "- Não use adjetivo de torcida ('brilhante', 'desastroso', 'corajoso').\n"
    "  Deixe os números falarem; o leitor tira a conclusão.\n"
    "- Se o resultado é ambíguo (melhorou X mas piorou Y), DIGA que é ambíguo.\n\n"

    "FOCO NO TEMA:\n"
    "- O post é sobre o TÓPICO pedido. Aprofunde NELE. Um paralelo ou\n"
    "  comparação é bem-vindo se ILUMINA o tema, mas NÃO desvie o post pra\n"
    "  outro assunto. Se o tema é a Argentina, a maior parte é Argentina.\n\n"

    "RETENÇÃO ENTRE PARÁGRAFOS (cada parágrafo vira um slide — o leitor só\n"
    "vê o próximo se este der motivo; o algoritmo mede a taxa de conclusão):\n"
    "- TERMINE 30-40%% dos parágrafos com uma frase-ponte que cria tensão pro\n"
    "  seguinte: 'Mas o produto que mudou tudo tem outro nome.', 'E o gatilho\n"
    "  veio de fora.', 'Restava um pilar segurando o índice.' A frase-ponte é\n"
    "  COMPLETA (sujeito + verbo), curta, e aponta pra frente sem entregar.\n"
    "- COMECE alguns parágrafos costurando com o anterior: 'Esses números\n"
    "  explicam...', 'Essa decisão custou caro.', 'O começo quase não aconteceu.'\n"
    "- Pelo menos UM detalhe hiperespecífico memorável no texto (o 'porão do\n"
    "  dentista', o 'conta-bolsão', o 'metanol no Porto de Paranaguá') — de\n"
    "  preferência apresentado na abertura e pago no meio.\n"
    "- NUNCA gaste a tensão à toa: cada loop aberto TEM que ser pago depois.\n\n"

    "RITMO E FLUIDEZ — O JEITO VAROS (regra crítica, leia com atenção):\n"
    "- FRASES CURTAS. Uma ideia por frase, 8 a 16 palavras. É o ritmo do Varos:\n"
    "  o texto anda rápido, cada frase entrega uma coisa e passa a bola.\n"
    "- PARÁGRAFO DE NO MÁXIMO 3 LINHAS (1 a 2 frases curtas). MUITO respiro,\n"
    "  linha em branco entre os parágrafos. Slide arejado é mais Varos que denso.\n"
    "- A FLUIDEZ vem do SENTIDO, não de empilhar vírgula. Cada frase puxa a\n"
    "  próxima pelo conteúdo, às vezes com um conector leve ('mas', 'só que',\n"
    "  'por isso', 'e aí', 'então'). É história andando, não tópico jogado.\n"
    "- PROIBIDO frase arrastada (mais de ~18 palavras, cheia de vírgulas, que\n"
    "  vira bloco de 4 linhas). Se a ideia é longa, QUEBRE em 2 ou 3 frases.\n"
    "  RUIM (uma frase só, arrastada): 'A indústria recuou 6%, mas no mesmo\n"
    "  período a inflação despencou de 13% para 2% ao mês, o que devolveu poder\n"
    "  de compra ao salário real.'\n"
    "  BOM (curto e fluido, Varos): 'A indústria recuou 6%. Mas a inflação\n"
    "  despencou, de 13% para 2% ao mês. E o salário real voltou a comprar mais.'\n"
    "- O proibido MESMO é o picote DESCONEXO, frases que não se ligam ('Queda.\n"
    "  Alta. Recuperação.'). O alvo é curto E conectado pelo sentido.\n"
    "- FRASE CURTA DE IMPACTO — permitida com PARCIMÔNIA (macete do Varos):\n"
    "  uma frase curtíssima SOZINHA, pra cravar uma conclusão do argumento,\n"
    "  pode e fica ótima: 'Monopólio.', 'Não é coincidência.', 'É a estrutura.'\n"
    "  Use no MÁXIMO 3 no carrossel todo, e SÓ depois de ter construído o ponto\n"
    "  (ela crava, não substitui o raciocínio).\n"
    "- PROIBIDO é o PICOTAMENTO: VÁRIAS curtas seguidas sem conexão ('Queda.\n"
    "  Alta. Recuperação.') ou drama vazio que não conclui nada ('E não para\n"
    "  por aí.'). Curtas em sequência cansam e têm cara de IA. A de impacto é\n"
    "  uma só, isolada entre frases normais.\n\n"

    "CONSTRUÇÃO DA FRASE — ORDEM DIRETA (regra de manual de redação):\n"
    "- Escreva na ordem direta: SUJEITO + VERBO + COMPLEMENTO. O leitor entende\n"
    "  na primeira passada. Inversões e intercalações longas confundem.\n"
    "- TODA frase tem um AGENTE claro fazendo algo com VERBO forte. Prefira\n"
    "  'Os fundos sacaram R$ 36 bilhões' a 'Houve um saque de R$ 36 bilhões'.\n"
    "  Prefira verbo a substantivo abstrato: 'investidores realizaram lucros'\n"
    "  em vez de 'a realização de lucros aconteceu' (nominalização esconde quem faz).\n"
    "- PROIBIDO ANDAIME (frase-suporte vazia que adia o fato): 'Foi o que\n"
    "  aconteceu', 'O resultado é que', 'A leitura é que', 'O detalhe é que',\n"
    "  'diz muito sobre', 'O problema é que', 'A verdade é que'. Corte o andaime\n"
    "  e afirme o fato direto.\n"
    "- PROIBIDO FRASE CLIVADA ('Foi esse dinheiro que levou o índice', 'São\n"
    "  esses motivos que explicam'). Use ordem direta: 'Esse dinheiro levou o\n"
    "  índice', 'Esses motivos explicam'.\n"
    "- Voz ativa por padrão. Passiva só quando o agente é desconhecido ou\n"
    "  irrelevante.\n\n"

    "PERGUNTAS — USE COM PROPÓSITO (o benchmark Varos usa, e funcionam):\n"
    "  O Varos abre o loop da CAPA e faz transições COM perguntas curtas e\n"
    "  concretas: 'Mas quem fica com essa montanha de dinheiro depois que a\n"
    "  Copa acaba?', 'Então pra onde vai o dinheiro?'. PODE e DEVE usar assim:\n"
    "  (a) a ÚLTIMA linha da capa pode ser uma pergunta-gancho em negrito;\n"
    "  (b) 1 ou 2 transições entre blocos podem ser uma pergunta curta cuja\n"
    "  resposta vem nos parágrafos seguintes. Limite: ~3 no carrossel inteiro.\n"
    "  A pergunta é sempre CONCRETA e sobre o tema. PROIBIDA a pergunta retórica\n"
    "  VAZIA/filosófica ('será que vale a pena?', 'o que isso nos ensina?',\n"
    "  'até quando?'). Fora os ganchos, é afirmação atrás de afirmação.\n\n"

    "CONTRASTE: use 'de um lado X, do outro Y' ou 'enquanto X, Y' pra mostrar\n"
    "  que você entende os dois lados (dá credibilidade analítica).\n\n"

    "════ MACETES DO BENCHMARK (Varos) — é o que deixa denso e gostoso de ler ════\n"
    "Use estes recursos (não todos no mesmo slide; alterne com prosa normal):\n"
    "1) SETAS (→) PARA CADEIA CAUSAL: quando uma coisa puxa a outra, mostre a\n"
    "   escada com setas, UMA POR LINHA (quebra simples \\n entre elas):\n"
    "      → Mais seleções significa mais jogos\n"
    "      → Mais jogos significa mais transmissões, patrocínios e ingressos\n"
    "      → Mais receita entrando pra FIFA\n"
    "   Use quando há encadeamento lógico (causa puxando consequência).\n"
    "2) BULLETS (•) — CADA UM CARREGA UMA INFORMAÇÃO, nunca um rótulo solto.\n"
    "   Comece o bullet em negrito e complete com o porquê/o dado. UM POR LINHA:\n"
    "      • **Ela não paga salário de jogador.** Isso é dos clubes.\n"
    "      • **Ela não constrói estádios.** Isso é dos governos.\n"
    "   Série de dados também vira bullet ('• A Copa de 2014 gerou US$ 4,8 bi.').\n"
    "   PROIBIDO bullet genérico de rótulo (só um nome solto). RUIM: '• Haiti\\n"
    "   • Irã\\n• Senegal'. Isso é uma etiqueta, não diz nada. Ou cada item ganha\n"
    "   contexto ('• Irã, que cai no grupo do Brasil') ou então NÃO use bullet,\n"
    "   escreve em prosa fluida ('O veto atinge torcedores de Haiti, Irã,\n"
    "   Costa do Marfim e Senegal, quatro seleções que vão jogar a Copa').\n"
    "3) ÂNCORA DE ESCALA / ANALOGIA VÍVIDA: pra dar tamanho a um número, compare\n"
    "   com algo conhecido ('Para comparar, ...') ou crie imagem memorável\n"
    "   ('A FIFA tem menos rotatividade no cargo que o Papa no Vaticano.').\n"
    "4) FRASE-CHAVE EM NEGRITO FECHANDO O BLOCO: termine vários slides com a\n"
    "   conclusão do raciocínio em negrito ('**O modelo financeiro sustenta o\n"
    "   modelo político.**', '**É um negócio construído pra nunca perder.**').\n"
    "FORMATAÇÃO DESTES BLOCOS: bullets e setas usam quebra SIMPLES (\\n) entre\n"
    "  as linhas da lista, e o slide inteiro continua separado do próximo por\n"
    "  linha em branco (\\n\\n). NUNCA misture a lista com \\n\\n no meio.\n\n"

    "DADOS — SEMPRE específicos, nunca vagos:\n"
    "- BOM: 'subiu mais de 8x em 12 meses', 'R$ 3,8 bilhões', 'alta de 21%'\n"
    "- RUIM: 'subiu muito', 'bilhões de reais', 'cresceu bastante'\n"
    "- Nunca um parágrafo longo sem um dado concreto ancorando.\n"
    "- Arredonde: 'R$ 14 bilhões' não 'R$ 14,247 bilhões'.\n\n"

    "VOCABULÁRIO: técnico mas SEMPRE ancorado em linguagem simples ao redor.\n"
    "  Pode usar termo técnico (HBM, P/L, FGC, Selic) explicando implícito no\n"
    "  contexto. SEM gíria, SEM palavra de influencer ('top', 'incrível').\n"
    "  VARIE O VOCABULÁRIO — não repita a mesma palavra-chave o tempo todo.\n"
    "  Ex: em vez de usar 'tese' em todo slide, alterne com 'argumento',\n"
    "  'leitura', 'aposta', 'raciocínio', 'caso', 'visão', 'cenário'. A\n"
    "  repetição da mesma palavra cansa e denuncia texto automatizado. Releia\n"
    "  e troque qualquer palavra de conteúdo que apareça 3+ vezes no carrossel.\n\n"

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
    "10) FRASE CURTA DE EFEITO/SUSPENSE — e o DISFARCE do dois-pontos. NÃO\n"
    "    crie pausa dramática com frase curtinha sentenciosa. Exemplos do que\n"
    "    NÃO fazer: 'A resposta muda tudo.', 'Não é tese política.', 'E não\n"
    "    para por aí.', 'O número assusta.', 'Aí que mora o problema.'\n"
    "    ATENÇÃO AO TRUQUE: às vezes você evita o dois-pontos mas escreve\n"
    "    'Frase curta de efeito. Explicação...' — é o MESMO vício disfarçado,\n"
    "    o ponto ali era pra ser dois-pontos. NÃO faça. Junte numa frase só,\n"
    "    afirmando direto. Ex RUIM: 'A resposta surpreende. O déficit dobrou.'\n"
    "    Ex BOM: 'O déficit dobrou no período, contrariando a expectativa.'\n"
    "    'Muda tudo' / 'a resposta é...' / negação curta de efeito: PROIBIDO.\n"
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
    "  o próximo (cliffhanger natural), porque o corte será exatamente ali.\n"
    "- Um parágrafo PODE conter uma lista (bullets • ou setas →) com quebras\n"
    "  SIMPLES (\\n) entre os itens — isso continua sendo UM parágrafo, logo UM\n"
    "  slide. A linha em branco (\\n\\n) só separa um slide do outro.\n\n"

    "TAMANHO (CRÍTICO — não entregue artigo curto, é o erro mais comum):\n"
    "- Cada parágrafo/slide tem ~350-420 caracteres: DENSO de conteúdo, mas\n"
    "  em frases curtas (a densidade vem da substância, não de frase longa).\n"
    "  NÃO entregue slide raso ou curto demais. Igual aos melhores posts.\n"
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
    "  sem clichê e SEM frase de efeito poética/aforismo. SEM hashtag aqui.\n"
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
    "- Corte SEMPRE no FIM de uma frase completa (depois de . ! ?). NUNCA\n"
    "  corte no meio de uma frase nem termine um slide em vírgula. Cada slide\n"
    "  começa com letra maiúscula e termina com ponto final.\n"
    "- Corte em pontos de CLIFFHANGER NATURAL: onde uma frase termina\n"
    "  deixando curiosidade pro próximo ('Mas o produto que mudou tudo tem\n"
    "  outro nome.'). Esses cortes já existem no texto, você só os encontra.\n"
    "- Cada slide tem UMA ideia central — UM TEMA SÓ por slide, NUNCA dois.\n"
    "  Se o slide termina um assunto e começa outro, o corte está no lugar\n"
    "  errado: mova o corte pra exatamente onde o assunto vira. Slide com\n"
    "  dois temas quebra a fluidez da leitura.\n"
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

    "TAMANHO — SLIDES DENSOS E FLUIDOS AO MESMO TEMPO (o equilíbrio do Varos):\n"
    "- Cada slide tem entre 330 e 440 caracteres. CHEIO de conteúdo, MAS em\n"
    "  frases curtas e parágrafos curtos. A densidade vem da SUBSTÂNCIA (dado,\n"
    "  causa, consequência), não de frase longa. Slide raso/curto demais é ERRO.\n"
    "- O slide 1 (hook) pode ser um pouco mais curto. NUNCA acima de 470.\n\n"

    "FORMATO VISUAL — O JEITO VAROS (frase curta + MUITO respiro):\n"
    "- FRASES CURTAS: uma ideia por frase, 8 a 16 palavras. Sem empilhar\n"
    "  vírgula. Se a frase passou de ~16 palavras, quebre em duas frases.\n"
    "- PARÁGRAFO DE NO MÁXIMO 3 LINHAS (1 a 2 frases curtas). Cada parágrafo\n"
    "  separado por linha em branco (\\n\\n). O respiro é o que faz fluir.\n"
    "- Um slide tem 3 ou 4 parágrafos CURTOS, nunca 2 blocões.\n"
    "  RUIM (bloco denso, uma frase arrastada de 4 linhas):\n"
    "    O ICE, a agência de imigração e alfândega dos EUA, estará presente nos\n"
    "    estádios durante todo o torneio, oficialmente no papel de segurança.\n"
    "  BOM (Varos, curto e fluido, respirando):\n"
    "    O ICE vai estar nos estádios o torneio inteiro.\n\n"
    "    Oficialmente, só como segurança.\n\n"
    "    Mas a agência não descartou fazer prisões ali dentro.\n"
    "- Cada parágrafo puxa o próximo. Fluido, jamais picotado e desconexo.\n"
    "- LISTA DO ARTIGO (bullets • ou setas →): se o artigo trouxe uma lista,\n"
    "  PRESERVE-A idêntica, com quebra SIMPLES (\\n) entre os itens, toda no\n"
    "  MESMO slide. NÃO vire a lista em prosa, NÃO separe os itens em slides\n"
    "  diferentes, NÃO troque \\n por \\n\\n entre os itens.\n\n"

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
    "  * APROVEITE LUGARES/ESTRUTURAS REAIS quando o slide tratar de algo\n"
    "    concreto. Em vez de stock genérico, mapeie o conceito pro ÍCONE\n"
    "    real e use photo_source='real'. Exemplos de mapeamento:\n"
    "      energia/hidrelétrica -> 'Itaipu Dam' / 'Belo Monte Dam'\n"
    "      bolsa/mercado/ações BR -> 'B3 stock exchange Sao Paulo building'\n"
    "      petróleo/pré-sal -> 'Petrobras oil platform' / 'P-51 platform Brazil'\n"
    "      política/governo/fiscal BR -> 'Congresso Nacional Brasilia' /\n"
    "        'Palacio do Planalto'\n"
    "      juros/Banco Central -> 'Banco Central do Brasil building Brasilia'\n"
    "      minério/nióbio -> 'Araxa mine Brazil' / 'iron ore mine Carajas'\n"
    "      cidade/economia -> 'Sao Paulo skyline' / 'Avenida Paulista'\n"
    "    Use o NOME da estrutura/lugar (prédio, usina, plataforma, mina,\n"
    "    monumento) — esses têm foto real boa. EVITE conceito amplo como\n"
    "    'soybean field', 'amazon river', 'hydroelectric' (viram satélite).\n"
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
    """Formata o slide com RESPIRO VISUAL + fluidez. O equilibrio certo:
    varios paragrafos separados por linha em branco (espaco pra respirar),
    MAS cada paragrafo tem 1-2 frases CONECTADAS (~150 chars), nao uma frase
    curta solta picotada. O conteudo fluido vem do prompt; aqui so agrupamos
    em paragrafos de tamanho confortavel.
    Preserva listas de bullet (•/-/*) e cadeias de seta (→) intactas — sao
    macetes do Varos e a estrutura de linhas e proposital."""
    if not text:
        return text
    # Bullets E setas: estrutura de linhas eh intencional, nao mexe
    if re.search(r'(?m)^\s*(?:[•\-\*→›▸◦]|->)\s+', text):
        return text
    # Junta tudo num texto plano (remove quebras que o Claude tenha posto)
    flat = re.sub(r'\s*\n+\s*', ' ', text).strip()
    sentences = re.split(r'(?<=[.!?])\s+', flat)
    if len(sentences) <= 1:
        return flat
    # Quebra frase LONGA (>120 chars) na virgula mais proxima do meio, pra
    # nenhum paragrafo virar um blocao de 4+ linhas. Estilo Varos: respiro.
    def _quebra_frase_longa(fr):
        if len(fr) <= 130:
            return [fr]
        virgs = [m.end() for m in re.finditer(r",\s", fr)]
        # so corta numa virgula que deixe AMBOS os lados com >=45 chars, pra
        # nao criar fragmento solto tipo 'Desde 2 de abril de 2026,'.
        bons = [v for v in virgs if v >= 45 and (len(fr) - v) >= 45]
        if not bons:
            return [fr]
        meio = len(fr) / 2
        corte = min(bons, key=lambda p: abs(p - meio))
        return [fr[:corte].strip(), fr[corte:].strip()]
    # Agrupa em paragrafos CURTOS (~90 chars = ate ~3 linhas), muito respiro,
    # estilo Varos. Frase longa quebrada: cada pedaco vira paragrafo proprio.
    # Frases curtas se juntam ATE ~90 chars; nunca passam disso (sem re-merge).
    ALVO = 90
    paragraphs, current, clen = [], [], 0
    for s in sentences:
        pieces = _quebra_frase_longa(s)
        if len(pieces) > 1:
            if current:
                paragraphs.append(" ".join(current)); current, clen = [], 0
            paragraphs.extend(pieces)
            continue
        u = pieces[0]
        # fecha o paragrafo antes de estourar o ALVO (nao soma alem do alvo)
        if current and clen + len(u) > ALVO:
            paragraphs.append(" ".join(current)); current, clen = [], 0
        current.append(u)
        clen += len(u) + 1
    if current:
        if paragraphs and clen < 35:
            paragraphs[-1] += " " + " ".join(current)
        else:
            paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def _juntar_frases_curtas(text: str) -> str:
    """Junta frases ULTRA-CURTAS de efeito SO quando vem em SEQUENCIA (2+
    seguidas) — esse e o picotamento que cansa e tem cara de IA:
    'Queda. Alta. Recuperacao.' -> 'Queda, alta, recuperacao.'
    Uma frase curta SOZINHA, cercada de frases normais, e macete legitimo do
    benchmark (Varos): 'Monopolio.', 'Nao e coincidencia.', 'E a estrutura.'
    Essa e PRESERVADA — ela crava um ponto do argumento, nao picota."""
    if not text:
        return text
    partes = re.split(r'(?<=[.!?])\s+', text.strip())

    def _is_curta(p):
        nuc = p.strip().rstrip(".!?").strip().strip("*").strip()
        pal = nuc.split()
        return (1 <= len(pal) <= 3 and len(nuc) <= 24
                and not any(c.isdigit() for c in nuc) and nuc[:1].isupper())

    flags = [_is_curta(p) for p in partes]
    out = []
    for i, p in enumerate(partes):
        # so junta se esta curta E faz parte de um RUN (vizinha tambem curta)
        em_run = flags[i] and ((i > 0 and flags[i - 1]) or
                               (i + 1 < len(flags) and flags[i + 1]))
        if em_run and out:
            nuc = p.strip().rstrip(".!?").strip().strip("*").strip()
            ant = out[-1].rstrip()
            if ant and ant[-1] in ".!?":
                ant = ant[:-1]
            out[-1] = ant + ", " + nuc[0].lower() + nuc[1:] + p.strip()[len(nuc):]
        else:
            out.append(p)
    return " ".join(out)


def _juntar_antitese(text: str) -> str:
    """Junta antitese de efeito 'Nao e X. E Y.' numa frase so com ', e sim '.
    'Nao e questao politica. E geologia.' -> 'Nao e questao politica, e sim geologia.'
    Elimina o picote dramatico que o Gabriel sinalizou como vies de IA."""
    if not text:
        return text
    # 'Nao e/foi/sao X' + '. E/Sao ' -> ', e sim '
    return re.sub(
        r'(N[ãa]o\s+(?:é|e|foi|são|sao|era)\s+[^.!?]{2,70})\.\s+(?:[EÉ]|S[ãa]o)\s+',
        r'\1, e sim ',
        text,
    )


def _sanitizar_slide_varos(text: str) -> str:
    """Sanitizacao pro fluxo de geracao em 2 fases. Remove travessao/aspas,
    junta frases ultra-curtas e antitese, limpa cliche, e FORMATA em
    paragrafos com respiro (2-3 blocos fluidos por slide)."""
    text = _strip_em_dash(text or "")
    text = _remover_dois_pontos_anuncio(text)
    text = _aspas_simples_pra_duplas(text)
    text = _juntar_antitese(text)
    text = _juntar_frases_curtas(text)
    text = _limpar_cliches_abertura(text)
    text = _formatar_paragrafos_varos(text)
    text = _truncar_slide_se_grande(text)
    # Rede de seguranca: slide NUNCA deve terminar em virgula/ponto-e-virgula
    # (fatiador cortou no meio de frase). Troca por ponto final.
    text = text.rstrip()
    if text.endswith((",", ";")):
        text = text[:-1] + "."
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


# ── Radar de temas virais ──────────────────────────────────────────────────
# Varre noticias/mercado agora e sugere temas de carrossel, pontuados pelos
# 2 padroes que os proprios dados de engajamento do perfil confirmaram:
# polarizacao politica (controversia/fala de politico virando manchete) e
# urgencia de preco/binario em cripto e mercado. Cache em disco: cada rodada
# gasta busca web real, entao NAO recalcula a cada carregamento de pagina.

RADAR_PATH = DATA_DIR / "radar.json"

# ── Radar 100% Python: RSS de fontes reais + CoinGecko, ZERO chamada de API
# de LLM (zero custo). Classificacao por regra, nao por modelo:
#  - "comprovado" = casa com um dos 2 padroes que o engajamento do perfil
#    confirmou (politico quente + verbo de conflito // alerta de preco cripto).
#  - "aposta" = achado de nicho (fora dessas 2 regras) com palavra de
#    novidade/ineditismo — pouca gente comentando ainda.
# Toda fonte tem que responder 200 ANTES de entrar na lista (senao descarta
# o tema inteiro — nada de link riscado/morto na tela).

RADAR_FEEDS = {
    "política":  ["https://g1.globo.com/rss/g1/politica/",
                  "https://www.poder360.com.br/feed/"],
    "economia":  ["https://www.infomoney.com.br/feed/",
                  "https://g1.globo.com/rss/g1/economia/",
                  "https://exame.com/feed/"],
    "cripto":    ["https://cointelegraph.com.br/rss",
                  "https://livecoins.com.br/feed/"],
    "nicho":     ["https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml"],
}

RADAR_POLITICOS = ["lula", "bolsonaro", "flávio", "flavio", "michelle", "moraes",
                   "renan santos", "tarcísio", "tarcisio", "ciro gomes", "marina silva",
                   "alckmin", "eduardo bolsonaro", "gleisi", "boulos", "milei", "trump"]
RADAR_CONFLITO = ["racha", "ataca", "ataque", "amea", "acusa", "detona", "ironiza",
                   "humilha", "processa", "provoca", "rompe", "critica", "chama de",
                   "rebate", "dispara contra", "cobra", "exonera", "demite", "polêmica",
                   "polemica", "expõe", "expoe"]
RADAR_ALERTA_PRECO = ["dispara", "despenca", "colapso", "colapsa", "crash", "recorde",
                       "máxima histórica", "maxima historica", "mínima", "minima",
                       "tomba", "derrete", "queda livre"]
RADAR_NOVIDADE = ["pela primeira vez", "inédito", "inedito", "revela", "descoberta",
                   "aprova lei", "proíbe", "proibe", "estudo mostra", "novo recorde",
                   "avanço", "avanco", "surpreende"]

RADAR_COINS = [("bitcoin", "Bitcoin"), ("ethereum", "Ethereum"), ("solana", "Solana"),
               ("ripple", "XRP"), ("dogecoin", "Dogecoin"), ("cardano", "Cardano")]


def _radar_ler():
    if not RADAR_PATH.exists():
        return None
    try:
        return json.loads(RADAR_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _url_viva(url, timeout=6):
    """Confere (HEAD, fallback GET) se a URL realmente abre. Tema cuja fonte
    nao responde 200 e descartado inteiro (nao mostramos link morto)."""
    try:
        import requests as _req
        headers = {"User-Agent": "Mozilla/5.0 (compatible; BearlzRadar/1.0)"}
        r = _req.head(url, timeout=timeout, allow_redirects=True, headers=headers)
        if r.status_code >= 400 or r.status_code == 405:
            r = _req.get(url, timeout=timeout, allow_redirects=True, headers=headers, stream=True)
        return r.status_code < 400
    except Exception:
        return False


def _radar_parse_rss(url, limite=20):
    """Baixa e parseia um feed RSS 2.0 padrao (item/title/link/description).
    Sem dependencia nova: xml.etree (stdlib) + requests (ja no projeto)."""
    import requests as _req
    import xml.etree.ElementTree as ET
    import html as _htmlmod
    itens = []
    try:
        r = _req.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 (compatible; BearlzRadar/1.0)"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.iter("item"):
            titulo = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            resumo = (item.findtext("description") or "").strip()
            resumo = re.sub(r"<[^>]+>", " ", resumo)              # tira tags/CDATA-HTML
            resumo = _htmlmod.unescape(re.sub(r"\s+", " ", resumo)).strip()
            if titulo and link:
                itens.append({"titulo": _htmlmod.unescape(titulo), "link": link, "resumo": resumo[:240]})
            if len(itens) >= limite:
                break
    except Exception:
        pass
    return itens


def _radar_classificar(item, categoria_feed):
    """Aplica as regras (nao IA) e devolve o dict de classificacao, ou None
    se o item nao for forte o suficiente pra entrar no radar."""
    texto = (item["titulo"] + " " + item["resumo"]).lower()

    tem_politico = any(p in texto for p in RADAR_POLITICOS)
    tem_conflito = any(v in texto for v in RADAR_CONFLITO)
    if tem_politico and tem_conflito:
        return {"categoria": "política", "tipo": "comprovado", "potencial": "alto",
                "motivo": "Cita uma figura política de alta repercussão junto com tom de "
                          "conflito/controvérsia — o padrão que já rendeu o maior "
                          "engajamento do perfil (polarização política)."}

    if categoria_feed == "cripto" and any(k in texto for k in RADAR_ALERTA_PRECO):
        return {"categoria": "cripto", "tipo": "comprovado", "potencial": "alto",
                "motivo": "Manchete de cripto com linguagem de urgência (alta/queda "
                          "forte) — puxa curtidas mesmo com poucos comentários."}

    if categoria_feed == "economia" and any(
            k in texto for k in RADAR_ALERTA_PRECO + ["selic", "juros", "dólar", "dolar", "inflação", "inflacao"]):
        return {"categoria": "economia", "tipo": "comprovado", "potencial": "medio",
                "motivo": "Tema de mercado/economia com sinal de urgência (juro, câmbio "
                          "ou inflação em movimento forte)."}

    if any(k in texto for k in RADAR_NOVIDADE):
        cat = "nicho" if categoria_feed == "nicho" else categoria_feed
        return {"categoria": cat, "tipo": "aposta", "potencial": "medio",
                "motivo": "Fato inédito ou pouco comentado ainda — fora dos padrões "
                          "comprovados, mas com ângulo de novidade que pode surpreender."}

    return None


def _radar_topicos_cripto_preco():
    """CoinGecko (API pública, sem chave) — detecta variacao forte de preco em
    24h e gera um topico determinístico (nao depende de RSS ter noticiado)."""
    import requests as _req
    ids = ",".join(c[0] for c in RADAR_COINS)
    try:
        r = _req.get("https://api.coingecko.com/api/v3/coins/markets",
                      params={"vs_currency": "usd", "ids": ids, "price_change_percentage": "24h"},
                      timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        dados = r.json()
    except Exception:
        return []
    nomes = dict(RADAR_COINS)
    out = []
    for d in dados:
        pct = d.get("price_change_percentage_24h_in_currency")
        if pct is None or abs(pct) < 5:
            continue
        nome = nomes.get(d["id"], d["id"].title())
        preco = d.get("current_price")
        subindo = pct > 0
        verbo = "disparou" if subindo else "despencou"
        pergunta = "ainda vai subir mais?" if subindo else "é hora de comprar ou vai cair mais?"
        out.append({
            "titulo": f"{nome} {verbo} {abs(pct):.1f}% em 24h: {pergunta}",
            "categoria": "cripto", "tipo": "comprovado",
            "potencial": "alto" if abs(pct) >= 8 else "medio",
            "gancho": f"Preço atual: US$ {preco:,.0f}, variação de {pct:+.1f}% nas últimas 24h (CoinGecko).",
            "motivo": "Movimento de preço forte e mensurável agora — padrão de urgência/alerta "
                      "de preço que já puxou muita curtida no perfil.",
            "fontes": [f"https://www.coingecko.com/en/coins/{d['id']}"],
        })
    return out


def _radar_normalizar_titulo(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower())[:60]


def _radar_scan():
    """Varredura completa: RSS + CoinGecko + classificacao por regra +
    verificacao real de link. ZERO chamada de API de LLM, ZERO custo."""
    topicos = []
    for categoria, urls in RADAR_FEEDS.items():
        for url in urls:
            for item in _radar_parse_rss(url):
                cls = _radar_classificar(item, categoria)
                if not cls:
                    continue
                topicos.append({
                    "titulo": item["titulo"],
                    "gancho": item["resumo"] or item["titulo"],
                    "fontes": [item["link"]],
                    **cls,
                })
    topicos += _radar_topicos_cripto_preco()

    # Dedup por titulo normalizado (a mesma noticia sai em varios feeds)
    vistos, dedup = set(), []
    for t in topicos:
        chave = _radar_normalizar_titulo(t["titulo"])
        if chave in vistos:
            continue
        vistos.add(chave)
        dedup.append(t)

    # So entra tema cuja fonte principal realmente abre (nada de link morto)
    from concurrent.futures import ThreadPoolExecutor
    urls_checar = [t["fontes"][0] for t in dedup if t.get("fontes")]
    with ThreadPoolExecutor(max_workers=10) as ex:
        status = dict(zip(urls_checar, ex.map(_url_viva, urls_checar)))
    vivos = [t for t in dedup if status.get((t.get("fontes") or [""])[0])]

    # Ordena: potencial alto primeiro, comprovado antes de aposta; limita o total
    ordem_potencial = {"alto": 0, "medio": 1}
    ordem_tipo = {"comprovado": 0, "aposta": 1}
    vivos.sort(key=lambda t: (ordem_tipo.get(t["tipo"], 1), ordem_potencial.get(t["potencial"], 1)))
    comprovados = [t for t in vivos if t["tipo"] == "comprovado"][:12]
    apostas = [t for t in vivos if t["tipo"] == "aposta"][:5]
    return comprovados + apostas


@app.route("/radar")
def radar_index():
    dados = _radar_ler()
    return render_template("radar.html",
                           topicos=(dados or {}).get("topicos") or [],
                           gerado_em=(dados or {}).get("gerado_em"))


@app.route("/api/radar/atualizar", methods=["POST"])
def api_radar_atualizar():
    """Roda a varredura (RSS + CoinGecko, sem LLM) e regrava o cache."""
    try:
        topicos = _radar_scan()
        payload = {
            "gerado_em": datetime.utcnow().isoformat() + "Z",
            "topicos": topicos,
        }
        RADAR_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return jsonify({"ok": True, **payload})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        # Palavras que denunciam imagem que NAO eh foto normal (satelite,
        # selo, mapa, diagrama, bandeira, brasao). Essas aparecem quando a
        # query eh conceitual ('soybean field' -> imagem Sentinel/Copernicus).
        LIXO_TITULO = ("satellite", "sentinel", "copernicus", "cbers", "aster",
                       " esa", "landsat", "stamp", "selo", "map of", "mapa",
                       "diagram", "diagrama", "logo", "coat of arms", "flag of",
                       "brasão", "seal of", "chart", "graph")
        # Se a busca eh sobre o Brasil, descarta foto que claramente eh de
        # OUTRO pais (ex: query 'mine Brazil' devolveu 'Chuquicamata, Chile').
        q_low = (query or "").lower()
        quer_brasil = ("brazil" in q_low or "brasil" in q_low)
        OUTROS_PAISES = ("chile", "argentina", "peru", "bolivia", "colombia",
                         "mexico", "uruguay", "paraguay", "venezuela", "ecuador",
                         "chuquicamata", "calama")
        for pid, page in pages.items():
            ii = (page.get("imageinfo") or [{}])[0]
            mime = ii.get("mime", "")
            if mime not in MIMES_OK:
                continue
            url = ii.get("url", "") or ""
            # Reforco por extensao (alguns mimes vem errados)
            if not url.lower().rsplit("?", 1)[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            # Descarta satelite/selo/mapa/logo pelo titulo (nao sao fotos)
            titulo_low = page.get("title", "").lower()
            if any(lx in titulo_low for lx in LIXO_TITULO):
                continue
            # Busca sobre Brasil mas foto eh de outro pais -> descarta
            if quer_brasil and any(p in titulo_low for p in OUTROS_PAISES):
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
        (r"\bmuda tudo\b\.?", "'Muda tudo' é fechamento dramático cara de IA (qualquer sujeito). Mostre O QUE muda concretamente."),
        (r"\bA resposta\s+(?:muda|[eé]|est[aá]|surpreende|vai|vir[áa]|ser[áa])", "'A resposta muda/é/virá...' é gancho de suspense de IA. Afirme o ponto direto."),
        (r"\bA (?:pergunta|d[úu]vida) que (?:fica|resta|permanece|importa)\b", "'A pergunta que fica' é fechamento dramático de IA. Conclua direto."),
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
        # Andaimes (frase-suporte vazia que adia o fato) — manual de redação:
        # afirme direto, em ordem direta (sujeito + verbo + complemento).
        (r"\bfoi o que aconteceu\b", "'Foi o que aconteceu' é andaime de IA (pergunta retórica + resposta vazia). Afirme o fato direto."),
        (r"\bdiz(?:em)? muito sobre\b", "'Diz muito sobre' é vago, cara de IA. Diga concretamente O QUE mostra."),
        (r"\bo detalhe [eé] que\b", "'O detalhe é que' é andaime de IA. Corte e afirme direto."),
        (r"\ba leitura (?:do mercado |geral |dos analistas )?[eé] que\b", "'A leitura é que' é andaime. Use agente + verbo: 'O mercado trata X como...'."),
        (r"(?:^|(?<=\n)|(?<=[.!?]\s)|(?<=\*\*))O resultado(?: disso)? (?:[eé]|foi)\b", "'O resultado é/foi' abrindo frase é andaime. Diga o resultado em ordem direta (sujeito + verbo)."),
        # Frases clivadas ('Foi esse X que...', 'São esses X que...') — ordem direta
        # (?<=\*\*) cobre frase que abre com negrito: '**O resultado é...'
        (r"(?:^|(?<=\n)|(?<=[.!?]\s)|(?<=\*\*))(?:Foi|[ÉE])\s+(?:ess[ea]s?|es[st]e|aquel[ea]s?)\s[^.!?]{2,45}\bque\b", "Frase clivada ('Foi esse X que fez Y'). Prefira ordem direta: 'Esse X fez Y'."),
        (r"(?:^|(?<=\n)|(?<=[.!?]\s)|(?<=\*\*))S[ãa]o ess[ea]s?\s[^.!?]{2,40}\bque\b", "Frase clivada ('São esses X que...'). Ordem direta: 'Esses X fazem...'."),
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

    # 8. Frase de efeito "poetica" (aforismo/rima/paralelismo) — a IA tentando
    # ser poeta. Generico e vazio. Detecta os padroes mais classicos.
    frase_efeito = [
        (r"\bNem\s+to[dt]o\b[^.!?]{3,70}\b(?:mas\s+)?to[dt][oa]\b",
         "Frase de efeito (paralelismo 'nem todo X... todo Y'). Soa proverbio de almanaque. Diga o ponto direto."),
        (r"\bn[ãa]o\s+importa\s+(?:a\s+cor|o\s+nome|de\s+que|quem\s|onde\s|qual\s)",
         "'Não importa a cor/o nome...' é relativização de efeito. Va direto ao ponto concreto."),
        (r"\bn[ãa]o\s+[eé]\s+sobre\s+[^.!?]{3,40},?\s+[eé]\s+sobre\b",
         "Antítese de efeito ('não é sobre X, é sobre Y'). Clichê de IA. Reescreva direto."),
        (r"\b([a-zçãõáéíóúâêô]+)\s+n[ãa]o\s+\1\b",
         "Repetição/quiasmo de efeito. Soa frase de almanaque."),
        # 'Não é X.' / 'Não é X. É Y.' — negação curta de efeito (o Claude
        # usa pra criar drama no lugar do dois-pontos que ele evita).
        (r"(?:^|(?<=[.!?]\s)|(?<=\n))N[ãa]o\s+(?:é|e|foi|será|sera)\s+[^.!?,;]{2,28}\.",
         "'Não é X.' como frase curta isolada é efeito dramático de IA. Afirme o que É, direto."),
        # Frase curtíssima sentenciosa de suspense no meio do texto
        (r"(?:^|(?<=[.!?]\s))(?:E n[ãa]o para por a[íi]|A[íi] que (?:mora|est[áa]) o)\b[^.!?]{0,30}\.",
         "Frase de suspense ('e não para por aí', 'aí que mora o...'). Tique de IA. Corte."),
    ]
    for pat, msg in frase_efeito:
        for m in re.finditer(pat, texto, re.IGNORECASE):
            matches.append({
                "offset": m.start(),
                "length": m.end() - m.start(),
                "message": msg,
                "short": "Frase de efeito poética",
                "suggestions": [],
                "category": "Vício de IA",
                "type": "AI_AFORISMO",
                "context": texto[max(0,m.start()-15):min(len(texto), m.end()+25)],
            })

    # 9. PICOTAMENTO: sentencas ULTRA-CURTAS (1-3 palavras) em SEQUENCIA.
    # 'Queda. Alta. Recuperacao.' cansa e tem cara de IA. MAS uma curta SOZINHA
    # ('Monopolio.', 'Nao e coincidencia.') e macete legitimo do Varos — NAO
    # flagra. So vira vicio quando vem 2+ seguidas (a vizinha tambem e curta).
    _sent = list(re.finditer(r"(?:^|(?<=[.!?]\s))([^.!?\n]{2,40}?)([.!?])(?=\s|$)", texto))
    def _curta(sm):
        fr = sm.group(1).strip().strip("*").strip()
        if not fr or any(c.isdigit() for c in fr):
            return False
        return len(fr.split()) <= 3 and len(fr) <= 24 and fr[0:1].isupper()
    _flags = [_curta(sm) for sm in _sent]
    for i, sm in enumerate(_sent):
        if not _flags[i]:
            continue
        vizinha_curta = (i > 0 and _flags[i-1]) or (i+1 < len(_flags) and _flags[i+1])
        if not vizinha_curta:
            continue  # curta isolada = punch line do Varos, permitida
        frase = sm.group(1).strip().strip("*").strip()
        matches.append({
            "offset": sm.start(1),
            "length": len(sm.group(1)),
            "message": f"'{frase}.' faz parte de uma sequência de frases ultra-curtas (picotamento, cara de IA). Junte numa frase com desenvolvimento. (Uma curta sozinha de impacto é ok.)",
            "short": "Picotamento",
            "suggestions": [],
            "category": "Vício de IA",
            "type": "AI_FRASE_CURTA",
            "context": texto[max(0,sm.start()-20):min(len(texto), sm.end()+15)],
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
- NUNCA dois-pontos de anúncio ("X: Y") — vira frase de PowerPoint. Reescreva fluindo.
- Frases picotadas ("Queda. Alta.") → reescreva como ideia fluida

FRASE DE EFEITO "POÉTICA" — REMOVA SEMPRE (vício de IA fingindo ser poeta):
- Aforismo/máxima de almanaque: "Nem todo remédio amargo cura, mas todo
  veneno doce mata devagar." → CORTE. Diga o ponto concreto e direto.
- Paralelismo/rima/antítese bonitinha: "não é sobre X, é sobre Y", "não
  importa a cor da bandeira", "futuro pago com presente", "o trade-off está
  nu" → REESCREVA como afirmação direta e específica.
- Metáfora elaborada ("cirurgia sem anestesia, o paciente gritou") → diga
  o fato real sem floreio.
- Regra: o fechamento é uma CONCLUSÃO concreta do caso, NÃO um provérbio
  que quer se destacar. Se a frase parece feita pra ser citada/rimar, corte.

GANCHOS DRAMÁTICOS DE IA — REMOVA SEMPRE (são a cara da IA):
- "muda tudo" (qualquer sujeito) / "muda o jogo" / "vira o jogo" → REMOVA
  o fechamento dramático e EXPLIQUE concretamente o que muda.
- FRASE CURTA DE EFEITO/SUSPENSE: "A resposta muda tudo.", "Não é tese
  política.", "E não para por aí.", "O número assusta." → CORTE e junte
  numa frase só, afirmando direto. CUIDADO com o disfarce do dois-pontos:
  "Frase curta de efeito. Explicação" era pra ser dois-pontos; junte tudo
  numa afirmação direta, sem a pausa dramática.
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
                            num_slides, system_artigo, usage_acc=None):
    """Geracao em 2 fases (estilo Varos):
      FASE 1: escreve o ARTIGO corrido fluido (system_artigo / SYSTEM_ARTIGO)
      FASE 2: fatia o artigo em num_slides slides + escolhe imagens (SYSTEM_FATIAR)

    Retorna (titulo, slides_raw, artigo, legenda, hashtags, fase_debug).
    Levanta ValueError se alguma fase falhar de forma irrecuperavel."""
    # Tamanho-alvo do artigo: ~350-420 chars por slide. Densidade igual ao
    # post de referencia (CazeTV ~376 chars/slide). Denso de conteudo MAS
    # fluido (frase curta). Piso alto pra nao gerar slide raso.
    min_chars = max(num_slides * 340, 1200)
    max_chars = num_slides * 430
    sys_artigo = (system_artigo
                  .replace("{min_chars}", str(min_chars))
                  .replace("{max_chars}", str(max_chars))
                  .replace("{num_slides}", str(num_slides)))

    # ── FASE 1: ARTIGO CORRIDO ──
    prompt_artigo = (
        f"TÓPICO: {topico}\n\n"
        f"BRIEF/CONTEÚDO:\n{brief_enriched or topico}\n\n"
        f"Escreva o artigo completo ({min_chars}-{max_chars} caracteres) "
        "seguindo a arquitetura narrativa do system: capa que abre com FATO + "
        "NÚMERO e fecha num gancho (afirmação concreta OU pergunta curta), "
        "desenvolvimento que reconstrói o raciocínio com dados e usa os macetes "
        "(setas →, bullets •, âncora de escala, frase-chave em negrito), "
        "fechamento com conclusão DIRETA e concreta (SEM frase de efeito "
        "poética, aforismo ou rima). O FECHAMENTO são 1-2 frases curtas, "
        "conclusivas e COMPLETAS; NUNCA termine o artigo com uma citação longa "
        "nem pare no meio de uma frase ou ideia. Texto fluido, SEM CTA. Inclua legenda e "
        "hashtags. Retorne SOMENTE JSON."
        "\n\nVERIFICAÇÃO OBRIGATÓRIA (antes de escrever): use a ferramenta "
        "web_search para CONFIRMAR cada número e fato que pretende usar "
        "(faça 4-6 buscas objetivas). Regras: "
        "1) NUNCA escreva um dado que você não confirmou numa fonte; "
        "2) RECÊNCIA: o dado CENTRAL do post deve ser confirmado em DUAS "
        "fontes, sempre preferindo a mais recente — cheque a DATA da fonte "
        "(um valor de 2024 pode estar muito defasado hoje; busque 'X 2026' "
        "ou 'X hoje' antes de cravar); "
        "3) se as fontes divergirem, use a mais recente e diga o período; "
        "4) atribua previsões e opiniões a quem as fez; "
        "5) se um dado do brief estiver errado ou desatualizado, corrija no "
        "artigo (não copie o erro). Depois de verificar, escreva o artigo "
        "completo e retorne SOMENTE o JSON final."
    )
    # max_tokens generoso: thinking + artigo grande + legenda + hashtags.
    # A fase 1 agora PESQUISA (web search server-side) antes de escrever.
    out1 = _claude_fase1_com_busca(client, usage_acc,
        model=GERAR_MODEL, max_tokens=16000,
        system=sys_artigo,
        messages=[{"role": "user", "content": prompt_artigo}]
    )
    if out1.startswith("```"):
        out1 = re.sub(r"^```[a-z]*\n?", "", out1)
        out1 = re.sub(r"\n?```$", "", out1).strip()
    dados1 = _parse_claude_json(out1)
    if not (dados1 and isinstance(dados1, dict) and dados1.get("artigo")):
        # JSON quebrou (tipico: aspas internas nao escapadas). Extrai o artigo
        # por delimitador de chave, tolerante a aspas — evita salvar o JSON cru
        # como se fosse o artigo (bug que embutia '{"titulo":...' no slide 1).
        recuperado = _extrair_campos_artigo(out1)
        if recuperado:
            dados1 = recuperado
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
        model=GERAR_MODEL, max_tokens=max_tok_fatiar,
        system=sys_fatiar,
        messages=[{"role": "user", "content": prompt_fatiar}]
    )
    _acumular_usage(usage_acc, resp2)
    out2 = _extrair_texto_resp(resp2)
    if out2.startswith("```"):
        out2 = re.sub(r"^```[a-z]*\n?", "", out2)
        out2 = re.sub(r"\n?```$", "", out2).strip()
    dados2 = _parse_claude_json(out2)
    if dados2 and isinstance(dados2, dict) and dados2.get("slides"):
        slides_raw = dados2.get("slides", [])
    else:
        # FALLBACK: o fatiador (LLM) as vezes devolve JSON impossivel de
        # parsear (tipico: aspas duplas nao escapadas dentro do texto, que
        # este conteudo tem de monte por causa de slogans citados). Em vez
        # de falhar a geracao inteira, fatiamos o ARTIGO por paragrafos —
        # que e exatamente a filosofia do projeto: 1 paragrafo = 1 slide,
        # carrossel = artigo verbatim. Sem imagem (adicionada na revisao).
        import sys as _sys
        print(f"[FATIAR-FALLBACK] num_slides={num_slides} max_tok={max_tok_fatiar} "
              f"out_len={len(out2)} tail={out2[-200:]!r}", file=_sys.stderr, flush=True)
        paras = [p.strip() for p in re.split(r"\n\s*\n", artigo) if p.strip()]
        if not paras:
            raise ValueError("Fase 2 (fatiar) falhou e o artigo não tem parágrafos")
        paras = _consolidar_paragrafos(paras, num_slides)
        slides_raw = [
            {"texto": p, "image_type": "photo", "photo_topic": "",
             "photo_source": "stock", "image_from_link": None}
            for p in paras
        ]

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
               .replace("{min_chars}", str(11 * 320))
               .replace("{max_chars}", str(11 * 400))
               .replace("{num_slides}", "11"))
    return jsonify({"system": preview})


def _problemas_slide(texto, idx):
    """Valida um slide com as regras da casa (mesmo criterio do corretor da
    UI + limites de tamanho). Retorna lista de problemas legiveis."""
    probs = []
    for m in _detect_vicios_ia(texto):
        if m.get("short") == "Negrito mal usado":
            continue
        trecho = texto[m["offset"]:m["offset"] + m["length"]][:40]
        probs.append(f"vício de IA ({m.get('short')}): {trecho!r}")
    if "—" in texto or "–" in texto:
        probs.append("travessão (proibido; use vírgula/ponto/parênteses)")
    if texto.count("**") % 2:
        probs.append("negrito desbalanceado (** aberto sem fechar)")
    for block in texto.split("\n\n"):
        lines = block.split("\n")
        islist = any(re.match(r"^\s*[•→]", ln) for ln in lines)
        if islist:
            for ln in lines:
                if re.match(r"^\s*[•→]", ln) and len(ln) > 66:
                    probs.append(f"bullet/seta com {len(ln)} chars (máx 64)")
        elif len(block) > 112:
            probs.append(f"parágrafo de prosa com {len(block)} chars (máx ~108, 3 linhas)")
    # Slide raso: a reclamação nº 1. Capa pode ser mais enxuta.
    minimo = 220 if idx == 0 else 265
    if len(texto) < minimo:
        probs.append(f"slide raso ({len(texto)} chars; alvo 300-400) — aprofunde "
                     "com dado/mecanismo DO ARTIGO, sem encher linguiça")
    return probs


def _corrigir_slides_vicios(client, slides_out, artigo, usage_acc, max_rodadas=2):
    """FASE 2.5: valida cada slide e manda o modelo REESCREVER só os que têm
    problema (vício de IA, parágrafo longo, slide raso, travessão...). Fecha
    o loop que faltava no gerador: antes publicava com defeito e ninguém
    corrigia — a correção manual era o motivo do conteúdo sair 'inútil'."""
    relatorio = {"rodadas": 0, "corrigidos": [], "restantes": {}}
    for rodada in range(max_rodadas):
        pendentes = {i: _problemas_slide(s["texto"], i)
                     for i, s in enumerate(slides_out)
                     if _problemas_slide(s["texto"], i)}
        if not pendentes:
            break
        relatorio["rodadas"] = rodada + 1
        lista = []
        for i, probs in sorted(pendentes.items()):
            lista.append(f"SLIDE {i+1}:\n{slides_out[i]['texto']}\n"
                         "PROBLEMAS:\n- " + "\n- ".join(probs))
        prompt = (
            "Você revisa slides de um carrossel de análise econômica (estilo "
            "Varos: denso, direto, sem cara de IA). Reescreva APENAS os slides "
            "abaixo, corrigindo os problemas apontados em cada um.\n\n"
            "REGRAS DURAS: parágrafo de prosa com no máx ~105 caracteres "
            "(3 linhas); bullet (•) e seta (→) com no máx 62; 1 parágrafo "
            "INTEIRO em negrito (**assim**) por slide; slide entre 300 e 400 "
            "caracteres; sem travessão (— –); aspas duplas; sem frase de "
            "efeito/aforismo/antítese; sem dois-pontos de anúncio no meio de "
            "frase (dois-pontos SÓ antes de lista de bullets); zero clichê "
            "de IA. Para aprofundar slide raso, use SOMENTE fatos do "
            "ARTIGO-FONTE abaixo — NUNCA invente dado novo.\n\n"
            f"ARTIGO-FONTE (única fonte de fatos permitida):\n{artigo}\n\n"
            'Retorne SOMENTE JSON: {"slides":[{"i":N,"texto":"..."}]} '
            "com i = número do slide (1-based) que você corrigiu.\n\n"
            + "\n\n".join(lista)
        )
        try:
            resp = claude_call_with_retry(client, model=GERAR_MODEL,
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}])
            _acumular_usage(usage_acc, resp)
            out = _extrair_texto_resp(resp)
            if out.startswith("```"):
                out = re.sub(r"^```[a-z]*\n?", "", out)
                out = re.sub(r"\n?```$", "", out).strip()
            dados = _parse_claude_json(out)
            for item in (dados or {}).get("slides", []):
                try:
                    i = int(item.get("i", 0)) - 1
                except (ValueError, TypeError):
                    continue
                novo = str(item.get("texto") or "").strip()
                if 0 <= i < len(slides_out) and novo:
                    slides_out[i]["texto"] = _sanitizar_slide_varos(
                        _remover_dois_pontos_anuncio(_strip_em_dash(novo)))
                    if (i + 1) not in relatorio["corrigidos"]:
                        relatorio["corrigidos"].append(i + 1)
        except Exception as e:
            relatorio["erro"] = str(e)
            break
    relatorio["restantes"] = {
        i + 1: _problemas_slide(s["texto"], i)
        for i, s in enumerate(slides_out) if _problemas_slide(s["texto"], i)
    }
    return slides_out, relatorio


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
        # timeout alto: fase 1 pesquisa na web antes de escrever (2-4 min)
        client = _anthropic_lib.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=480.0)
        usage_acc = _novo_usage()
        # ══ GERACAO EM 2 FASES (estilo Varos): artigo corrido -> fatiar ══
        (titulo_gerado, slides_raw, artigo_gerado, legenda_gerada,
         hashtags_geradas, fase_debug) = _gerar_conteudo_2fases(
            client, topico, brief_enriched, imagens_block, num_slides,
            system_artigo, usage_acc=usage_acc
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

        # ══ FASE 2.5: VALIDAR + CORRIGIR ══ Roda o corretor da casa em cada
        # slide e manda o modelo reescrever os que tem vicio/raso/estouro.
        slides_out, correcao_rel = _corrigir_slides_vicios(
            client, slides_out, artigo_gerado, usage_acc)

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
        # Custo da geracao (tokens + buscas), mostrado no dashboard
        api_usage = dict(usage_acc)
        api_usage["custo_usd"] = _custo_usd(usage_acc)
        api_usage["model"] = GERAR_MODEL
        with get_db() as conn:
            conn.execute("""
                INSERT INTO carrosseis (slug, titulo, arquivo, num_slides, status,
                                        artigo, legenda, hashtags, api_usage)
                VALUES (?, ?, ?, ?, 'rascunho', ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    titulo=excluded.titulo, arquivo=excluded.arquivo,
                    num_slides=excluded.num_slides, artigo=excluded.artigo,
                    legenda=excluded.legenda, hashtags=excluded.hashtags,
                    api_usage=excluded.api_usage,
                    updated_at=datetime('now')
            """, (slug, titulo_gerado, nome, len(slides_out),
                  artigo_gerado, legenda_gerada, hashtags_str,
                  json.dumps(api_usage, ensure_ascii=False)))

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
            # Custo/uso da API desta geracao + o que a fase 2.5 corrigiu
            "usage": api_usage,
            "correcao": correcao_rel,
            "debug": {
                "system_used":      system_artigo,
                "user_prompt":      prompt_para_debug,
                "fluxo":            "2-fases (artigo + fatiar)",
                "artigo_gerado":    fase_debug.get("artigo", ""),
                "artigo_chars":     fase_debug.get("artigo_chars", 0),
                "prompt_fatiar":    fase_debug.get("prompt_fatiar", ""),
                "urls_fetched":     urls_info,
                "system_is_custom": bool(system_override),
                "model":            GERAR_MODEL,
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
    # No HTML os \n vem ESCAPADOS (literal '\'+'n'). Desescapa pra quebra real,
    # senao o '\n' literal gruda na palavra ('ano?\\n\\nA resposta') e quebra
    # os regex do detector que usam \b (vicios passavam batido no verificar).
    def _unesc(t):
        return t.replace("\\n", "\n").replace("\\`", "`").replace("\\\\", "\\").strip()
    return [{"idx": i, "text": _unesc(t)} for i, t in enumerate(textos)]


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

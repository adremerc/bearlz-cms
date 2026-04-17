# Bearlz CMS — Deploy Guide

## Testar localmente (antes de publicar)

```bash
cd "C:\Users\adren\Dropbox\PC\Downloads\bearlz-cms"
pip install -r requirements.txt
python app.py
```

Abrir: http://localhost:5000

---

## Compartilhar com Gabriel agora (sem deploy)

1. Instalar cloudflared (uma vez):
   https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

2. Com o servidor rodando (python app.py), abrir outro terminal:
   ```
   cloudflared tunnel --url http://localhost:5000
   ```

3. Copiar a URL que aparecer (ex: https://pleasant-elephant.trycloudflare.com)
4. Enviar para o Gabriel — ele acessa direto no browser

Limitação: funciona só enquanto seu PC estiver ligado e o servidor rodando.

---

## Deploy permanente no Render.com (site sempre online, grátis)

### 1. Criar repositório no GitHub

```bash
# Instalar Git se não tiver: https://git-scm.com/download/win
cd "C:\Users\adren\Dropbox\PC\Downloads\bearlz-cms"
git init
git add .
git commit -m "Bearlz CMS inicial"
```

Criar repositório privado em https://github.com/new (nome: bearlz-cms)
Depois copiar e rodar os dois comandos que o GitHub mostrar (git remote add + git push)

### 2. Conectar carrosseis ao repositório

Para que os carrosseis apareçam no site, a pasta carrosseis/ precisa estar junto:

```bash
# No repositório, criar um link simbólico ou copiar
# Opção mais simples: copiar a pasta carrosseis dentro de bearlz-cms/
xcopy "C:\Users\adren\Dropbox\PC\Downloads\carrosseis" "C:\Users\adren\Dropbox\PC\Downloads\bearlz-cms\carrosseis" /E /I
git add carrosseis/
git commit -m "Adicionar carrosseis existentes"
git push
```

### 3. Deploy no Render.com

1. Acessar https://render.com → Sign in com GitHub
2. New → Web Service
3. Conectar o repositório bearlz-cms
4. Configurações (Render detecta automaticamente pelo render.yaml):
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT`
5. Clicar em "Create Web Service"
6. Aguardar o deploy (~3 minutos)

URL gerada: https://bearlz-cms.onrender.com (ou similar)

### 4. Anotar a CMS_API_KEY

No painel do Render:
- Environment → Variables → CMS_API_KEY → copiar o valor gerado
- Colar em idea-bot/config.py:
  ```python
  CMS_URL     = "https://bearlz-cms.onrender.com"
  CMS_API_KEY = "valor-copiado-aqui"
  ```

### 5. Workflow após o deploy

Quando gerar novos carrosseis:
```bash
python gerar-lote.py
# → carrosseis gerados + registrados automaticamente no site

# Copiar HTMLs novos para o repositório e publicar:
xcopy "C:\Users\adren\Dropbox\PC\Downloads\carrosseis\carrossel-*-hoje.html" ".\carrosseis\" /Y
git add carrosseis/
git commit -m "Novos carrosseis"
git push
# Render faz o deploy automático em ~2 minutos
```

---

## Notas

- **Free tier do Render**: O site "dorme" após 15 minutos sem visitas.
  Na primeira visita após dormir, pode demorar ~30 segundos para carregar.
  Para uso profissional, o plano pago (US$7/mês) deixa sempre ativo.

- **SQLite**: O banco de dados (status das notas) fica no disco do Render.
  O render.yaml configura 1GB de disco persistente para isso.
  Os dados NÃO são perdidos entre deploys.

- **Segurança**: O site não tem login — qualquer pessoa com a URL pode ver.
  Se quiser proteção, adicione HTTP Basic Auth no app.py ou use Cloudflare Access.

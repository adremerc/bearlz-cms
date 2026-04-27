FROM python:3.11-slim

# Evita .pyc e força stdout sem buffer (logs aparecem no fly logs em tempo real)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Instala dependencias Python primeiro (cache de layer)
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copia o resto do codigo
COPY . .

# /app/data sera mountado como volume do Fly. Cria o ponto de mount caso o
# volume nao exista (primeiro deploy local, dev, etc).
RUN mkdir -p /app/data

EXPOSE 8080

# gunicorn.conf.py do repo eh auto-carregado pelo gunicorn (timeout=180 etc).
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "2"]

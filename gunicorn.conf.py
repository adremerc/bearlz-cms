# Config do gunicorn carregada automaticamente (o gunicorn procura por este
# arquivo no CWD antes de iniciar). Isto significa que mudancas aqui tem efeito
# no proximo `git push` sem precisar mexer no render.yaml ou fazer Manual Deploy.

# Timeout por request (segundos). /api/gerar chama Claude, que pode demorar
# 20-40s pra gerar 8-14 slides com max_tokens=6000. Default do gunicorn eh 30s,
# o que causava 500 + HTML de erro (frontend quebrava com "Unexpected token '<'").
timeout = 180

# Tempo para workers completarem requests em andamento antes de serem mortos
# durante reload/restart. Ajuda a evitar que requests em voo sejam abortados.
graceful_timeout = 60

# Quantos segundos um worker ocioso fica vivo antes de ser reciclado. Ajuda a
# liberar memoria no Render free tier.
keepalive = 5

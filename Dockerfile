# Всё окружение бота живёт внутри образа: свой Node, свой Python, свой Claude
# Code. На VPS ничего не ставится, кроме самого Docker, поэтому соседние
# проекты бот не задевает.
FROM node:22-bookworm-slim

# Claude Code CLI — Python SDK запускает его подпроцессом.
# git нужен самому CLI для части операций; ca-certificates — для HTTPS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

WORKDIR /app

COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY bot ./bot
COPY scripts ./scripts

# Работаем не от root. uid фиксирован: deploy.sh отдаёт ему права на workspace
# на стороне хоста, иначе в примонтированную папку не записать.
# .claude создаём заранее и отдаём боту: иначе Docker создаст её при монтировании
# от root, и CLI не сможет писать транскрипты сессий.
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/workspace /home/bot/.claude \
    && chown -R bot:bot /app /home/bot

USER bot

CMD ["python", "-m", "bot.main"]

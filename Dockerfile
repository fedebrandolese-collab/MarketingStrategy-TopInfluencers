# The wearable_wishlist wishlist in a container. One image, two services in docker-compose.yml: the dashboard and the scheduler.
# Code and pinned dependencies (requirements.lock, make lock) are in the image. data/ and config/ are mounted from the
# host, and the keys come from .env (env_file). Build with make docker-build. Runbook: docs/OPERATIONS.md.
FROM python:3.12-slim

# A container has no Claude Code login, so classification uses the Anthropic API (ANTHROPIC_API_KEY in .env).
ENV LLM_BACKEND=anthropic \
    WEARABLE_WISHLIST_NO_DESKTOP_NOTIFY=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Everything runs as this user, who must be able to write the mounted ./data and ./config. Build with your own ids
# (WEARABLE_WISHLIST_UID=$(id -u) WEARABLE_WISHLIST_GID=$(id -g) docker compose build) or give both directories to 1000:1000.
ARG WEARABLE_WISHLIST_UID=1000
ARG WEARABLE_WISHLIST_GID=1000
RUN groupadd --non-unique --gid "$WEARABLE_WISHLIST_GID" wearable_wishlist \
 && useradd --non-unique --uid "$WEARABLE_WISHLIST_UID" --gid "$WEARABLE_WISHLIST_GID" --create-home --shell /usr/sbin/nologin wearable_wishlist

WORKDIR /app
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
COPY app ./app
COPY db ./db
COPY config ./config
COPY .streamlit ./.streamlit
# Editable, as make setup does: config.ROOT is the directory above src/, so the code must run from /app/src, not from
# site-packages. pipeline.py rewrites COSTS.md at the root after each run, its only write outside data/ and config/.
RUN pip install --no-deps -e . \
 && python -c "from wearable_wishlist import config; assert str(config.ROOT) == '/app', config.ROOT" \
 && mkdir -p data \
 && touch COSTS.md \
 && chown wearable_wishlist:wearable_wishlist data COSTS.md

USER wearable_wishlist
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4)"
CMD ["streamlit", "run", "app/dashboard.py", "--server.address", "0.0.0.0", "--server.port", "8501", \
     "--server.headless", "true", "--server.fileWatcherType", "none", "--browser.gatherUsageStats", "false"]

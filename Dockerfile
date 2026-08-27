FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TABULITE_SOURCE_DIR=/project/source \
    TABULITE_WORKSPACE_DIR=/project/workspace \
    TABULITE_HOST=0.0.0.0 \
    TABULITE_PORT=8000

WORKDIR /app

# Install dependencies first so edits to src/ do not invalidate the layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# The project directory is bind-mounted at runtime; create the mount points and
# hand them to the non-root user the server runs as.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /project/source /project/workspace \
    && chown -R app:app /project
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["tabulite-mcp"]

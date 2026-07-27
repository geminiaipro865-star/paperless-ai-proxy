FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CHATGPT_TOKEN_FILE=/data/auth.json \
    PROXY_HOST=0.0.0.0 \
    PROXY_PORT=8080

WORKDIR /app

COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY chatgpt_proxy ./chatgpt_proxy
RUN pip install --no-cache-dir --no-deps -e .

# The token file lives here; mount a volume so the login survives a restart.
RUN mkdir -p /data \
    && useradd --system --uid 10001 --create-home chatgptproxy \
    && chown -R chatgptproxy /data
USER chatgptproxy

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).status==200 else 1)"

ENTRYPOINT ["chatgpt-proxy"]
CMD ["serve"]

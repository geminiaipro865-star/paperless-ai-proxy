FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CHATGPT_TOKEN_FILE=/data/auth.json \
    PROXY_HOST=0.0.0.0 \
    PROXY_PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY chatgpt_proxy ./chatgpt_proxy

# The token file lives here; mount a volume so the login survives a restart.
RUN mkdir -p /data \
    && useradd --system --uid 10001 --create-home chatgptproxy \
    && chown -R chatgptproxy /data
USER chatgptproxy

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).status==200 else 1)"

ENTRYPOINT ["python", "-m", "chatgpt_proxy"]
CMD ["serve"]

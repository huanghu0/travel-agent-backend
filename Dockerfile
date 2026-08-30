FROM ghcr.io/astral-sh/uv:0.9.5-python3.12-bookworm@sha256:19a8c92b461bbc32e8bd30c15132cec1d16c49c61f4359c9225262938485f513

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN python -c "import ssl; assert ssl.OPENSSL_VERSION.startswith('OpenSSL 3.')"

COPY requirements.txt requirements.txt
RUN uv pip install --system --no-cache -r requirements.txt

COPY alembic.ini alembic.ini
COPY main.py main.py
COPY app app
COPY migrations migrations
COPY scripts scripts

RUN python -m compileall -q app main.py scripts migrations

EXPOSE 8000

CMD ["sh", "-c", "python scripts/wait_for_dependencies.py && exec python -m uvicorn main:app --host 0.0.0.0 --port 8000"]

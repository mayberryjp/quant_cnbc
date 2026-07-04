FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git vim procps \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/mayberryjp/quant_cnbc.git .

RUN python3 -m pip install --upgrade pip \
    && python3 -m pip install -e ".[dev]" \
    && python3 -m pip install supervisor

CMD ["supervisord", "-c", "/app/supervisord.conf"]

# syntax=docker/dockerfile:1

# ---- Builder stage: compiliers and libraries ----
FROM dhi.io/python:3.13-debian13-dev AS builder

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  gcc \
  g++ \
  libgdal-dev \
  libmagic1t64 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY gunicorn.py manage.py pyproject.toml uv.lock ./
COPY harvester ./harvester
COPY indexer ./indexer
COPY prs ./prs
COPY referral ./referral
COPY reports ./reports

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/
RUN uv sync \
  --no-group dev \
  --link-mode=copy \
  --compile-bytecode \
  --no-python-downloads \
  --frozen \
  && rm -rf /bin/uv uv.lock

ENV PATH="/app/.venv/bin:$PATH"
RUN python -m compileall -q prs harvester indexer referral reports \
  && python manage.py collectstatic --noinput

# ---- runtime stage: minimal packages needed to run the application ----
FROM dhi.io/python:3.13-debian13-dev AS runtime
LABEL org.opencontainers.image.authors=asi@dbca.wa.gov.au
LABEL org.opencontainers.image.source=https://github.com/dbca-wa/prs

RUN apt-get update && apt-get install -y --no-install-recommends \
  gdal-bin \
  proj-bin \
  libgdal36 \
  libmagic1t64 \
  # Run shared library linker after installing spatial packages
  && ldconfig \
  && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

WORKDIR /app
COPY --from=builder --chown=nonroot:nonroot /app /app

ENV PYTHONUNBUFFERED=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  PATH="/app/.venv/bin:$PATH"

USER nonroot
EXPOSE 8080
CMD ["gunicorn", "prs.wsgi", "--config", "gunicorn.py"]

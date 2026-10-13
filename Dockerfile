# Dagu scheduler image with the onboarding scripts and their Python runtime baked in.
ARG DAGU_IMAGE=ghcr.io/dagucloud/dagu:alpine
FROM ${DAGU_IMAGE}

USER root
RUN apk add --no-cache python3 py3-pip ca-certificates \
 && python3 -m venv /opt/onboarding

COPY requirements.txt /app/requirements.txt
RUN /opt/onboarding/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY onboard.py workflow.py /app/
COPY helpers /app/helpers
COPY clients /app/clients
COPY configs /app/configs

# The Dagu image runs workflows as PUID/PGID (default 1000:1000). Keep these
# in step with PUID/PGID in docker-compose.yaml so the steps can write state.
ARG RUN_UID=1000
ARG RUN_GID=1000
RUN mkdir -p /var/lib/onboarding \
 && chown -R ${RUN_UID}:${RUN_GID} /var/lib/onboarding \
 && chmod 700 /var/lib/onboarding

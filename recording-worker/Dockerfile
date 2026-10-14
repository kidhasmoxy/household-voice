# Candidate build; review the final image digest before publication.
FROM python:3.14-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72
ARG BUILD_VERSION=0.0.3
ARG BUILD_ARCH
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="app" io.hass.arch="${BUILD_ARCH}" \
      org.opencontainers.image.licenses="MIT"
WORKDIR /app
COPY --chown=0:0 requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt && adduser -D -u 10001 worker
COPY --chown=0:0 recording_worker /app/recording_worker
COPY --chown=0:0 LICENSE /app/LICENSE
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER worker
CMD ["python", "-m", "recording_worker"]

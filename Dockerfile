FROM python:3.12-alpine

# Build arguments for metadata
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION

# Labels following OCI Image Format Specification
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.authors="amdschuurman" \
      org.opencontainers.image.url="https://github.com/amdschuurman/TwitchDropsMiner" \
      org.opencontainers.image.documentation="https://github.com/amdschuurman/TwitchDropsMiner/blob/main/README.md" \
      org.opencontainers.image.source="https://github.com/amdschuurman/TwitchDropsMiner" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.vendor="amdschuurman" \
      org.opencontainers.image.title="Arend's Twitch Drops Miner" \
      org.opencontainers.image.description="Automated Twitch drops mining application with web-based interface (forked from rangermix/TwitchDropsMiner with reliability and filter fixes)"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Set working directory
WORKDIR /app

# Install su-exec for privilege drop after chowning bind-mounted volumes.
RUN apk add --no-cache su-exec

# Create non-root user so the container does not run as root after the
# entrypoint re-execs the application. UID/GID 1000 matches the common host
# default so bind-mounted data/ files line up out of the box.
RUN addgroup -g 1000 -S tdm && adduser -u 1000 -S -G tdm -h /app tdm

# Copy project metadata and install dependencies
COPY pyproject.toml .

# Install Python dependencies
RUN pip install --no-cache-dir .

# Copy application code
COPY main.py ./
COPY src/ ./src/
COPY lang/ ./lang/
COPY icons/ ./icons/
COPY web/ ./web/

# Create data + log dirs owned by the non-root user with restrictive perms.
RUN mkdir -p /app/data /app/logs && chown -R tdm:tdm /app /app/data /app/logs && chmod 700 /app/data /app/logs

# Entrypoint fixes ownership of bind-mounted volumes that may still be owned
# by root from older container generations, then drops to the tdm user.
# Preserves existing data/cookies.jar (the Twitch session) across the upgrade.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Expose web port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/status')" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
# Run the application (web GUI is now default)
CMD ["python", "main.py"]

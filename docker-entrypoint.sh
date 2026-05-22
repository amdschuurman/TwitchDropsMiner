#!/bin/sh
# Container entrypoint.
#
# Ensures /app/data and /app/logs are owned by the unprivileged ``tdm`` user
# before dropping privileges. This preserves existing user data (cookies.jar,
# settings.json) when upgrading from an older image that ran as root and
# created root-owned files in the bind-mounted volume.
#
# If the container is already running as a non-root user (e.g. via
# ``docker run --user``), the chown is skipped and the command is executed
# in place.

set -e

if [ "$(id -u)" = "0" ]; then
    # Best effort: fix bind-mounted volumes that may still be root-owned.
    # We intentionally do not chmod the files — only directories — so any
    # restrictive perms applied by the application (cookies.jar 0600, etc.)
    # are preserved.
    chown -R tdm:tdm /app/data /app/logs 2>/dev/null || true
    chmod 700 /app/data /app/logs 2>/dev/null || true
    exec su-exec tdm "$@"
fi

exec "$@"

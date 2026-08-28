#!/bin/bash
# Railway wrapper around docker/entrypoint.sh.
#
# Runs as root for two things the base entrypoint cannot do for itself, then
# hands off to it as the unprivileged `repowise` user:
#
#   * Railway attaches persistent volumes owned by root, so /data would be
#     unwritable to a non-root process. chown it on every boot — it is cheap
#     when ownership is already correct.
#   * Railway injects $PORT and expects the service to listen on it. The base
#     entrypoint reads PORT_FRONTEND, so map one onto the other.
#   * HOME must be re-pointed. A `USER` directive makes Docker set HOME from
#     the passwd entry, but dropping privileges at runtime does not: the root
#     HOME=/root survives into the child, and the app then tries to write
#     ~/.repowise/provider_config.json to a directory it has no rights to.
#     Pointing it at the volume also means a saved API key outlives a deploy.
set -e

export PORT_FRONTEND="${PORT:-${PORT_FRONTEND:-3000}}"
export PORT_BACKEND="${PORT_BACKEND:-7337}"

APP_HOME=/data/home

mkdir -p /data "${APP_HOME}"
chown -R repowise:repowise /data 2>/dev/null || \
  echo "WARNING: could not chown /data; the volume may be read-only for the app user."

# Already unprivileged (Railway can be configured to run as non-root): just go.
if [ "$(id -u)" != "0" ]; then
  export HOME="${APP_HOME}"
  exec /app/entrypoint.sh
fi

# Drop privileges with whichever util-linux helper the base image ships.
# Each form sets HOME explicitly rather than trusting what it inherits.
if command -v setpriv >/dev/null 2>&1; then
  exec setpriv --reuid=repowise --regid=repowise --init-groups \
    env HOME="${APP_HOME}" /app/entrypoint.sh
elif command -v runuser >/dev/null 2>&1; then
  exec runuser -u repowise -- env HOME="${APP_HOME}" /app/entrypoint.sh
elif command -v su >/dev/null 2>&1; then
  exec su -s /bin/bash repowise -c "HOME='${APP_HOME}' /app/entrypoint.sh"
fi

echo "WARNING: no privilege-drop helper found; running as root."
exec /app/entrypoint.sh

#!/bin/sh
# Read-only deployment acceptance. No reload/recreate, no secret output.
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=${1:-.env}
compose() { docker compose --env-file "$ENV_FILE" "$@"; }

host_hash=$(sha256sum nginx.conf | cut -d ' ' -f 1)
mounted_hash=$(compose exec -T gateway sha256sum /etc/nginx/conf.d/default.conf | cut -d ' ' -f 1 | tr -d '\r')
if [ "$host_hash" != "$mounted_hash" ]; then
    echo 'FAIL: gateway still mounts a different nginx.conf. Reload cannot replace an old bind-mount inode.'
    echo 'After draining requests, recreate ONLY gateway; then run this check again.'
    exit 1
fi
if ! compose exec -T gateway grep -q 'gateway-routing: dynamic-backends-v1' /etc/nginx/conf.d/default.conf; then
    echo 'FAIL: dynamic-backends-v1 is missing from the mounted gateway configuration.'
    exit 1
fi
if ! compose exec -T gateway nginx -t >/dev/null 2>&1; then
    echo 'FAIL: nginx configuration validation failed.'
    exit 1
fi
echo 'PASS: host/mounted configuration hashes agree; dynamic routing marker and nginx syntax are valid.'

compose exec -T app0 /app/.venv/bin/python - <<'PY'
import json, os, socket, urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None

key = os.environ.get('CHATGPT2API_AUTH_KEY', '')
if not key:
    try:
        with open('/app/src_extract/config.json') as f:
            key = json.load(f).get('auth-key', '')
    except (OSError, ValueError):
        pass
if not key:
    raise SystemExit('FAIL: admin authentication unavailable; cannot verify owner routing.')
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
try:
    for path in ['/api/dashboard', '/api/accounts/maintenance-status']:
        request = urllib.request.Request('http://gateway'+path, headers={'Authorization':'Bearer '+key})
        with opener.open(request, timeout=12) as response:
            data = json.load(response)
        if path == '/api/dashboard':
            if data.get('runtime', {}).get('instance_name') != socket.gethostname():
                raise SystemExit('FAIL: gateway dashboard route is not app0, even though HTTP returned 200.')
        else:
            progress = data.get('maintenance_progress', {})
            if progress.get('owner') is not True or progress.get('instance') != 'app0':
                raise SystemExit('FAIL: maintenance status is not from the owner app0.')
            if not progress.get('available'):
                raise SystemExit('FAIL: app0 maintenance has not started or is unreachable; retry after startup.')
    print('PASS: dashboard routes to app0; authoritative maintenance telemetry is available.')
except (OSError, ValueError):
    raise SystemExit('FAIL: admin route verification failed; no credentials were printed.')
PY

"""Real nginx + Docker DNS/IP reuse regression, with an old-config control.

Run: python scripts/test_gateway_routing.py (requires a Linux Docker engine).
Creates only uniquely named disposable containers/network and synthetic HTTP
roles. No application DB, real tokens, generation service, or public listeners.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
PYTHON_IMAGE = "python:3.13-alpine"
NGINX_IMAGE = "nginx:1.29-alpine"


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError(f"docker {' '.join(args)}: {result.stderr}")
    return (result.stdout if check else result.stdout + result.stderr).strip()


def request(port, path, *, method="GET", body=None):
    req = Request(f"http://127.0.0.1:{port}{path}", data=body, method=method,
                  headers={"Host": "gateway-fixture.invalid"})
    try:
        response = urlopen(req, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"raw": raw.decode(errors="replace")[:200]}
        return response.status, data


def eventually(check, description, timeout=40):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last:
                return
        except (OSError, URLError) as exc:
            last = str(exc)
        time.sleep(0.5)
    raise AssertionError(f"Timed out: {description}; last={last!r}")


def main():
    docker("info", "--format", "{{.ServerVersion}}")
    config = (ROOT / "nginx.conf").read_text(encoding="utf-8")
    assert "gateway-routing: dynamic-backends-v1" in config
    for name, host in (("chatgpt2api_admin", "app0"), ("chatgpt2api_importer", "importer")):
        assert f"upstream {name}" in config and f"server {host}:80 resolve;" in config
        assert f"proxy_pass http://{name};" in config
        assert f"proxy_pass http://{host}:80;" not in config
    # Same role topology and requests; only static vs dynamic proxy resolution
    # differs. The control must reproduce 401 -> 404 on reuse of app0's old IP.
    legacy = config.replace("proxy_pass http://chatgpt2api_admin;", "proxy_pass http://app0:80;")
    legacy = legacy.replace("proxy_pass http://chatgpt2api_importer;", "proxy_pass http://importer:80;")
    prefix = "gateway-probe-" + uuid4().hex[:12]
    network = prefix + "-net"
    containers = []
    gateways = []

    def run(name, args, image, *command):
        full_name = prefix + "-" + name
        containers.append(full_name)
        docker("run", "-d", "--name", full_name, "--network", network, *args, image, *command)
        return full_name

    def backend(instance, role, aliases=(), ip=None):
        args = ["-e", f"BACKEND_ROLE={role}", "-e", f"BACKEND_INSTANCE={instance}",
                "-v", f"{ROOT / 'scripts/fixtures/gateway_backend.py'}:/fixture.py:ro"]
        for alias in aliases:
            args.extend(["--network-alias", alias])
        if ip:
            args.extend(["--ip", ip])
        return run(instance, args, PYTHON_IMAGE, "python", "/fixture.py")

    def ip_of(container):
        info = json.loads(docker("inspect", container))[0]
        return info["NetworkSettings"]["Networks"][network]["IPAddress"]

    try:
        docker("pull", PYTHON_IMAGE)
        docker("pull", NGINX_IMAGE)
        # An explicitly configured subnet permits intentional IP reuse. Pick an
        # unused private /24; never attach to or modify an existing network.
        existing = docker("network", "ls", "-q").splitlines()
        import ipaddress
        occupied = [ipaddress.ip_network(c["Subnet"]) for n in json.loads(docker("network", "inspect", *existing))
                    for c in ((n.get("IPAM") or {}).get("Config") or []) if c.get("Subnet")]
        subnet = next(ipaddress.ip_network(f"172.29.{i}.0/24") for i in range(256)
                      if not any(ipaddress.ip_network(f"172.29.{i}.0/24").overlaps(other)
                                 for other in occupied if other.version == 4))
        docker("network", "create", "--subnet", str(subnet), network)
        aliases = tuple(f"app{i}" for i in range(8))
        app = backend("app-v1", "app", aliases)
        importer = backend("importer-v1", "importer", ("importer",))
        with tempfile.TemporaryDirectory(prefix="gateway-routing-") as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            ports = {}
            for label, content in (("fixed", config), ("legacy", legacy)):
                path = root / (label + ".conf")
                path.write_text(content, encoding="utf-8")
                gateway = run(label, ["-p", "127.0.0.1::80", "-v", f"{path}:/etc/nginx/conf.d/default.conf:ro",
                                      "-v", f"{root / 'data'}:/app/src_extract/data:ro"], NGINX_IMAGE)
                gateways.append(gateway)
                docker("exec", gateway, "nginx", "-t")
                port = int(docker("port", gateway, "80/tcp").rsplit(":", 1)[1])
                ports[label] = port
                eventually(lambda: request(port, "/api/dashboard")[0] == 401, label + " startup")
            fixed, old = ports["fixed"], ports["legacy"]

            # Real route precedence, URI/query/body/Host preservation, fallback,
            # and no exposure of the internal-only endpoints.
            routes = [
                ("/api/dashboard", "app", 401), ("/api/logs?value=a%2Fb&limit=1", "app", 200),
                ("/api/account-import-jobs", "importer", 200),
                ("/api/account-import-jobs/job/logs?offset=1", "importer", 200),
                ("/account-import.html", "importer", 200),
                ("/v1/image-tasks/task?detail=1", "app", 200),
                ("/images/missing.png?key=synthetic", "app", 200),
                ("/image-thumbnails/missing.png?key=synthetic", "app", 200),
            ]
            for path, role, status in routes:
                actual, data = request(fixed, path)
                assert actual == status and data["role"] == role, (path, actual, data)
                assert data["path"] == path and data["host"] == "gateway-fixture.invalid", data
            assert request(fixed, "/internal/monitor/load")[0] == 404
            assert request(fixed, "/internal/monitor/account-maintenance")[0] == 404
            for path, role in (("/v1/images/generations", "app"), ("/api/account-import-jobs", "importer")):
                status, data = request(fixed, path, method="POST", body=b'{"synthetic":true}')
                assert status == 200 and data["role"] == role and data["method"] == "POST", data
                assert data["body"] == '{"synthetic":true}'
            print("PASS: route/role, URI/query/body/Host, image fallback and internal isolation", flush=True)

            req = Request(f"http://127.0.0.1:{fixed}/v1/images/generations?stream=1", data=b"{}", method="POST")
            with urlopen(req, timeout=10) as response:
                assert response.readline() == b"data: start\n"
                docker("exec", gateways[0], "nginx", "-s", "reload")
                assert response.read() == b"\ndata: done\n\n"
            print("PASS: graceful gateway reload preserves an in-flight synthetic SSE response", flush=True)

            old_app_ip = ip_of(app)
            docker("rm", "-f", app)
            backend("reused-app-ip", "importer", ip=old_app_ip)
            new_app = backend("app-v2", "app", aliases)
            assert ip_of(new_app) != old_app_ip
            eventually(lambda: request(fixed, "/api/dashboard") == (
                401, {"role": "app", "instance": "app-v2", "path": "/api/dashboard", "method": "GET",
                      "body": "", "host": "gateway-fixture.invalid"}), "admin DNS refresh without reload")
            # /version goes through the already-dynamic public pool. It remains
            # healthy even when the old admin proxy is pointing at the importer.
            eventually(lambda: request(old, "/version")[1].get("instance") == "app-v2", "legacy public pool refresh")
            status, wrong = request(old, "/api/dashboard")
            assert status == 404 and wrong["instance"] == "reused-app-ip", (status, wrong)
            for path in ("/api/logs", "/v1/image-tasks/task", "/images/missing.png", "/image-thumbnails/missing.png"):
                status, data = request(fixed, path)
                assert status == 200 and data["instance"] == "app-v2", (path, status, data)
            print("PASS: control reproduces healthy /version + dashboard 404; fixed proxy recovers correct app0", flush=True)

            old_importer_ip = ip_of(importer)
            docker("rm", "-f", importer)
            backend("reused-importer-ip", "app", ip=old_importer_ip)
            backend("importer-v2", "importer", ("importer",))
            eventually(lambda: request(fixed, "/api/account-import-jobs")[1].get("instance") == "importer-v2",
                       "importer DNS refresh without reload")
            status, wrong = request(old, "/api/account-import-jobs")
            assert status == 404 and wrong["instance"] == "reused-importer-ip", (status, wrong)
            for path in ("/api/account-import-jobs", "/api/account-import-jobs/job/logs?offset=1", "/account-import.html"):
                status, data = request(fixed, path)
                assert status == 200 and data["instance"] == "importer-v2", (path, status, data)
            print("PASS: importer IP reuse no longer leaves import routes pointing at the wrong role", flush=True)

            # git checkout/pull replaces the host inode. A single-file bind
            # mount still points at the old config; nginx -t/reload can succeed
            # without installing the new dynamic routing configuration.
            replacement = root / "next.conf"
            replacement.write_text(config, encoding="utf-8")
            replacement.replace(root / "legacy.conf")
            assert (root / "legacy.conf").read_text(encoding="utf-8") == config
            mounted = docker("exec", gateways[1], "cat", "/etc/nginx/conf.d/default.conf")
            assert mounted == legacy.strip() and mounted != config.strip()
            docker("exec", gateways[1], "nginx", "-t")
            docker("exec", gateways[1], "nginx", "-s", "reload")
            assert docker("exec", gateways[1], "cat", "/etc/nginx/conf.d/default.conf") == legacy.strip()
            docker("rm", "-f", gateways[1])
            gateways[1] = run("legacy", ["-p", "127.0.0.1::80", "-v",
                f"{root / 'legacy.conf'}:/etc/nginx/conf.d/default.conf:ro"], NGINX_IMAGE)
            port = int(docker("port", gateways[1], "80/tcp").rsplit(":", 1)[1])
            assert docker("exec", gateways[1], "cat", "/etc/nginx/conf.d/default.conf") == config.strip()
            eventually(lambda: request(port, "/api/dashboard")[1].get("instance") == "app-v2",
                       "recreated gateway binds new inode and routes to app0")
            print("PASS: replaced host inode stays stale across reload; gateway recreation binds the new config", flush=True)
    except BaseException:
        for gateway in gateways:
            print(docker("logs", "--tail", "30", gateway, check=False), flush=True)
        raise
    finally:
        for container in reversed(containers):
            docker("rm", "-f", container, check=False)
        docker("network", "rm", network, check=False)


if __name__ == "__main__":
    main()

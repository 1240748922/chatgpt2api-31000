from sms_tool import registration_preflight as module


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class _Session:
    def __init__(self):
        self.proxies = None
        self.trust_env = True
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return _Response(401 if "usage" in url else 200)

    def close(self):
        pass


def test_report_runs_the_same_four_registration_stages(monkeypatch):
    session = _Session()
    monkeypatch.setattr(module.curl_requests, "Session", lambda: session)
    monkeypatch.setattr(module, "curl_cffi_capabilities", lambda: {"version_ok": True})
    monkeypatch.setattr(module, "auth_fingerprint_capabilities", lambda: {"missing": []})
    monkeypatch.setattr(module, "auth_impersonate", lambda: "chrome146")
    monkeypatch.setattr(module, "current_auth_fingerprint", lambda: {"impersonate": "chrome146"})
    monkeypatch.setattr(module, "_resolve_proxy_scheme", lambda value: value)
    monkeypatch.setattr(module, "_preflight_checks", lambda: (
        ("chatgpt-login", "https://chatgpt.com/login", "https://chatgpt.com/", False),
        ("auth-login", "https://auth.openai.com/log-in", "https://chatgpt.com/login", False),
        ("sentinel-frame", "https://sentinel.openai.com/frame", "https://auth.openai.com/log-in", False),
        ("chatgpt-backend", "https://chatgpt.com/usage", "https://chatgpt.com/", True),
    ))

    result = module.registration_network_preflight_report("http://proxy.example:8080", proxy_attempts=1)

    assert result["ok"] is True
    assert [item["name"] for item in result["checks"]] == [
        "chatgpt-login", "auth-login", "sentinel-frame", "chatgpt-backend"
    ]
    assert result["checks"][-1]["expected_http_error"] is True
    assert session.proxies == {"http": "http://proxy.example:8080", "https": "http://proxy.example:8080"}

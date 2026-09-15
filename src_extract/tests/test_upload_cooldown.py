from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from threading import Condition

import pytest

from services.account_capabilities import upload_blocked, record_upload_throttle
from services.account_service import AccountService
from services.account_view import account_row
from services.image_failure import image_failure, classify_image_exception
from utils.helper import UpstreamHTTPError, parse_retry_after


def test_upload_429_records_only_upload_cooldown(monkeypatch):
    service = AccountService.__new__(AccountService)
    service._image_slot_condition = Condition()
    service._accounts = OrderedDict(token={
        "access_token": "token", "status": "正常", "quota": 8,
        "image_quota_unknown": False, "last_remote_checked_at": datetime.now(timezone.utc).isoformat(),
    })
    monkeypatch.setattr(service, "_resolve_access_token_locked", lambda token: token)
    monkeypatch.setattr(service, "_release_image_slot_locked", lambda _: None)
    monkeypatch.setattr(service, "_save_accounts", lambda **_: True)
    monkeypatch.setattr(service, "_schedule_account_refresh_after_image_failure", lambda *a, **kw: pytest.fail("unnecessary auth verification"))
    error = UpstreamHTTPError("/backend-api/files", 429, {"error": "file upload throttled"}, retry_after=3600)
    failure = classify_image_exception(error)
    assert failure.code == "file_upload_throttled"
    assert failure.switch_account is True
    result = service.mark_image_result("token", False, failure=failure)
    assert result["status"] == "正常"
    assert result["quota"] == 8
    assert result["image_quota_unknown"] is False
    assert result["fail"] == 1
    assert upload_blocked(result)
    row = account_row(result, available=True, unlimited_quota=False)
    assert row["file_upload_limited"] is True
    assert row["status_label"] == "上传受限 · 可文生图"
    assert row["status_tone"] == "warning"


def test_retry_after_is_respected_and_overlaps_never_shorten_cooldown():
    account = {}
    record_upload_throttle(account, 3600, 1000)
    record_upload_throttle(account, 60, 1100)
    assert account["file_upload_blocked_until"] == 4600
    assert upload_blocked(account, 4599)
    assert not upload_blocked(account, 4600)


@pytest.mark.parametrize("previous", ["broken", None, float("nan")])
def test_bad_persisted_cooldown_does_not_break_failure_handling(previous):
    account = {"file_upload_blocked_until": previous}
    record_upload_throttle(account, 60, 1000)
    assert account["file_upload_blocked_until"] == 1060


@pytest.mark.parametrize("code", ["upstream_rate_limited", "image_quota_exhausted"])
def test_non_upload_429_does_not_rotate_credentials(code):
    assert image_failure(code).switch_account is False


def test_reference_upload_429_rotates_to_another_account():
    assert image_failure("file_upload_throttled").switch_account is True


def test_retry_after_accepts_seconds_and_http_date():
    now = datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after("120", now=now) == 120
    assert parse_retry_after("0", now=now) == 0
    assert parse_retry_after("Tue, 15 Sep 2026 00:02:00 GMT", now=now) == 120
    assert parse_retry_after("broken", now=now) is None


def test_upload_limit_response_preserves_retry_after():
    from services.log_service import _image_error_response
    from services.protocol.conversation import ImageGenerationError
    error = ImageGenerationError("upload limited", failure=image_failure("file_upload_throttled", retry_after=120))
    response = _image_error_response(error)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "120"
    assert b"file_upload_throttled" in response.body

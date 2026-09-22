"""Only synthetic credentials; no input files or developer account store."""
import base64
import json


def cleanup_accounts():
    def expired(label):
        data = base64.urlsafe_b64encode(json.dumps({"exp": 1, "sub": label}).encode()).decode().rstrip("=")
        return f"test.{data}.not-a-signature"

    rows = [
        dict(name="expired-no-rt", access_token=expired("expired"), status="正常", quota=10),
        dict(name="remote-invalid", access_token="synthetic-remote-invalid", status="正常", quota=18,
             last_remote_check_result="invalid"),
        dict(name="both-invalid", access_token=expired("both"), refresh_token="synthetic-revoked-rt",
             refresh_token_invalid_at="2026-01-01T00:00:00+00:00", status="限流", quota=0),
        dict(name="stored-abnormal", access_token="synthetic-stored-abnormal", status="异常", quota=3),
        dict(name="recoverable", access_token=expired("recoverable"), refresh_token="synthetic-recovery-rt", status="正常", quota=5),
        dict(name="healthy", access_token="synthetic-healthy", status="正常", quota=9),
        dict(name="quota-only", access_token="synthetic-quota-only", status="限流", quota=0),
        dict(name="timeout-only", access_token="synthetic-timeout-only", status="正常", quota=7,
             last_remote_check_result="error", last_refresh_error="Image generation timed out"),
        dict(name="disabled", access_token=expired("disabled"), status="禁用", quota=10),
    ]
    for row in rows:
        row.update(email=row["name"] + "@example.test", management_id=row["name"], type="free")
    return rows

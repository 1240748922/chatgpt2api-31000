"""Exact observed refusal templates, not customer prompts or real accounts."""
import pytest

from services.image_failure import (
    _POLICY_NOTICE_TEXTS, classify_message_facts, classify_upstream_message, public_image_error_message,
)


@pytest.mark.parametrize("notice", sorted(_POLICY_NOTICE_TEXTS))
def test_terminal_notice_is_request_refusal_not_account_failure(notice):
    failure = classify_upstream_message({"author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": [notice]}, "status": "finished_successfully"})
    assert failure.code == "content_policy_violation"
    assert failure.status_code == 400 and failure.scope == "request"
    assert not failure.switch_account and not failure.account_failure and not failure.retryable
    assert public_image_error_message(failure) == notice


@pytest.mark.parametrize("overrides", [{"role": "user"}, {"status": "in_progress"},
                                     {"content_type": "code"}, {"has_image_output": True}])
def test_other_contexts_do_not_trigger_notice_matching(overrides):
    fields=dict(role="assistant", content_type="text", status="finished_successfully", has_text=True,
                raw_detail=next(iter(_POLICY_NOTICE_TEXTS)))
    failure=classify_message_facts(**{**fields, **overrides})
    assert failure is None or failure.code != "content_policy_violation"


def test_quoted_explanation_is_still_plain_text_reply():
    failure=classify_message_facts(role="assistant", content_type="text", end_turn=True, has_text=True,
                                  raw_detail='引用："'+next(iter(_POLICY_NOTICE_TEXTS))+'"')
    assert failure.code == "upstream_text_reply"

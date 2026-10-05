"""Exact observed refusal templates, not customer prompts or real accounts."""
import pytest

from services.image_failure import (
    _IMAGE_GENERATION_ERROR_TEXTS, _POLICY_NOTICE_TEXTS, classify_message_facts,
    classify_upstream_message, public_image_error_message,
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


@pytest.mark.parametrize("notice", sorted(_IMAGE_GENERATION_ERROR_TEXTS))
def test_terminal_image_generation_error_rotates_without_marking_account_invalid(notice):
    failure = classify_upstream_message({"author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": [notice]}, "status": "finished_successfully"})
    assert failure.code == "upstream_image_generation_error"
    assert failure.status_code == 502 and failure.scope == "transient"
    assert failure.switch_account and not failure.account_failure and failure.retryable
    assert public_image_error_message(failure) == "The image generation tool encountered an error. Please try again."


@pytest.mark.parametrize("text", [
    "我无法生成这张图片。",
    "请上传参考图后再试。",
    "用户提示：由于我这边发生了错误，我未能生成图片。",
])
def test_generic_or_quoted_image_text_does_not_rotate(text):
    failure = classify_upstream_message({"author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": [text]}, "status": "finished_successfully"})
    assert failure is None or failure.code != "upstream_image_generation_error"

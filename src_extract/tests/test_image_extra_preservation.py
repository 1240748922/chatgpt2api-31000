"""Archive genuine extras privately without changing the image API contract."""
import base64
import json
from collections import OrderedDict
from threading import Lock
from types import SimpleNamespace

import pytest

from services import image_task_service as tasks
from services import log_service as logs
from services.protocol import conversation as protocol
from test_image_result_count import multi_asset
from test_log_detail_api import log_api
from test_image_account_retries import retry_flow
from test_image_account_selection import _account


def format_fixture(fixture, images=None, **kwargs):
    return protocol.format_image_result(
        [{"b64_json": base64.b64encode(data).decode("ascii")}
         for data in (fixture.images if images is None else images)],
        "fixture prompt", "b64_json", max_images=1, index=1, **kwargs,
    )


def test_archives_originals_without_upscale_or_public_base64(monkeypatch, multi_asset):
    upscaled = []
    monkeypatch.setattr(protocol, "upscale_image_if_needed",
                        lambda data, *a, **kw: upscaled.append(data) or data)
    result = format_fixture(multi_asset)
    assert upscaled == multi_asset.images[:1]
    assert multi_asset.saved == multi_asset.images
    assert result["_image_result_counts"] == {
        "image_candidate_count": 3, "image_unique_count": 3,
        "image_returned_count": 1, "image_saved_count": 3,
        "image_extra_saved_count": 2, "image_extra_failed_count": 0,
    }
    assert len(result["_image_extras"]) == 2
    assert all(set(extra) == {"url", "width", "height", "slot"}
               and extra["slot"] == 1 for extra in result["_image_extras"])
    public = logs._strip_internal_response_fields(result)
    assert len(public["data"]) == 1
    assert set(public) == {"created", "data"}
    assert set(logs._collect_urls(result)) == {result["data"][0]["url"]}
    metrics = logs.image_result_metrics(result)
    assert metrics["result_b64_count"] == metrics["result_data_count"] == 1
    assert metrics["extra_images"] == result["_image_extras"]


def test_duplicate_bytes_do_not_create_extra_archive(multi_asset):
    a, b, _ = multi_asset.images
    result = format_fixture(multi_asset, [a, a, b])
    assert multi_asset.saved == [a, b]
    counts = result["_image_result_counts"]
    assert counts["image_candidate_count"] == 3
    assert counts["image_unique_count"] == counts["image_saved_count"] == 2
    assert counts["image_extra_saved_count"] == 1


@pytest.mark.parametrize("failure", [OSError, TimeoutError])
def test_extra_archive_failure_preserves_primary_and_continues(monkeypatch, multi_asset, failure):
    attempts = []
    def save(data, *args, **kwargs):
        attempts.append(data)
        if data == multi_asset.images[1]:
            raise failure("fixture storage failure")
        return f"http://localhost/images/{len(attempts)}.png"
    monkeypatch.setattr(protocol, "save_image_bytes", save)
    result = format_fixture(multi_asset)
    assert attempts == multi_asset.images
    assert len(result["data"]) == len(result["_image_extras"]) == 1
    counts = result["_image_result_counts"]
    assert counts["image_extra_failed_count"] == 1
    assert counts["image_saved_count"] == 2
    assert counts["image_extra_saved_count"] == 1


def test_primary_archive_failure_still_raises(monkeypatch, multi_asset):
    def save(*args, **kwargs):
        raise OSError("primary storage failure")
    monkeypatch.setattr(protocol, "save_image_bytes", save)
    with pytest.raises(OSError, match="primary storage failure"):
        format_fixture(multi_asset)


def test_malformed_extra_does_not_discard_primary_or_later_extras(multi_asset):
    result = protocol.format_image_result([
        {"b64_json": base64.b64encode(multi_asset.images[0]).decode()},
        {"b64_json": "invalid-base64"},
        {"b64_json": base64.b64encode(multi_asset.images[2]).decode()},
    ], "fixture", "b64_json", max_images=1)
    assert len(result["data"]) == len(result["_image_extras"]) == 1
    assert result["_image_result_counts"]["image_extra_failed_count"] == 1
    assert multi_asset.saved == [multi_asset.images[0], multi_asset.images[2]]


def test_empty_extra_storage_url_is_reported(monkeypatch, multi_asset):
    monkeypatch.setattr(protocol, "save_image_bytes", lambda data, *a, **kw:
                        "http://localhost/images/primary.png" if data == multi_asset.images[0] else "")
    result = format_fixture(multi_asset)
    assert len(result["data"]) == 1
    assert result["_image_result_counts"]["image_extra_failed_count"] == 2
    assert result["_image_result_counts"]["image_saved_count"] == 1


@pytest.mark.parametrize("failed", [False, True])
def test_cleanup_retains_upstream_conversation_if_archive_is_incomplete(monkeypatch, failed):
    monkeypatch.setattr(protocol, "config", SimpleNamespace(image_remove_conversation_after_result=True))
    deleted = []
    backend = SimpleNamespace(delete_conversation=deleted.append)
    output = protocol.ImageOutput(kind="result", model="gpt-image-2", index=1, total=1,
        conversation_id="fixture-conversation",
        result_counts={"image_extra_failed_count": int(failed)})
    protocol._cleanup_image_conversations_after_success(backend, iter([output]))
    assert deleted == ([] if failed else ["fixture-conversation"])


def test_extra_archive_failure_does_not_retry_upstream(monkeypatch, retry_flow, multi_asset):
    retry_flow.service._accounts = OrderedDict(
        (str(i), _account(str(i), quota=8, unknown=False)) for i in range(2))
    generated = []
    def save(data, *args, **kwargs):
        if data != multi_asset.images[0]:
            raise TimeoutError("fixture archive deadline")
        return "http://localhost/images/primary.png"
    monkeypatch.setattr(protocol, "save_image_bytes", save)
    def stream(backend, request, index, total):
        generated.append(backend.token)
        yield protocol._image_result_output_from_urls(
            multi_asset.backend, request, "fixture-conversation", multi_asset.urls, index, total)
    monkeypatch.setattr(protocol, "stream_image_outputs", stream)
    outputs = protocol._generate_single_image(protocol.ConversationRequest(
        model="gpt-image-2", response_format="b64_json"), 1, 1)
    assert generated == retry_flow.selected == retry_flow.closed == ["0"]
    assert retry_flow.outcomes == [("0", True)]
    assert len(outputs[0].data) == 1
    assert outputs[0].result_counts["image_extra_failed_count"] == 2


def fixture_output(multi_asset):
    return protocol._image_result_output_from_urls(
        multi_asset.backend,
        protocol.ConversationRequest(model="gpt-image-2", n=1, response_format="b64_json"),
        "fixture-conversation", multi_asset.urls, 1, 1,
    )


def test_stream_logs_archive_metadata_once(multi_asset):
    output = fixture_output(multi_asset)
    chunks = list(protocol.stream_image_chunks([output], partial_images=3))
    assert len(chunks) == 1
    assert len(chunks[0]["_image_extras"]) == 2
    metrics = logs.image_result_metrics(chunks)
    assert metrics["image_returned_count"] == metrics["result_b64_count"] == 1
    assert metrics["image_extra_saved_count"] == 2
    assert "_image_extras" not in logs._strip_internal_response_fields(chunks)[0]


@pytest.mark.parametrize("endpoint", ["images", "chat", "responses"])
def test_stream_protocols_log_extras_once_and_strip_public_output(monkeypatch, multi_asset, endpoint):
    from services.protocol import openai_v1_chat_complete as chat
    from services.protocol import openai_v1_response as responses
    output = fixture_output(multi_asset)
    monkeypatch.setattr(responses, "count_text_tokens", lambda *a: 1)
    if endpoint == "images":
        events = protocol.stream_image_chunks([output])
        path = "/v1/images/generations"
    elif endpoint == "chat":
        events = chat.stream_image_chat_completion([output], "gpt-image-2")
        path = "/v1/chat/completions"
    else:
        events = responses.stream_image_response([output], "fixture", "gpt-image-2")
        path = "/v1/responses"
    call = logs.LoggedCall({}, path, "gpt-image-2", "fixture", image_request=True)
    recorded = []
    call.log = lambda *args, **kwargs: recorded.append(kwargs)
    public = list(call.stream(events))
    assert len(recorded) == 1
    counts = recorded[0]["extra"]
    assert counts["image_returned_count"] == 1
    assert counts["image_saved_count"] == 3
    assert counts["image_extra_saved_count"] == len(counts["extra_images"]) == 2
    public_json = json.dumps(public)
    assert "_image_result_counts" not in public_json
    assert "_image_extras" not in public_json
    assert all(extra["url"] not in public_json for extra in output.extra_images)
    assert set(recorded[0]["urls"]) == set(output.image_urls)


def test_multi_slot_stream_sums_counts_and_preserves_slot_link(multi_asset):
    outputs = [protocol._image_result_output_from_urls(
        multi_asset.backend, protocol.ConversationRequest(model="gpt-image-2", n=2, response_format="b64_json"),
        f"fixture-{index}", multi_asset.urls, index, 2,
    ) for index in (1, 2)]
    call = logs.LoggedCall({}, "/v1/images/generations", "gpt-image-2", "fixture", image_request=True)
    recorded = []
    call.log = lambda *args, **kwargs: recorded.append(kwargs)
    public = list(call.stream(protocol.stream_image_chunks(outputs)))
    assert len(public) == 2
    metrics = recorded[0]["extra"]
    assert metrics["image_returned_count"] == 2
    assert metrics["image_extra_saved_count"] == 4
    assert [extra["slot"] for extra in metrics["extra_images"]] == [1, 1, 2, 2]


@pytest.mark.parametrize("endpoint", ["chat", "responses"])
def test_nonstream_protocols_preserve_private_archive_metadata(monkeypatch, multi_asset, endpoint):
    from services.protocol import openai_v1_chat_complete as chat
    from services.protocol import openai_v1_response as responses
    output = fixture_output(multi_asset)
    if endpoint == "chat":
        monkeypatch.setattr(chat, "chat_image_args", lambda body: ("gpt-image-2", "fixture", 1, [], None))
        monkeypatch.setattr(chat, "stream_image_outputs_with_pool", lambda request: iter([output]))
        monkeypatch.setattr(chat, "count_text_tokens", lambda *a: 1)
        result = chat.image_chat_response({})
    else:
        monkeypatch.setattr(responses, "count_text_tokens", lambda *a: 1)
        result = responses.collect_response(responses.stream_image_response([output], "fixture", "gpt-image-2"))
    assert result["_image_extras"] == output.extra_images
    assert result["_image_result_counts"] == output.result_counts
    public = json.dumps(logs._strip_internal_response_fields(result))
    assert all(extra["url"] not in public for extra in output.extra_images)


def test_persisted_admin_detail_lists_all_images_but_counts_only_primary(log_api, multi_asset):
    service, client = log_api
    result = format_fixture(multi_asset)
    service.append_item({"id": "fixture-extra-log", "type": "call", "summary": "fixture", "detail": {
        "endpoint": "/v1/images/generations", "model": "gpt-image-2",
        "status": "success", "request_meta": {"n": 1},
        "urls": result["_image_urls"], "call_id": "fixture-call",
        **logs.image_result_metrics(result),
    }})
    response = client.get("/api/logs/fixture-extra-log")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert len(detail["image_urls"]) == 3
    assert len(detail["raw_detail"]["extra_images"]) == 2
    assert detail["raw_detail"]["call_id"] == "fixture-call"
    fields = detail["detail_presentation"]["primary_fields"]
    assert any(field["label"] == "接口返回张数" and str(field["value"]) == "1" for field in fields)
    assert any(field["label"] == "额外保存张数" and str(field["value"]) == "2" for field in fields)
    from services.call_view import call_image_counts, call_image_urls
    item = {"detail": detail["raw_detail"]}
    assert call_image_counts(item)[:2] == (1, 1)
    assert len(call_image_urls(item)) == 1


def test_completed_task_persists_archive_and_call_link(tmp_path, multi_asset):
    result = format_fixture(multi_asset)
    service = tasks.ImageTaskService.__new__(tasks.ImageTaskService)
    service.path = tmp_path / "tasks.json"
    service._lock = Lock()
    service._tasks = {"fixture": {"id": "fixture-task", "owner_id": "fixture-owner",
                                  "status": tasks.TASK_STATUS_RUNNING, "n": 1}}
    service._update_task(
        "fixture", status=tasks.TASK_STATUS_SUCCESS, data=result["data"],
        extra_images=result["_image_extras"], image_result_counts=result["_image_result_counts"],
        call_id="fixture-call",
    )
    restored = next(iter(service._load_locked().values()))
    assert restored["extra_images"] == result["_image_extras"]
    assert restored["image_result_counts"] == result["_image_result_counts"]
    assert restored["call_id"] == "fixture-call"
    assert len(restored["data"]) == 1
    from services.image_task_view import image_task_row
    public = image_task_row(restored)
    assert "extra_images" not in public


def test_task_execution_keeps_extras_with_request_log(monkeypatch, tmp_path, multi_asset):
    result = format_fixture(multi_asset)
    service = tasks.ImageTaskService.__new__(tasks.ImageTaskService)
    service.path = tmp_path / "tasks.json"
    service._lock = Lock()
    service._tasks = {"fixture": {"id": "fixture-task", "owner_id": "fixture-owner",
                                  "status": tasks.TASK_STATUS_QUEUED, "n": 1}}
    service.generation_handler = lambda body: result
    recorded, gallery = [], []
    service._log_call = lambda *args, **kwargs: recorded.append(kwargs)
    monkeypatch.setattr(tasks, "realtime_monitor_service", SimpleNamespace(
        start=lambda *a, **kw: None, stage=lambda *a, **kw: None))
    monkeypatch.setattr(tasks, "auto_push_gallery_urls", lambda urls, **kw: gallery.append(urls))
    service._run_task("fixture", "generate", {"prompt": "fixture", "n": 1}, {}, "gpt-image-2",
                      protocol.time.time(), protocol.time.perf_counter())
    task = service._tasks["fixture"]
    assert task["status"] == tasks.TASK_STATUS_SUCCESS
    assert task["extra_images"] == result["_image_extras"]
    assert task["call_id"] == recorded[-1]["call_id"]
    assert recorded[-1]["result"]["_image_extras"] == result["_image_extras"]
    assert gallery == [result["_image_urls"]]

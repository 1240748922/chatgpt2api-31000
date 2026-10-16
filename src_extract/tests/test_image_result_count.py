"""Keep upstream multi-asset replies within the requested image count, offline."""
import base64
from io import BytesIO
from threading import Lock
from types import SimpleNamespace

import pytest
from PIL import Image

from services.protocol import conversation as protocol
from services.protocol import openai_v1_image_edit, openai_v1_image_generations


@pytest.fixture
def multi_asset(monkeypatch):
    images = []
    for color in ("red", "green", "blue"):
        buffer = BytesIO()
        Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
        images.append(buffer.getvalue())
    saved = []
    lock = Lock()

    def save(data, *args, **kwargs):
        with lock:
            saved.append(data)
            return f"http://localhost/images/result-{len(saved)}.png"

    monkeypatch.setattr(protocol, "save_image_bytes", save)
    monkeypatch.setattr(protocol, "upscale_image_if_needed", lambda data, *a, **kw: data)
    monkeypatch.setattr(protocol, "count_text_tokens", lambda *a: 1)
    monkeypatch.setattr(openai_v1_image_generations, "count_text_tokens", lambda *a: 1)
    monkeypatch.setattr(openai_v1_image_edit, "count_text_tokens", lambda *a: 1)
    monkeypatch.setattr(openai_v1_image_edit, "count_image_inputs_tokens", lambda *a: 1)
    backend = SimpleNamespace(download_image_bytes=lambda urls: list(images))
    urls = [f"https://example.invalid/output-{i}.png" for i in range(3)]
    return SimpleNamespace(images=images, saved=saved, backend=backend, urls=urls)


@pytest.mark.parametrize("response_format", ["url", "b64_json"])
@pytest.mark.parametrize("path", ["", "stream_timeout_recovery"])
def test_one_upstream_turn_with_three_assets_returns_one(multi_asset, response_format, path):
    request = protocol.ConversationRequest(model="gpt-image-2", n=1, response_format=response_format)
    output = protocol._image_result_output_from_urls(
        multi_asset.backend, request, "fixture-conversation", multi_asset.urls, 1, 1, path=path,
    )
    assert len(output.data) == 1
    assert len(output.image_urls) == 1
    assert multi_asset.saved == multi_asset.images
    assert output.data[0]["url"] == output.image_urls[0]
    if response_format == "b64_json":
        assert base64.b64decode(output.data[0]["b64_json"]) == multi_asset.images[0]


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("n", [1, 2, 4])
@pytest.mark.parametrize("response_format", ["url", "b64_json"])
def test_pool_limits_each_slot_without_losing_requested_slots(monkeypatch, multi_asset, parallel, n, response_format):
    monkeypatch.setattr(protocol, "config", SimpleNamespace(
        image_request_timeout_secs=30, image_parallel_generation=parallel,
    ))

    def generate(request, index, total):
        return [protocol._image_result_output_from_urls(
            multi_asset.backend, request, f"fixture-{index}", multi_asset.urls, index, total,
        )]

    monkeypatch.setattr(protocol, "_generate_single_image", generate)
    request = protocol.ConversationRequest(model="gpt-image-2", n=n, response_format=response_format)
    outputs = list(protocol.stream_image_outputs_with_pool(request))
    callbacks = []
    result = protocol.collect_image_outputs(outputs, result_callback=callbacks.append)
    assert len(result["data"]) == n
    assert len(result["_image_urls"]) == n
    assert len(result["_image_extras"]) == 2 * n
    assert result["_image_result_counts"]["image_returned_count"] == n
    assert len(multi_asset.saved) == 3 * n
    assert sorted(output.index for output in outputs) == list(range(1, n + 1))
    assert all(output.total == n and len(output.data) == 1 for output in outputs)
    assert [len(items) for items in callbacks] == list(range(1, n + 1))
    if response_format == "b64_json":
        chunks = list(protocol.stream_image_chunks(outputs, partial_images=3))
        assert len(chunks) == n
        assert all(item["type"] == "image_generation.completed" for item in chunks)
        assert all(len(item["_image_urls"]) == 1 for item in chunks)


@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_generate_and_edit_handlers_return_one_image(monkeypatch, multi_asset, edit, stream):
    def generate(request, index, total):
        return [protocol._image_result_output_from_urls(
            multi_asset.backend, request, "fixture-conversation", multi_asset.urls, index, total,
        )]

    monkeypatch.setattr(protocol, "_generate_single_image", generate)
    body = {"prompt": "a colored square", "model": "gpt-image-2", "n": 1,
            "stream": stream, "response_format": "b64_json", "partial_images": 3}
    if edit:
        body["images"] = [(multi_asset.images[1], "input.png", "image/png")]
    handler = openai_v1_image_edit.handle if edit else openai_v1_image_generations.handle
    result = handler(body)
    if stream:
        events = list(result)
        assert len(events) == 1
        assert events[0]["type"] == ("image_edit.completed" if edit else "image_generation.completed")
    else:
        assert len(result["data"]) == 1
    assert multi_asset.saved == multi_asset.images


def test_codex_repeated_events_deduplicate_and_archive_distinct_extras(multi_asset):
    encoded = [base64.b64encode(data).decode("ascii") for data in multi_asset.images]
    first = {"type": "image_generation_call", "result": encoded[0]}
    backend = SimpleNamespace(iter_codex_image_response_events=lambda **kw: iter([
        {"type": "response.output_item.done", "item": first},
        {"type": "response.completed", "response": {"output": [
            first, {"type": "image_generation_call", "result": encoded[1]},
        ]}},
    ]))
    outputs = list(protocol.stream_codex_image_outputs(
        backend, protocol.ConversationRequest(model="gpt-image-2", n=1),
    ))
    result = protocol.collect_image_outputs(outputs)
    assert len(result["data"]) == 1
    assert multi_asset.saved == multi_asset.images[:2]
    assert len(result["_image_extras"]) == 1


def test_formatter_default_preserves_existing_multi_image_callers(multi_asset):
    result = protocol.format_image_result(
        [{"b64_json": base64.b64encode(data).decode("ascii")} for data in multi_asset.images],
        "fixture", "url",
    )
    assert len(result["data"]) == 3
    assert len(multi_asset.saved) == 3


def test_limit_counts_results_not_empty_candidates(multi_asset):
    result = protocol.format_image_result(
        [{}, {"b64_json": ""}] + [
            {"b64_json": base64.b64encode(data).decode("ascii")} for data in multi_asset.images
        ], "fixture", "b64_json", max_images=1,
    )
    assert len(result["data"]) == 1
    assert multi_asset.saved == multi_asset.images


@pytest.mark.parametrize("limit", [0, -1])
def test_invalid_limit_rejected_before_storage(multi_asset, limit):
    with pytest.raises(ValueError, match="max_images must be positive"):
        protocol.format_image_result([], "fixture", "url", max_images=limit)
    assert not multi_asset.saved


@pytest.mark.parametrize("parallel", [False, True])
def test_partial_slot_failure_keeps_success_without_returning_extras(monkeypatch, multi_asset, parallel):
    monkeypatch.setattr(protocol, "config", SimpleNamespace(
        image_request_timeout_secs=30, image_parallel_generation=parallel,
    ))

    def generate(request, index, total):
        if index == 2:
            raise protocol.ImageGenerationError("fixture failed slot")
        return [protocol._image_result_output_from_urls(
            multi_asset.backend, request, "fixture-success", multi_asset.urls, index, total,
        )]

    monkeypatch.setattr(protocol, "_generate_single_image", generate)
    outputs = list(protocol.stream_image_outputs_with_pool(
        protocol.ConversationRequest(model="gpt-image-2", n=2),
    ))
    result = protocol.collect_image_outputs(outputs)
    assert len(result["data"]) == 1
    assert len(result["_image_urls"]) == 1
    assert outputs[0].index == 1 and outputs[0].total == 2
    assert multi_asset.saved == multi_asset.images


@pytest.mark.parametrize("n", [1, 2])
def test_resumed_task_never_exceeds_original_count(monkeypatch, multi_asset, n):
    from services import image_task_service as tasks
    from services import openai_backend_api as api

    service = tasks.ImageTaskService.__new__(tasks.ImageTaskService)
    service._lock = Lock()
    service._tasks = {"fixture": {"n": n, "quality": "auto", "size": "8x8"}}
    updates, logs, gallery, closed = [], [], [], []
    service._update_task = lambda key, **kwargs: updates.append(kwargs)
    service._log_call = lambda *args, **kwargs: logs.append(kwargs)
    monkeypatch.setattr(tasks, "auto_push_gallery_urls", lambda urls, **kw: gallery.append(urls))
    backend = multi_asset.backend
    backend._poll_image_results = lambda *args: (["output"], [])
    backend.resolve_conversation_image_urls = lambda *a, **kw: multi_asset.urls
    backend.close = lambda: closed.append(True)
    monkeypatch.setattr(api, "OpenAIBackendAPI", lambda: backend)
    service._run_resume_poll(
        "fixture", "fixture-conversation", 1, {"key_id": "fixture"}, "generate", "gpt-image-2",
        "http://localhost", protocol.time.time(), protocol.time.perf_counter(),
    )
    assert updates[-1]["status"] == tasks.TASK_STATUS_SUCCESS
    assert len(updates[-1]["data"]) == n
    assert len(logs[-1]["result"]["data"]) == n
    assert len(gallery[-1]) == n
    assert len(multi_asset.saved) == 3
    assert len(updates[-1]["extra_images"]) == 3 - n
    assert logs[-1]["call_id"] == updates[0]["call_id"]
    assert logs[-1]["conversation_id"] == "fixture-conversation"
    assert closed == [True]

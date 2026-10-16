"""Synthetic replay of reference metadata leaking into generated outputs."""
import json
from types import SimpleNamespace

import pytest

from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import conversation as protocol

INPUT = "file_00000000" + "a" * 24
OUTPUT_A = "file_00000000" + "b" * 24
OUTPUT_B = "file_00000000" + "c" * 24


def image_message(role, asset, **metadata):
    return {
        "author": {"role": role}, "status": "finished_successfully",
        "content": {"content_type": "multimodal_text", "parts": [
            {"content_type": "image_asset_pointer", "asset_pointer": "sediment://" + asset},
        ]},
        "metadata": metadata,
    }


@pytest.mark.parametrize("role", ["tool", "assistant"])
def test_output_ids_exclude_comparison_source_and_attachments(role):
    message = image_message(role, OUTPUT_A,
        comparison_source_asset_pointer="sediment://" + INPUT,
        attachments=[{"asset_pointer": "file-service://" + INPUT}],
        nested={"comparison_source_asset_pointer": "file-service://" + INPUT},
    )
    files, sediments = OpenAIBackendAPI._extract_image_output_ids(message)
    assert set(files + sediments) == {OUTPUT_A}
    # Diagnostic extraction remains intentionally broad.
    files, sediments = OpenAIBackendAPI._extract_image_reference_ids(message)
    assert INPUT in files + sediments


def test_poll_keeps_two_real_images_and_excludes_input_even_when_echoed():
    backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
    messages = [image_message("user", INPUT),
                image_message("tool", OUTPUT_A, comparison_source_asset_pointer="sediment://" + INPUT),
                image_message("tool", OUTPUT_B), image_message("assistant", INPUT)]
    records = backend._extract_image_tool_records({"mapping": {
        str(i): {"message": message} for i, message in enumerate(messages)
    }})
    assert {asset for r in records for asset in r["file_ids"] + r["sediment_ids"]} == {OUTPUT_A, OUTPUT_B}


def feed(state, event):
    protocol.update_conversation_state(state, json.dumps(event), event)


def test_stream_keeps_both_candidates_and_excludes_reference_metadata():
    state = protocol.ConversationState()
    feed(state, {"message": image_message("user", INPUT)})
    for asset in [OUTPUT_A, OUTPUT_B]:
        feed(state, {"message": image_message("tool", asset, comparison_source_asset_pointer="sediment://" + INPUT)})
    assert set(state.file_ids + state.sediment_ids) == {OUTPUT_A, OUTPUT_B}


@pytest.mark.parametrize("wrapped", [False, True])
def test_stream_content_patch_not_metadata_patch_is_output(wrapped):
    state = protocol.ConversationState(message_facts={"role": "tool"})
    patches = [
        {"p": "/message/metadata/comparison_source_asset_pointer", "o": "replace", "v": "sediment://" + INPUT},
        {"p": "/message/content/parts/0/asset_pointer", "o": "replace", "v": "sediment://" + OUTPUT_A},
    ]
    # Production envelopes carry o=patch; individual operations are nested.
    if wrapped:
        feed(state, {"p": "", "o": "patch", "v": patches})
    else:
        for patch in patches:
            feed(state, {"o": "patch", "v": [patch]})
    assert set(state.file_ids + state.sediment_ids) == {OUTPUT_A}


def test_task_recovery_excludes_comparison_input_without_user_message():
    backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
    ids = ([], [])
    protocol._collect_task_image_outputs(backend, {"image_gen_message": image_message(
        "tool", OUTPUT_A, comparison_source_asset_pointer="sediment://" + INPUT,
    )}, ids)
    assert set(ids[0] + ids[1]) == {OUTPUT_A}


@pytest.mark.parametrize("path", [
    "/message/content/attachments/0/asset_pointer",
    "/message/content/referenced_image_ids/0",
    "/message/content/parts/0/comparison_source_asset_pointer",
])
def test_reference_only_content_patch_is_not_an_output(path):
    state = protocol.ConversationState(message_facts={"role": "tool"})
    feed(state, {"o": "patch", "v": [
        {"p": path, "o": "replace", "v": "sediment://" + INPUT},
    ]})
    assert not state.file_ids and not state.sediment_ids


def test_codex_duplicate_final_events_keep_distinct_candidates():
    first = {"id": "fixture-a", "type": "image_generation_call", "result": "YWJj"}
    second = {"id": "fixture-b", "type": "image_generation_call", "result": "ZGVm"}
    assert protocol._codex_response_images([
        {"type": "response.output_item.done", "item": first},
        {"type": "response.completed", "response": {"output": [first, second]}},
        {"type": "image_generation_call", "result": "data:image/png;base64,YWJj"},
    ]) == ["YWJj", "ZGVm"]

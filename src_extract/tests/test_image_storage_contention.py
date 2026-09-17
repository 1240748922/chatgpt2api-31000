"""Exercise actual files/locks/catalog, never the developer's image directory."""
import hashlib
import io
import json
import multiprocessing
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from PIL import Image

from services import image_storage_service as storage
from services.storage.image_index_journal import ImageIndexJournal


@pytest.fixture
def store(tmp_path, monkeypatch):
    # Precreate the daily directory so Windows' non-strict path resolver does
    # not race creation of missing ancestors during the parallel test.
    (tmp_path / "images" / storage.beijing_now().strftime("%Y/%m/%d")).mkdir(parents=True)
    monkeypatch.setattr(storage, "config", SimpleNamespace(
        images_dir=tmp_path / "images", image_thumbnails_dir=tmp_path / "thumbs",
        base_url="http://example.test",
        get_image_storage_settings=lambda: {"mode": "local"},
    ))
    return storage.ImageStorageService(tmp_path / "image_index.json")


def png():
    result = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(result, "PNG")
    return result.getvalue()


def test_save_does_not_wait_for_a_busy_gallery_catalog(store):
    done, failures = Event(), []
    def save():
        try:
            store.save(png())
        except Exception as exc:
            failures.append(exc)
        finally:
            done.set()
    # The old save path waited here then rewrote the entire JSON, while also
    # holding a shared stripe. A new save must complete BEFORE lock release.
    with store._index_guard():
        worker = Thread(target=save)
        worker.start()
        finished_while_locked = done.wait(3)
    worker.join(5)
    assert finished_while_locked
    assert not failures


def test_unrelated_images_no_longer_share_a_lock_stripe(store):
    buckets = {}
    for i in range(1000):
        rel = f"2026/09/17/{i}.png"
        stripe = hashlib.sha256(rel.encode()).hexdigest()[:2]
        if stripe in buckets:
            assert store._item_lock_path(rel) != store._item_lock_path(buckets[stripe])
            return
        buckets[stripe] = rel
    pytest.fail("collision fixture not found")


def test_concurrent_saves_are_durable_unique_and_visible_to_other_instances(store):
    other = storage.ImageStorageService(store.index_file)
    payload = png()
    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(executor.map(lambda _: store.save(payload), range(128)))
    rels = {r.rel for r in results}
    assert len(rels) == 128, "identical bytes in the same second must not overwrite"
    for rel in rels:
        assert other.get_bytes(rel) == payload
    assert set(other._load_clean_index()) == rels
    assert other.compact_index() == 128
    assert not list(store._journal.directory.glob("*.json"))
    assert set(storage.ImageStorageService(store.index_file)._load_clean_index()) == rels


def test_catalog_compaction_does_not_ack_new_concurrent_entries(store):
    first = store.save(png())
    with store._index_guard():
        snapshot = store._load_clean_index()
        second = store.save(png())
        store._save_index(snapshot)
    assert set(store._load_clean_index()) == {first.rel, second.rel}
    assert len(list(store._journal.directory.glob("*.json"))) == 1


def test_crash_after_delete_commit_before_journal_cleanup_does_not_resurrect(store, monkeypatch):
    saved = store.save(png())
    monkeypatch.setattr(ImageIndexJournal, "acknowledge", lambda *a: None)
    assert store.delete(saved.rel)
    assert list(store._journal.directory.glob("*.json")), "simulate crash before cleanup"
    restarted = storage.ImageStorageService(store.index_file)
    assert saved.rel not in restarted._load_clean_index()
    assert not storage.image_local_path(saved.rel).exists()
    assert saved.rel not in {i["rel"] for i in restarted.list_items("http://example.test")}


def test_legacy_metadata_survives_overlay_update_and_deletion(store):
    rel = "2026/09/16/legacy.png"
    path = storage.image_local_path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png())
    store.index_file.write_text(json.dumps({"items": {rel: {
        "rel": rel, "storage": "local", "local": True, "size": path.stat().st_size,
        "generation": "old", "created_at": "2026-09-16 00:00:00",
    }}}), encoding="utf-8")
    fresh = store.save(png())
    items = store.list_items("http://example.test")
    assert {i["rel"] for i in items} == {rel, fresh.rel}
    store.record_genbox_push(fresh.rel, status="success", sha256="test", updated_at="now")
    assert store.get_genbox_push_state(fresh.rel)["status"] == "success"
    assert store.delete(rel)
    assert set(store._load_clean_index()) == {fresh.rel}


def test_journal_commit_failure_does_not_return_a_success_url(store, monkeypatch):
    def fail(*args):
        raise OSError("fixture journal write failed")
    monkeypatch.setattr(store._journal, "publish", fail)
    with pytest.raises(OSError, match="journal write failed"):
        store.save(png())


def test_save_records_file_and_catalog_timings(store):
    timings = {}
    store.save(png(), timings=timings)
    assert set(timings) == {"storage_lock_ms", "storage_write_ms", "storage_catalog_ms"}
    assert all(value >= 0 for value in timings.values())


def _process_writer(index_file, count, queue):
    root = Path(index_file).parent
    storage.config = SimpleNamespace(images_dir=root / "images", base_url="http://example.test",
                                     get_image_storage_settings=lambda: {"mode": "local"})
    service = storage.ImageStorageService(Path(index_file))
    try:
        queue.put({"rels": [service.save(png()).rel for _ in range(count)]})
    except Exception as exc:
        queue.put({"error": repr(exc)})


def test_eight_processes_share_files_and_journal_without_lost_records(store):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [ctx.Process(target=_process_writer, args=(str(store.index_file), 16, queue)) for _ in range(8)]
    for process in processes:
        process.start()
    try:
        results = [queue.get(timeout=60) for _ in processes]
        assert all("error" not in result for result in results), results
        rels = [rel for result in results for rel in result["rels"]]
        assert len(set(rels)) == 128
        assert set(store._load_clean_index()) == set(rels)
        store.compact_index()
        assert set(store._load_clean_index()) == set(rels)
    finally:
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
        queue.close()


def test_export_includes_uncompacted_journal_for_legacy_compatible_backups(store):
    saved = store.save(png())
    exported = store.export_index()
    assert saved.rel in exported["items"]
    assert not exported.get("journal_applied"), "export must stand alone without pending files"


@pytest.mark.parametrize("mode", ["webdav", "both"])
def test_remote_storage_journal_and_deletion_preserve_existing_semantics(store, monkeypatch, mode):
    remote = {}
    monkeypatch.setattr(storage.config, "get_image_storage_settings", lambda: {"mode": mode})
    class WebDAV:
        def __init__(self, settings):
            self.session = SimpleNamespace(close=lambda: None)
        def put(self, rel, data):
            remote[rel] = data
            return f"http://remote.test/{rel}"
        def get(self, rel):
            return remote[rel]
        def delete(self, rel):
            return remote.pop(rel, None) is not None
    monkeypatch.setattr(storage, "WebDAVClient", WebDAV)
    saved = store.save(png())
    assert saved.storage == mode
    assert store.get_bytes(saved.rel) == png()
    assert store.export_index()["items"][saved.rel]["remote_url"].startswith("http://remote.test/")
    assert store.delete(saved.rel)
    assert not remote
    assert saved.rel not in store._load_clean_index()

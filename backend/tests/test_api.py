"""Task 4 (REST API + background jobs) and Task 5 (database + search), end to end.

Needs PostgreSQL with pgvector (``docker compose up -d``); skipped otherwise.
Runs without network: rules-only extraction and the offline hashing embedder.
"""

from __future__ import annotations

import io
from pathlib import Path

import factory
import pymupdf
import pytest

API = "/api/v1"


def invoice_pdf(path: Path, *, number="INV-2024-0042", supplier="ACME Supplies Ltd", total="225.50",
                items=(("Widget A", "10", "12.50", "125.00"), ("Gadget B", "2", "40.00", "80.00")),
                subtotal="205.00", tax="20.50", title=None) -> Path:
    lines = [supplier, f"Invoice Number: {number}", "Invoice Date: 2024-03-15", "Bill To: Globex Corporation", "",
             "Description          Qty    Unit Price     Amount"]
    lines += [f"{d:<20} {q:>3}   {u:>10}   {a:>10}" for d, q, u, a in items]
    lines += ["", f"Subtotal: {subtotal}", f"Tax (10%): {tax}", f"Total: {total} USD"]
    doc = pymupdf.open()
    factory._write_invoice(doc.new_page(), lines)
    if title:
        doc.set_metadata({"title": title})  # same text, different bytes
    doc.save(path)
    return path


def upload(client, *paths, **params):
    files = [("files", (p.name, p.read_bytes(), "application/octet-stream")) for p in paths]
    r = client.post(f"{API}/documents", files=files, params=params)
    assert r.status_code == 202, r.text
    return r.json()


def one_invoice(client, document_id):
    doc = client.get(f"{API}/documents/{document_id}").json()
    assert len(doc["invoices"]) == 1, doc
    return client.get(f"{API}/invoices/{doc['invoices'][0]['id']}").json()


# ------------------------------------------------------------------ happy path ----------


def test_upload_is_processed_in_the_background(client, run_jobs, tmp_path):
    pdf = factory.digital_pdf(tmp_path / "batch.pdf", pages=2)
    [res] = upload(client, pdf)
    assert res["document"]["status"] == "queued" and res["job"]["status"] == "queued"
    assert not res["duplicate_upload"]

    assert run_jobs() == 1
    job = client.get(f"{API}/jobs/{res['job']['id']}").json()
    assert (job["status"], job["stage"], job["progress"], job["attempts"]) == ("succeeded", "done", 100, 1)

    doc = client.get(f"{API}/documents/{res['document']['id']}").json()
    assert doc["status"] == "processed" and doc["filename"] == "batch.pdf"
    assert doc["invoice_count"] == 2 and doc["validation_status"] == "valid" and not doc["needs_review"]
    assert doc["search_status"] == "indexed" and doc["page_count"] == 2
    assert [i["invoice_number"] for i in doc["invoices"]] == ["INV-2024-0042", "INV-2024-0043"]

    inv = client.get(f"{API}/invoices/{doc['invoices'][0]['id']}").json()
    assert (inv["total"], inv["subtotal"], inv["tax"], inv["currency"]) == ("225.50", "205.00", "20.50", "USD")
    assert inv["invoice_date"] == "2024-03-15" and inv["supplier_name"] == "ACME Supplies Ltd"
    assert inv["fields"]["total"]["evidence"] == "Total: 225.50 USD" and inv["fields"]["total"]["page"] == 1
    assert [li["description"] for li in inv["line_items"]] == ["Widget A", "Gadget B"]
    assert inv["checks"]["totals"] == "passed" and inv["issues"] == []

    pages = client.get(f"{API}/documents/{doc['id']}/pages").json()
    assert len(pages) == 2 and "INV-2024-0043" in pages[1]["text"]
    result = client.get(f"{API}/documents/{doc['id']}/result").json()
    assert result["validation"]["status"] == "valid" and len(result["invoices"]) == 2
    assert client.get(f"{API}/documents/{doc['id']}/file").content == pdf.read_bytes()


def test_page_images_and_inline_file(client, run_jobs, tmp_path):
    pdf_res, tif_res, csv_res = upload(client, factory.digital_pdf(tmp_path / "two.pdf", pages=2),
                                       factory.multipage_tiff(tmp_path / "fax.tiff", frames=2),
                                       factory.csv_utf8_bom(tmp_path / "list.csv"))
    run_jobs()
    for res in (pdf_res, tif_res):
        doc_id = res["document"]["id"]
        r = client.get(f"{API}/documents/{doc_id}/pages/2/image", params={"width": 400})
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert r.content.startswith(bytes([0x89]) + b"PNG")
        from PIL import Image

        assert Image.open(io.BytesIO(r.content)).width == 400
        assert client.get(f"{API}/documents/{doc_id}/pages/9/image").status_code == 404
    assert client.get(f"{API}/documents/{csv_res['document']['id']}/pages/1/image").status_code == 404
    r = client.get(f"{API}/documents/{pdf_res['document']['id']}/file", params={"inline": "true"})
    assert r.headers["content-type"] == "application/pdf" and r.headers["content-disposition"].startswith("inline")


def test_health(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["database"].startswith("pgvector")
    assert h["embeddings"] == "hashing-v1" and "queued" in h["queue"]


# ------------------------------------------------------------------- uploads ----------


def test_same_file_twice_is_not_processed_twice(client, run_jobs, tmp_path):
    pdf = factory.digital_pdf(tmp_path / "a.pdf", pages=1)
    [first] = upload(client, pdf)
    run_jobs()
    [again] = upload(client, pdf)
    assert again["duplicate_upload"] and again["document"]["id"] == first["document"]["id"]
    assert again["job"]["id"] == first["job"]["id"] and run_jobs() == 0
    [redo] = upload(client, pdf, reprocess="true")
    assert redo["job"]["id"] != first["job"]["id"] and run_jobs() == 1
    assert client.get(f"{API}/documents").json()["total"] == 1


def test_several_files_in_one_request(client, run_jobs, tmp_path):
    a = invoice_pdf(tmp_path / "a.pdf", number="INV-1")
    b = invoice_pdf(tmp_path / "b.pdf", number="INV-2")
    assert len(upload(client, a, b)) == 2
    assert run_jobs() == 2
    assert client.get(f"{API}/invoices").json()["total"] == 2


@pytest.mark.parametrize("name,content,status", [
    ("notes.docx", b"PK\x03\x04 not supported", 415),
    ("empty.pdf", b"", 400),
    ("huge.csv", b"a,b\n" * 700_000, 413),  # limit is 2 MB in the test settings
])
def test_rejected_uploads(client, name, content, status):
    r = client.post(f"{API}/documents", files=[("files", (name, content, "application/octet-stream"))])
    assert r.status_code == status, r.text
    assert client.get(f"{API}/documents").json()["total"] == 0


def test_path_tricks_in_filenames_are_neutralised(client, run_jobs, tmp_path, app_settings):
    pdf = invoice_pdf(tmp_path / "x.pdf")
    r = client.post(f"{API}/documents", files=[("files", ("..\\..\\evil.pdf", pdf.read_bytes(), "application/pdf"))])
    doc = r.json()[0]["document"]
    assert doc["filename"] == "evil.pdf"
    from docintel.db import Document

    with client.app.state.session_factory() as s:
        stored = Path(s.get(Document, doc["id"]).storage_path)
    assert app_settings.storage_path in stored.parents and stored.parent.name == doc["sha256"]
    assert stored.name == "evil.pdf"


def test_unreadable_file_fails_gracefully(client, run_jobs, tmp_path):
    [res] = upload(client, factory.garbage_with_pdf_header(tmp_path / "broken.pdf"))
    run_jobs()
    job = client.get(f"{API}/jobs/{res['job']['id']}").json()
    doc = client.get(f"{API}/documents/{res['document']['id']}").json()
    assert job["status"] == "succeeded"  # the pipeline handled it - nothing to retry
    assert doc["status"] == "failed" and doc["error"] and doc["validation_status"] == "failed"
    assert doc["issues"][0]["code"] == "EXTRACTION_FAILED"


def test_api_key(db, app_settings, pipeline):
    from fastapi.testclient import TestClient

    from docintel.api import create_app

    app = create_app(app_settings.with_overrides(api_key="s3cret"), pipeline=pipeline, inline_worker=False)
    with TestClient(app) as c:
        assert c.get(f"{API}/documents").status_code == 401
        assert c.get(f"{API}/documents", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get(f"{API}/documents", headers={"X-API-Key": "s3cret"}).status_code == 200
        assert c.get("/health").status_code == 200  # monitoring needs no key


# ------------------------------------------------------------ duplicates (DB) ----------


def test_duplicate_invoice_in_a_later_upload(client, run_jobs, tmp_path):
    first = upload(client, invoice_pdf(tmp_path / "march.pdf"))[0]
    run_jobs()
    second = upload(client, invoice_pdf(tmp_path / "copy.pdf", title="resent"))[0]  # different bytes
    run_jobs()
    assert one_invoice(client, first["document"]["id"])["validation_status"] == "valid"
    dup = one_invoice(client, second["document"]["id"])
    assert dup["validation_status"] == "invalid"
    [issue] = [i for i in dup["issues"] if i["code"] == "DUPLICATE_INVOICE"]
    assert issue["details"]["duplicate_of"] == "march.pdf (invoice 1)"


# ------------------------------------------------------------------- review ----------


def test_review_correct_then_approve(client, run_jobs, tmp_path):
    res = upload(client, invoice_pdf(tmp_path / "typo.pdf", total="252.50"))[0]
    run_jobs()
    inv = one_invoice(client, res["document"]["id"])
    assert inv["validation_status"] == "invalid" and inv["needs_review"]
    assert any(i["code"] == "TOTAL_MISMATCH" for i in inv["issues"])
    url = f"{API}/invoices/{inv['id']}/review"

    r = client.post(url, json={"action": "approve"})
    assert r.status_code == 409 and "TOTAL" not in r.json()["detail"] and r.json()["details"]["errors"]

    assert client.post(url, json={"corrections": {"total": "12,5x"}}).status_code == 422
    r = client.post(url, json={"corrections": {"invoice_date": "03/04/2024"}})
    assert r.status_code == 422 and "2024-03-04 or 2024-04-03" in r.json()["detail"]

    fixed = client.post(url, json={"corrections": {"total": "225.50"}, "note": "OCR misread"}).json()
    assert fixed["validation_status"] == "valid" and fixed["total"] == "225.50"
    assert fixed["fields"]["total"]["source"] == "manual" and fixed["corrected_fields"] == ["total"]
    assert fixed["review_status"] == "pending" and fixed["review_note"] == "OCR misread"

    done = client.post(url, json={"action": "approve"}).json()
    assert done["review_status"] == "approved" and not done["needs_review"] and done["reviewed_at"]
    doc = client.get(f"{API}/documents/{res['document']['id']}").json()
    assert doc["validation_status"] == "valid" and not doc["needs_review"]
    # the machine output is kept untouched for audit
    original = client.get(f"{API}/documents/{res['document']['id']}/result").json()
    assert original["invoices"][0]["total"]["value"] == "252.50"


def test_force_approve_and_reject(client, run_jobs, tmp_path):
    res = upload(client, invoice_pdf(tmp_path / "bad.pdf", total="999.00"))[0]
    run_jobs()
    inv = one_invoice(client, res["document"]["id"])
    url = f"{API}/invoices/{inv['id']}/review"
    assert client.post(url, json={"action": "approve", "force": True}).json()["review_status"] == "approved"
    assert client.post(url, json={"action": "reject", "note": "not ours"}).json()["review_status"] == "rejected"
    reopened = client.post(url, json={"action": "reopen"}).json()
    assert reopened["review_status"] == "pending" and reopened["needs_review"]
    assert client.post(url, json={}).status_code == 422


# ------------------------------------------------------------------- listing ----------


def test_invoice_filters_sorting_paging(client, run_jobs, tmp_path):
    upload(client, invoice_pdf(tmp_path / "a.pdf", number="INV-1"),
           invoice_pdf(tmp_path / "b.pdf", number="INV-2", supplier="Initech GmbH", total="300.00"),
           invoice_pdf(tmp_path / "c.pdf", number="INV-3", subtotal="1000.00", tax="100.00", total="1100.00",
                       items=(("Server rack", "1", "1000.00", "1000.00"),)))
    run_jobs()

    def numbers(**params):
        return [i["invoice_number"] for i in client.get(f"{API}/invoices", params=params).json()["items"]]

    assert numbers(validation_status="invalid") == ["INV-2"]
    assert numbers(issue_code="total_mismatch") == ["INV-2"]
    assert numbers(supplier="initech") == ["INV-2"]
    assert numbers(min_total="1000") == ["INV-3"]
    assert numbers(sort="total", order="asc") == ["INV-1", "INV-2", "INV-3"]
    page = client.get(f"{API}/invoices", params={"sort": "total", "limit": 1, "offset": 1}).json()
    assert page["total"] == 3 and [i["invoice_number"] for i in page["items"]] == ["INV-2"]
    assert page["items"][0]["errors"] == 1
    assert client.get(f"{API}/invoices", params={"date_from": "2025-01-01"}).json()["total"] == 0

    stats = client.get(f"{API}/stats").json()
    assert stats["documents"] == {"processed": 3}
    assert stats["invoices"] == {"valid": 2, "invalid": 1} and stats["needs_review"] == 1
    assert stats["totals_by_currency"]["USD"] == "1625.50"
    assert stats["top_issues"][0]["code"] == "TOTAL_MISMATCH"


def test_delete_document(client, run_jobs, tmp_path, app_settings):
    res = upload(client, invoice_pdf(tmp_path / "gone.pdf"))[0]
    run_jobs()
    doc_id = res["document"]["id"]
    files = list(app_settings.storage_path.rglob("*.pdf"))
    assert client.delete(f"{API}/documents/{doc_id}").status_code == 204
    assert client.get(f"{API}/documents/{doc_id}").status_code == 404
    assert client.get(f"{API}/invoices").json()["total"] == 0
    assert not any(f.exists() for f in files)


# -------------------------------------------------------------------- search ----------


@pytest.fixture
def indexed(client, run_jobs, tmp_path):
    upload(client,
           invoice_pdf(tmp_path / "widgets.pdf", number="INV-2024-0042"),
           invoice_pdf(tmp_path / "rack.pdf", number="DC-7781", supplier="Initech GmbH",
                       subtotal="1000.00", tax="100.00", total="1100.00",
                       items=(("Server rack cabinet", "1", "1000.00", "1000.00"),)))
    assert run_jobs() == 2
    return client


def test_search_by_invoice_number_is_exact(indexed):
    r = indexed.get(f"{API}/search", params={"q": "inv 2024 0042"}).json()
    assert r["mode"] == "hybrid" and r["embedding_model"] == "hashing-v1"
    assert r["invoices"][0]["invoice_number"] == "INV-2024-0042"
    assert "keyword" in r["invoices"][0]["matched_by"]


def test_search_by_content_ranks_the_right_invoice_first(indexed):
    r = indexed.get(f"{API}/search", params={"q": "server rack"}).json()
    top = r["invoices"][0]
    assert top["invoice_number"] == "DC-7781" and set(top["matched_by"]) == {"semantic", "keyword"}
    assert top["similarity"] > 0.12 and "Server rack cabinet" in top["summary"]
    assert r["passages"][0]["filename"] == "rack.pdf" and "rack" in r["passages"][0]["snippet"].lower()


def test_search_filters_and_scope(indexed):
    r = indexed.get(f"{API}/search", params={"q": "invoice", "supplier": "acme", "scope": "invoices"}).json()
    assert {h["supplier_name"] for h in r["invoices"]} == {"ACME Supplies Ltd"} and r["passages"] == []
    r = indexed.get(f"{API}/search", params={"q": "invoice", "min_total": "1000", "scope": "passages"}).json()
    assert {h["filename"] for h in r["passages"]} == {"rack.pdf"} and r["invoices"] == []
    assert indexed.get(f"{API}/search", params={"q": "zzqx unrelated words"}).json()["invoices"] == []


def test_supplier_typo_still_found(indexed):
    r = indexed.get(f"{API}/search", params={"q": "Inittech", "scope": "invoices"}).json()
    assert r["invoices"] and r["invoices"][0]["supplier_name"] == "Initech GmbH"


def test_embedding_outage_degrades_to_keyword_search(client, pipeline, run_jobs, tmp_path):
    from docintel.search.embeddings import EmbeddingError

    class Down:
        model, dim, min_similarity = "down", 768, 0.5

        def embed_documents(self, texts):
            raise EmbeddingError("service unavailable")

        embed_query = embed_documents

    pipeline.embedder = Down()
    res = upload(client, invoice_pdf(tmp_path / "a.pdf"))[0]
    run_jobs()
    doc = client.get(f"{API}/documents/{res['document']['id']}").json()
    assert doc["status"] == "processed" and doc["search_status"] == "keyword_only"
    r = client.get(f"{API}/search", params={"q": "Widget"}).json()
    assert r["mode"] == "keyword" and "unavailable" in r["note"] and r["invoices"]


# --------------------------------------------------------------- job queue ----------


def test_crashing_job_is_retried_then_failed(client, pipeline, run_jobs, tmp_path, monkeypatch):
    from docintel.jobs import queue
    from docintel.jobs import worker as worker_mod

    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(worker_mod, "run_job", boom)
    monkeypatch.setattr(queue, "RETRY_BASE_SECONDS", 0)  # retry immediately
    res = upload(client, invoice_pdf(tmp_path / "a.pdf"))[0]
    run_jobs()
    job = client.get(f"{API}/jobs/{res['job']['id']}").json()
    assert len(calls) == 3 and job["status"] == "failed" and job["attempts"] == 3
    assert "disk on fire" in job["error"]
    doc = client.get(f"{API}/documents/{res['document']['id']}").json()
    assert doc["status"] == "failed" and "after 3 attempt(s)" in doc["error"]


def test_two_workers_never_get_the_same_job(db, tmp_path):
    from docintel.db import Document, session_scope
    from docintel.jobs import claim_next, enqueue

    with session_scope(db) as s:
        for i in range(2):
            d = Document(sha256=f"{i:064d}", filename=f"{i}.pdf", size_bytes=1, storage_path="x")
            s.add(d)
            s.flush()
            enqueue(s, d.id)
    a, b, c = db(), db(), db()
    try:
        ja, jb = claim_next(a, "worker-a"), claim_next(b, "worker-b")  # both transactions open
        assert ja and jb and ja.id != jb.id
        assert claim_next(c, "worker-c") is None  # both rows locked or taken
    finally:
        for s in (a, b, c):
            s.rollback()
            s.close()


def test_job_of_a_dead_worker_is_requeued(db):
    from datetime import timedelta

    from docintel.db import Document, Job, session_scope
    from docintel.db.repository import utcnow
    from docintel.jobs import claim_next, enqueue, requeue_stale

    with session_scope(db) as s:
        d = Document(sha256="f" * 64, filename="x.pdf", size_bytes=1, storage_path="x")
        s.add(d)
        s.flush()
        job_id = enqueue(s, d.id).id
    with session_scope(db) as s:
        claim_next(s, "crashed-worker")
        s.get(Job, job_id).heartbeat_at = utcnow() - timedelta(hours=1)
    with session_scope(db) as s:
        assert requeue_stale(s, timedelta(minutes=10)) == 1
    with session_scope(db) as s:
        job = s.get(Job, job_id)
        assert job.status == "queued" and "stopped responding" in job.error


# -------------------------------------------------------------------- units ----------


def test_chunking_overlaps_and_keeps_pages():
    from docintel.search.indexer import chunk_pages

    class P:
        def __init__(self, number, text):
            self.number, self.text = number, text

    text = "\n".join(f"line {i:03d} " + "x" * 40 for i in range(80))
    chunks = chunk_pages([P(1, text), P(2, ""), P(3, "short page")], size=600, overlap=100)
    assert {c.page_number for c in chunks} == {1, 3}
    page1 = [c.text for c in chunks if c.page_number == 1]
    assert all(len(t) <= 600 for t in page1) and len(page1) > 5
    assert page1[0][-30:] in page1[1]  # overlap: context is not cut off between chunks
    assert all(t.startswith("line") for t in page1[1:])  # cut at line breaks


def test_gemini_embedder_batches_and_normalises():
    from types import SimpleNamespace

    from docintel.search.embeddings import EmbeddingError, GeminiEmbedder

    emb = GeminiEmbedder(api_key="AIza-test", dim=3, batch_size=2)
    seen = []

    def embed_content(model, contents, config):
        seen.append((len(contents), config.task_type, config.output_dimensionality))
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[3.0, 4.0, 0.0]) for _ in contents])

    emb.client = SimpleNamespace(models=SimpleNamespace(embed_content=embed_content))
    vecs = emb.embed_documents(["a", "b", "c"])
    assert seen == [(2, "RETRIEVAL_DOCUMENT", 3), (1, "RETRIEVAL_DOCUMENT", 3)]
    assert vecs[0] == pytest.approx([0.6, 0.8, 0.0])  # unit length
    assert emb.embed_query("q") == pytest.approx([0.6, 0.8, 0.0]) and seen[-1][1] == "RETRIEVAL_QUERY"

    emb.client = SimpleNamespace(models=SimpleNamespace(
        embed_content=lambda **kw: SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 0, 0])])))
    with pytest.raises(EmbeddingError):  # 1 vector for 2 texts must not be accepted silently
        emb.embed_documents(["a", "b"])


def test_embedding_batches_respect_token_budget():
    from docintel.search.embeddings import batches_by_tokens, estimate_tokens

    texts = ["x" * 3000] * 10  # ~1000 tokens each
    batches = batches_by_tokens(texts, max_items=50, max_tokens=3500)
    assert [len(b) for b in batches] == [3, 3, 3, 1]
    assert all(sum(estimate_tokens(t) for t in b) <= 3500 for b in batches)
    assert batches_by_tokens(["y" * 30000], 50, 3500) == [["y" * 30000]]  # oversized text still sent alone
    assert batches_by_tokens(["a"] * 5, max_items=2, max_tokens=10**6) == [["a", "a"], ["a", "a"], ["a"]]


def test_gemini_embedder_waits_out_per_minute_limit_but_not_daily():
    from types import SimpleNamespace

    from docintel.search.embeddings import EmbeddingError, GeminiEmbedder

    slept: list[float] = []
    emb = GeminiEmbedder(api_key="AIza-test", dim=3, sleep=slept.append)
    per_minute = ("429 RESOURCE_EXHAUSTED. Quota exceeded for metric: embed_content_free_tier_tokens, "
                  "limit: 30000 ... Embed content tokens per minute limit exceeded. Please retry in 10.4s.")
    calls = {"n": 0}

    def flaky(model, contents, config):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(per_minute)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 0, 0]) for _ in contents])

    emb.client = SimpleNamespace(models=SimpleNamespace(embed_content=flaky))
    assert len(emb.embed_documents(["a", "b"])) == 2
    assert calls["n"] == 2 and slept == [pytest.approx(11.4)]

    def daily(model, contents, config):
        raise RuntimeError("429 RESOURCE_EXHAUSTED. Quota exceeded: requests per day, limit: 1000")

    emb.client = SimpleNamespace(models=SimpleNamespace(embed_content=daily))
    slept.clear()
    with pytest.raises(EmbeddingError):  # waiting a minute will not fix a daily quota
        emb.embed_documents(["a"])
    assert slept == []


def test_gemini_embedder_paces_large_documents():
    from types import SimpleNamespace

    from docintel.search.embeddings import GeminiEmbedder

    slept: list[float] = []
    now = [0.0]

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    emb = GeminiEmbedder(api_key="AIza-test", dim=3, tokens_per_minute=3000, sleep=sleep, clock=lambda: now[0])
    emb.client = SimpleNamespace(models=SimpleNamespace(embed_content=lambda model, contents, config: SimpleNamespace(
        embeddings=[SimpleNamespace(values=[1.0, 0, 0]) for _ in contents])))
    # 6 texts of ~600 tokens = 3600 tokens > 80% of 3000/min, so it must pause once
    # instead of sending everything at once and getting refused.
    emb.embed_documents(["z" * 1800] * 6)
    assert len(slept) >= 1 and all(s > 0 for s in slept)


def test_settings_from_env(monkeypatch):
    from docintel.settings import Settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:1/d")
    monkeypatch.setenv("DOCINTEL_MAX_UPLOAD_MB", "7")
    monkeypatch.setenv("DOCINTEL_INLINE_WORKER", "false")
    monkeypatch.setenv("DOCINTEL_CORS_ORIGINS", "http://a, http://b")
    s = Settings.from_env()
    assert (s.database_url, s.max_upload_mb, s.inline_worker) == ("postgresql+psycopg://u:p@h:1/d", 7, False)
    assert s.cors_origins == ("http://a", "http://b")


import json
import shutil

import factory
import pytest

from docintel.processing import ProcessingConfig
from docintel.processing.batch import BatchProcessor, discover_files
from docintel.processing.cli import main as cli_main


def _mixed_inbox(root):
    inbox = root / "inbox"
    (inbox / "sub").mkdir(parents=True)
    factory.digital_pdf(inbox / "a.pdf")
    factory.multisheet_xlsx(inbox / "b.xlsx")
    factory.csv_utf8_bom(inbox / "sub" / "c.csv")
    factory.encrypted_pdf(inbox / "locked.pdf")
    factory.empty_file(inbox / "empty.pdf")
    factory.multisheet_xls(inbox / "d.xls")
    (inbox / "notes.md").write_text("ignored: unsupported extension")
    return inbox


@pytest.mark.parametrize("workers", [1, 3])
def test_batch_mixed_inbox(tmp_path, workers):
    inbox = _mixed_inbox(tmp_path)
    out = tmp_path / f"out{workers}"
    seen = []
    runner = BatchProcessor(ProcessingConfig(), output_dir=out, workers=workers,
                            on_document=lambda d, i, n: seen.append(d.metadata.filename))
    files = discover_files([inbox])
    assert len(files) == 6
    s = runner.run(files)

    assert (s.succeeded, s.partial, s.failed) == (4, 0, 2)
    assert s.success_rate == round(4 / 6, 4) and s.failure_rate == round(2 / 6, 4)
    assert {f.codes[0] for f in s.failures} == {"ENCRYPTED", "EMPTY_FILE"}
    assert sorted(seen) == sorted(f.name for f in files)
    assert len(list((out / "documents").glob("*.json"))) == 5  # empty file has no hash
    assert len((out / "index.jsonl").read_text().splitlines()) == 6
    summary = json.loads((out / "summary.json").read_text())
    assert summary["files_total"] == 6


def test_batch_resume_and_dedup(tmp_path):
    inbox = _mixed_inbox(tmp_path)
    shutil.copy(inbox / "a.pdf", inbox / "a_copy.pdf")  # byte-identical duplicate
    out = tmp_path / "out"
    files = discover_files([inbox])

    first = BatchProcessor(ProcessingConfig(), output_dir=out, workers=1).run(files)
    assert first.duplicates == 1

    second = BatchProcessor(ProcessingConfig(), output_dir=out, workers=1).run(files)
    assert second.skipped_existing == 4
    assert second.duplicates == 1
    assert second.succeeded == 0  # no successful document is reprocessed; files rejected
    assert second.failed == 2  # at validation (cheap) are re-checked in case they were fixed


@pytest.mark.slow
def test_large_pdf_split_across_workers(tmp_path):
    """A 600-page PDF is split into 24 units and spread over the pool; output is
    complete and in order."""
    pdf = factory.large_digital_pdf(tmp_path / "huge.pdf", 600)
    out = tmp_path / "out"
    s = BatchProcessor(ProcessingConfig(chunk_pages=25), output_dir=out, workers=4).run([pdf])
    assert s.succeeded == 1 and s.pages_total == 600
    doc = json.loads(next((out / "documents").glob("*.json")).read_text(encoding="utf-8"))
    assert [p["number"] for p in doc["pages"]] == list(range(1, 601))
    assert all(f"PAGE-MARKER-{p['number']:05d}" in p["text"] for p in doc["pages"])
    assert s.pages_per_second > 50


def test_worker_crash_is_retried_then_reported(tmp_path, monkeypatch):
    """Simulate a native crash: the unit fails once, the pool is rebuilt, retry succeeds."""
    from docintel.processing import batch as batch_mod

    calls = {"n": 0}
    real = batch_mod._unit_task

    def crashing(unit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise batch_mod.BrokenProcessPool("simulated crash")
        return real(unit)

    monkeypatch.setattr(batch_mod, "_unit_task", crashing)
    pdf = factory.digital_pdf(tmp_path / "a.pdf")
    s = BatchProcessor(ProcessingConfig(), output_dir=tmp_path / "out", workers=1).run([pdf])
    assert s.worker_restarts == 1
    assert s.succeeded == 1


def test_real_worker_crash_only_fails_the_culprit(tmp_path, monkeypatch):
    """A worker process is hard-killed (os._exit, like a native segfault) whenever it
    touches 'crashme'. Innocent files in flight at the same time must still succeed."""
    monkeypatch.setenv("DOCINTEL_FAULT_INJECT_CRASH", "crashme")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for i in range(6):
        factory.digital_pdf(inbox / f"good_{i}.pdf", pages=1 + i)
    factory.digital_pdf(inbox / "crashme.pdf", pages=2)

    s = BatchProcessor(ProcessingConfig(), output_dir=tmp_path / "out", workers=3).run(
        discover_files([inbox]))
    assert s.succeeded == 6
    assert s.failed == 1
    assert s.failures[0].path.endswith("crashme.pdf")
    assert s.failures[0].codes == ["INTERNAL_ERROR"]
    assert s.worker_restarts >= 2  # main pool crash + the confirming crash in quarantine


def test_cli_process_and_inspect(tmp_path, capsys):
    inbox = _mixed_inbox(tmp_path)
    out = tmp_path / "cli_out"
    assert cli_main(["process", str(inbox), "--out", str(out), "--workers", "1"]) == 0
    printed = capsys.readouterr().out
    assert "Processing 6 file(s)" in printed and '"succeeded": 4' in printed

    assert cli_main(["inspect", str(inbox / "a.pdf")]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "success" and data["metadata"]["page_count"] == 3

    assert cli_main(["inspect", str(inbox / "locked.pdf")]) == 1

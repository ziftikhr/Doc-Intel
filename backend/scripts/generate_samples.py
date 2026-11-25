"""Write one example of every supported (and every broken) input to ./samples.

    python scripts/generate_samples.py [--out samples]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import factory  # noqa: E402

SAMPLES = {
    "good/invoice_digital.pdf": factory.digital_pdf,
    "good/invoice_scanned.pdf": factory.scanned_pdf,
    "good/invoice_mixed.pdf": factory.mixed_pdf,
    "good/invoice.png": factory.png_invoice,
    "good/invoice_phone_photo.jpg": factory.poor_quality_jpeg,
    "good/invoice_sideways.png": factory.rotated_png,
    "good/invoice_transparent.png": factory.transparent_png,
    "good/fax_batch.tiff": factory.multipage_tiff,
    "good/scan_16bit.tif": factory.sixteen_bit_tiff,
    "good/invoice_register.xlsx": factory.multisheet_xlsx,
    "good/legacy_register.xls": factory.multisheet_xls,
    "good/export_utf8.csv": factory.csv_utf8_bom,
    "good/export_german.csv": factory.csv_semicolon_cp1252,
    "good/export_ragged.tsv": factory.csv_tab_ragged,
    "good/erp_export_really_tsv.xls": factory.csv_as_xls,
    "degraded/invoice_truncated.pdf": factory.truncated_pdf,
    "degraded/invoice_truncated.jpg": factory.truncated_jpeg,
    "degraded/blank_page.png": factory.blank_png,
    "broken/empty.pdf": factory.empty_file,
    "broken/random_bytes.pdf": factory.random_bytes,
    "broken/garbage_after_header.pdf": factory.garbage_with_pdf_header,
    "broken/password_protected.pdf": factory.encrypted_pdf,
    "broken/password_protected.xlsx": factory.encrypted_xlsx_container,
    "broken/damaged.xlsx": factory.corrupted_xlsx,
    "broken/damaged.xls": factory.corrupted_xls,
    "broken/word_document.docx": factory.docx_like,
    "broken/html_report.xls": factory.html_as_xls,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples")
    args = parser.parse_args()
    root = Path(args.out)
    for rel, maker in SAMPLES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        maker(path)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()

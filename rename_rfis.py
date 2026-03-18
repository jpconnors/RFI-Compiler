"""
rename_rfis.py
--------------
Processes a folder of RFI PDF files and:
  1. Renames each file to: "RFI 0-PB-### Subject.pdf"
  2. For any Official Response that has a linked attachment, downloads the
     attachment PDF and appends it to the cover sheet, producing a single
     combined PDF.
  3. Writes rfis_with_attachments.txt listing RFI numbers that had attachments.

Requirements:
    pip install pdfplumber pypdf requests

Usage:
    python rename_rfis.py <folder_path>

    Optional flags:
      --dry-run   Preview changes without renaming or downloading anything
"""

import io
import os
import re
import sys
import argparse
import tempfile

import requests
import pdfplumber
from pypdf import PdfReader, PdfWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize(name: str) -> str:
    """Remove or replace characters that are illegal or undesirable in filenames."""
    name = name.replace("&", "and")
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def extract_hyperlinks(pdf_path: str) -> list:
    """
    Return all hyperlinks embedded in the PDF as a list of dicts:
        [{"uri": "https://...", "page": 0}, ...]
    Uses pypdf's annotation reader.
    """
    links = []
    reader = PdfReader(pdf_path)
    for page_num, page in enumerate(reader.pages):
        annotations = page.get("/Annots")
        if not annotations:
            continue
        for annot in annotations:
            obj = annot.get_object()
            if obj.get("/Subtype") == "/Link":
                action = obj.get("/A")
                if action and action.get("/S") == "/URI":
                    uri = action.get("/URI")
                    if uri:
                        links.append({"uri": str(uri), "page": page_num})
    return links


def find_response_attachment_urls(pdf_path: str) -> list:
    """
    Return URLs of attachments that appear under an Official Response block.

    Strategy:
      - Extract all hyperlinks from the PDF with their page numbers.
      - Identify which pages contain "Official Response" text.
      - Return links found on those pages (skipping mailto/anchor links).
    """
    all_links = extract_hyperlinks(pdf_path)
    if not all_links:
        return []

    # Find which pages contain "Official Response"
    response_pages = set()
    reader = PdfReader(pdf_path)
    for page_num, page in enumerate(reader.pages):
        page_text = page.extract_text() or ""
        if re.search(r"Official Response", page_text, re.IGNORECASE):
            response_pages.add(page_num)

    # Collect URLs only from those pages
    urls = []
    seen = set()
    for link in all_links:
        if link["page"] not in response_pages:
            continue
        uri = link["uri"].strip()
        if not uri or uri.startswith("mailto:") or uri.startswith("#"):
            continue
        if uri not in seen:
            seen.add(uri)
            urls.append(uri)

    return urls


def download_pdf(url: str, timeout: int = 30):
    """
    Download a file from a URL. Returns raw bytes or None on failure.
    Handles redirects automatically.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (RFI-Processor/1.0)"}
        response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")
        is_pdf = (
            "pdf" in content_type
            or "octet-stream" in content_type
            or response.content[:4] == b"%PDF"
        )

        if is_pdf:
            return response.content

        print(f"      [WARNING] URL did not return a PDF (Content-Type: {content_type}). Skipping.")
        return None

    except requests.RequestException as exc:
        print(f"      [WARNING] Could not download {url}: {exc}")
        return None


def merge_pdfs(base_pdf_path: str, attachment_bytes_list: list, output_path: str) -> bool:
    """
    Merge the base PDF with one or more attachment PDFs (as bytes) and write
    the result to output_path. Returns True on success.
    """
    try:
        writer = PdfWriter()

        base_reader = PdfReader(base_pdf_path)
        for page in base_reader.pages:
            writer.add_page(page)

        for attachment_bytes in attachment_bytes_list:
            att_reader = PdfReader(io.BytesIO(attachment_bytes))
            for page in att_reader.pages:
                writer.add_page(page)

        with open(output_path, "wb") as f:
            writer.write(f)

        return True

    except Exception as exc:
        print(f"      [ERROR] PDF merge failed: {exc}")
        return False


def extract_rfi_info(pdf_path: str) -> dict:
    """
    Extract RFI number, subject, and attachment URLs from Official Response blocks.
    """
    result = {
        "rfi_number": None,
        "subject": None,
        "response_attachment_urls": [],
        "error": None,
    }

    try:
        full_text_pages = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    full_text_pages.append(text)

        if not full_text_pages:
            result["error"] = "No text could be extracted."
            return result

        full_text = "\n".join(full_text_pages)

        # ------------------------------------------------------------------
        # 1. RFI number and subject from bold heading
        #    Format: "RFI #0-PB-149: Support of Stair 2, ..."
        # ------------------------------------------------------------------
        heading_match = re.search(
            r"RFI\s+#(0-PB-\d+)\s*:\s*(.+)",
            full_text,
            re.IGNORECASE,
        )
        if heading_match:
            result["rfi_number"] = heading_match.group(1).strip()
            subject_raw = heading_match.group(2).split("\n")[0].strip()
            result["subject"] = sanitize(subject_raw)
        else:
            result["error"] = "Could not find RFI heading (RFI #0-PB-###: Subject)."
            return result

        # ------------------------------------------------------------------
        # 2. Find hyperlinks embedded in Official Response pages
        # ------------------------------------------------------------------
        result["response_attachment_urls"] = find_response_attachment_urls(pdf_path)

    except Exception as exc:
        result["error"] = str(exc)

    return result


def build_new_name(rfi_number: str, subject: str) -> str:
    return f"RFI {rfi_number} {subject}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Rename RFI PDFs, merge attachments, and log.")
    parser.add_argument("folder", help="Path to folder containing RFI PDFs.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without renaming, downloading, or merging.",
    )
    args = parser.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        print(f"ERROR: '{folder}' is not a valid directory.")
        sys.exit(1)

    pdf_files = sorted(
        f for f in os.listdir(folder) if f.lower().endswith(".pdf")
    )

    if not pdf_files:
        print("No PDF files found in the specified folder.")
        sys.exit(0)

    attachment_rfi_numbers = []
    errors = []

    print(f"\nProcessing {len(pdf_files)} PDF(s) in: {folder}")
    if args.dry_run:
        print("** DRY RUN — no files will be modified **\n")

    for filename in pdf_files:
        pdf_path = os.path.join(folder, filename)
        print(f"\n  Reading: {filename}")

        info = extract_rfi_info(pdf_path)

        if info["error"]:
            print(f"    [SKIPPED] {info['error']}")
            errors.append(f"{filename}: {info['error']}")
            continue

        new_base = build_new_name(info["rfi_number"], info["subject"])
        new_filename = new_base + ".pdf"
        new_path = os.path.join(folder, new_filename)

        # ------------------------------------------------------------------
        # Download and merge Official Response attachments
        # ------------------------------------------------------------------
        urls = info["response_attachment_urls"]

        if urls:
            attachment_rfi_numbers.append(info["rfi_number"])
            print(f"    [ATTACHMENT(S) FOUND] {len(urls)} link(s) in Official Response:")
            for u in urls:
                print(f"      {u}")

            if not args.dry_run:
                downloaded = []
                for url in urls:
                    print(f"      Downloading: {url}")
                    data = download_pdf(url)
                    if data:
                        downloaded.append(data)
                        print(f"      OK — {len(data):,} bytes")

                if downloaded:
                    # Write temp file to the same folder as the PDF to avoid
                    # cross-device errors when replacing (e.g. /tmp vs /workspaces)
                    with tempfile.NamedTemporaryFile(
                        delete=False, suffix=".pdf", dir=folder
                    ) as tmp:
                        tmp_path = tmp.name

                    success = merge_pdfs(pdf_path, downloaded, tmp_path)
                    if success:
                        os.replace(tmp_path, pdf_path)
                        print(f"    [MERGED] {len(downloaded)} attachment(s) appended to cover sheet.")
                    else:
                        os.unlink(tmp_path)
                        errors.append(f"{filename}: PDF merge failed.")
                else:
                    print(f"    [WARNING] No downloadable PDF attachments found at the linked URLs.")
        else:
            print(f"    No response attachments detected.")

        # ------------------------------------------------------------------
        # Rename
        # ------------------------------------------------------------------
        if filename == new_filename:
            print(f"    [UNCHANGED] Already correctly named.")
        else:
            print(f"    -> {new_filename}")
            if not args.dry_run:
                if os.path.exists(new_path) and os.path.abspath(new_path) != os.path.abspath(pdf_path):
                    print(f"    [WARNING] Target already exists; skipping rename.")
                    errors.append(f"{filename}: target '{new_filename}' already exists.")
                else:
                    os.rename(pdf_path, new_path)

    # ------------------------------------------------------------------
    # Write attachment log
    # ------------------------------------------------------------------
    attachment_log = os.path.join(folder, "rfis_with_attachments.txt")
    if not args.dry_run:
        with open(attachment_log, "w", encoding="utf-8") as f:
            f.write("RFIs with attachments in the Official Response\n")
            f.write("=" * 47 + "\n")
            if attachment_rfi_numbers:
                for num in sorted(attachment_rfi_numbers):
                    f.write(num + "\n")
            else:
                f.write("(none found)\n")
        print(f"\nAttachment log written to: {attachment_log}")
    else:
        print("\nRFIs with response attachments (dry run):")
        for num in sorted(attachment_rfi_numbers):
            print(f"  {num}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(f"\nDone. {len(pdf_files) - len(errors)} file(s) processed, {len(errors)} issue(s).")
    if errors:
        print("\nErrors / warnings:")
        for e in errors:
            print(f"  - {e}")


if __name__ == "__main__":
    main()

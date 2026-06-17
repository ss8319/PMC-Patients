import argparse
import xml.etree.cElementTree as ET
from multiprocessing import Pool
import os
from pathlib import Path
import pandas as pd
import json
import re
from tqdm import trange, tqdm
import sys
sys.path.append("..")
from xml_utils import parse_paragraph, getTitle, getText, getSection, clean_text, clean_refs, extract_article_tables, extract_article_figures
sys.path.insert(0, str(Path(__file__).resolve().parent))
from provenance import stamp  # vendored append-only per-row trace (Stage 0 survivor stamp)


# CC variants accepted at Stage A. ND-tagged variants (CC BY-ND, CC BY-NC-ND) are excluded
# per the redistribution contract; see CLAUDE.md §6.1. Module-level so multiprocessing
# workers can access it without pickling the function closure.
ALLOWED_LICENSES = {"CC BY", "CC BY-SA", "CC BY-NC", "CC BY-NC-SA", "CC0"}

# Journal allowlist — populated from journal_config.json in __main__ before Pool() forks workers.
# Workers inherit module-level state via fork() on Linux. See RareArena/dataset_collection/journal_config.json.
JOURNAL_DERM_PATTERNS: list = []
JOURNAL_ADJACENT: set = set()

# Case-section detection patterns. Module-level so they exist on any import path
# (not just the Linux-fork PMC worker) and can be reused by the scraped-journal
# extractor below. Previously defined inside __main__; hoisting also fixes a latent
# bug where spawn-based (non-Linux) workers re-import the module without running
# __main__ and would see these as undefined.
# Section_title_trigger, such as "case report", "patient representation", etc.
title_pattern = re.compile(r'(clinical )?((patient)|(case))(( ((illustrations?)|(report)|(descriptions?)|(information)|(details)|(discussions?)|((re)?presentation))([^a-z]|$))|$)')
# Detect and further remove label in title such as "3.1" in "3.1 case one"
label_pattern = re.compile(r'^[0-9]\.?[0-9]?\.?[0-9]?\.? ?')
# Multi_patient_trigger, for paragraphs staring with "Case 1" and "The first patient", respectively
case_1_pattern = re.compile(r'^(clinical )?((patient)|(case))( ((illustration)|(report)|(description)|(information)|(details)|(discussion)|((re)?presentation)))?.?\(?(([0-9]{1,2})|([abcde])|(i{1,3}|(i?vi?))|((one)|(two)|(three)|(four)|(five)))\)?($|[^a-z])')
first_pattern = re.compile(r'^((the)|(our)) ((first)|(second)|(third)|(fourth)|(fifth)|(sixth)|(seventh)|(eighth)|(nineth)|(1-?st)|(2-?nd)|(3-?rd)|([456789]-?th)) ((case)|(patient))')

# License normalization: our scraped data uses "CC-BY-4.0" / "CC-BY-NC-4.0" etc.;
# PMC XML uses "CC BY" / "CC BY-NC". Normalize scraped values before gating against
# ALLOWED_LICENSES so the same allowlist governs both pipelines.
SCRAPED_LICENSE_MAP = {
    "CC-BY-4.0":       "CC BY",
    "CC-BY-SA-4.0":    "CC BY-SA",
    "CC-BY-NC-4.0":    "CC BY-NC",
    "CC-BY-NC-SA-4.0": "CC BY-NC-SA",
    "CC0-1.0":         "CC0",
    "CC0":             "CC0",
}

# Spanish case-section heading patterns (RAD Argentina). Layered on top of
# title_pattern (English) — keeps the original untouched.
SPANISH_TITLE_PATTERN = re.compile(
    r'(caso cl[ií]nico|reporte de caso|presentaci[oó]n del caso|'
    r'descripci[oó]n del caso|relato de caso|caso cl[ií]nico patol[oó]gico)'
)

# Fallback journal allowlist when journal_config.json isn't reachable (e.g. running
# the scraped path on a machine without the DermArena repo). Our 4 scraped journals
# all contain "dermat"; not a substitute for the full curated config on PMC sweeps.
_DEFAULT_DERM_PATTERNS = ["dermat", "skin"]


def _normalize_journal(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip().lower()


def _journal_is_allowlisted(journal_title):
    """True if journal_title matches the derm-substring rule OR the adjacent-journal allowlist."""
    if not journal_title:
        return False
    j_lower = journal_title.lower()
    if any(p in j_lower for p in JOURNAL_DERM_PATTERNS):
        return True
    return _normalize_journal(journal_title) in JOURNAL_ADJACENT


def _parse_pub_date(root):
    """Return earliest publicly-available ISO-8601 date, or None if unparseable.

    PMC articles can carry multiple <pub-date pub-type="..."> elements (epub, ppub,
    collection, ...). Try them in order of typical earliest-availability.
    """
    candidates = ["epub", "ppub", "collection", None]
    for pub_type in candidates:
        if pub_type is None:
            node = root.find(".//article-meta//pub-date")
        else:
            node = root.find(f".//article-meta//pub-date[@pub-type='{pub_type}']")
        if node is None:
            continue
        year = (node.findtext("year") or "").strip()
        if not year.isdigit():
            continue
        month_raw = (node.findtext("month") or "01").strip()
        day_raw = (node.findtext("day") or "01").strip()
        try:
            month = int(month_raw)
            day = int(day_raw)
        except ValueError:
            month, day = 1, 1
        return f"{int(year):04d}-{month:02d}-{day:02d}"
    return None


def _parse_pmcid(root):
    """Return PMC<int> form, or None. PMC XML stores the integer; we normalize."""
    node = root.find(".//article-meta//article-id[@pub-id-type='pmc']")
    raw = getText(node) if node is not None else None
    if not raw:
        return None
    raw = raw.strip()
    return raw if raw.startswith("PMC") else f"PMC{raw}"


"""
    Counting.
"""
def stat():
    print("Article: ", article_count)
    print("Case report type articles: ", case_report_type_count)
    print("Patient: ", patient_count)
    print("Patient in case report type articles: ", patient_in_case_count)
    print("Error: ", error_count)

"""
    Section_title_trigger, stricter, for single patient extraction
"""
def match_title(title):
    return title_pattern.match(title.lower())

"""
    Section_title_trigger, easier, for first step.
"""
def section_title_trigger(title):
    title = title.lower()
    if ("case" in title or "patient" in title or "clinical" in title or "medical" in title) \
        and "consent" not in title and "approv" not in title:
        return True
    return False

"""
    Get section and subsection names in a hiearchical pattern.
    Input:
        body element of article xml.
"""
def hier_parse(body):
    results = []
    results.append([body])
    while len(results[-1]) > 0:
        results.append([])
        for sec in results[-2]:
            for subsec in sec.iterfind('./sec'):
                results[-1].append(subsec)
    return results[1:-1]


# ════════════════════════════════════════════════════════════════════════
# Scraped-journal extraction (Stage 0, non-PMC sources)
# ════════════════════════════════════════════════════════════════════════
# extract() above consumes PMC XML, which PMC normalizes to the NLM DTD with a
# semantic <sec><title>Case Report</title> that opens with the patient. Our 4
# scraped journals are native HTML/PDF with no such marker (validated: only RAD
# exposes a "Caso clinico" heading; TurkJDerm uses opaque <div id="s1"> or is
# abstract-only for older imports; ODermatol uses generic INTRODUCTION/RESULTS;
# Acta APA PDFs have no headings).
#
# filters.py demo_filter (Stage A1) only reads the OPENING of patient_text and
# expects it to begin with the patient ("A 45-year-old man presented ..."), the
# way PMC's section extraction delivers it. So instead of fragile per-source
# section isolation, we take the article body text and then "surgically open" it
# at the first sentence carrying an age+sex cue — emulating what PMC's case
# section yields. This is robust to front-matter noise (title/authors/abstract/
# running headers) because the cue scan jumps past it.
#
# v1 scope: English sources only. Non-English (RAD = Spanish; some older
# TurkJDerm = Turkish) are rejected as language_excluded — filters.py's age/sex
# regex is English-only, so they yield ~0 and would only add noise. The
# multilingual follow-up is tracked in the Stage 0 scraped-source PR.

# Age+sex case-opening cue. Deliberately broader than a single form but far
# simpler than filters.py's full age grammar — it only needs to locate WHERE the
# patient narrative starts; filters.py does the authoritative parse afterward.
_CASE_OPEN_CUE = re.compile(
    r'\b('
    r'\d{1,3}[\s\-]?(?:year|yr|month|week|day)[\s\-]?old'          # 45-year-old
    r'|(?:aged|age)\s+\d{1,3}'                                      # aged 45
    r'|in\s+(?:his|her)\s+(?:early\s+|late\s+)?\d0s'                # in his late 40s
    r'|(?:man|woman|male|female|boy|girl|patient|infant|baby|child|neonate)'
    r'\s+(?:aged|of)\s+\d{1,3}'                                     # woman aged 45
    r')\b',
    re.IGNORECASE,
)


def _surgical_open(text):
    """Return text starting at the first sentence containing an age+sex case-opening
    cue (to end of body). Falls back to the full text if no cue is found — those
    will typically be rejected by demo_filter, which is correct for non-cases."""
    if not text:
        return ""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    for i, s in enumerate(sentences):
        if _CASE_OPEN_CUE.search(s):
            return " ".join(sentences[i:]).strip()
    return text


def _scraped_clean(text):
    """Collapse whitespace + strip. Same intent as clean_text() in xml_utils."""
    return re.sub(r"\s+", " ", text).strip() if text else ""


def _scraped_body_text(record):
    """Get the article body text per source (minimal source-specific code — the
    surgical-open step handles front-matter, so we only need the main content
    container, not precise section isolation)."""
    source = record.get("source", "")
    html_path = record.get("html_path", "")
    pdf_path = record.get("pdf_path", "")

    if source == "acta_apa":
        return _scraped_pdf_text(pdf_path)

    if html_path and os.path.isfile(html_path):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(open(html_path, encoding="utf-8").read(), "lxml")
        if source == "turkjderm":
            container = soup.find("div", class_="article") or soup.find(id="content") or soup
        elif source == "odermatol":
            container = soup.find("div", class_="post-content") or soup
        else:  # rad_argentina or unknown -> the saved fulltext is the content body
            container = soup
        text = _scraped_clean(container.get_text(" ", strip=True))
        # For TurkJDerm older imports the HTML is abstract-only; fall back to PDF
        # text when the HTML body is too short to contain a case.
        if source == "turkjderm" and len(text.split()) < 120 and pdf_path:
            pdf_text = _scraped_pdf_text(pdf_path)
            if len(pdf_text.split()) > len(text.split()):
                return pdf_text
        return text

    return _scraped_pdf_text(pdf_path) if pdf_path else ""


def _scraped_pdf_text(pdf_path):
    """Whole-PDF text via PyMuPDF (born-digital PDFs — no OCR). surgical-open
    handles the title/author/header preamble downstream."""
    if not pdf_path or not os.path.isfile(pdf_path):
        return ""
    import fitz  # PyMuPDF
    doc = fitz.open(pdf_path)
    text = "\n".join(page.get_text("text") for page in doc)
    doc.close()
    return _scraped_clean(text)


def _scraped_load_figures(manifest_path):
    """Translate our images/manifest.json into the figures[] shape that
    extract_article_figures() emits for PMC: {fig_id, label, label_number,
    caption, panels}. Only captioned entries are kept (rephrase.py criterion 5
    needs the caption text)."""
    if not manifest_path or not os.path.isfile(manifest_path):
        return []
    try:
        manifest = json.loads(open(manifest_path, encoding="utf-8").read())
    except Exception:
        return []
    figs = []
    for img in manifest:
        if not img.get("caption"):
            continue
        figs.append({
            "fig_id": img.get("filename", ""),
            "label": img.get("figure_label", ""),
            "label_number": img.get("figure_number"),
            "caption": img.get("caption", ""),
            "panels": [],
        })
    return figs


def _scraped_extract_tables(html_path):
    """Walk <table> in saved HTML -> same shape as extract_article_tables() for
    PMC: {table_id, label, label_number, caption, structured_rows}. Acta APA
    (PDF-only) returns []."""
    if not html_path or not os.path.isfile(html_path):
        return []
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(open(html_path, encoding="utf-8").read(), "lxml")
    out = []
    for tnum, tbl in enumerate(soup.find_all("table"), start=1):
        rows = []
        for tr in tbl.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        if not rows:
            continue
        cap_el = tbl.find("caption")
        out.append({
            "table_id": tbl.get("id", ""),
            "label": f"Table {tnum}",
            "label_number": tnum,
            "caption": cap_el.get_text(" ", strip=True) if cap_el else "",
            "structured_rows": rows,
        })
    return out


"""
    Scraped-journal extractor — sibling of extract().
    Same 6-tuple return shape and same patient-dict schema; reads a scraped
    metadata record + saved PDF/HTML instead of PMC XML. One case per article
    for v1 (our journals publish predominantly single-case reports).
    Input:  (record_dict, scraped_root)
    Output: (article_count, case_report_type_count, patient_count, error_count,
             patients, article_reject)
"""
def extract_scraped_article(msg):
    record, _scraped_root = msg
    article_count = 0
    case_report_type_count = 0
    patient_count = 0
    error_count = 0
    patients = []
    article_tables = []
    article_figures = []

    file_path = record.get("html_path") or record.get("pdf_path") or ""
    journal_name = record.get("journal", "")
    source = record.get("source", "")
    raw_license = record.get("license", "")
    license_canonical = SCRAPED_LICENSE_MAP.get(raw_license, raw_license)
    doi_or_id = record.get("doi", "") or record.get("journal_article_id", "")

    def finalize(reject_reason=None):
        n = len(patients)
        for i, p in enumerate(patients):
            p["cases_in_article"] = n
            p["case_index_in_article"] = i + 1
            p["tables"] = article_tables
            p["figures"] = article_figures
        article_reject = ({"PMID": "", "file_path": file_path, "doi": doi_or_id,
                           "source": source, "stage": "extractor_stage0_scraped",
                           "reason": reject_reason}
                          if reject_reason is not None else None)
        return article_count, case_report_type_count, patient_count, error_count, patients, article_reject

    # Stage A license filter (same allowlist as PMC, after normalization).
    if license_canonical not in ALLOWED_LICENSES:
        return finalize("license_excluded")

    # Language gate (v1: English only). Our scrape tags language reliably per
    # journal; filters.py's age/sex regex is English-only so non-English yields ~0.
    if (record.get("language", "") or "").lower() != "en":
        return finalize("language_excluded")

    # Journal allowlist (reuse PMC-path gate + module-level state).
    if not _journal_is_allowlisted(journal_name):
        return finalize("journal_not_allowlisted")

    article_count += 1
    article_type = record.get("article_type", "") or record.get("article_type_raw", "")
    if article_type == "case_report":
        case_report_type_count += 1

    try:
        body = _scraped_body_text(record)
        patient_text = _surgical_open(body)
    except Exception as e:
        error_count += 1
        return finalize(f"text_extraction_error:{type(e).__name__}")

    if not patient_text:
        error_count += 1
        return finalize("no_patient_text")

    article_figures = _scraped_load_figures(record.get("images_manifest_path", ""))
    article_tables = _scraped_extract_tables(record.get("html_path", ""))

    patients.append({
        "title": record.get("title", ""),
        "journal": journal_name,
        "file_path": file_path,
        "PMID": "",
        "pmcid": "",
        "publication_date": record.get("published_date", ""),
        "license": license_canonical,
        "patient": patient_text,
        "article_type": article_type,
        # Scraped-source provenance + stable identity. PMC rows key patient_uid
        # off PMID; scraped rows have none, so filters.py builds the uid from
        # `source` + `doi`/`journal_article_id` instead. Carried through by
        # filters.py's dict(patient) preservation.
        "source": source,
        "doi": doi_or_id,
    })
    stamp(patients[-1], "extractor_stage0_scraped", "kept",
          journal=journal_name, license=license_canonical, source=source)
    patient_count += 1
    return finalize()



"""
    Extractor.
    Input:
        file_path and PMID
    Output:
        several counts and patient_notes extracted.
"""
def extract(msg):
    file_path, PMID, License = msg
    article_count = 0
    case_report_type_count = 0
    patient_count = 0
    error_count = 0
    patients = []
    article_tables = []   # populated once body is available; closure-captured by finalize().
    article_figures = []  # ditto — figure captions for the caption-aware rephrase rule (2026-05-21).

    def finalize(reject_reason=None):
        # Attach per-article case multiplicity to each patient row. Empty-patients
        # returns are no-ops. cases_in_article > 1 signals that the article's
        # Discussion section (and any cross-patient prose) is shared across
        # multiple patients — relevant for Stage F leakage masking and any CoT
        # construction that depends on per-patient attribution.
        # tables: article-level <table-wrap> content. Single-patient articles get
        # them all; multi-patient cases get the union (downstream v0.1 drops
        # multi-patient anyway — see CLAUDE.md §6.1).
        # figures: article-level <fig> caption content. Used by rephrase.py
        # caption-aware criterion 5 as ground truth for what is image-recoverable
        # (replaces the morphology-vs-non-morphology taxonomy heuristic, 2026-05-21).
        n = len(patients)
        for i, p in enumerate(patients):
            p["cases_in_article"] = n
            p["case_index_in_article"] = i + 1
            p["tables"] = article_tables
            p["figures"] = article_figures
        # Article-level reject record (provenance): Stage 0 drops WHOLE articles
        # (license / parse-error / no-body-title / journal) before any patient row
        # exists, so the reason can't ride a per-row trace — the main loop writes it
        # to a separate article-rejects sidecar. None on the survivor/empty paths.
        article_reject = ({"PMID": PMID, "file_path": file_path,
                           "stage": "extractor_stage0", "reason": reject_reason}
                          if reject_reason is not None else None)
        return article_count, case_report_type_count, patient_count, error_count, patients, article_reject

    # Stage A license filter: exclude ND variants (see ALLOWED_LICENSES at module top).
    if License not in ALLOWED_LICENSES:
        return finalize("license_excluded")
    f = False
    article_count += 1

    try:
        tree = ET.parse(os.path.join(data_dir, file_path))
        root = tree.getroot()
        clean_refs(root)
    except Exception as e:
        error_count += 1
        return finalize("xml_parse_error")

    article_type = root.attrib['article-type']
    if article_type == 'case-report':
        case_report_type_count += 1
    body = root.find(".//body")
    article_title = root.find(".//article-meta").find(".//article-title")

    # Remove articles without body or title.
    if (body is None) or (article_title is None):
        error_count += 1
        return finalize("no_body_or_title")

    article_title = getText(article_title)

    # Extract additional metadata
    journal_node = root.find(".//journal-meta//journal-title")
    journal_name = getText(journal_node) if journal_node is not None else "Unknown"

    # Journal allowlist: skip case detection unless journal matches derm-substring rule
    # OR adjacent-journal allowlist. Cheap reject before the expensive XML walks below.
    if not _journal_is_allowlisted(journal_name):
        return finalize("journal_not_allowlisted")

    # Walk <table-wrap> elements once at article level (root, not just body —
    # publishers sometimes place tables in <floats-group> as a sibling of body).
    # finalize() attaches the result to every patient (single-patient is the v0.1
    # scope per CLAUDE.md §6.1).
    article_tables = extract_article_tables(root)
    # Same pattern for figures — captions feed rephrase.py's caption-aware
    # criterion 5 (2026-05-21). Both walks must happen AFTER clean_refs(root)
    # so that <xref> labels inside captions are resolved.
    article_figures = extract_article_figures(root)

    pmcid = _parse_pmcid(root)
    publication_date = _parse_pub_date(root)

    # Extract section / subsection with titles like "Case 1 xxx" or "Patient B"
    hierarchical_secs = hier_parse(body)
    for layer in range(len(hierarchical_secs)):
        if f:
            break
        for sec in hierarchical_secs[layer]:
            title = getTitle(sec)
            # Assume each section with such titles is a single patient note
            if case_1_pattern.match(title.lower()):
                patient = getSection(sec)
                if len(patient) > 0:
                    patients.append({"title": article_title, "journal": journal_name, "file_path": file_path, "PMID": PMID, "pmcid": pmcid, "publication_date": publication_date, "license": License, "patient": patient, "article_type": article_type})
                    stamp(patients[-1], "extractor_stage0", "kept", journal=journal_name, license=License)
                    patient_count += 1
                    f = True

    if f:
        return finalize()

    # Extract paragraphs fullmatch "Case 1"
    index = []
    paras = parse_paragraph(body)
    for j in range(len(paras)):
        title = paras[j][0]
        paragraph_text = paras[j][1]
        # Section_title_trigger and "case 1" paragraph indicates multiple notes, trach the paragraph ids
        if section_title_trigger(title) and case_1_pattern.fullmatch(paragraph_text.lower()):
            index.append(j)

    if len(index) > 1:
        # The last patient note is taken till end of the section.
        last = len(paras)
        for j in range(index[-1] + 1, len(paras)):
            if paras[j][0] != paras[index[-1]][0]:
                last = j
                break
        index.append(last)
        # Multi_patients_extractor, extract texts between successive paragraph ids.
        # Note triggerring paragraphs are NOT included.
        for k in range(len(index) - 1):
            patient = ""
            for j in range(index[k] + 1, index[k + 1]):
                patient += paras[j][1] + '\n'
            patient = patient.strip()
            if len(patient) > 0:
                patients.append({"title": article_title, "journal": journal_name, "file_path": file_path, "PMID": PMID, "pmcid": pmcid, "publication_date": publication_date, "license": License, "patient": patient, "article_type": article_type})
                stamp(patients[-1], "extractor_stage0", "kept", journal=journal_name, license=License)
                patient_count += 1
                f = True
    
    if f:
        return finalize()

    # Extract paragraphs with "Case 1 xxx" / "The first case xx"
    index = []
    for j in range(len(paras)):
        title = paras[j][0]
        paragraph_text = paras[j][1]
        # Section_title_trigger and "Case 1:" or "The first patient" paragraph indicates multiple notes, trach the paragraph ids
        if section_title_trigger(title) and (case_1_pattern.match(paragraph_text.lower()) or first_pattern.match(paragraph_text.lower())):
            index.append(j)

    if len(index) > 1:
        # The last patient note is taken till end of the section.
        last = len(paras)
        for j in range(index[-1] + 1, len(paras)):
            if paras[j][0] != paras[index[-1]][0]:
                last = j
                break
        index.append(last)
        # Multi_patients_extractor, extract texts between successive paragraph ids.
        # Note triggering paragraphs are included
        for k in range(len(index) - 1):
            patient = ""
            for j in range(index[k], index[k + 1]):
                patient += paras[j][1] + '\n'
            patient = patient.strip()
            if len(patient) > 0:
                patients.append({"title": article_title, "journal": journal_name, "file_path": file_path, "PMID": PMID, "pmcid": pmcid, "publication_date": publication_date, "license": License, "patient": patient, "article_type": article_type})
                stamp(patients[-1], "extractor_stage0", "kept", journal=journal_name, license=License)
                patient_count += 1
                f = True

    if f:
        return finalize()

    # Extract section with title like "Case Report"
    for layer in range(len(hierarchical_secs)):
        if f:
            break
        for sec in hierarchical_secs[layer]:
            title = getTitle(sec)
            # No multiple patients identified, assume single note and extract whole section.
            if match_title(title.lower()):
                patient = getSection(sec)
                if len(patient) > 0:
                    patients.append({"title": article_title, "journal": journal_name, "file_path": file_path, "PMID": PMID, "pmcid": pmcid, "publication_date": publication_date, "license": License, "patient": patient, "article_type": article_type})
                    stamp(patients[-1], "extractor_stage0", "kept", journal=journal_name, license=License)
                    patient_count += 1
                    f = True
                    break

    return finalize()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract patient-note candidates from PMC XML and/or scraped journals")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("/mnt/hdd/sdc/ssim/pmc_bulk_downloads/"),
        help="Directory containing the extracted PMC XML files",
    )
    parser.add_argument(
        "--meta-csv",
        type=Path,
        default=Path("/mnt/hdd/sdc/ssim/meta_data/PMC_OA_meta.csv"),
        help="PMC OA metadata CSV with file_path, PMID, and License columns",
    )
    parser.add_argument(
        "--output-jsonl",
        type=Path,
        default=Path("/mnt/hdd/sdc/ssim/meta_data/patient_note_candidates.jsonl"),
        help="Output JSONL file for extracted patient note candidates",
    )
    parser.add_argument(
        "--checkpoint-file",
        type=Path,
        default=Path("/mnt/hdd/sdc/ssim/meta_data/extractor_checkpoint.txt"),
        help="Checkpoint file used to resume extraction",
    )
    parser.add_argument("--workers", type=int, default=18, help="Parallel workers")
    parser.add_argument("--chunksize", type=int, default=32, help="imap chunksize")
    parser.add_argument(
        "--journal-config",
        type=Path,
        # Default resolves relative to this file: RareArena is a sibling of
        # PMC-Patients under the Data_collection workspace (parents[4]). Keeps the
        # full derm-substring + adjacent-journal allowlist in play without a
        # machine-specific absolute path; override if the config lives elsewhere.
        # Same parents[N]/__file__ style as --scraped-root below.
        default=Path(__file__).resolve().parents[4] / "RareArena" / "dataset_collection" / "journal_config.json",
        help="Path to journal_config.json (derm_match patterns + adjacent_journals allowlist)",
    )
    # Source dispatch. "pmc" = the existing PMC OA XML path (unchanged). "scraped"
    # = the 4 local-scrape journals. Both write to the SAME --output-jsonl so
    # filters.py consumes them together.
    parser.add_argument(
        "--source",
        nargs="+",
        choices=["pmc", "scraped"],
        default=["pmc"],
        help="Which source(s) to extract from (default: pmc)",
    )
    parser.add_argument(
        "--scraped-root",
        type=Path,
        default=Path(__file__).resolve().parents[3] / "scrap" / "output",
        help="Root containing scrap/output/<journal>/metadata.jsonl (for --source scraped)",
    )
    args = parser.parse_args()

    # Load journal allowlist into module-level state BEFORE Pool() forks workers
    # (Linux fork inheritance). Graceful fallback to a minimal derm allowlist when
    # journal_config.json isn't reachable (e.g. running the scraped path on a box
    # without the DermArena repo) — OK for the 4 scraped journals; for full PMC
    # sweeps point --journal-config at DermArena/dataset_collection/journal_config.json.
    if args.journal_config.is_file():
        journal_cfg = json.loads(args.journal_config.read_text())
        JOURNAL_DERM_PATTERNS = [p.lower() for p in journal_cfg["derm_match"]["patterns"]]
        JOURNAL_ADJACENT = {_normalize_journal(j) for j in journal_cfg["adjacent_journals"]}
        print(
            f"Journal allowlist loaded from {args.journal_config}: "
            f"{len(JOURNAL_DERM_PATTERNS)} substring patterns + {len(JOURNAL_ADJACENT)} adjacent journals",
            flush=True,
        )
    else:
        JOURNAL_DERM_PATTERNS = list(_DEFAULT_DERM_PATTERNS)
        JOURNAL_ADJACENT = set()
        print(
            f"WARN: --journal-config {args.journal_config} not found; using built-in fallback "
            f"{JOURNAL_DERM_PATTERNS}. Fine for the 4 scraped journals; supply the full config for PMC sweeps.",
            flush=True,
        )

    # Case-section patterns (title_pattern, label_pattern, case_1_pattern,
    # first_pattern) are now defined at module level — see top of file.

    output_jsonl = args.output_jsonl
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)

    # Shared counters (stat() reads these module globals). The scraped path keeps
    # its own tallies and prints its own summary.
    article_count = 0
    case_report_type_count = 0
    patient_count = 0
    patient_in_case_count = 0
    error_count = 0

    # ───────────────────────────── PMC OA path ─────────────────────────────
    if "pmc" in args.source:
        data_dir = str(args.data_dir)
        meta_csv = args.meta_csv
        if not os.path.isfile(meta_csv):
            raise FileNotFoundError(
                f"{meta_csv} not found. Run PMC_OA_meta.py first (writes there), or copy from "
                f"{os.path.join(data_dir.rstrip('/'), 'PMC_OA_meta.csv')} if you have an older build."
            )
        file_list = pd.read_csv(
            meta_csv,
            dtype={"file_path": str, "PMID": str, "License": str},
            low_memory=False,
        )

        checkpoint_file = args.checkpoint_file

        # Resume logic: check if we have a checkpoint
        start_idx = 0
        if os.path.isfile(checkpoint_file):
            with open(checkpoint_file, "r") as f:
                start_idx = int(f.read().strip())
            print(f"Resuming from article {start_idx:,} (checkpoint found)", flush=True)
            file_list = file_list.iloc[start_idx:].reset_index(drop=True)

        # Build work queue (vectorized - fast)
        n = len(file_list)
        print(f"Building work queue ({n:,} articles)...", flush=True)
        msgs = list(zip(file_list["file_path"], file_list["PMID"], file_list["License"]))

        # Open output file in append mode (safe for resume)
        mode = "a" if start_idx > 0 else "w"
        checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        output_file = open(output_jsonl, mode, buffering=1)  # Line buffered
        # Stage 0 article-level rejects (provenance): whole articles dropped by the
        # license / journal / parse / no-body gates, which have no patient row to carry
        # a trace. Same resume `mode` as the main output.
        article_rejects_file = open(output_jsonl.with_suffix(".article_rejects.jsonl"), mode, buffering=1)

        pool = Pool(processes=args.workers)
        processed = 0
        checkpoint_interval = 10000  # Save checkpoint every 10k articles

        # NOTE: ordered imap (not imap_unordered) — the positional checkpoint below
        # (start_idx + processed) is only correct if results return in input order.
        # With unordered completion, a resume could skip articles that never finished.
        for result in tqdm(
            pool.imap(extract, msgs, chunksize=args.chunksize),
            total=n,
            desc="Extracting articles",
        ):
            article_count += result[0]
            case_report_type_count += result[1]
            patient_count += result[2]
            patient_in_case_count += result[1] * result[2]
            error_count += result[3]

            # Write each patient note immediately (JSONL format)
            for patient in result[4]:
                output_file.write(json.dumps(patient) + "\n")

            # Stage 0 article-level reject (license / journal / parse / no-body)
            if result[5] is not None:
                article_rejects_file.write(json.dumps(result[5]) + "\n")

            processed += 1

            # Checkpoint every N articles (AFTER write completes)
            if processed % checkpoint_interval == 0:
                output_file.flush()  # Force write to disk
                os.fsync(output_file.fileno())  # Ensure OS writes to disk
                with open(checkpoint_file, "w") as cf:
                    cf.write(str(start_idx + processed))
                    cf.flush()
                    os.fsync(cf.fileno())  # Ensure checkpoint on disk

        pool.close()
        pool.join()
        output_file.close()
        article_rejects_file.close()

        # Final checkpoint
        with open(checkpoint_file, "w") as f:
            f.write(str(start_idx + processed))

        stat()
        print(f"\nDone! Output saved to: {output_jsonl}")
        print(f"Total patient notes extracted: {patient_count:,}")

        # Clean up checkpoint on successful completion
        if os.path.isfile(checkpoint_file):
            os.remove(checkpoint_file)

    # ──────────────────────────── Scraped path ─────────────────────────────
    if "scraped" in args.source:
        # Our 4 dermatology journals. Each has scrap/output/<journal>/metadata.jsonl
        # with one row per downloaded article + saved PDF/HTML referenced by path.
        SCRAPED_JOURNALS = ["acta_apa", "turkjderm", "odermatol", "rad_argentina"]
        scraped_root = args.scraped_root

        # Append if the PMC path already wrote this run; otherwise start fresh.
        scraped_mode = "a" if "pmc" in args.source else "w"
        out_f = open(output_jsonl, scraped_mode, buffering=1)
        rej_f = open(output_jsonl.with_suffix(".article_rejects.jsonl"), scraped_mode, buffering=1)

        s_articles = s_patients = s_rejects = 0
        reason_counts = {}
        for journal in SCRAPED_JOURNALS:
            meta_path = scraped_root / journal / "metadata.jsonl"
            if not meta_path.is_file():
                print(f"  [skip] {journal}: {meta_path} not found", flush=True)
                continue
            records = [json.loads(l) for l in open(meta_path, encoding="utf-8") if l.strip()]
            print(f"  {journal}: {len(records)} articles", flush=True)
            for record in tqdm(records, desc=f"scraped:{journal}", unit="art"):
                ac, crt, pc, ec, patients, article_reject = extract_scraped_article((record, str(scraped_root)))
                article_count += ac
                case_report_type_count += crt
                patient_count += pc
                error_count += ec
                for p in patients:
                    out_f.write(json.dumps(p) + "\n")
                    s_patients += 1
                if patients:
                    s_articles += 1
                if article_reject is not None:
                    rej_f.write(json.dumps(article_reject) + "\n")
                    s_rejects += 1
                    r = article_reject["reason"]
                    reason_counts[r] = reason_counts.get(r, 0) + 1

        out_f.close()
        rej_f.close()
        print(f"\n=== Scraped extraction complete ===")
        print(f"  Articles with >=1 patient : {s_articles:,}")
        print(f"  Patient candidates written: {s_patients:,}")
        print(f"  Article-level rejects     : {s_rejects:,}")
        for r, c in sorted(reason_counts.items(), key=lambda kv: -kv[1]):
            print(f"      {c:>6}  {r}")
        print(f"  Output: {output_jsonl}")

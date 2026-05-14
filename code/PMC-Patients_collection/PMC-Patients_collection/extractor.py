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
from xml_utils import parse_paragraph, getTitle, getText, getSection, clean_text, clean_refs, extract_article_tables


# CC variants accepted at Stage A. ND-tagged variants (CC BY-ND, CC BY-NC-ND) are excluded
# per the redistribution contract; see CLAUDE.md §6.1. Module-level so multiprocessing
# workers can access it without pickling the function closure.
ALLOWED_LICENSES = {"CC BY", "CC BY-SA", "CC BY-NC", "CC BY-NC-SA", "CC0"}

# Journal allowlist — populated from journal_config.json in __main__ before Pool() forks workers.
# Workers inherit module-level state via fork() on Linux. See DermArena/dataset_collection/journal_config.json.
JOURNAL_DERM_PATTERNS: list = []
JOURNAL_ADJACENT: set = set()


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
    article_tables = []  # populated once body is available; closure-captured by finalize().

    def finalize():
        # Attach per-article case multiplicity to each patient row. Empty-patients
        # returns are no-ops. cases_in_article > 1 signals that the article's
        # Discussion section (and any cross-patient prose) is shared across
        # multiple patients — relevant for Stage F leakage masking and any CoT
        # construction that depends on per-patient attribution.
        # tables: article-level <table-wrap> content. Single-patient articles get
        # them all; multi-patient cases get the union (downstream v0.1 drops
        # multi-patient anyway — see CLAUDE.md §6.1).
        n = len(patients)
        for i, p in enumerate(patients):
            p["cases_in_article"] = n
            p["case_index_in_article"] = i + 1
            p["tables"] = article_tables
        return article_count, case_report_type_count, patient_count, error_count, patients

    # Stage A license filter: exclude ND variants (see ALLOWED_LICENSES at module top).
    if License not in ALLOWED_LICENSES:
        return finalize()
    f = False
    article_count += 1

    try:
        tree = ET.parse(os.path.join(data_dir, file_path))
        root = tree.getroot()
        clean_refs(root)
    except Exception as e:
        error_count += 1
        return finalize()

    article_type = root.attrib['article-type']
    if article_type == 'case-report':
        case_report_type_count += 1
    body = root.find(".//body")
    article_title = root.find(".//article-meta").find(".//article-title")

    # Remove articles without body or title.
    if (body is None) or (article_title is None):
        error_count += 1
        return finalize()

    article_title = getText(article_title)

    # Extract additional metadata
    journal_node = root.find(".//journal-meta//journal-title")
    journal_name = getText(journal_node) if journal_node is not None else "Unknown"

    # Journal allowlist: skip case detection unless journal matches derm-substring rule
    # OR adjacent-journal allowlist. Cheap reject before the expensive XML walks below.
    if not _journal_is_allowlisted(journal_name):
        return finalize()

    # Walk <table-wrap> elements once at article level (root, not just body —
    # publishers sometimes place tables in <floats-group> as a sibling of body).
    # finalize() attaches the result to every patient (single-patient is the v0.1
    # scope per CLAUDE.md §6.1).
    article_tables = extract_article_tables(root)

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
                    patient_count += 1
                    f = True
                    break

    return finalize()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract patient-note candidates from PMC XML")
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
    parser.add_argument("--chunksize", type=int, default=32, help="imap_unordered chunksize")
    parser.add_argument(
        "--journal-config",
        type=Path,
        default=Path("/mnt/hdd/sdc/ssim/DermArena/dataset_collection/journal_config.json"),
        help="Path to journal_config.json (derm_match patterns + adjacent_journals allowlist)",
    )
    args = parser.parse_args()

    # Load journal allowlist into module-level state BEFORE Pool() forks workers (Linux fork inheritance).
    journal_cfg = json.loads(args.journal_config.read_text())
    JOURNAL_DERM_PATTERNS = [p.lower() for p in journal_cfg["derm_match"]["patterns"]]
    JOURNAL_ADJACENT = {_normalize_journal(j) for j in journal_cfg["adjacent_journals"]}
    print(
        f"Journal allowlist loaded: {len(JOURNAL_DERM_PATTERNS)} substring patterns + {len(JOURNAL_ADJACENT)} adjacent journals",
        flush=True,
    )

    # Section_title_trigger, such as "case report", "patient representation", etc.
    title_pattern = re.compile(r'(clinical )?((patient)|(case))(( ((illustrations?)|(report)|(descriptions?)|(information)|(details)|(discussions?)|((re)?presentation))([^a-z]|$))|$)')
    # Detect and further remove label in title such as "3.1" in "3.1 case one"
    label_pattern = re.compile(r'^[0-9]\.?[0-9]?\.?[0-9]?\.? ?')
    # Multi_patient_trigger, for paragraphs staring with "Case 1" and "The first patient", respectively
    case_1_pattern = re.compile(r'^(clinical )?((patient)|(case))( ((illustration)|(report)|(description)|(information)|(details)|(discussion)|((re)?presentation)))?.?\(?(([0-9]{1,2})|([abcde])|(i{1,3}|(i?vi?))|((one)|(two)|(three)|(four)|(five)))\)?($|[^a-z])')
    first_pattern = re.compile(r'^((the)|(our)) ((first)|(second)|(third)|(fourth)|(fifth)|(sixth)|(seventh)|(eighth)|(nineth)|(1-?st)|(2-?nd)|(3-?rd)|([456789]-?th)) ((case)|(patient))')
    # Convert several white space character into " "
    space = r"[\u3000\u2009\u2002\u2003\u00a0\u200a\xa0]"

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

    # Output paths
    output_jsonl = args.output_jsonl
    checkpoint_file = args.checkpoint_file
    
    # Resume logic: check if we have a checkpoint
    start_idx = 0
    if os.path.isfile(checkpoint_file):
        with open(checkpoint_file, "r") as f:
            start_idx = int(f.read().strip())
        print(f"Resuming from article {start_idx:,} (checkpoint found)", flush=True)
        file_list = file_list.iloc[start_idx:].reset_index(drop=True)
    
    article_count = 0
    case_report_type_count = 0
    patient_count = 0
    patient_in_case_count = 0
    error_count = 0

    # Build work queue (vectorized - fast)
    n = len(file_list)
    print(f"Building work queue ({n:,} articles)...", flush=True)
    msgs = list(zip(file_list["file_path"], file_list["PMID"], file_list["License"]))

    # Open output file in append mode (safe for resume)
    mode = "a" if start_idx > 0 else "w"
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    output_file = open(output_jsonl, mode, buffering=1)  # Line buffered
    
    pool = Pool(processes=args.workers)
    processed = 0
    checkpoint_interval = 10000  # Save checkpoint every 10k articles
    
    for result in tqdm(
        pool.imap_unordered(extract, msgs, chunksize=args.chunksize),
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
    
    # Final checkpoint
    with open(checkpoint_file, "w") as f:
        f.write(str(start_idx + processed))

    stat()
    print(f"\nDone! Output saved to: {output_jsonl}")
    print(f"Total patient notes extracted: {patient_count:,}")
    
    # Clean up checkpoint on successful completion
    if os.path.isfile(checkpoint_file):
        os.remove(checkpoint_file)


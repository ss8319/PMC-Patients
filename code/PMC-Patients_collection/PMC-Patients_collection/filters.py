import argparse
import json
import re
import os
from multiprocessing import Pool
from pathlib import Path

from tqdm import trange, tqdm
from word2number import w2n

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from provenance import stamp, reject, rejects_path_for  # vendored append-only per-row trace

# Usual age pattern such as "3 years old", "1-year- and 2-month-old"
age_pattern = r"(((?P<year>[0-9]+\.?[0-9]?([ /\._\-\‐]and[ /\._\-\‐](((a)|(one))[ /\._\-\‐])?half)?)[ /\._\-\‐]*((y((ear)|r)?)s?)([ /\._\-\‐]*and[ /\._\-\‐])?)|((?P<month>[0-9]+\.?[0-9]?([ /\._\-\‐]and[ /\._\-\‐](((a)|(one))[ /\._\-\‐])?half)?)[ /\._\-\‐]*((m(onth)?)s?)([ /\._\-\‐]*and[ /\._\-\‐])?)|((?P<week>[0-9]+\.?[0-9]?([ /\._\-\‐]and[ /\._\-\‐](((a)|(one))[ /\._\-\‐])?half)?)[ /\._\-\‐]*((w(eek)?)s?)([ /\._\-\‐]*and[ /\._\-\‐])?)|((?P<day>[0-9]+\.?[0-9]?([ /\._\-\‐]and[ /\._\-\‐](((a)|(one))[ /\._\-\‐])?half)?)[ /\._\-\‐]*((d(ay)?)s?)([ /\._\-\‐]*and[ /\._\-\‐])?)|((?P<hour>[0-9]+\.?[0-9]?([ /\._\-\‐]and[ /\._\-\‐](((a)|(one))[ /\._\-\‐])?half)?)[ /\._\-\‐]*((h(our)?)s?)))+"
age_pattern1 = re.compile(age_pattern + "[ /\._\-\‐](o(ld)?)[^a-z]")
# Age pattern using words, such as "nine years old", "thirteen years old"
word_age_pattern = r"(((?P<year>((twenty)|(thirty)|(forty)|(fifty)|(sixty)|(seventy)|(eighty)|(ninety))?[ /\._\-\‐]*[a-z]*([ /\._\-\‐]and[ /\._\-\‐](((a)|(one))[ /\._\-\‐])?half)?)[ /\._\-\‐]((y((ear)|r)?)s?)([ /\._\-\‐]*and[ /\._\-\‐])?)|((?P<month>((twenty)|(thirty)|(fifty)|(sixty)|(seventy)|(eighty)|(ninety))?[ /\._\-\‐]*[a-z]*([ /\._\-\‐]and[ /\._\-\‐](((a)|(one))[ /\._\-\‐])?half)?)[ /\._\-\‐]((m(onth)?)s?)([ /\._\-\‐]*and[ /\._\-\‐])?)|((?P<week>((twenty)|(thirty)|(forty)|(fifty)|(sixty)|(seventy)|(eighty)|(ninety))?[ /\._\-\‐]*[a-z]*([ /\._\-\‐]and[ /\._\-\‐](((a)|(one))[ /\._\-\‐])?half)?)[ /\._\-\‐]((w(eek)?)s?)([ /\._\-\‐]*and[ /\._\-\‐])?)|((?P<day>((twenty)|(thirty)|(forty)|(fifty)|(sixty)|(seventy)|(eighty)|(ninety))?[ /\._\-\‐]*[a-z]*([ /\._\-\‐]and[ /\._\-\‐](((a)|(one))[ /\._\-\‐])?half)?)[ /\._\-\‐]((d(ay)?)s?)([ /\._\-\‐]*and[ /\._\-\‐])?)|((?P<hour>((twenty)|(thirty)|(forty)|(fifty)|(sixty)|(seventy)|(eighty)|(ninety))?[ /\._\-\‐]*[a-z]*([ /\._\-\‐]and[ /\._\-\‐](((a)|(one))[ /\._\-\‐])?half)?)[ /\._\-\‐]((h(our)?)s?)))+"
word_age_pattern1 = re.compile(word_age_pattern + "[ /\._\-\‐](o(ld)?)[^a-z]")
# "Male aged 51 years"
age_pattern2 = re.compile(r"(^|[^a-z])((male)|((gentle)?(police)?man)|(boy)|(female)|(lady)|(girl)|(housewife)|((police)?woman)|(.*gravida)|(infant)|(baby)|(child)|(patient)),? aged " + age_pattern + r"[^a-z]")
# "A boy, aged 8"
age_pattern3 = re.compile(r"(^|[^a-z])((male)|((gentle)?(police)?man)|(boy)|(female)|(lady)|(girl)|(housewife)|((police)?woman)|(.*gravida)|(infant)|(baby)|(child)|(patient)),? aged (?P<year>[0-9]+\.?[0-9]?)[^a-z]")
# "Male aged forty six years"
word_age_pattern2 = re.compile(r"(^|[^a-z])((male)|((gentle)?(police)?man)|(boy)|(female)|(lady)|(girl)|(housewife)|((police)?woman)|(.*gravida)|(infant)|(baby)|(child)|(patient)),? aged " + word_age_pattern + r"[^a-z]")
# "Boy aged eight"
word_age_pattern3 = re.compile(r"(^|[^a-z])((male)|((gentle)?(police)?man)|(boy)|(female)|(lady)|(girl)|(housewife)|((police)?woman)|(.*gravida)|(infant)|(baby)|(child)|(patient)),? aged (?P<year>((twenty)|(thirty)|(forty)|(fifty)|(sixty)|(seventy)|(eighty)|(ninety))?[ /\._\-\‐]*[a-z]*([ /\._\-\‐]and[ /\._\-\‐](((a)|(one))[ /\._\-\‐])?half)?)[^a-z]")
# Detect words indicating male.
male_pattern = re.compile(r"(^|[^a-z])((he)|(male)|((gentle)?(police)?man)|(boy)|(prostat[a-z]*)|(mr))[^a-z]")
# Detect words indication female.
female_pattern = re.compile(r"(^|[^a-z])((she)|(female)|(lady)|(girl)|(housewife)|((police)?woman)|([a-z]*gravida)|(pregnan[a-z]*)|((g[0-9])|(p[0-9]))|(mentrua[a-z]*)|(uteri[a-z]*)|(mrs)|(ms))[^a-z]")
# Detect words indicating groups of males. (If group, filter)
males_pattern = re.compile(r"(^|[^a-z])((males)|((gentle)?men)|(boys))[^a-z]")
# Detect words indicating groups of females. (If group, filter)
females_pattern = re.compile(r"(^|[^a-z])((females)|(ladies)|(girls)|(women))[^a-z]")
# "Male in his (early/late) 70s"
age_pattern4 = re.compile(r"(^|[^a-z])((male)|((gentle)?(police)?man)|(boy)|(female)|(lady)|(girl)|(housewife)|((police)?woman)|(.*gravida)|(infant)|(baby)|(child)|(patient)),? in ((his)|(her)) (?P<time>((early)|(late)) )?(?P<year>[0-9]0s)[^a-z]")
# "Male in his (early/late) twenties"
word_age_pattern4 = re.compile(r"(^|[^a-z])((male)|((gentle)?(police)?man)|(boy)|(female)|(lady)|(girl)|(housewife)|((police)?woman)|(.*gravida)|(infant)|(baby)|(child)|(patient)),? in ((his)|(her)) (?P<time>((early)|(late)) )?(?P<year>((twenties)|(thirties)|(fourties)|(fifties)|(sixties)|(seventies)|(eighties)|(nineties)))[^a-z]")


"""
    Extract and return age of the patient.
    Input:
        text: patient note candidate
    Output:
        list of ages of different units, each is a tuple (number, unit).
        e.g. [[1.0, "year"], ["3.0", "month"]]
        Note that age is a float since there could be input "one and a half year" with output '[[1.5, "year"]]'
"""
def age_extract(text):
    results = []
    age = age_pattern1.search(text)
    if age:
        for unit in ['year', 'month', 'week', 'day', 'hour']:
            if age.group(unit):
                if "half" in age.group(unit):
                    temp = re.search(r"[0-9]+\.?[0-9]?", age.group(unit)).group()
                    results.append([float(temp) + 0.5, unit])
                else:
                    results.append([float(age.group(unit)), unit])
    word_age = word_age_pattern1.search(text.replace('fourty', 'forty').replace('ninty', 'ninety'))
    if word_age and len(results) == 0:
        for unit in ['year', 'month', 'week', 'day', 'hour']:
            if word_age.group(unit):
                try:
                    results.append([float(w2n.word_to_num(word_age.group(unit))), unit])
                except Exception as e:
                    continue
                if "half" in word_age.group(unit):
                    results[-1][0] += 0.5

    age = age_pattern2.search(text)
    if age and len(results) == 0:
        for unit in ['year', 'month', 'week', 'day', 'hour']:
            if age.group(unit):
                if "half" in age.group(unit):
                    temp = re.search(r"[0-9]+\.?[0-9]?", age.group(unit)).group()
                    results.append([float(temp) + 0.5, unit])
                else:
                    results.append([float(age.group(unit)), unit])
    word_age = word_age_pattern2.search(text.replace('fourty', 'forty').replace('ninty', 'ninety'))
    if word_age and len(results) == 0:
        for unit in ['year', 'month', 'week', 'day', 'hour']:
            if word_age.group(unit):
                try:
                    results.append([float(w2n.word_to_num(word_age.group(unit))), unit])
                except Exception as e:
                    continue
                if "half" in word_age.group(unit):
                    results[-1][0] += 0.5

    age = age_pattern3.search(text)
    if age and len(results) == 0:
        for unit in ['year']:
            if age.group(unit):
                if "half" in age.group(unit):
                    temp = re.search(r"[0-9]+\.?[0-9]?", age.group(unit)).group()
                    results.append([float(temp) + 0.5, unit])
                else:
                    results.append([float(age.group(unit)), unit])
    word_age = word_age_pattern3.search(text.replace('fourty', 'forty').replace('ninty', 'ninety'))
    if word_age and len(results) == 0:
        for unit in ['year']:
            if word_age.group(unit):
                try:
                    results.append([float(w2n.word_to_num(word_age.group(unit))), unit])
                except Exception as e:
                    continue
                if "half" in word_age.group(unit):
                    results[-1][0] += 0.5

    age = age_pattern4.search(text)
    if age and len(results) == 0:
        for unit in ['year']:
            if age.group(unit):
                results.append([float(age.group(unit)[:-1]), unit])
                if age.group('time'):
                    if 'early' in age.group('time'):
                        results[-1][0] += 2.5
                    else:
                        results[-1][0] += 7.5
                else:
                    results[-1][0] += 5
    word_age = word_age_pattern4.search(text.replace('fourties', 'forties').replace('ninties', 'nineties'))
    if word_age and len(results) == 0:
        for unit in ['year']:
            if word_age.group(unit):
                try:
                    results.append([float(w2n.word_to_num(word_age.group(unit).replace('ties', 'ty'))), unit])
                except Exception as e:
                    continue
                if word_age.group('time'):
                    if 'early' in word_age.group('time'):
                        results[-1][0] += 2.5
                    else:
                        results[-1][0] += 7.5
                else:
                    results[-1][0] += 5
    return results

"""
    Extract gender. Note when both male and female, or males / females are detected, the candidate is filtered.
    Input:
        patient note candidate
    Output:
        "M" or "F"
"""
def gender_extract(text):
    male_match = male_pattern.search(text)
    males_match = males_pattern.search(text)
    female_match = female_pattern.search(text)
    females_match = females_pattern.search(text)
    # If both male and female, or males / females are detected, filter the candidate
    if (male_match or males_match) and (female_match or females_match):
        male_span = male_match.span() if (not males_match) or (male_match and males_match and males_match.span()[0] > male_match.span()[0]) else males_match.span()
        female_span = female_match.span() if (not females_match) or (female_match and females_match and females_match.span()[0] > female_match.span()[0]) else females_match.span()
        if min(abs(female_span[0] - male_span[1]), abs(female_span[1] - male_span[0])) < 20:
            return None

    if male_pattern.search(text):
        return "M"
    else:
        if female_pattern.search(text):
            return "F"
        else:
            return None

"""
    Extract and return age and gender. 
    Input:
        Patient note candidate.
    Output:
        See functions above.
"""
def demo_filter(text):
    text = text.strip().lower()
    texts = text.split('. ')
    text = texts[0] + '. '
    for i in range(0, len(texts) - 1):
        if len(texts[i].strip().split()) < 10:
            text += texts[i + 1] + '. '
        else:
            break
    age = age_extract(text)
    gender = gender_extract(text)
    if (not age) or (not gender):
        return None
    else:
        return (age, gender)

"""
    If language is English. Filter non-English candidate.
"""
def en_filter(case):
    count = 0
    for char in case:
        if ord(char) >= 128:
            count += 1
    return count / len(case) <= 0.03

"""
    If length of the text is greater than 10. Filter too short candidate.
"""
def length_filter(case):
    return len(case.strip().split()) >= 10


def _filter_one_record(dat):
    """
    Per-record CPU work (safe to run in parallel). Returns:
      ('length'|'en'|'demo', None) on reject, ('ok', enriched_dict) on pass.
    demo_filter is called once per passing row.
    """
    patient = dat.get("patient") or ""
    if not length_filter(patient):
        return ("length", None)
    if not en_filter(patient):
        return ("en", None)
    demo = demo_filter(patient)
    if not demo:
        return ("demo", None)
    age, gender = demo
    out = dict(dat)
    out["age"] = age
    out["gender"] = gender
    return ("ok", out)


# ═══════════════════════════════════════════════════════════
# Schema-conformance helpers (case_schema_v0.1.json)
# ═══════════════════════════════════════════════════════════

# Map "year"/"month"/etc. → divisor to convert to years.
_AGE_UNIT_TO_YEARS = {
    "year": 1.0,
    "month": 1.0 / 12.0,
    "week": 1.0 / 52.1429,
    "day": 1.0 / 365.25,
    "hour": 1.0 / (365.25 * 24),
}


def _normalize_license(raw):
    """PMC OA metadata uses 'CC BY' (space); schema enum uses 'CC-BY' (hyphen).
    Returns None for unrecognized values (downstream renderer can decide)."""
    if not raw:
        return None
    canon = raw.strip().replace(" ", "-")
    allowed = {"CC-BY", "CC-BY-SA", "CC-BY-NC", "CC-BY-NC-SA", "CC0"}
    return canon if canon in allowed else None


def _age_to_years(age_tuples):
    """Convert PMC-Patients' [[value, unit], ...] to a single age_years float.
    First tuple wins; sums any same-row mixed units (e.g. '3 years and 2 months')."""
    if not age_tuples:
        return None
    total = 0.0
    for value, unit in age_tuples:
        factor = _AGE_UNIT_TO_YEARS.get(unit)
        if factor is None:
            return None
        total += float(value) * factor
    return round(total, 3)


def _normalize_sex(raw):
    """Schema sex enum: 'male' / 'female' / 'other' / null."""
    if not raw:
        return None
    s = raw.strip().lower()
    if s in ("m", "male"):
        return "male"
    if s in ("f", "female"):
        return "female"
    return "other"


def _build_case_uid(pmid, case_index_in_article):
    """Schema case_uid pattern: ^pmid_[0-9]+_[0-9]+$.
    Single-patient articles use _1 (case_index_in_article == 1)."""
    if not pmid or case_index_in_article is None:
        return None
    return f"pmid_{pmid}_{case_index_in_article}"


if __name__ == "__main__":
    default_meta = Path("/mnt/hdd/sdc/ssim/meta_data")
    parser = argparse.ArgumentParser(description="PMC-Patients demographic / language filters")
    parser.add_argument(
        "--input",
        type=Path,
        default=default_meta / "patient_note_candidates_10k.json",
        help="JSON array of patient note candidates (e.g. 10k subset)",
    )
    parser.add_argument(
        "--output-patients",
        type=Path,
        default=default_meta / "PMC-Patients_10k_subset.json",
        help="Filtered output JSON (PMC-Patients schema)",
    )
    parser.add_argument(
        "--output-pmids",
        type=Path,
        default=default_meta / "PMIDs_10k_subset.json",
        help="Unique PMIDs JSON list",
    )
    parser.add_argument("--workers", type=int, default=18, help="Parallel workers for per-record filters")
    parser.add_argument(
        "--chunksize",
        type=int,
        default=32,
        help="Pool.map chunksize (tune for small batches)",
    )
    args = parser.parse_args()

    if os.environ.get("FILTERS_DEBUG"):
        import ipdb  # noqa: T100

        ipdb.set_trace()

    with args.input.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # Ordered parallel pass: pool.map preserves result order vs input (needed for dedupe "first wins")
    with Pool(processes=args.workers) as pool:
        filtered = pool.map(
            _filter_one_record,
            data,
            chunksize=max(1, args.chunksize),
        )

    low_length_count = 0
    not_en_count = 0
    no_demo_count = 0
    dup_count = 0
    new_data = []
    seen_patients = set()
    patient_count = 0
    patient_in_case_count = 0

    # A1 reject sidecar (provenance option a). Clear stale entries each run.
    _rejects = rejects_path_for(args.output_patients)
    _rejects.unlink(missing_ok=True)
    _drop_reason = {"length": "length_lt_10", "en": "not_english_or_high_nonascii",
                    "demo": "no_parseable_age_or_gender"}

    for _dat, (status, temp) in tqdm(
        list(zip(data, filtered)),
        desc="Dedupe + assign ids",
        total=len(data),
    ):
        if status in ("length", "en", "demo"):
            if status == "length": low_length_count += 1
            elif status == "en": not_en_count += 1
            else: no_demo_count += 1
            reject(_dat, "A1_filters", _drop_reason[status], _rejects)
            continue
        if temp["patient"] in seen_patients:
            dup_count += 1
            reject(_dat, "A1_filters", "duplicate_patient_text", _rejects)
            continue
        seen_patients.add(temp["patient"])
        temp["patient_id"] = str(len(new_data))
        new_data.append(temp)
        patient_count += 1
        if str(temp.get("article_type", "")).strip() == "case-report":
            patient_in_case_count += 1

    print("Patient count: ", patient_count)
    print("Patient in case report type count: ", patient_in_case_count)
    print("Length lt 10 count: ", low_length_count)
    print("Not English count: ", not_en_count)
    print("No demographic count: ", no_demo_count)
    print("Duplicate patient text skipped: ", dup_count)
    print("PMC-Patients count: ", len(new_data))

    patients_out = []
    PMIDs = []
    for i in range(len(new_data)):
        patient = new_data[i]
        PMCID = patient["file_path"].split("/")[-1][3:-4]
        if (i == 0) or (patients_out[i - 1]["patient_uid"].split("-")[0] != PMCID):
            index = "1"
        else:
            index = str(int(patients_out[i - 1]["patient_uid"].split("-")[1]) + 1)
        patient_uid = PMCID + "-" + index
        # Schema-conformant fields (case_schema_v0.1.json) added alongside
        # legacy fields so existing downstream code keeps working while
        # downstream renderers can switch to the canonical shape.
        case_uid = _build_case_uid(patient.get("PMID"), patient.get("case_index_in_article"))
        license_norm = _normalize_license(patient.get("license"))
        age_years = _age_to_years(patient["age"])
        sex = _normalize_sex(patient["gender"])
        # Start with everything the upstream extractor produced (this carries
        # forward unknown fields like `tables[]` added 2026-05-13 — without
        # this, A1 silently drops any field not in the legacy allowlist below).
        # Then layer the normalised / schema-aligned overrides on top.
        temp = dict(patient)
        temp.update({
            "case_uid": case_uid,
            "patient_id": patient["patient_id"],
            "patient_uid": patient_uid,
            "PMID": patient["PMID"],
            "pmcid": patient.get("pmcid"),
            "file_path": patient["file_path"],
            "journal": patient.get("journal"),
            "license": license_norm,                # normalized to schema enum
            "license_raw": patient.get("license"),  # preserved original for audit
            "publication_date": patient.get("publication_date"),
            "article_type": patient.get("article_type"),
            "cases_in_article": patient.get("cases_in_article"),
            "case_index_in_article": patient.get("case_index_in_article"),
            "title": patient["title"],
            "patient": patient["patient"],
            "age": patient["age"],          # legacy: list-of-tuples
            "age_years": age_years,         # schema-conformant numeric
            "gender": patient["gender"],    # legacy: 'M'/'F'
            "sex": sex,                     # schema-conformant enum
        })
        stamp(temp, "A1_filters", "kept",
              age_years=age_years, sex=sex, license=license_norm)
        PMIDs.append(temp["PMID"])
        patients_out.append(temp)

    args.output_patients.parent.mkdir(parents=True, exist_ok=True)
    with args.output_patients.open("w", encoding="utf-8") as f:
        json.dump(patients_out, f, indent=4)
    with args.output_pmids.open("w", encoding="utf-8") as f:
        json.dump(list(set(PMIDs)), f, indent=4)

    print("Wrote", args.output_patients)
    print("Wrote", args.output_pmids)


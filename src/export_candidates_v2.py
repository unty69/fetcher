"""
export_candidates_v2.py — Step 4 production candidate export.

Scope: the FULL is_alt_cluster population (2+ phone OR 2+ username, excluding
the 4 suspected_non_discovery mega-clusters) -- NOT alt_cluster_review.py's
select_candidates() (username_count>=2 only), which validate_v2.py showed
returns exactly the already-labeled 135 -- a dead end for export. This script
deliberately uses the broader definition.

Excludes all 135 canonical_id already present in the root ground-truth
workbook (any вердикт, including the blank row), then applies:

  HARD excludes (cleared by validate_v2.py: 0 false positives on both known
  web identities) -- has_military_flag, has_agency_self_id, empty categories
  (full message text, not sample_titles/title_of()).

  SOFT signals only, used for ranking, never for exclusion -- employer_count
  (bucketed per the CLAUDE.md hypothesis: 2-3 up, 0 neutral, 1 or 4+ down;
  still unconfirmed at scale), content_volume_flag, has_ref_link, corp_domain,
  channel_count, message_count (channel/message count bucketed by tertiles
  computed on the actual surviving population, not guessed cut points).

Threshold: score > 0 (net positive evidence over negative) -- the natural
zero-crossing of a signed additive score, not a percentile picked to hit a
row-count target. Main/borderline split is by score-threshold PROXIMITY
(median split of the exported population), not by content_volume_flag --
that flag is boolean (see report) and CLAUDE.md instructs falling back to
score-proximity for the split when it's boolean rather than tri-state.
content_volume_flag still feeds the score itself as one soft input.

Writes output/candidates_v2_main.xlsx and output/candidates_v2_borderline.xlsx.
Neither is committed (see .gitignore -- output/ is already ignored).
Never writes to cache/, never touches the ground-truth workbook (read_only).
"""
import statistics
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
import openpyxl

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import alt_cluster_review as acr  # noqa: E402

GROUND_TRUTH_PATH = BASE_DIR / "alt_clusters_review.xlsx"
OUTPUT_MAIN = BASE_DIR / "output" / "candidates_v2_main.xlsx"
OUTPUT_BORDERLINE = BASE_DIR / "output" / "candidates_v2_borderline.xlsx"


def load_ground_truth_cids():
    """All canonical_id present in the root workbook, regardless of вердикт
    or blank status -- read_only, never modifies the file."""
    wb = openpyxl.load_workbook(GROUND_TRUTH_PATH, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows_iter = ws.iter_rows(values_only=True)
        header = next(rows_iter)
        cid_col = {h: i for i, h in enumerate(header)}["canonical_id"]
        return {row[cid_col] for row in rows_iter
                if row is not None and not all(v is None for v in row) and row[cid_col]}
    finally:
        wb.close()


def employer_bucket_score(employer_count):
    """CLAUDE.md hypothesis (still unconfirmed at scale -- soft rank only):
    0=weak/unknown (neutral), 1=possible negative, 2-3=possible positive,
    4+=possible aggregator/noise."""
    if employer_count in (2, 3):
        return 2
    if employer_count == 0:
        return 0
    return -1  # 1 or 4+


def tertile_bucketer(values):
    """Returns a function mapping a value to 0/1/2 by tertile of the ACTUAL
    surviving population -- not a guessed cut point. Falls back to constant
    0 if the population is too small/uniform for statistics.quantiles."""
    clean = sorted(v for v in values if v is not None)
    if len(clean) < 3 or clean[0] == clean[-1]:
        return lambda x: 0
    q1, q2 = statistics.quantiles(clean, n=3)

    def bucket(x):
        if x > q2:
            return 2
        if x > q1:
            return 1
        return 0

    return bucket, q1, q2


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    graph = acr.load_identity_graph()
    identities = graph["identities"]
    contact_to_canonical = graph["contact_to_canonical"]

    alt_cluster_pool = {
        cid for cid, ident in identities.items()
        if ident["is_alt_cluster"] and not ident["suspected_non_discovery"]
    }
    print(f"[1] Full is_alt_cluster non-suspND population: {len(alt_cluster_pool)}")

    gt_cids = load_ground_truth_cids()
    print(f"[2] Ground-truth canonical_ids to exclude (any вердикт incl. blank): {len(gt_cids)}")
    gt_in_pool = gt_cids & alt_cluster_pool
    print(f"    of which present in the alt-cluster pool: {len(gt_in_pool)}")

    export_pool = alt_cluster_pool - gt_cids
    print(f"[3] Export pool (alt-cluster minus already-reviewed): {len(export_pool)}")

    print("\nComputing evidence (full message text) for the export pool -- one pass over all dumps...")
    evidence = acr.collect_evidence(export_pool, contact_to_canonical)

    # --- report content_volume_flag / soft-signal distributions on the RAW
    # ~7047 population, before any hard exclusion (what was asked for) ---
    print("\n" + "=" * 78)
    print(f"Soft-signal distributions on the raw export pool (n={len(export_pool)}, "
          f"BEFORE hard excludes)")
    print("=" * 78)
    cv_true = sum(1 for cid in export_pool if evidence[cid]["max_text_len"] >= acr.CONTENT_VOLUME_MIN_CHARS)
    print(f"content_volume_flag (max_text_len >= {acr.CONTENT_VOLUME_MIN_CHARS}, boolean, "
          f"NOT tri-state -- see collect_evidence()/build_rows() in alt_cluster_review.py):")
    print(f"  True (sufficient):  {cv_true}/{len(export_pool)} ({100*cv_true/len(export_pool):.1f}%)")
    print(f"  False (low-content): {len(export_pool)-cv_true}/{len(export_pool)} "
          f"({100*(len(export_pool)-cv_true)/len(export_pool):.1f}%)")
    print("  This is meaningfully different from the 100% seen on the hand-picked 135 in Step 3 --")
    print("  that population was pre-selected by earlier review, this one is not.")

    max_len_values = sorted(evidence[cid]["max_text_len"] for cid in export_pool)
    if max_len_values:
        pcts = statistics.quantiles(max_len_values, n=10) if len(set(max_len_values)) > 1 else [max_len_values[0]] * 9
        print(f"\n  max_text_len percentiles (chars): p10={pcts[0]:.0f} p25={pcts[1]:.0f} "
              f"p50(median)={pcts[4]:.0f} p75={pcts[6]:.0f} p90={pcts[8]:.0f} max={max_len_values[-1]}")

    emp_dist = {}
    for cid in export_pool:
        n = len(evidence[cid]["brands"])
        b = str(n) if n < 4 else "4+"
        emp_dist[b] = emp_dist.get(b, 0) + 1
    print(f"\nemployer_count distribution: " +
          ", ".join(f"{b}={emp_dist.get(b, 0)}" for b in ["0", "1", "2", "3", "4+"]))

    # --- hard excludes ---
    print("\n" + "=" * 78)
    print("Hard excludes (validated in Step 3: 0 false positives on known web)")
    print("=" * 78)
    mil = {cid for cid in export_pool if evidence[cid]["has_military_flag"]}
    ag = {cid for cid in export_pool if evidence[cid]["has_agency_self_id"]}
    empty_cat = {cid for cid in export_pool if len(evidence[cid]["categories"]) == 0}
    excluded = mil | ag | empty_cat
    print(f"has_military_flag=True:        {len(mil)}")
    print(f"has_agency_self_id=True:       {len(ag)}")
    print(f"categories empty (full text):  {len(empty_cat)}")
    print(f"  overlap military & agency:        {len(mil & ag)}")
    print(f"  overlap military & empty_cat:     {len(mil & empty_cat)}")
    print(f"  overlap agency & empty_cat:        {len(ag & empty_cat)}")
    print(f"  overlap all three:                 {len(mil & ag & empty_cat)}")
    print(f"Union excluded (any of the three):    {len(excluded)}")

    survivors = export_pool - excluded
    print(f"\nSurviving after hard excludes: {len(survivors)}")
    print("NOTE: employer_count is explicitly NOT required > 0, and does NOT rescue an "
          "empty-category identity -- category gate stands alone (the 'Яндекс ищет менеджера' trap).")

    # --- soft scoring on survivors only ---
    print("\n" + "=" * 78)
    print(f"Soft-signal distributions on SURVIVORS (n={len(survivors)}, post hard-exclude, "
          f"the actual scoring population)")
    print("=" * 78)
    cv_true_surv = sum(1 for cid in survivors if evidence[cid]["max_text_len"] >= acr.CONTENT_VOLUME_MIN_CHARS)
    print(f"content_volume_flag True: {cv_true_surv}/{len(survivors)} "
          f"({100*cv_true_surv/len(survivors):.1f}%)" if survivors else "n/a")

    channel_counts = {cid: len(identities[cid]["channels"]) for cid in survivors}
    message_counts = {cid: identities[cid]["message_count"] for cid in survivors}
    ch_bucket, ch_q1, ch_q2 = tertile_bucketer(channel_counts.values())
    msg_bucket, msg_q1, msg_q2 = tertile_bucketer(message_counts.values())
    print(f"channel_count tertile cut points (from actual survivor distribution): "
          f"q1={ch_q1:.1f} q2={ch_q2:.1f}")
    print(f"message_count tertile cut points (from actual survivor distribution): "
          f"q1={msg_q1:.1f} q2={msg_q2:.1f}")

    # --- score ---
    print("\nScore formula (additive integer, deterministic, all inputs SOFT per instruction):")
    print("  employer_count bucket: 2-3 -> +2, 0 -> 0, 1 or 4+ -> -1  (CLAUDE.md hypothesis, unconfirmed)")
    print("  content_volume_flag:   True -> +1, False -> 0")
    print("  has_ref_link:          True -> +1, False -> 0")
    print("  corp_domain:           True -> -1, False -> 0  (mirrors existing sort direction)")
    print("  channel_count tertile:  top -> +2, mid -> +1, bottom -> 0")
    print("  message_count tertile:  top -> +2, mid -> +1, bottom -> 0")

    scores = {}
    for cid in survivors:
        ev = evidence[cid]
        ident = identities[cid]
        s = 0
        s += employer_bucket_score(len(ev["brands"]))
        s += 1 if ev["max_text_len"] >= acr.CONTENT_VOLUME_MIN_CHARS else 0
        s += 1 if ev["has_ref_link"] else 0
        s += -1 if acr.has_corp_domain(ident) else 0
        s += ch_bucket(channel_counts[cid])
        s += msg_bucket(message_counts[cid])
        scores[cid] = s

    score_values = sorted(scores.values())
    if score_values:
        print(f"\nScore distribution over {len(score_values)} survivors: "
              f"min={score_values[0]} p25={statistics.quantiles(score_values, n=4)[0]:.1f} "
              f"median={statistics.median(score_values):.1f} "
              f"p75={statistics.quantiles(score_values, n=4)[2]:.1f} max={score_values[-1]}")
        from collections import Counter
        hist = Counter(score_values)
        print("Full histogram (score: count): " +
              ", ".join(f"{k}:{v}" for k, v in sorted(hist.items())))

    # --- threshold: score > 0 (net positive evidence), the natural
    # zero-crossing of a signed additive score -- not a percentile chosen to
    # hit a target row count ---
    THRESHOLD = 0
    exported = {cid: s for cid, s in scores.items() if s > THRESHOLD}
    print(f"\nThreshold: score > {THRESHOLD} (net positive soft evidence). "
          f"Exported: {len(exported)}/{len(survivors)}")

    # main/borderline split by score-threshold PROXIMITY (median split),
    # NOT by content_volume_flag -- see module docstring / report.
    if exported:
        exp_scores_sorted = sorted(exported.values())
        split_median = statistics.median(exp_scores_sorted)
    else:
        split_median = 0
    main_ids = {cid for cid, s in exported.items() if s > split_median}
    borderline_ids = {cid for cid, s in exported.items() if s <= split_median}
    print(f"Main/borderline split at median score ({split_median}) of the exported population:")
    print(f"  main (score > {split_median}):        {len(main_ids)}")
    print(f"  borderline (score <= {split_median}, still > {THRESHOLD}): {len(borderline_ids)}")

    # --- AC-4, now genuinely measurable on the main batch ---
    print("\n" + "=" * 78)
    print("AC-4: Content Sufficiency (now measured -- this population did not exist before Step 4)")
    print("=" * 78)
    if main_ids:
        main_cv_true = sum(1 for cid in main_ids if evidence[cid]["max_text_len"] >= acr.CONTENT_VOLUME_MIN_CHARS)
        pct = 100 * main_cv_true / len(main_ids)
        print(f"candidates_v2_main.xlsx: {main_cv_true}/{len(main_ids)} ({pct:.1f}%) content_volume_flag=True")
        ac4_verdict = "PASS" if pct >= 90 else "FAIL"
        print(f"AC-4 (orientation >=90%): {ac4_verdict} ({pct:.1f}%)")
    else:
        print("main batch is empty -- AC-4 NOT MEASURED")
        ac4_verdict = "NOT MEASURED (empty main batch)"

    # --- write output files ---
    FIELDNAMES = ["вердикт", "canonical_id", "score", "usernames", "phones", "corp_domain",
                  "hr_hint", "has_ref_link", "brands", "employer_count", "multibrand",
                  "categories", "has_military_flag", "has_agency_self_id", "content_volume_flag",
                  "channel_count", "message_count", "first_seen", "last_seen", "sample_titles",
                  "channels", "channel_links"]
    COLUMN_WIDTHS = {
        "вердикт": 14, "canonical_id": 14, "score": 8, "usernames": 45, "phones": 35,
        "corp_domain": 12, "hr_hint": 10, "has_ref_link": 13, "brands": 35,
        "employer_count": 14, "multibrand": 12, "categories": 40,
        "has_military_flag": 16, "has_agency_self_id": 18, "content_volume_flag": 18,
        "channel_count": 14, "message_count": 14, "first_seen": 18, "last_seen": 18,
        "sample_titles": 80, "channels": 55, "channel_links": 60,
    }

    def build_row(cid):
        ident = identities[cid]
        ev = evidence[cid]
        brands = sorted(ev["brands"])
        categories = sorted(ev["categories"])
        channels_list = ident.get("channels") or []
        return {
            "вердикт": "",
            "canonical_id": cid,
            "score": scores[cid],
            "usernames": ", ".join(sorted(c["value"] for c in ident["contacts"] if c["type"] == "username")),
            "phones": ", ".join(sorted(c["value"] for c in ident["contacts"] if c["type"] == "phone")),
            "corp_domain": acr.has_corp_domain(ident),
            "hr_hint": acr.has_hr_hint(ident),
            "has_ref_link": ev["has_ref_link"],
            "brands": ", ".join(brands),
            "employer_count": len(brands),
            "multibrand": len(brands) >= 2,
            "categories": ", ".join(categories),
            "has_military_flag": ev["has_military_flag"],
            "has_agency_self_id": ev["has_agency_self_id"],
            "content_volume_flag": ev["max_text_len"] >= acr.CONTENT_VOLUME_MIN_CHARS,
            "channel_count": len(channels_list),
            "message_count": ident["message_count"],
            "first_seen": acr.parse_dt(ident["first_seen"]),
            "last_seen": acr.parse_dt(ident["last_seen"]),
            "sample_titles": " | ".join(ev["titles"]),
            "channels": ", ".join(channels_list),
            "channel_links": " ".join(f"https://t.me/s/{ch}" for ch in channels_list),
        }

    def write_export(cids, path):
        rows = sorted((build_row(cid) for cid in cids), key=lambda r: -r["score"])
        wb = Workbook()
        ws = wb.active
        ws.title = "candidates"
        ws.append(FIELDNAMES)
        for cell in ws[1]:
            cell.font = Font(bold=True, name="Arial")
        for r in rows:
            ws.append([r[k] for k in FIELDNAMES])
        dv = DataValidation(type="list", formula1='"веб,не_веб,агентство,не_уверен"', allow_blank=True)
        dv.errorTitle = "Недопустимое значение"
        dv.error = "Выбери значение из списка: веб, не_веб, агентство, не_уверен"
        ws.add_data_validation(dv)
        dv.add(f"A2:A{max(len(rows) + 1, 2)}")
        for i, header in enumerate(FIELDNAMES, start=1):
            ws.column_dimensions[get_column_letter(i)].width = COLUMN_WIDTHS[header]
        ws.freeze_panes = "A2"
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
        return len(rows)

    n_main = write_export(main_ids, OUTPUT_MAIN)
    n_border = write_export(borderline_ids, OUTPUT_BORDERLINE)
    print(f"\nWrote {OUTPUT_MAIN.relative_to(BASE_DIR)}: {n_main} rows")
    print(f"Wrote {OUTPUT_BORDERLINE.relative_to(BASE_DIR)}: {n_border} rows")
    print(f"\nAC-4 verdict: {ac4_verdict}")


if __name__ == "__main__":
    main()

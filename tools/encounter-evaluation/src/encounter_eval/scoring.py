"""Species-action scores with explicit partial-label and abstention semantics."""

from collections import Counter


def score(photos, answers, groups):
    counts = Counter(photos=len(photos), encounters=len(groups), labeled_photos=len(answers))
    predictions, membership = {}, {}
    for i, group in enumerate(groups):
        gold_sets = set()
        for pid in group.photo_ids:
            predictions[pid] = group.roster
            membership[pid] = i
            answer = answers.get(str(pid))
            if answer and answer["complete"]:
                gold_sets.add(tuple(answer["taxa"]))
        if len(gold_sets) > 1:
            counts["mixed_encounters"] += 1
            counts["photos_in_mixed_encounters"] += len(group.photo_ids)
    for pid, answer in answers.items():
        roster = predictions[int(pid)]
        expected = set(answer["taxa"])
        counts["positive_labels"] += len(expected)
        counts["complete_labels"] += bool(answer["complete"])
        sources = answer["sources"]
        cohort = "manual" if sources == ["manual"] else "unknown_or_mixed_provenance"
        counts[f"{cohort}_photos"] += 1
        if roster is None:
            counts["abstained_photos"] += 1
            counts["unresolved_positive_labels"] += len(expected)
            continue
        counts["resolved_photos"] += 1
        missed = len(expected - set(roster))
        counts["missing_species"] += missed
        counts[f"{cohort}_missing_species"] += missed
        counts["recovered_positive_labels"] += len(expected & set(roster))
        if answer["complete"]:
            counts["incorrect_additions"] += len(set(roster) - expected)
            counts["exact_roster_matches"] += set(roster) == expected
        else:
            counts["unverified_additions"] += len(set(roster) - expected)
    for a, b in zip(photos, photos[1:], strict=False):
        left, right = answers.get(str(a["id"])), answers.get(str(b["id"]))
        if not left or not right or not left["complete"] or not right["complete"]:
            continue
        cut = membership[a["id"]] != membership[b["id"]]
        if set(left["taxa"]) != set(right["taxa"]):
            counts["known_species_boundaries"] += 1
            counts["missed_species_boundaries"] += not cut
        else:
            counts["equal_roster_pairs"] += 1
            counts["equal_roster_subdivisions"] += cut
    return dict(counts)


def summarize(counts):
    c = Counter(counts)
    n = c["labeled_photos"]
    complete = c["complete_labels"]
    # Cost on resolved photos plus a separate abstention penalty. Positive-only
    # labels cannot establish false additions; those remain explicitly unscored.
    loss = ((2 * c["incorrect_additions"] + c["missing_species"] + c["abstained_photos"]
             + 0.02 * c["encounters"]) / n) if n else None
    return {"counts": dict(c), "objective": loss,
            "review_coverage": c["resolved_photos"] / n if n else None,
            "positive_recall": c["recovered_positive_labels"] / c["positive_labels"] if c["positive_labels"] else None,
            "exact_roster_accuracy": c["exact_roster_matches"] / complete if complete else None,
            "incorrect_additions_per_1000": 1000 * c["incorrect_additions"] / complete if complete else None,
            "missing_species_per_1000": 1000 * c["missing_species"] / n if n else None,
            "encounters_per_1000_photos": 1000 * c["encounters"] / c["photos"] if c["photos"] else None,
            "objective_scope": "complete and partial labels" if complete else "positive-only; extra species unverified"}

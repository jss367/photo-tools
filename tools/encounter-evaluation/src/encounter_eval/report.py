"""Local, escaped HTML comparisons with optional existing thumbnail previews."""

from __future__ import annotations

import html
from pathlib import Path

from .algorithms import run_algorithm
from .library import read_bundle


def esc(value):
    return html.escape(str(value), quote=True)


def number(value):
    return "—" if value is None else f"{value:.3f}"


def write_report(output, manifest, results, *, candidate=None, examples=12):
    output = Path(output)
    rows = []
    for result in results:
        m = result["metrics"]
        rows.append("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in (
            result["algorithm"], result["partition"], result["params"], number(m["objective"]),
            number(m["review_coverage"]), number(m["positive_recall"]), number(m["exact_roster_accuracy"]),
            number(m["incorrect_additions_per_1000"]), number(m["encounters_per_1000_photos"]),
            number(result["runtime_seconds"]),
        )) + "</tr>")
    display = manifest["taxonomy_display"]
    verdict = "No eligible candidate selected."
    if candidate:
        reference = next((r for r in results if r["algorithm"] == "production"
                          and r["partition"] == candidate["partition"]), None)
        if reference and candidate["metrics"]["objective"] is not None:
            verdict = ("The candidate has a lower measured cost on this partition. Validate before changing production."
                       if candidate["metrics"]["objective"] < reference["metrics"]["objective"]
                       else "The candidate did not improve the production baseline on this partition. Keep the current production settings.")

    def roster(value):
        if value is None:
            return "Needs review"
        return ", ".join(display.get(t, t.removeprefix("name:")) for t in value) or "Empty"

    examples_html = []
    if candidate:
        baseline = next((r for r in results if r["algorithm"] == "production"
                         and r["partition"] == candidate["partition"]), None)
        old_scores = {s["id"]: s["metrics"]["objective"] for s in (baseline or {}).get("sessions", [])}
        new_scores = {s["id"]: s["metrics"]["objective"] for s in candidate["sessions"]}
        # Include both largest regressions and improvements, then disagreements.
        ordered = sorted(new_scores, key=lambda sid: (-(new_scores[sid] - old_scores.get(sid, 0)), sid))
        selected = list(dict.fromkeys([*ordered[:max(1, examples // 2)], *reversed(ordered[-max(1, examples // 2):])]))[:examples]
        entries = {s["id"]: s for s in manifest["sessions"]}
        for sid in selected:
            bundle = read_bundle(output, entries[sid])
            old = run_algorithm("production", bundle["photos"], grouping_config=manifest["grouping_config"])
            new = run_algorithm(candidate["algorithm"], bundle["photos"], candidate["params"], manifest["grouping_config"])
            old_map = {pid: (i, g) for i, g in enumerate(old) for pid in g.photo_ids}
            new_map = {pid: (i, g) for i, g in enumerate(new) for pid in g.photo_ids}
            changed = [i for i, p in enumerate(bundle["photos"])
                       if old_map[p["id"]][1].roster != new_map[p["id"]][1].roster
                       or (i > 0 and (old_map[p["id"]][0] != old_map[bundle["photos"][i-1]["id"]][0])
                           != (new_map[p["id"]][0] != new_map[bundle["photos"][i-1]["id"]][0]))]
            indices = set()
            for i in (changed or [0])[:12]:
                indices.update(range(max(0, i - 2), min(len(bundle["photos"]), i + 3)))
            photo_rows = []
            for i in sorted(indices):
                p = bundle["photos"][i]
                pid = p["id"]
                meta = bundle["presentation"][str(pid)]
                answer = bundle["answers"].get(str(pid))
                expected = roster(answer["taxa"]) if answer else "Unlabeled"
                if answer and not answer["complete"]:
                    expected += " (positive labels only)"
                image = ""
                thumb = meta.get("thumbnail")
                if thumb and Path(thumb).is_absolute() and Path(thumb).is_file():
                    image = f'<img loading="lazy" src="{esc(Path(thumb).as_uri())}" alt="Existing photo thumbnail">'
                oi, og = old_map[pid]
                ni, ng = new_map[pid]
                predictions = "; ".join(f"{entry[0]}: {entry[1]:.2f} ({entry[2]})" for entry in p["species_top5"])
                photo_rows.append(f"<tr><td>{image}{esc(meta['filename'])}<br>{esc(p['timestamp'])}</td>"
                                  f"<td>{esc(expected)}</td><td>Group {oi + 1}: {esc(roster(og.roster))}</td>"
                                  f"<td>Group {ni + 1}: {esc(roster(ng.roster))}<br>{esc(ng.reason)}</td>"
                                  f"<td>{esc(predictions)}</td></tr>")
            change = new_scores[sid] - old_scores.get(sid, 0)
            examples_html.append(f"<details open><summary>Session {esc(sid)} · objective change {change:+.3f}"
                                 f" · {len(bundle['photos'])} photos</summary><p>Showing {len(indices)} context photos. "
                                 "Positive change means higher cost.</p><table><thead><tr><th>Photo</th>"
                                 "<th>Human labels</th><th>Production</th><th>Candidate</th><th>Cached predictions</th>"
                                 f"</tr></thead><tbody>{''.join(photo_rows)}</tbody></table></details>")
    limitations = "".join(f"<li>{esc(item)}</li>" for item in manifest["limitations"])
    inventory = "".join(f"<li>{esc(key.replace('_', ' '))}: {esc(value)}</li>" for key, value in manifest["inventory"].items())
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src file: data:;">
<title>Encounter grouping comparison</title><style>
body{{font:15px system-ui,sans-serif;margin:2rem;color:#182a32;background:#f7f9f8}}
h1,h2{{color:#184e42}}table{{border-collapse:collapse;background:white;width:100%;margin:1rem 0}}
th,td{{padding:.65rem;border:1px solid #d8e0db;text-align:left;vertical-align:top}}th{{background:#e7efe9}}
img{{display:block;max-width:170px;max-height:120px}}details{{margin:1.5rem 0}}summary{{font-weight:600}}
.scroll{{overflow-x:auto}}code{{overflow-wrap:anywhere}}
</style><h1>Encounter grouping comparison</h1>
<p>Created {esc(manifest['created_at'])}. These are offline recommendations; application settings are unchanged.</p>
<p><strong>{esc(verdict)}</strong></p>
<p>Data revision <code>{esc(manifest['data_digest'])}</code>. Code revision <code>{esc(manifest['code']['revision'])}</code>.</p>
<h2>Available data</h2><ul>{inventory}</ul><h2>Scores</h2>
<p>Lower objective is better: (2 × incorrect additions + missing species + unresolved labeled photos
+ 0.02 × encounter count) / labeled photos. Coverage is the fraction of labeled photos with a suggested roster.
Exact accuracy and incorrect additions require complete labels; a dash means they cannot be measured.
Positive recall includes unresolved photos in its denominator. The objective uses positive evidence only where rosters are incomplete.</p>
<div class="scroll"><table><thead><tr><th>Algorithm</th><th>Partition</th><th>Parameters</th><th>Objective</th>
<th>Coverage</th><th>Positive recall</th><th>Exact roster accuracy</th><th>Incorrect additions / 1,000 complete photos</th>
<th>Encounters / 1,000 photos</th><th>Runtime seconds</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<h2>Interpretation limits</h2><ul>{limitations}</ul>
<p>The current baseline uses production code with recorded default or explicitly supplied configuration.
It does not silently read the running app's workspace overrides. Input files are retained for exact replay.
New comparisons reread current labels. Tuning does not evaluate the test partition.</p>
<h2>Example improvements and regressions</h2>{''.join(examples_html) or '<p>No candidate examples available.</p>'}
</html>"""
    (output / "report.html").write_text(document, encoding="utf-8")

"""Species identity shared by classifier enrichment and review views.

Model prompts are evidence, not primary keys. Source taxon IDs outrank name
lookups; an ambiguous name must never silently pick the first matching taxon.
"""

import hashlib
import json
from dataclasses import dataclass

from keyword_normalization import keyword_match_key

RESOLUTION_VERSION = "species-identity-v3"

# Verified against iNaturalist taxon 18976 and Cornell's A. viridigenalis
# account. Older DWCA snapshots assign this English name to A. rhodocorytha.
# This is a common-name correction, NOT a synonym between those two species.
COMMON_NAME_CORRECTIONS = {
    "red-crowned amazon": {
        "taxon_id": 18976,
        "scientific_name": "Amazona viridigenalis",
        "common_name": "Red-crowned Parrot",
        "rank": "species",
    },
}
COMMON_NAME_CORRECTIONS["red-crowned parrot"] = COMMON_NAME_CORRECTIONS["red-crowned amazon"]


def resolution_identity():
    return hashlib.sha256(json.dumps(
        [RESOLUTION_VERSION, COMMON_NAME_CORRECTIONS], sort_keys=True,
    ).encode()).hexdigest()


@dataclass(frozen=True)
class SpeciesIdentity:
    key: str
    display_name: str
    scientific_name: str | None = None
    taxon_id: int | None = None  # iNaturalist ID, never a local SQLite row ID
    rank: str | None = None


class SpeciesResolver:
    def __init__(self, taxonomy=None, db=None):
        self.taxonomy = taxonomy
        self.db = db
        self._cache = {}
        self._common_names_verified = False
        self._ambiguous_common = set()
        if db is not None:
            from taxonomy import COMMON_NAME_IDENTITY_VERSION
            self._common_names_verified = db.get_meta("common_name_identity_version") == str(COMMON_NAME_IDENTITY_VERSION)
            self._ambiguous_common = set(json.loads(db.get_meta("ambiguous_common_names") or "[]"))

    def _lookup_id(self, taxon_id):
        if self.taxonomy is not None:
            lookup = getattr(self.taxonomy, "lookup_id", None)
            return lookup(taxon_id) if lookup else None
        if self.db is not None:
            row = self.db.conn.execute(
                "SELECT inat_id AS taxon_id, name AS scientific_name, common_name, rank "
                "FROM taxa WHERE inat_id = ?", (taxon_id,),
            ).fetchone()
            return dict(row) if row else None
        return None

    def _lookup(self, name, scientific=False):
        if not name:
            return None
        if self.taxonomy is not None:
            lookup = getattr(self.taxonomy, "lookup", None)
            return lookup(name) if lookup else None
        if self.db is None:
            return None
        correction = COMMON_NAME_CORRECTIONS.get(keyword_match_key(name)) if not scientific else None
        if correction:
            return self._lookup(correction["scientific_name"], scientific=True) or correction
        # Existing DB indexes also lost alternate-name collisions. Until a
        # taxonomy import records its provenance, only explicit science/IDs
        # and the curated corrections above are evidence of identity.
        if not scientific and (not self._common_names_verified or name.lower().strip() in self._ambiguous_common):
            return self._lookup(name, scientific=True)
        if scientific:
            rows = self.db.conn.execute(
                "SELECT inat_id AS taxon_id, name AS scientific_name, common_name, rank "
                "FROM taxa WHERE name = ?", (name,),
            ).fetchall()
            if not rows:
                from taxonomy import load_scientific_synonyms
                current = load_scientific_synonyms().get(name.lower())
                if current:
                    rows = self.db.conn.execute(
                        "SELECT inat_id AS taxon_id, name AS scientific_name, common_name, rank "
                        "FROM taxa WHERE name = ?", (current,),
                    ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT DISTINCT t.inat_id AS taxon_id, t.name AS scientific_name, "
                "t.common_name, t.rank FROM taxa t WHERE t.name = ? OR t.common_name = ? "
                "UNION SELECT t.inat_id, t.name, t.common_name, t.rank "
                "FROM taxa_common_names cn JOIN taxa t ON t.id = cn.taxon_id "
                "WHERE cn.name = ? COLLATE NOCASE",
                (name, name, name),
            ).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    def resolve(self, name, scientific_name=None, source=None):
        name = str(name or "").strip()
        cache_key = (name, scientific_name, json.dumps(source, sort_keys=True))
        if cache_key in self._cache:
            return self._cache[cache_key]
        fallback = SpeciesIdentity("name:" + keyword_match_key(name), name)
        if source and source.get("ambiguous"):
            return fallback
        # Explicit identity from the label source or fixed model head wins.
        evidence = source or COMMON_NAME_CORRECTIONS.get(keyword_match_key(name))
        if scientific_name and not source:
            evidence = {"scientific_name": scientific_name}
        if evidence:
            sci = evidence.get("scientific_name")
            taxon = self._lookup_id(evidence["taxon_id"]) if evidence.get("taxon_id") else None
            taxon = taxon or self._lookup(sci, scientific=True)
            # A changed taxon assignment requires reconciliation, not a blind merge.
            if (taxon and evidence.get("taxon_id") and taxon.get("taxon_id")
                    and taxon["taxon_id"] != evidence["taxon_id"]):
                taxon = None
            taxon = taxon or evidence
        else:
            taxon = self._lookup(name)
        if taxon and (taxon.get("taxon_id") or taxon.get("scientific_name")):
            tid = taxon.get("taxon_id")
            sci = taxon.get("scientific_name")
            display = taxon.get("common_name") or sci or name
            if tid and not sci:
                # The source distinguishes these taxa even when this catalog
                # cannot name them. Make that distinction visible in review.
                display = f"{name} (taxon {tid})"
            if evidence and sci and display != sci and (self.taxonomy is not None or self.db is not None):
                display_taxon = self._lookup(display)
                if (not display_taxon or display_taxon.get("scientific_name") != sci):
                    display = f"{display} ({sci})"
            result = SpeciesIdentity(
                f"taxon:{tid}" if tid else "scientific:" + sci.casefold(),
                display,
                sci, tid, taxon.get("rank"),
            )
        else:
            result = fallback
        self._cache[cache_key] = result
        return result

    def prediction(self, row):
        """Fixed-head scientific names are primary evidence. Old custom-label
        metadata was inferred from text and can contain group-level mistakes.
        New source-backed custom predictions carry the exact source binomial.
        """
        row = dict(row)
        model = row.get("classifier_model", row.get("model", "")) or ""
        native = row.get("labels_fingerprint") == "tol" or model.startswith("iNat")
        source = None
        if row.get("source_taxon_id"):
            source = {"taxon_id": row["source_taxon_id"], "scientific_name": row.get("scientific_name")}
        return self.resolve(row.get("species"), row.get("scientific_name") if native else None, source)


def correct_common_name_index(by_common, by_scientific):
    """Overlay verified corrections without mutating the on-disk taxonomy."""
    for name, evidence in COMMON_NAME_CORRECTIONS.items():
        entry = by_scientific.get(evidence["scientific_name"].lower())
        if entry and entry.get("taxon_id") == evidence["taxon_id"]:
            by_common[name] = entry


def species_entry_key(entry):
    """Read the optional identity key on a serialized prediction tuple."""
    return entry[3] if len(entry) > 3 and entry[3] else entry[0]

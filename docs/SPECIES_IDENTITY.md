# Species identity and naming

Vireo retains the text used as a classifier prompt separately from the species
identity used for comparison and display. Two common names can refer to one
species, and the same common name can appear on different taxa.

Regional label downloads store the iNaturalist taxon ID, scientific name, and
rank in the label set's JSON metadata. The text file remains a list of prompts.
The metadata includes a digest of that text, so editing a prompt cannot silently
attach another label's old identity. Legacy text files continue to work without
source metadata. Conflicting source identities for the same prompt stop model
construction with a request to use distinct scientific names.
When merged label sets use two names for the same source taxon, Vireo keeps one
deterministically chosen prompt so the classifier does not split that species'
probability between duplicate classes.

Source taxon IDs are persisted in `predictions.source_taxon_id` and carried in
portable classifier artifacts. These are iNaturalist IDs, not local `taxa.id`
values. The original prediction text and confidence remain available. The shared
resolver uses explicit source identity first, model-native scientific names next,
and unambiguous taxonomy lookup for legacy common-name labels. Unresolved names
retain their text. Hybrids are not merged into either parent species.

Pipeline feature loading, cached Process Review names, culling, classifier burst
comparison, and ID Conflicts use this resolution. Cached names are refreshed on
read without rewriting confirmed keywords. Grouping fingerprints change when
resolution rules change, so an old encounter arrangement is marked outdated;
regrouping recomputes membership without rerunning image classification.

## Repair on upgrade

The upgrade includes a narrowly scoped correction for the old taxonomy mapping
from “Red-crowned Amazon” to *Amazona rhodocorytha*. The intended species is
*Amazona viridigenalis*, also called Red-crowned Parrot. The evidence is
[Cornell's species account](https://birdnet.cornell.edu/taxonomy/species/Amazona%20viridigenalis)
and iNaturalist taxon 18976.

On the first normal database initialization after upgrade, Vireo repairs matching
legacy custom-label BioCLIP predictions. It preserves prediction IDs, raw labels,
confidence, keywords, and every workspace's review decisions. Fixed-head outputs,
source-backed predictions, hybrids, and other ambiguous historical mismatches
are excluded. This is not a general replacement of “Amazon” with “Parrot.”

Every changed row has a before/after record in `species_identity_repairs`, including
the reason, resolution version digest, and time. The repair is transactional and
idempotent. A preview is available programmatically through
`species_identity_repair.plan_repairs(connection)` on an initialized schema;
it performs only SELECT queries.

To inspect the audit:

```sql
SELECT prediction_id, before_json, after_json, reason, repaired_at
FROM species_identity_repairs
ORDER BY id;
```

Classifier cache identity includes both the label source metadata and the species
resolution policy. Old portable artifacts cannot satisfy the new runtime identity.
Text-embedding identity still depends on the prompt strings, so changing taxonomy
metadata does not require recomputing those embeddings. Corrected historical runs
are marked legacy until republished or classified under the new runtime.

Older downloaded taxonomy files and database name indexes did not retain all
alternate-name collisions. Until taxonomy is downloaded and imported again,
those unverified common names retain their raw text instead of inferring a taxon;
explicit source IDs, scientific names, and the verified red-crowned correction
continue to resolve. Downloads carry a common-name identity format version, and
imports preserve the ambiguity exclusions for database-backed review and culling.

Pipeline prediction tuples retain their identity key alongside display name,
confidence, and model through serialization and cache refresh. Encounter scoring,
consensus, rarity protection, and culling use that key. If an explicit source ID
has no scientific name in the catalog, review labels include the taxon number so
different unresolved taxa with the same common name remain distinguishable.

Accepting a source-backed prediction retains its iNaturalist ID on the keyword
and binds the local taxon directly. Existing keywords for another taxon are not
reassigned; a distinct name is used when necessary. IDs missing from the local
taxonomy remain on the keyword and are linked after a later taxonomy import,
without renaming already confirmed keywords. Label-set metadata updates for the
same source ID do not make the prompt ambiguous.

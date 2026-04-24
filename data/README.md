# Migration Eval Data

## Gold anchor set

`gold_anchor_template.json` is shipped as an empty JSON array `[]`. The
populated `gold_anchor.json` is **harvested automatically** by
[`scripts/mine_gold_anchor.py`](../scripts/mine_gold_anchor.py) from public
OSS migration PRs that were merged and survived ≥30 days without a revert
- no human reviewer time required.

See [docs/gold_anchor.md](../docs/gold_anchor.md) for scope, re-anchoring
cadence, and the merge-survival labeling procedure.

### Schema

Each entry is an object validated by
[`schemas/gold_anchor_entry.schema.json`](../schemas/gold_anchor_entry.schema.json):

| Field            | Type   | Notes                                                       |
| ---------------- | ------ | ----------------------------------------------------------- |
| `repo_url`       | string | Canonical repository URL.                                   |
| `commit_sha`     | string | Merge commit of the source PR.                              |
| `human_verdict`  | string | `"accept"` (merged + survived ≥30d) or `"reject"`.          |
| `reviewer_notes` | string | Provenance - typically the source PR URL + check method.    |
| `labeled_at`     | string | ISO 8601 timestamp the label was harvested.                 |

The `human_verdict` field name is preserved for schema backward
compatibility; semantically it is the implicit maintainer verdict observed
via merge-survival.

### 12-month half-life

Labels older than 12 months should be treated as stale and re-harvested
before being used to compute `gold_anchor_correlation`. The quarterly
re-harvest cadence keeps median label age well under 6 months in practice.
See the gold-anchor doc for the full rationale.

# Migration Eval Data

## Gold anchor set

`gold_anchor_template.json` is shipped as an empty JSON array `[]`. Real
labels are never committed to this repository — they live in a private store
and are loaded at analysis time. See
[docs/gold_anchor.md](../docs/gold_anchor.md)
for scope, re-anchoring cadence, and privacy handling.

### Schema

Each entry in the gold set is an object validated by
[schemas/gold_anchor_entry.schema.json](../../schemas/gold_anchor_entry.schema.json):

| Field            | Type   | Notes                                    |
| ---------------- | ------ | ---------------------------------------- |
| `repo_url`       | string | Canonical repository URL.                |
| `commit_sha`     | string | Commit SHA that was reviewed.            |
| `human_verdict`  | string | Must be `"accept"` or `"reject"`.        |
| `reviewer_notes` | string | Free-form reviewer commentary.           |
| `labeled_at`     | string | ISO 8601 timestamp of the labeling act.  |

### 12-month half-life

Gold labels decay: a human review conducted 12 months ago is less reliable
as an anchor than one conducted last week because both the ecosystem and the
oracle funnel have moved. Labels older than 12 months should be treated as
stale and re-reviewed before being used to compute
`gold_anchor_correlation`. See the module doc for the full rationale and
operational cadence.

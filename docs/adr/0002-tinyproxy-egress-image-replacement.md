# ADR 0002: Replace `vimagick/tinyproxy` with `kalaksi/tinyproxy` for the egress sidecar

- **Status:** Accepted
- **Date:** 2026-04-28
- **Bead:** migration-evals-csm
- **Related beads:** migration-evals-eg8 (digest pin that surfaced this gap),
  migration-evals-91m / migration-evals-0ez (sidecar threat model)

## Context

`DockerSandboxAdapter` runs an HTTP CONNECT proxy as a sidecar
(`SandboxPolicy.proxy_image`) so a `network = "pull"` workload can
reach an allowlisted set of registries without being given the host
network namespace. The previous default,
`vimagick/tinyproxy@sha256:72b441b9...`, was pinned by digest
([migration-evals-eg8](../../README.md)) which closed the supply-
chain float of the `:latest` tag. That fix made the bytes immutable
but left two underlying weaknesses:

1. **Image is a single-maintainer, abandoned artifact.** The
   `vimagick/tinyproxy` Docker Hub image was last pushed
   2021-07-22. Whatever CVEs land in `tinyproxy` upstream after that
   date — including any in the version it actually ships
   (`tinyproxy 1.11.0` on Alpine 3.14) — cannot be picked up by
   refreshing the digest, because the upstream image itself never
   gets a new build.
2. **Alpine base is end-of-life.** Alpine 3.14 reached upstream
   end-of-support in 2023-05; security errata for the base layer have
   stopped flowing.

The wave-4 review marked this HIGH and deferred remediation to this
bead. The action items were: (1) audit known `tinyproxy` CVEs since
1.11.0 and (2) evaluate alternatives.

## CVE audit

The four publicly-tracked `tinyproxy` CVEs since the 1.11.0 release
(2021-04-16):

| CVE | Year | Severity | Function | Affects 1.11.0? | Fixed in |
| --- | --- | --- | --- | --- | --- |
| CVE-2022-40468 | 2022 | Medium (info-leak) | `process_request()` (uninit buffer used by custom error templates) | NVD CPE lists 1.10.0 + 1.11.1; Alpine treats all `<1.11.2-r0` as affected | 1.11.2 |
| CVE-2023-49606 | 2023 | **Critical (CVSS 9.8 — UAF → RCE)** | `remove_connection_headers()` in `src/reqs.c` | NVD CPE lists 1.10.0 + 1.11.1 only; Alpine and Snyk advisories conservatively flag all `<1.11.2-r0` because the buggy header-handling code path is the same | 1.11.2 (commit `12a8484`) |
| CVE-2023-40533 | 2023 | n/a (rejected / withdrawn) | — | — | — |
| CVE-2025-63938 | 2025 | Medium (CVSS 6.4 — port-parser integer overflow → filter bypass) | `strip_return_port()` in `src/reqs.c` | Affects all `<= 1.11.2` | 1.11.3 |

Threat-model relevance (per migration-evals-91m / migration-evals-0ez):

- **CVE-2023-49606** is the load-bearing one. RCE inside the egress
  sidecar would give an attacker code execution on a container that
  is attached to the default bridge for outbound traffic — exactly
  the blast radius the sidecar exists to contain. Even though the
  NVD CPE conservatively lists only 1.10.0 + 1.11.1, the underlying
  buggy code path (`remove_connection_headers()` deleting and then
  re-reading freed memory) was present continuously through the
  1.11.x line until the 1.11.2 fix. A digest pin to the historical
  vimagick image cannot pick this fix up.
- **CVE-2022-40468** is lower-impact for us because we never
  configure the custom-error-template feature that triggers the
  uninit read; still, "fixed by upgrading the base image" is the
  cheapest mitigation.
- **CVE-2025-63938** lets an oversized numeric port (e.g.
  `example.com:4294967440`) wrap past `tinyproxy`'s configured
  `ConnectPort` allowlist. In our deployment the proxy's `Allow`
  directive is pinned to the per-sandbox internal subnet (one
  workload container, one trial), so the only client able to send
  that crafted CONNECT is the workload we are already executing
  inside the sandbox. Filter-bypass therefore moves the attacker
  from "I can ask my own sandbox to reach an unallowlisted host"
  back to "I can ask my own sandbox to reach an unallowlisted
  host" — no new privilege. A future bump to 1.11.3 closes this for
  hygiene, but it is not RCE.
- The HTTP CONNECT bypass / auth-bypass class is out of scope: we
  configure no upstream auth and no `BasicAuth`.

## Alternatives considered

### (a) Stay on the current `vimagick/tinyproxy@sha256:` pin

Cost: zero. Fit: poor. The digest pin freezes the bits, but the bits
contain unpatched 1.11.0 with the `remove_connection_headers()` UAF
code path. The wave-4 reviewer's framing is correct: this is a
deferred HIGH, not a closed one.

### (b) Switch to a maintained community fork on Docker Hub

Surveyed the obvious candidates by Docker Hub last-push date and
upstream-repo activity:

| Image | Last push | Base | tinyproxy | Notes |
| --- | --- | --- | --- | --- |
| `vimagick/tinyproxy` | 2021-07-22 | Alpine 3.14 | 1.11.0 | (current) |
| `monokal/tinyproxy` | ~2020 (>5y) | unspecified | 1.8.x | abandoned |
| `dannydirect/tinyproxy` | several years | — | — | unmaintained |
| `ajoergensen/tinyproxy` | ~2025 (>1y) | — | — | low-volume |
| **`kalaksi/tinyproxy`** | **2026-04-24** | **Alpine 3.23.4** | **1.11.2** | **builds via GitLab CI; SLSA in-toto provenance attached; 87 commits, semver tags (1.7, 1.6, 1.2)** |

`kalaksi/tinyproxy:1.7` was end-to-end smoke-tested locally:

- Boots cleanly under our launch flags (`--read-only --tmpfs /tmp
  --user 1000:1000 -v <conf>:/etc/tinyproxy:ro`). The image's
  `CMD` does a `if [ ! -f "$CONFIG" ]; then ... cp default.conf ...
  fi; exec /usr/bin/tinyproxy -d` shell trampoline; because we
  mount our own conf, the `if` is skipped and `exec` falls through
  to `tinyproxy -d`.
- `nc` is at `/usr/bin/nc` (busybox 1.37.0) — the existing
  `_wait_for_proxy_ready` probe (`nc -z 127.0.0.1 <port>`) works
  unchanged.
- tinyproxy version reports `1.11.2`. Filter directives
  (`Filter`, `FilterDefaultDeny`, `FilterExtended`, `Allow`) are
  identical to the upstream we already target, so the existing
  `_render_proxy_config` and `_render_proxy_filter` templates are
  byte-compatible.
- Multi-arch (linux/amd64 + linux/arm64) under one OCI image-index;
  pinning the index digest preserves the host's native arch on
  pull.

### (c) Switch to a different proxy entirely (`goproxy`, `mitmproxy`, `squid`)

The `tinyproxy.conf` template (`adapters_docker.py:_render_proxy_config`)
uses `tinyproxy`-specific directive syntax — `FilterDefaultDeny Yes`
plus an anchored regex per allowlisted host in a sibling file. None
of the alternatives accept the same conf:

- `goproxy` is a Go library, not a daemon — switching would mean
  shipping a first-party Go binary and re-implementing the deny-
  by-default + per-host regex semantics in code.
- `mitmproxy --mode=upstream` is a Python daemon with an addon API;
  drop-in image size is ~70MB compressed (vs ~3MB for tinyproxy),
  the deny-by-default logic must be written as a Python addon, and
  the busybox-compatibility assumption for the `nc -z` probe no
  longer holds.
- `squid` ships a much larger config surface; we'd be configuring
  away features (caching, ICAP, ICP, SNMP) we do not want exposed.
  Image is ~50MB+.

All three would require rewriting `_render_proxy_config`,
`_render_proxy_filter`, and the readiness probe — and the
maintenance burden of "we now own the proxy semantics layer"
exceeds the maintenance burden of "we depend on a community
tinyproxy image and refresh the digest pin." Out of scope.

### (d) Build a first-party tinyproxy image and publish to GHCR

Plausible, but the cost is non-trivial: writing the `Dockerfile`,
publishing under an org account, signing with `cosign`, and adding
a refresh cadence (a CI job that bumps the Alpine base on each
upstream patch release and re-pins the digest). For the same end-
state as option (b) — tinyproxy 1.11.2+ on a current Alpine —
option (d) just internalizes the build pipeline. Defer until (b)
fails an evaluation cycle.

## Decision

**Pick (b).** Replace the egress sidecar's default image with
`kalaksi/tinyproxy@sha256:6eddb7eba70227000b2a8948e84ecbf88db87bc910a54682ebef58cef9eb3887`
(the multi-arch image-index digest of `kalaksi/tinyproxy:1.7`,
inspected via `docker buildx imagetools inspect` on 2026-04-28).

Reasoning, anchored to the wave-4 criteria:

1. **Maintained upstream.** Image was rebuilt 4 days before this
   ADR; upstream repo has 87 commits on the master branch and
   tracks Alpine releases (currently Alpine 3.23.4, supported
   through ~2027). The previous pin was static for almost 5 years.
2. **Alpine-busybox parity preserved.** The image keeps the
   busybox `nc` we depend on for `_wait_for_proxy_ready`, so the
   readiness probe works without modification. Verified locally.
3. **Sidecar-class size.** ~7MB compressed (vimagick was ~3MB);
   well below the threshold that would justify swapping out
   tinyproxy entirely.
4. **CVE coverage.** Ships `tinyproxy 1.11.2`, which patches
   CVE-2023-49606 (the only critical in the audit) and
   CVE-2022-40468. Residual CVE-2025-63938 (medium, port-parser
   integer overflow) is mitigated for our threat model by the
   existing per-sandbox `Allow <subnet>` (only the workload we are
   running can reach the proxy).
5. **Supply-chain hardening.** The image carries SLSA in-toto
   provenance attestations attached to the OCI image-index — a
   stronger signal than the previous image, which had none.
   Combined with the sha256 digest pin, this is the minimum
   defense-in-depth for a sidecar with bridge-network egress.

## Consequences

1. `DEFAULT_PROXY_IMAGE` in `src/migration_evals/sandbox_policy.py`
   changes from `vimagick/tinyproxy@sha256:72b441b9...` to
   `kalaksi/tinyproxy@sha256:6eddb7eb...`. The accompanying
   operator-facing comment is rewritten so the digest-refresh
   recipe targets the new repo.
2. `tests/test_sandbox_policy.py::test_default_proxy_image_pinned_to_sha256_digest`
   and `test_default_proxy_image_digest_has_expected_length` are
   updated to assert the `kalaksi/tinyproxy@sha256:` prefix. They
   still fail loudly if a future change reverts to a floating tag.
3. Operators must pre-pull the new image on each runner host
   (`docker pull "$(python -c 'from migration_evals.sandbox_policy import DEFAULT_PROXY_IMAGE; print(DEFAULT_PROXY_IMAGE)')"`).
   The previous pre-pull recipe in `docs/sandbox_policy.md` is
   updated to point at the constant rather than a hard-coded
   image name, so future digest refreshes do not silently leave
   the doc stale.
4. The 1.11.0-vs-1.11.2 CONNECT-port-suffix difference is already
   handled — `_anchored_host_regex` emits `^<host>(:[0-9]+)?$`,
   which matches both port-stripped and port-retained forms.
   Existing test (`test_anchored_host_regex_tolerates_port_suffix`)
   continues to enforce this, with a docstring update so the
   "verified locally" reference points at the right image.

## Re-evaluation triggers

Refresh this ADR (and the digest pin) when any of the following
occur:

1. A `tinyproxy` CVE rated HIGH or CRITICAL is published against
   1.11.2 or later, **and** the upstream `kalaksi/tinyproxy`
   image has not picked up the patch within ~30 days. (At that
   point: open a child bead and either bump to a newer tag or
   exercise option (d) above.)
2. `kalaksi/tinyproxy` upstream goes silent for 12+ months
   (no Docker Hub pushes, no commits to `kalaksi/docker-tinyproxy`).
3. Alpine 3.23 reaches end-of-life (currently scheduled for
   ~2027-05) without `kalaksi/tinyproxy` having moved to a
   supported release.
4. CVE-2025-63938 turns out to be exploitable beyond
   filter-bypass in our deployment shape (e.g. a research note
   demonstrates a path to RCE through `strip_return_port()` in
   1.11.2 specifically). At that point, bump to 1.11.3 or later.

## Follow-up beads

None at this time. The decision is implementable in this bead and
the re-evaluation triggers above are written as standing
documentation rather than scheduled work.

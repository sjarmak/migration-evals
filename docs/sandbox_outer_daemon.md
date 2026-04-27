# Outer-Daemon Hardening (gk0)

The Docker-backed sandbox in `src/migration_evals/adapters_docker.py`
hardens the *inside* of the trial container (read [sandbox_policy.md](./sandbox_policy.md)
for that layer). This doc covers the *outside*: the daemon that
launches those containers. Today that daemon runs as root on the host;
this is the design space and the recommended migration path for
running it rootless.

## Threat model

Even with `cap_drop=ALL`, `no_new_privileges`, non-root user, and a
read-only repo mount, the host still trusts the docker daemon to:

1. Mount arbitrary host paths (a misconfigured `-v` flag could expose
   `/var/run`, `/proc`, `/etc`).
2. Use any Linux capability when `docker run` is invoked, regardless
   of what the *container* gets — the daemon process itself is root.
3. Modify cgroups, network namespaces, and seccomp policy on the host.

A bug in the daemon, a privileged image we forgot to verify, or a
config-injection on the host side (a recipe that crafts an unexpected
`docker_bin` argument, say) can each escalate to host root because
the daemon has the keys. A rootless outer daemon shrinks that
surface: an exploit of the daemon yields the *user's* shell, not
root.

## Options

### Option A — Rootless Docker

Docker ships an official rootless mode
(<https://docs.docker.com/engine/security/rootless/>). The daemon
runs as the invoking user; containers run inside the user's
namespace.

**Install (per-user):**
```bash
dockerd-rootless-setuptool.sh install
export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock
```

**Constraints to verify before adopting:**

- **Storage driver.** Rootless requires `overlay2` on a kernel
  with unprivileged-overlayfs support, or falls back to
  `fuse-overlayfs` (slower, available everywhere). Performance
  regression on a large `mvn package` workload should be measured
  before committing.
- **Network.** Rootless uses `slirp4netns` or `pasta` for outbound
  network. `--network none` (our hardened default) still works
  because it disables the namespace entirely. `--network bridge`
  with the recipe's allowlist (the `network = "pull"` policy) goes
  through the userspace stack — throughput is lower than rootful
  bridge, MTU sometimes needs tuning.
- **Cgroups v2 required** for resource limits to apply to the
  container. v1 hosts get no `--memory` / `--cpus` enforcement
  rootless.
- **Privileged ports** (<1024) cannot be bound. Not a constraint
  for our use case; recorded for completeness.

**When this is the right pick:** the operator is on the same Docker
release train as the rest of the team and wants the smallest
diff from current state.

### Option B — Podman rootless as a drop-in

Podman aims for Docker-CLI compatibility
(<https://docs.podman.io/en/latest/markdown/podman.1.html>). The
adapter exposes `adapters.docker_bin` (see
`adapters_docker.py:DEFAULT_DOCKER_BIN` and the
`build_sandbox_adapter` factory) as the override knob, and the unit
test
[`test_docker_bin_override_threads_through_every_subprocess_call`](../tests/test_adapters_docker.py)
asserts that flipping that knob causes every subprocess call
(`run`, `exec`, `kill`, `rm -f`) to invoke the new binary.

**Adopt by editing the smoke YAML:**
```yaml
adapters:
  sandbox_provider: docker
  docker_bin: podman
  sandbox_policy:
    network: none
    # ... unchanged
```

**Verified by the unit test:** the binary swap reaches every
subprocess invocation. Nothing in the adapter assumes Docker-only
behaviour beyond the flag set already used.

**NOT verified locally — confirm before adopting:**

- `--security-opt=no-new-privileges:true` syntax. Podman accepts
  `no-new-privileges` per its docs but the `:true` suffix
  specifically should be smoke-tested.
- `--cap-drop=ALL` semantics under rootless. The user namespace
  already strips most capabilities; `cap-drop=ALL` should be a
  no-op-or-stricter, but worth a `podman inspect` confirmation.
- `--user 1000:1000` with rootless podman maps through `/etc/subuid`
  / `/etc/subgid`. The container UID `1000` is *not* host UID 1000
  — it lands somewhere in the subuid range. Anything that compares
  host-side file ownership against container-side must account for
  this.
- `--network none` works identically (both daemons just skip the
  network namespace). `--network bridge` differs: rootless podman
  uses slirp4netns/pasta; allowlist-label semantics carry over but
  any future egress filter built on iptables would need a podman
  port.
- Image storage lives under `~/.local/share/containers/` instead
  of `/var/lib/docker/`. Disk-budget alarms need to point at the
  right path.

**When this is the right pick:** the operator wants the strongest
isolation (no daemon at all — podman is daemonless) and is willing
to validate the divergences above on first adoption.

### Option C — Stay on rootful Docker with extra mitigations

The current state. The hardened container (see
[sandbox_policy.md](./sandbox_policy.md)) is the primary defense:
`cap_drop=ALL`, `no_new_privileges`, non-root user, read-only repo
mount, `--network none`. A daemon-level exploit *would* still grant
host root, but the trial code itself cannot reach the daemon
control socket from inside the container.

Mitigations that strengthen this stance without changing the
daemon:

- Keep the daemon socket (`/var/run/docker.sock`) off all bind
  mounts. The adapter does not mount it; this is enforced by
  inspection, not code.
- Run the eval workload under a non-root operator account that
  is in the `docker` group only on the eval host, never on a
  shared developer box.
- Pin the docker engine version in the eval-host Ansible /
  setup script (out of scope for this repo).

**When this is the right pick:** short-term operational continuity
while Option A or B is being validated.

## Recommended path

1. **Default for new deployments: Option B (podman rootless).**
   The drop-in cost is one config-line change, the upstream
   compatibility story is documented, and the daemonless model
   has a smaller blast radius than even rootless docker.
2. **Validate the five "NOT verified" items above** on the target
   host before flipping the knob in production. Record results in
   a follow-up bead.
3. **Fallback: Option A (rootless docker)** if a podman-specific
   divergence blocks adoption.
4. **Option C remains acceptable** for short-lived eval hosts where
   the operational cost of changing the daemon outweighs the
   marginal isolation gain — but document the choice on the host.

## Verification

How to confirm the daemon is actually rootless:

```bash
# Rootless docker: 'rootless' appears in the SecurityOptions list.
docker info --format '{{.SecurityOptions}}'
# -> [name=seccomp,profile=builtin name=rootless]

# Rootless docker: socket lives under XDG_RUNTIME_DIR, not /var/run.
echo "$DOCKER_HOST"
# -> unix:///run/user/1000/docker.sock

# Podman: there is no daemon to interrogate; verify the binary and
# that the storage root is in $HOME.
podman info --format '{{.Store.GraphRoot}}'
# -> /home/<user>/.local/share/containers/storage
```

A simple post-install smoke (host-side, not in CI):

```bash
# Should fail without privilege escalation under both rootless modes.
docker run --rm --privileged alpine true || echo "rootless OK"
```

## CI implications

The repo's only workflow today is `.github/workflows/publication_gate.yml`,
which runs the publication gate on PRs that touch
`runs/analysis/mig_*/`. It does not exercise the docker sandbox at
all — the test suite uses `subprocess.run` mocking
(see `tests/test_adapters_docker.py`).

A podman matrix smoke job is **not added in this change** because:

- `ubuntu-latest` runners do not ship podman by default; an
  `apt-get install podman` step would test the matrix wiring, not
  the actual sandbox claims (no rootless mode under the GHA
  runner's nested-virt constraints without extra setup).
- Faking a green podman job would be worse than no job — it would
  imply validation we did not perform.

**Recommended matrix shape for when podman lands in CI** (e.g.
self-hosted runner or a custom container with rootless podman
preinstalled):

```yaml
jobs:
  sandbox-smoke:
    strategy:
      fail-fast: false
      matrix:
        backend:
          - { name: docker, bin: docker }
          - { name: podman, bin: podman }
    runs-on: ubuntu-latest  # or self-hosted-rootless
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e '.[dev]'
      - name: Live sandbox roundtrip
        env:
          MIGRATION_EVAL_DOCKER_INTEGRATION: "1"
          DOCKER_BIN: ${{ matrix.backend.bin }}
        run: pytest -q tests/test_adapters_docker.py -k live_docker_roundtrip
```

The existing live integration test
(`test_live_docker_roundtrip`) already gates on
`MIGRATION_EVAL_DOCKER_INTEGRATION=1`; adopting the matrix above
also requires parameterising it on `DOCKER_BIN`. That change is
deferred until a runner with rootless podman is available.

## See also

- [sandbox_policy.md](./sandbox_policy.md) — the inner (per-container) hardening this layer composes with.
- [`src/migration_evals/adapters_docker.py`](../src/migration_evals/adapters_docker.py) — the `docker_bin` knob and the factory that threads it through.

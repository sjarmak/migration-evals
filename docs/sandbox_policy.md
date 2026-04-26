# Sandbox Policy (7gu)

The Docker-backed sandbox adapter (`src/migration_evals/adapters_docker.py`)
applies a hardened policy by default. Codex review #3 surfaced that the
prior adapter mounted the trial repo read-write, ran with the host's
default network, dropped no Linux capabilities, left
`no-new-privileges` off, and ran as root inside the container — a real
exfiltration / host-interaction surface for arbitrary repo code
(`build_cmd` and `test_cmd` come from a recipe, not from us).

## Defaults

`SandboxPolicy.hardened_default()` is what every trial gets unless a
recipe or smoke-config opts out. Every `docker run` issued by the
adapter therefore carries:

| flag                                        | effect                                                                                                                                                 |
|---------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------|
| `--network none`                            | No network namespace. DNS, curl, package-installer, exfil endpoints — none reachable. Recipes that need a registry pull opt in via `network = "pull"`. |
| `--cap-drop=ALL`                            | All Linux capabilities dropped. Recipes opt back in to a specific cap via `cap_add`.                                                                   |
| `--security-opt=no-new-privileges:true`     | A setuid binary inside the container cannot grant a capability the container lacks. Combined with `cap-drop=ALL`, setuid escalation is contained.      |
| `--user 1000:1000`                          | Rootless inside the container.                                                                                                                         |
| repo mount `:ro`                            | The trial repo is mounted read-only. Recipes that mutate the source tree (legacy `mvn -DoutputDirectory`, etc.) opt out via `repo_mount_readonly`.     |
| writable scratch volume at `/scratch`       | Build artefacts go here. Cleaned up with the container in `destroy_sandbox`.                                                                           |

## Opting in to looser stances

Configure under `adapters.sandbox_policy:` in the smoke YAML:

```yaml
adapters:
  sandbox_provider: docker
  sandbox_policy:
    network: pull
    network_allowlist:
      - registry-1.docker.io
      - proxy.golang.org
    cap_add:
      - SYS_PTRACE
    repo_mount_readonly: false
```

Constraints enforced at construction time:

- `network = "pull"` requires a non-empty `network_allowlist` (otherwise
  the policy raises `ValueError`).
- `network = "none"` (the default) requires `network_allowlist` to be
  empty.

The allowlist is currently recorded as container labels for
auditability; egress filtering itself is left to the host's
network/proxy layer (the adapter does not configure a per-container
egress firewall — that's a follow-up).

## Containment claims

The sandbox-hardening unit tests in `tests/test_adapters_docker.py`
assert that each malicious-patch class the codex review called out is
contained:

- **filesystem writes outside scratch** — read-only repo mount + the
  `1000:1000` user blocks `/etc`, `/var`, etc.
- **DNS exfiltration** — `--network none` denies any network operation.
- **setuid escalation** — `--security-opt=no-new-privileges` plus
  `--cap-drop=ALL` neuters setuid bits.

## Follow-ups

- Recipe-template `sandbox_policy:` block: the runner currently reads
  the policy from the smoke YAML's `adapters_cfg` only. A recipe
  template (`configs/recipes/<mig>.yaml`) that declares its own
  `sandbox_policy` block is not yet merged in. Tracked as a follow-up
  bead — the canonical place to encode "Java 17 needs SYS_PTRACE for
  jstack-based tests" is the recipe, not the smoke runner.
- Egress allowlist enforcement: today `network = "pull"` opens the
  default bridge and records the allowlist as a label. A real egress
  filter (proxy / nftables / userland) is a follow-up.
- Rootless Docker / podman: the user is rootless inside the container.
  Running the *outer* docker daemon rootless (or substituting podman
  rootless) is a follow-up.

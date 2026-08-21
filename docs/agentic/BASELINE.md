# Repository baseline

**Snapshot:** This file records observed state at one commit. It is not a live status page.

- Captured at: `2026-08-21T11:49:38+05:00`
- Target: `/private/tmp/remote-computer-cost-controls`
- Git root: `/private/tmp/remote-computer-cost-controls`
- Branch and commit: `codex/cost-controls` at `6f109bc2cd9849306dd12217b6665eeb47f48ab9`
- Pre-existing changes: clean

## Evidence convention

- **OBSERVED:** named file, command, or runtime result.
- **INFERRED:** evidence-backed interpretation not declared by the repository.
- **UNKNOWN:** absent, contradictory, inaccessible, or not safely testable.

## Instruction map

- **OBSERVED:** The repository has no `AGENTS.md`, `CLAUDE.md`, or contributor guide.
- **OBSERVED:** `README.md` is the root product and development entrypoint.
- **OBSERVED:** `SKILL.md` is the installed agent procedure and owns the provisioning workflow.
- **OBSERVED:** `references/aws.md`, `references/gcp.md`, `references/github.md`, and `references/registry.md` own provider and state details.

## Declared purpose

- **OBSERVED:** `README.md` declares a Codex and Claude skill for provisioning auditable AWS EC2 or Google Cloud Compute Engine development VMs.
- **OBSERVED:** The workflow discovers cloud tools, verifies cloud context, creates dedicated SSH access, optionally ports Git and GitHub configuration, tracks VMs in `~/.vms.json`, reconciles provider state, and hands off SSH access.
- **UNKNOWN:** The repository does not define a product-level cost ceiling, maximum runtime, automatic shutdown deadline, or billing-data integration.

## Topology and runtime

- **OBSERVED:** `SKILL.md` is the user-facing orchestration entrypoint.
- **OBSERVED:** `scripts/vm_bookkeeper.py` is the deterministic local control plane for registry validation, atomic writes, provider reconciliation, SSH keys, GitHub authentication metadata, and clipboard handoff.
- **OBSERVED:** `references/aws.md` and `references/gcp.md` contain the cloud CLI command templates that create instances.
- **OBSERVED:** `install-skill.sh` installs `SKILL.md`, `agents/`, `scripts/`, and `references/` into a Codex or Claude skill directory.
- **OBSERVED:** `.github/workflows/ci.yml` validates Python 3.10 and 3.12, strict Pyright, shell syntax, unit tests, skill metadata, and packaged output.
- **INFERRED:** `vm_bookkeeper.py` is the narrowest owner for provider-neutral cost-policy validation and registry metadata. Provider-specific boot-time wiring belongs in the provider references.

## Toolchain

- **OBSERVED:** Python 3.10 or newer, POSIX shell, `unittest`, PyYAML for skill validation, and Pyright through Node 22 in CI.
- **OBSERVED:** There is no Python package manifest, lockfile, build artifact, service runtime, migration layer, or deployed application.
- **OBSERVED:** Runtime integrations are local executables: `aws`, `gcloud`, `git`, `gh`, `gpg`, `ssh`, `scp`, `ssh-keygen`, and clipboard tools.

## Validation state

| Command | Result | Evidence or first actionable failure |
|---|---|---|
| `python3 -m unittest discover -s tests -v` | `PASS` | 26 tests passed at the captured commit. |
| `sh -n install-skill.sh` | `PASS` | Exit 0. |
| `python3 scripts/quick_validate.py .` | `NOT RUN` | Local prerequisite missing: `ModuleNotFoundError: No module named 'yaml'`. |
| `npx --yes pyright@1.1.413 --level error` | `NOT RUN` | Pyright was not already available locally during the no-install baseline. |
| Cloud provisioning or provider reconciliation | `NOT RUN` | No configured account, live resource, or external mutation is required for this feature baseline. |

## Existing control surfaces

| Capability | Owner or equivalent | Gap |
|---|---|---|
| Agent entry map | `README.md`, `SKILL.md` | No gap relevant to this task. |
| Architecture | `README.md`, provider references, `references/registry.md` | No explicit architecture record beyond the operational docs. |
| Product and quality | `SKILL.md`, tests, `.github/workflows/ci.yml` | No declared cost-control invariant. |
| Active plans and decisions | None | No versioned task contract or decision log existed. |
| Unified gate | `.github/workflows/ci.yml` | CI composes checks, but there is no single local gate command. |
| Runtime evaluation | Mocked provider CLI tests and direct helper CLI output | No live cloud fixture, preview account, or billing sandbox. |

## Risks, debt, and contradictions

- **OBSERVED:** The skill warns that charges may apply but does not compute or enforce a ceiling.
- **OBSERVED:** Machine preset prices are deliberately not hard-coded because regional prices and allowances change.
- **INFERRED:** A dollar cap based on a supplied compute rate can bound compute only. Boot disks, snapshots, network transfer, static IPs, taxes, discounts, and other services remain outside that estimate.
- **INFERRED:** A local-only deadline checker is not a reliable control when the operator closes the laptop. The VM needs an instance-side absolute shutdown timer to enforce elapsed-time limits without a local session.
- **OBSERVED:** Registry schema version 1 accepts legacy VM records and optional top-level GitHub metadata. Any additive cost metadata must preserve that compatibility.

## Unknowns

- **UNKNOWN:** Whether future users need provider billing API reconciliation, organization-wide budgets, alert destinations, or resource termination instead of stop.
- **UNKNOWN:** Whether non-Ubuntu images should receive an instance-side guard. The declared default is Ubuntu 24.04 LTS.

## Baseline boundary

The snapshot excludes live AWS and Google Cloud calls, cloud price queries, VM creation, billing exports, and provider cleanup. It records only repository files and local deterministic checks. The observation helper wrote its inventory to `/private/tmp/remote-computer-observation-20260821.json`, outside the repository.

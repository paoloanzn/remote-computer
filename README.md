# remote-computer

A Codex/Claude agent skill for provisioning auditable remote development VMs on AWS EC2 or Google Cloud Compute Engine. It discovers installed cloud CLIs, verifies account/project context, offers practical machine presets, creates dedicated SSH access, plans one uninterrupted compute run against an explicit budget, enforces an auto-stop deadline, optionally ports Git identity, GitHub authentication, and commit signing, tracks live instances in `~/.vms.json`, reconciles provider status, and places a direct SSH command on the clipboard.

## Install or update

```sh
curl -fsSL https://raw.githubusercontent.com/aidvgg/remote-computer/main/install-skill.sh | sh
```

The installer auto-detects Codex or Claude. Override with `AGENT=codex`, `AGENT=claude`, or `SKILLS_DIR=/custom/path`.

## Use

Ask the agent to use `$remote-computer`, for example: “Use `$remote-computer` to create an AWS development VM” or “Provision a small GCP VM and port my GitHub setup.” The skill guides the agent through provider and tier selection, identity/network checks, an explicit pre-creation confirmation, provisioning, optional Git/GitHub/GPG porting, registry update, live-state verification, and connection handoff.

| Tier | AWS | Google Cloud |
| --- | --- | --- |
| Starter | `t3.medium` · 2 vCPU · 4 GiB | `e2-medium` · 2 shared vCPU · 4 GiB |
| Standard | `t3.large` · 2 vCPU · 8 GiB | `e2-standard-2` · 2 vCPU · 8 GiB |
| Performance | `t3.xlarge` · 4 vCPU · 16 GiB | `e2-standard-4` · 4 vCPU · 16 GiB |
| Heavy | `t3.2xlarge` · 8 vCPU · 32 GiB | `e2-standard-8` · 8 vCPU · 32 GiB |
| Free Tier allowance | none | eligible `e2-micro` usage in selected US regions; limits apply |

Requirements: Python 3.10+, `ssh-keygen`, `ssh`, `scp`, and at least one configured provider CLI (`aws` or `gcloud`). GitHub configuration discovery uses local Git/GPG/SSH metadata and optionally `gh`; it never reads token values or private-key contents. Clipboard handoff uses `pbcopy` on macOS or `wl-copy`, `xclip`, or `xsel` on Linux, with a printed-command fallback.

## Cost controls

Every deployment can bind a current provider compute rate to a maximum runtime and USD compute budget for one single uninterrupted provider billing lifecycle before cloud creation:

```sh
python3 scripts/vm_bookkeeper.py plan-cost \
  --hourly-rate-usd 0.0832 --max-runtime-hours 8 --max-compute-usd 1.00
python3 scripts/vm_bookkeeper.py render-cost-guard \
  --stop-at <stop-at-from-plan> --output /tmp/remote-computer-cost-guard.sh
python3 scripts/vm_bookkeeper.py cost-status
```

Use `--max-runtime-seconds` instead of `--max-runtime-hours` for an exact whole-second deadline such as 60 seconds or five minutes. Hour values must convert exactly to whole seconds. `plan-cost` returns canonical `max_runtime_seconds` for registry upsert.

`plan-cost` exits nonzero when the projected compute amount exceeds the budget or the pricing check is more than 24 hours old. Its single-lifecycle projection applies one AWS or GCP 60-second minimum before per-second pricing. A repeating decimal charge is rounded upward to 28 decimal places, never downward into an approval. Admission inputs support at most 128 significant digits and exponent magnitude 128; generated projections reserve additional precision so accepted plans round-trip through the registry. `render-cost-guard` creates startup data that installs an absolute UTC systemd timer and a boot-time expiry check inside the VM. The guest shuts itself down at the deadline even when the local computer is offline, and a later manual restart after expiry stops again. The registry records the rate, source, check time, budget, runtime, projection, deadline, and guard type.

The budget comparison models one uninterrupted provider billing lifecycle only. Every provider stop/start begins another billing lifecycle and another minimum charge, so re-plan before starting a stopped VM. Repeated or out-of-band starts can exceed the recorded budget because a guest-side guard cannot prevent the provider from charging for the attempt. The projection also does not cap the total cloud bill. Boot disks, snapshots, network transfer, public IPs, premium images, taxes, discounts, credits, and other services remain outside it. Stopping a VM does not delete its disks or other resources.

## Safety and state

The skill never stores private-key material, tokens, or cloud credentials in its registry. VM-access Ed25519 keys are separate from GitHub authentication and commit-signing keys. The optional top-level `github_auth` field stores only shared-key paths, fingerprints, labels, and verified VM installation metadata while preserving registry schema version 1 and the existing VM-record shape. Registry writes are atomic with mode `0600`. SSH ingress defaults to the user's confirmed `/32` or `/128`; cloud resources are created only after the agent displays the resolved account, location, machine, disk, network exposure, keys, and potential billing impact and receives explicit confirmation. “Free Tier” is an allowance, not a guarantee of zero cost.

Provider and credential procedures live in [`references/aws.md`](references/aws.md), [`references/gcp.md`](references/gcp.md), [`references/github.md`](references/github.md), and [`references/registry.md`](references/registry.md). The deterministic helper is [`scripts/vm_bookkeeper.py`](scripts/vm_bookkeeper.py):

```sh
python3 scripts/vm_bookkeeper.py doctor
python3 scripts/vm_bookkeeper.py init
python3 scripts/vm_bookkeeper.py github-discover --verify-ssh
python3 scripts/vm_bookkeeper.py sync
```

## Develop

```sh
python3 -m pip install PyYAML
./check
```

CI runs strict Pyright, unit/integration tests, skill validation, and shell syntax checks on pushes and pull requests.

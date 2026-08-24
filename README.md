# remote-computer

A Codex/Claude agent skill for provisioning auditable remote development VMs on AWS EC2 or Google Cloud Compute Engine. It discovers installed cloud CLIs, verifies account/project context, offers practical machine presets, creates dedicated SSH access, optionally ports Git identity, GitHub authentication, and commit signing, tracks live instances in `~/.vms.json`, reconciles provider status, and places a direct SSH command on the clipboard.

## Install or update

```sh
curl -fsSL https://raw.githubusercontent.com/paoloanzn/remote-computer/main/install-skill.sh | sh
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
| Free Tier allowance | — | eligible `e2-micro` usage in selected US regions; limits apply |

Requirements: Python 3.10+, `ssh-keygen`, `ssh`, `scp`, and at least one configured provider CLI (`aws` or `gcloud`). GitHub configuration discovery uses local Git/GPG/SSH metadata and optionally `gh`; it never reads token values or private-key contents. OpenPGP signing transfer additionally requires local GnuPG and an apt-based Ubuntu/Debian VM with passwordless sudo. It installs remote `gnupg`, `gpg-agent`, and `pinentry-curses`, streams the selected protected key without a plaintext export, and verifies unlocking through a forced interactive SSH PTY. Clipboard handoff uses `pbcopy` on macOS or `wl-copy`, `xclip`, or `xsel` on Linux, with a printed-command fallback.

## Safety and state

The skill never stores private-key material, tokens, or cloud credentials in its registry. VM-access Ed25519 keys are separate from GitHub authentication and commit-signing keys. The optional top-level `github_auth` field stores only shared-key paths, fingerprints, labels, and verified VM installation metadata while preserving registry schema version 1 and the existing VM-record shape. Registry writes are atomic with mode `0600`. SSH ingress defaults to the user's confirmed `/32` or `/128`; cloud resources are created only after the agent displays the resolved account, location, machine, disk, network exposure, keys, and potential billing impact and receives explicit confirmation. “Free Tier” is an allowance, not a guarantee of zero cost.

Provider and credential procedures live in [`references/aws.md`](references/aws.md), [`references/gcp.md`](references/gcp.md), [`references/github.md`](references/github.md), and [`references/registry.md`](references/registry.md). The deterministic helper is [`scripts/vm_bookkeeper.py`](scripts/vm_bookkeeper.py):

```sh
python3 scripts/vm_bookkeeper.py doctor
python3 scripts/vm_bookkeeper.py init
python3 scripts/vm_bookkeeper.py github-discover --verify-ssh
python3 scripts/vm_bookkeeper.py github-install-gpg --provider aws --id i-example --fingerprint FULL_FINGERPRINT
python3 scripts/vm_bookkeeper.py github-verify-gpg --provider aws --id i-example --fingerprint FULL_FINGERPRINT
python3 scripts/vm_bookkeeper.py sync
```

## Develop

```sh
python3 -m pip install PyYAML
npx --yes pyright@1.1.413 --level error
python3 -m unittest discover -s tests -v
python3 scripts/quick_validate.py .
sh -n install-skill.sh
```

CI runs strict Pyright, unit/integration tests, skill validation, and shell syntax checks on pushes and pull requests.

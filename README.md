# remote-computer

A Codex/Claude agent skill for provisioning auditable development VMs on AWS EC2 or Google Cloud Compute Engine, or as local [apple/container](https://github.com/apple/container) Linux containers on an Apple silicon Mac. It discovers installed provider CLIs, verifies account/project context, offers practical machine presets, creates dedicated SSH access, optionally ports Git identity, GitHub authentication, and commit signing, tracks live instances in `~/.vms.json`, reconciles provider status, and places a direct SSH command on the clipboard.

## Install or update

```sh
curl -fsSL https://raw.githubusercontent.com/paoloanzn/remote-computer/main/install-skill.sh | sh
```

The installer auto-detects Codex or Claude. Override with `AGENT=codex`, `AGENT=claude`, or `SKILLS_DIR=/custom/path`.

## Use

Ask the agent to use `$remote-computer`, for example: “Use `$remote-computer` to create an AWS development VM”, “Provision a small GCP VM and port my GitHub setup”, or “Create a local Apple container for multi-language development.” The skill guides the agent through provider and tier selection, identity/network checks, an explicit pre-creation confirmation, provisioning, optional Git/GitHub/GPG porting, registry update, live-state verification, and connection handoff.

| Tier | AWS | Google Cloud |
| --- | --- | --- |
| Starter | `t3.medium` · 2 vCPU · 4 GiB | `e2-medium` · 2 shared vCPU · 4 GiB |
| Standard | `t3.large` · 2 vCPU · 8 GiB | `e2-standard-2` · 2 vCPU · 8 GiB |
| Performance | `t3.xlarge` · 4 vCPU · 16 GiB | `e2-standard-4` · 4 vCPU · 16 GiB |
| Heavy | `t3.2xlarge` · 8 vCPU · 32 GiB | `e2-standard-8` · 8 vCPU · 32 GiB |
| Free Tier allowance | — | eligible `e2-micro` usage in selected US regions; limits apply |

### Local Apple containers

On an Apple silicon Mac running macOS 26 with `container` 1.0 or newer (validated against 1.6.0), the agent reads the Mac's CPUs, memory, and free disk and proposes a size for the container. It asks whether you have a Dockerfile. If you do, it saves it as a custom configuration, adds the managed SSH layer, infers the workload tier, and proposes resources for you to confirm. If not, it offers three maintained defaults:

| Config | Workload | Contents | Example on an M4, 16 GB, 200+ GB free |
| --- | --- | --- | --- |
| `essentials` | Light: scripting, small projects | Ubuntu 24.04, Git, Python 3, GCC, CLI tools | 2-4 CPUs · 4 GiB · 20 GiB |
| `polyglot` | Standard: multi-language development | + Node.js LTS, Go, Rust, Java 21, uv, CMake | 4-6 CPUs · 6 GiB · 30-40 GiB |
| `fullstack` | Heavy: large builds, local services | + Clang/LLVM, Ninja, ccache, GDB, PostgreSQL, Redis | 7-8 CPUs · 8 GiB · 80 GiB |

Memory is a ceiling: the VM takes host memory only as the workload uses it. The disk size caps a sparse named volume mounted at `/home`. Defaults and custom configurations live in `~/Documents/Containers/<config>/Dockerfile`, alongside a `.remote-computer.json` manifest. The helper refreshes untouched defaults and never overwrites your edits.

Every configuration ends with a managed SSH layer: key-only `sshd` for user `ubuntu` with passwordless sudo, and host keys kept on the volume. The container is reached only on its host-only vmnet IP, never through a published port or a home-directory mount. The skill reads the container's host key through `container exec` to pin it in `known_hosts` under a stable alias. Records go in the same `~/.vms.json` (provider `apple-container`), and Git, GitHub, and GPG porting work exactly as on cloud VMs.

Requirements: Python 3.10+, `ssh-keygen`, `ssh`, `scp`, and at least one configured provider: the `aws` or `gcloud` CLI, or the apple/container `container` CLI. GitHub configuration discovery uses local Git/GPG/SSH metadata and optionally `gh`; it never reads token values or private-key contents. OpenPGP signing transfer additionally requires local GnuPG and an apt-based Ubuntu/Debian VM with passwordless sudo. It installs remote `gnupg`, `gpg-agent`, and `pinentry-curses`, streams the selected protected key without a plaintext export, and verifies unlocking through a forced interactive SSH PTY. Clipboard handoff uses `pbcopy` on macOS or `wl-copy`, `xclip`, or `xsel` on Linux, with a printed-command fallback.

## Safety and state

The skill never stores private-key material, tokens, or cloud credentials in its registry. VM-access Ed25519 keys are separate from GitHub authentication and commit-signing keys. The optional top-level `github_auth` field stores only shared-key paths, fingerprints, labels, and verified VM installation metadata while preserving registry schema version 1 and the existing VM-record shape. Registry writes are atomic with mode `0600`. SSH ingress defaults to the user's confirmed `/32` or `/128`; cloud resources are created only after the agent displays the resolved account, location, machine, disk, network exposure, keys, and potential billing impact and receives explicit confirmation. “Free Tier” is an allowance, not a guarantee of zero cost.

Provider and credential procedures live in [`references/aws.md`](references/aws.md), [`references/gcp.md`](references/gcp.md), [`references/apple-container.md`](references/apple-container.md), [`references/github.md`](references/github.md), and [`references/registry.md`](references/registry.md). Container Dockerfiles and the managed SSH layer live in [`assets/containers/`](assets/containers/). The deterministic helper is [`scripts/vm_bookkeeper.py`](scripts/vm_bookkeeper.py):

```sh
python3 scripts/vm_bookkeeper.py doctor
python3 scripts/vm_bookkeeper.py init
python3 scripts/vm_bookkeeper.py github-discover --verify-ssh
python3 scripts/vm_bookkeeper.py github-install-gpg --provider aws --id i-example --fingerprint FULL_FINGERPRINT
python3 scripts/vm_bookkeeper.py github-verify-gpg --provider aws --id i-example --fingerprint FULL_FINGERPRINT
python3 scripts/vm_bookkeeper.py sync
python3 scripts/vm_bookkeeper.py ssh-command --provider apple-container --id dev-box
python3 scripts/vm_bookkeeper.py container-plan
python3 scripts/vm_bookkeeper.py container-config-import --name my-app --dockerfile ./Dockerfile
python3 scripts/vm_bookkeeper.py container-status --id dev-box --wait 90
python3 scripts/vm_bookkeeper.py container-trust-host-key --id dev-box
```

## Develop

```sh
python3 -m pip install PyYAML
npx --yes pyright@1.1.413 --level error
python3 -m unittest discover -s tests -v
python3 scripts/quick_validate.py .
sh -n install-skill.sh
REMOTE_COMPUTER_DOCKER_TESTS=1 python3 -m unittest tests.test_apple_container  # optional: builds and SSH-tests the essentials image with Docker
```

CI runs strict Pyright, unit/integration tests, skill validation, and shell syntax checks on pushes and pull requests.

---
name: remote-computer
description: Configure and provision a cost-planned remote development VM on AWS EC2 or Google Cloud Compute Engine, verify cloud context, choose a machine tier, bind one uninterrupted billing lifecycle to a current compute rate, USD budget, and auto-stop deadline, create dedicated SSH keys, optionally port local Git/GitHub/GPG configuration, maintain the ~/.vms.json registry, reconcile status, and prepare direct SSH access. Use when a user asks to create, deploy, configure, resume, inspect, connect to, control cost for, or prepare GitHub access on a remote cloud development VM on AWS or GCP.
---

# Remote Computer

Provision one auditable development VM at a time. Resolve bundled `scripts/` and `references/` paths relative to the directory containing this `SKILL.md`, not the user's current project. Treat cloud creation, firewall changes, and billable resources as consequential actions: inspect first, show the exact deployment summary, and obtain explicit confirmation immediately before creating resources.

## Non-negotiable rules

- Never expose credentials, private key contents, access tokens, or cloud credential files.
- Treat VM-access keys, GitHub authentication keys, and commit-signing keys as separate roles. Never replace or remove one while configuring another.
- Never copy HTTPS tokens or credential-store contents between computers. Prefer a verified SSH key or an interactive `gh auth login` flow.
- Ask explicitly before copying any private SSH key or GPG secret key to a VM. Show paths and fingerprints, never key contents.
- Never assume that a tier is free. Show provider, region/zone, machine type, disk, image, network exposure, and likely billing status before confirmation.
- Never hard-code or recall a provider price. Resolve the current on-demand compute rate from an official provider source immediately before planning, record the source and check time, and state that the compute budget does not cap the total cloud bill.
- Require a positive maximum runtime and USD compute budget. Do not create a VM when `plan-cost` rejects the projection.
- Treat the budget as admission for one single uninterrupted provider billing lifecycle. Every provider stop/start begins another minimum charge. Never start a stopped VM under an old policy; obtain a fresh rate, budget, deadline, and explicit confirmation first.
- Install and verify the absolute UTC auto-stop timer on the declared Ubuntu systemd image. Do not claim unattended enforcement for an image that lacks systemd or for a timer that was not verified.
- Restrict SSH ingress to the user's confirmed public IPv4 `/32` or IPv6 `/128`; do not open port 22 to the world unless the user explicitly requests it after a warning.
- Create a dedicated unencrypted SSH key only when the user did not explicitly provide a key. Store private keys under `~/.ssh/remote-computer/` with mode `0600`; registry records contain paths, never key material.
- Do not delete instances, keys, disks, firewall rules, or registry entries unless the user explicitly requests cleanup and confirms the resolved targets.
- Do not mark a VM stopped or missing merely because a CLI, network, permission, or parsing error occurred. Preserve its previous status and report the failed check.
- Use `scripts/vm_bookkeeper.py` for keys, GitHub discovery/porting, registry writes, status reconciliation, and clipboard handling. Do not hand-edit `~/.vms.json` when the helper can perform the operation.

## Workflow

### 1. Discover local tools

Run:

```bash
python3 scripts/vm_bookkeeper.py doctor
```

Before changing the registry, note whether `~/.vms.json` already exists for the end-of-run reconciliation rule. Then locate, create, or validate it:

```bash
python3 scripts/vm_bookkeeper.py init
```

Identify `aws` and `gcloud` independently. Also report Git, GitHub CLI, GPG, SSH/SCP, and clipboard availability. Tell the user which cloud CLIs are installed, then always ask whether to deploy to AWS or Google Cloud. If the selected CLI is absent, pause provisioning and offer its official installation instructions; do not silently switch providers.

### 2. Ask for a tier

Offer these presets and allow a custom machine type if the user asks:

| Tier | Intended use | AWS | Google Cloud |
| --- | --- | --- | --- |
| Starter | Light coding and small services | `t3.medium` (2 vCPU, 4 GiB) | `e2-medium` (2 shared-core vCPU, 4 GiB) |
| Standard | General development | `t3.large` (2 vCPU, 8 GiB) | `e2-standard-2` (2 vCPU, 8 GiB) |
| Performance | Builds and multi-service work | `t3.xlarge` (4 vCPU, 16 GiB) | `e2-standard-4` (4 vCPU, 16 GiB) |
| Heavy | Large builds and data workloads | `t3.2xlarge` (8 vCPU, 32 GiB) | `e2-standard-8` (8 vCPU, 32 GiB) |
| Google Cloud Free Tier | Small, low-throughput work | Not applicable | One eligible non-preemptible `e2-micro` allowance in `us-west1`, `us-central1`, or `us-east1` |

Describe Google Cloud's option as a Free Tier allowance, not “guaranteed free.” Before selecting it, read `references/gcp.md` and state its region, disk, egress, billing-account, and aggregate monthly usage constraints. Existing usage can consume the allowance.

Ask for the provider first, then tier. Afterward resolve a VM name, AWS region or GCP zone, OS image, disk size/type, network, and SSH source CIDR. Default to Ubuntu 24.04 LTS, a provider-generated ephemeral public IP, user `ubuntu`, and a 30 GiB encrypted boot disk when compatible with the selected allowance.

### 3. Set the compute budget and deadline

Ask for a positive maximum runtime and maximum compute budget in USD for one single uninterrupted provider billing lifecycle. Read the selected provider reference and resolve the current on-demand hourly compute rate from an official provider pricing source. Record a short source label and the UTC check time. Do not use a price from this repository or assume a Free Tier allowance remains available. The helper rejects a pricing check more than 24 hours old or more than five minutes in the future.

Run the admission gate with the observed rate:

```bash
python3 scripts/vm_bookkeeper.py plan-cost \
  --hourly-rate-usd <current-rate> \
  --max-runtime-hours <hours> \
  --max-compute-usd <budget> \
  --start-at <pricing-checked-at-utc>
```

For an exact whole-second duration, especially common short deadlines such as 60 seconds or five minutes, replace `--max-runtime-hours <hours>` with `--max-runtime-seconds <seconds>`. Hour values must convert exactly to whole seconds. Preserve the returned canonical `max_runtime_seconds` for registry upsert. Decimal inputs support at most 128 significant digits and exponent magnitude 128.

If it exits nonzero, stop. Offer a smaller machine, shorter runtime, or a different explicit budget. Never raise the budget silently. The projection applies one AWS or GCP 60-second minimum, even when the requested runtime is shorter. A repeating decimal charge is rounded upward to 28 decimal places so admission never depends on downward rounding. It covers instance compute for the admitted lifecycle only. Every stop/start begins another provider minimum and requires a fresh plan before start. Repeated or out-of-band starts can exceed the recorded budget because the guest cannot prevent the provider from charging for each attempt. The projection does not cap the total cloud bill: boot disks, snapshots, network transfer, public IPs, premium images, taxes, discounts, credits, and other services remain outside it.

Use the returned `stop_at` to generate startup data in a temporary path:

```bash
python3 scripts/vm_bookkeeper.py render-cost-guard \
  --stop-at <absolute-utc-stop-at> \
  --output <temporary-cost-guard-script>
```

The generated script installs an enabled systemd timer with an absolute UTC deadline plus an enabled boot-time expiry check. The timer stops the initial run, and the boot check stops any later restart after the deadline. A post-deadline start can still incur a fresh provider minimum before the guest stops, so the boot check is runtime protection, not restart-cost prevention. The provider reference attaches it during creation. It contains no credentials. Preserve the file until provider creation and guard verification finish, then remove the temporary copy.

Also ask whether the user wants local Git identity, GitHub authentication, and commit-signing configuration ported after provisioning. Explain that these are independent choices. If yes, run the non-secret discovery before creating resources:

```bash
python3 scripts/vm_bookkeeper.py github-discover --verify-ssh
```

Summarize only identities, paths, fingerprints, tool/auth state, and the proposed strategy. Do not inspect or print token values, private key contents, `~/.git-credentials`, or cloud credentials. Read `references/github.md` before acting on the result.

### 4. Verify the selected provider

For AWS, read `references/aws.md`. Perform at least:

1. `aws --version` and `aws sts get-caller-identity`.
2. Resolve the active profile and region explicitly, then verify EC2 access and inspect the selected VPC/subnet.

For Google Cloud, read `references/gcp.md`. Perform at least:

1. `gcloud version` and `gcloud auth list --filter=status:ACTIVE`.
2. Resolve and confirm the project and zone explicitly, verify project access, and verify the Compute Engine API/network context.

Show the resolved account identity plus region/project without exposing secrets. If identity, billing context, permissions, or target location is ambiguous, ask the user to resolve it before provisioning.

### 5. Prepare SSH access

If the user supplies a key, validate that the private path and matching `.pub` file exist; never copy or print the private key. Otherwise run:

```bash
python3 scripts/vm_bookkeeper.py create-key --name <vm-name>
```

The helper uses Ed25519 and an empty passphrase. Import only the public key into AWS, or attach only the public key to GCP instance metadata as described by the provider reference. Use a unique provider-side key or firewall name derived from the VM name; detect collisions before creation.

### 6. Confirm and provision

Present one compact summary containing provider account/project, location, machine type, image, boot disk, network, SSH CIDR, public-key path, all resources to be created, current hourly compute rate and source, maximum runtime, projected compute amount for one uninterrupted lifecycle, USD compute budget, absolute auto-stop deadline, restart-cycle limitation, excluded cost classes, and the fact that charges may apply. Ask: “Create these cloud resources now?” Continue only after an explicit yes.

Follow the selected provider reference and attach the generated cost-guard script during creation. Capture provider-generated IDs from command output, wait for the instance to reach its running state, and verify that both the provider status and public IP are available. Do not claim success based only on a create command's exit code.

After SSH becomes available, verify the unattended control:

```bash
sudo systemctl is-enabled remote-computer-cost-guard.timer
sudo systemctl is-active remote-computer-cost-guard.timer
sudo systemctl is-enabled remote-computer-cost-guard.service
sudo systemctl list-timers remote-computer-cost-guard.timer --no-pager
```

Require the timer to be enabled and active, the boot service to be enabled, and the timer to show the expected UTC deadline before describing the guard as installed. If verification fails, report the VM as running without a verified guard and ask whether to repair it or stop the VM. Do not leave it described as cost-controlled.

### 7. Register the VM

The registry should already be initialized from step 1. After successful provisioning, upsert the VM using provider-native identity and the actual returned values:

```bash
python3 scripts/vm_bookkeeper.py upsert \
  --provider <aws|gcp> --id <instance-id-or-name> --name <name> \
  --location <aws-region-or-gcp-zone> --machine-type <type> \
  --public-ip <ip> --ssh-user <user> --private-key <path> \
  --public-key <path.pub> --cloud-key-name <provider-key-name> \
  --status running \
  --hourly-rate-usd <current-rate> --max-runtime-seconds <seconds-from-plan> \
  --max-compute-usd <budget> --stop-at <absolute-utc-stop-at> \
  --pricing-source '<official-source-label>' \
  --pricing-checked-at <pricing-checked-at-utc>
```

The registry schema and recovery rules are in `references/registry.md`. Record paths and metadata only. If provisioning partially fails, report created resource IDs and add a record only when an instance actually exists.

### 8. Configure Git and GitHub when requested

Read `references/github.md`. Present the exact non-secret plan and obtain explicit confirmation immediately before copying any private SSH/GPG material.

Prefer strategies in this order:

1. Reuse a shared GitHub key already recorded in the optional top-level `github_auth` registry field.
2. Register and port one existing local SSH key only when discovery verifies that its public fingerprint is registered to the intended active GitHub account and the user confirms the private-key path.
3. Otherwise create one shared local GitHub authentication key, upload only its public half with locally authenticated `gh`, then install that same key on this and future VMs.

Never generate the shared GitHub key on an individual VM. Keep it separate from each VM's access key under `~/.ssh/remote-computer/github/`. Do not use the shared GitHub key for cloud login. Do not use the cloud-login key for GitHub.

If commit signing is requested, preserve its original format and fingerprint. OpenPGP secret-key transfer and SSH signing are covered in `references/github.md`. Configuring GitHub authentication must not unset or replace `user.signingkey`, `commit.gpgsign`, or existing GPG material.

Verify GitHub SSH authentication and repository access before claiming success. GitHub's successful `ssh -T git@github.com` greeting normally exits with status 1 because GitHub provides no shell; evaluate the greeting, not the exit status alone.

### 9. Reconcile live status and cost controls before every handoff

If `~/.vms.json` existed before this session, run the following near the end of the run, including when provisioning was cancelled or failed:

```bash
python3 scripts/vm_bookkeeper.py sync
```

This checks every registered AWS/GCP VM for which the corresponding CLI is available and atomically updates status, IP, and `checked_at`. Report skipped or failed checks. If the file was created during this session, synchronization is optional, but ensure any newly created VM record already contains its verified live state.

Then audit every recorded compute policy without making another provider call:

```bash
python3 scripts/vm_bookkeeper.py cost-status
```

Report every uncontrolled, stopped, active, `expired-running`, and `expired-stopped` record. An `expired-running` result means the instance-side guard has not produced the expected provider state. Treat it as a live cost incident, verify the exact instance, and ask for authority before any provider-side stop or cleanup.

### 10. Prepare direct SSH access

Build a direct command from the registry values:

```bash
ssh -o IdentitiesOnly=yes -i <private-key-path> <ssh-user>@<public-ip>
```

Attempt clipboard copy with:

```bash
python3 scripts/vm_bookkeeper.py copy-ssh --command '<ssh command>'
```

The helper quickly discovers `pbcopy` on macOS or `wl-copy`, `xclip`, or `xsel` on Linux. If copying fails, print the command plainly. End by telling the user to open a new terminal, paste the command, and connect. Mention that first connection host-key verification is expected; do not disable it globally.

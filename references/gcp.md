# Google Cloud Compute Engine workflow

Use this reference only after the user selects Google Cloud. Commands are templates: resolve placeholders into explicit values, show the final resource summary, and obtain confirmation before enabling APIs or creating firewall rules/instances.

## Official sources

Fetched from official Google Cloud documentation on 2026-08-21:

- [Initialize the gcloud CLI](https://cloud.google.com/sdk/docs/initializing)
- [`gcloud auth list`](https://cloud.google.com/sdk/gcloud/reference/auth/list)
- [`gcloud compute instances create`](https://cloud.google.com/sdk/gcloud/reference/compute/instances/create)
- [Add SSH keys to VMs](https://cloud.google.com/compute/docs/connect/add-ssh-keys)
- [`gcloud compute instances describe`](https://cloud.google.com/sdk/gcloud/reference/compute/instances/describe)
- [Google Cloud Free Tier limits](https://cloud.google.com/free/docs/free-cloud-features#compute)
- [Linux VM startup scripts](https://cloud.google.com/compute/docs/instances/startup-scripts/linux)
- [Stop a Compute Engine instance from the guest OS](https://cloud.google.com/compute/docs/instances/stop-start-instance#stop_an_instance_from_inside)
- [Compute Engine VM pricing](https://cloud.google.com/compute/vm-instance-pricing)
- [Compute Engine instance lifecycle and billable states](https://docs.cloud.google.com/compute/docs/instances/instance-lifecycle)

Re-fetch the Free Tier page immediately before representing a deployment as allowance-eligible. Product docs establish limits and syntax; consult current pricing for costs outside those limits.

## Resolve the compute cost control

Immediately before confirmation, obtain the selected machine type's current Linux on-demand hourly compute rate for the exact region from an official Google Cloud pricing source. Do not use a rate copied from this repository or assume the Free Tier allowance remains unused. Record the source label and UTC check time, then run `vm_bookkeeper.py plan-cost` and `render-cost-guard` as required by `SKILL.md`.

Compute Engine vCPU and memory usage has a 60-second minimum for each start-to-stop billing lifecycle before per-second billing. The helper models one single uninterrupted provider billing lifecycle and applies one minimum even when the requested runtime is shorter. Starting a stopped VM begins another lifecycle and another minimum, so obtain a fresh admission before every start. Repeated or out-of-band starts can exceed the recorded budget because the guest guard cannot prevent Google Cloud from charging for a start attempt. The projection covers VM compute at the supplied rate for the admitted lifecycle. It does not cap the total cloud bill. Persistent disks and provisioned performance, snapshots, network transfer, external IPv4, premium images, taxes, discounts, credits, and other services remain outside it. Stopping the VM ends VM compute charges, but retained disks and other resources can continue to incur charges.

GCP continues CPU and memory charges while a VM is in `PENDING_STOP`, so `cost-status` treats that state as cost-incurring until the provider reaches `STOPPING` or `TERMINATED`. It also treats `SUSPENDING` and `SUSPENDED` conservatively because GCP continues memory charges in those states.

## Google Cloud Free Tier caveats

The fetched official page describes an aggregate monthly Compute Engine allowance that includes one non-preemptible `e2-micro` VM in `us-west1`, `us-central1`, or `us-east1`, 30 GB-months of standard persistent disk, and limited outbound transfer. It requires an eligible billing account, is measured across eligible regions, and does not make every attached service or byte free. Existing monthly usage, balanced/SSD disks, static IP behavior, snapshots, GPUs, extra disks, premium images, and traffic beyond the allowance can create charges.

For the Free Tier preset:

- use `e2-micro`;
- choose a zone inside `us-west1`, `us-central1`, or `us-east1`;
- use at most a 30 GB `pd-standard` boot disk;
- use a standard Ubuntu image with no premium license;
- do not use Spot/preemptible mode;
- explicitly warn that usage and billing eligibility cannot be guaranteed by machine type alone.

## Verify context

```bash
gcloud version
gcloud auth list --filter=status:ACTIVE --format='value(account)'
gcloud config get-value project
gcloud projects describe <project-id> --format=json
gcloud config get-value compute/zone
gcloud services list --project <project-id> --enabled --filter='name:compute.googleapis.com' --format='value(name)'
gcloud compute networks describe <network> --project <project-id> --format=json
```

Pass `--project <project-id>` and `--zone <zone>` explicitly in later commands. Confirm the active account, project ID, and zone with the user. If Compute Engine is disabled, explain that enabling it mutates the project and include `gcloud services enable compute.googleapis.com --project <project-id>` in the final confirmation. Do not run `gcloud init`, change global config, or enable services without user approval.

Check project metadata for OS Login and existing SSH behavior:

```bash
gcloud compute project-info describe --project <project-id> --format=json
```

The isolated metadata-key path below sets `enable-oslogin=FALSE` and `block-project-ssh-keys=TRUE` on the new VM. If organization policy enforces OS Login, stop and use the organization's approved OS Login flow instead of trying to bypass policy.

## Prepare instance SSH metadata

Create a temporary public metadata file containing one line in the format documented by Google:

```text
ubuntu:ssh-ed25519 AAAA... remote-computer:<vm-name>
```

Read the actual `.pub` file to construct this line; do not invent or truncate the key. The file contains public material only. Remove the temporary file after the create command. Never place private key content in metadata.

Derive unique firewall/network tags such as `remote-computer-<vm-name>`. Detect collisions:

```bash
gcloud compute instances describe <vm-name> --project <project-id> --zone <zone> --format=json
gcloud compute firewall-rules describe <firewall-name> --project <project-id> --format=json
```

Create SSH ingress limited to the confirmed source CIDR and only the tagged VM:

```bash
gcloud compute firewall-rules create <firewall-name> \
  --project <project-id> --network <network> --direction=INGRESS \
  --action=ALLOW --rules=tcp:22 --source-ranges=<confirmed-ip/32> \
  --target-tags=<network-tag> --description='SSH for remote-computer <vm-name>'
```

Record the firewall-rule name immediately. If a later step fails, report it; do not delete it without approval.

## Create and verify

For paid presets use `--boot-disk-type=pd-balanced`; for the Free Tier preset use `pd-standard`. Create from the official Ubuntu image family and attach only the dedicated instance SSH key:

```bash
gcloud compute instances create <vm-name> \
  --project <project-id> --zone <zone> --machine-type <machine-type> \
  --network <network> --tags <network-tag> \
  --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
  --boot-disk-size 30GB --boot-disk-type <pd-balanced-or-pd-standard> \
  --boot-disk-auto-delete \
  --metadata enable-oslogin=FALSE,block-project-ssh-keys=TRUE \
  --metadata-from-file ssh-keys=<temporary-public-metadata-file>,startup-script=<cost-guard-script> \
  --format=json
```

Capture the returned instance identity. Then query the exact name and zone:

```bash
gcloud compute instances describe <vm-name> --project <project-id> --zone <zone> --format='json(id,name,status,machineType,networkInterfaces,disks)'
```

Require `RUNNING` and capture `networkInterfaces[0].accessConfigs[0].natIP`. If there is no external IP, do not synthesize a direct SSH command; explain that IAP, VPN, or an explicit external access configuration is required.

Official Ubuntu images include the Google guest environment, which runs the attached startup script as root. A guest `shutdown -h now` stops the VM rather than deleting it. After SSH is available, verify the timer, boot-time check, and deadline with the commands in `SKILL.md`. If startup-script execution is incomplete, inspect `sudo journalctl -u google-startup-scripts.service --no-pager` without exposing unrelated credential output. Do not claim cost-control success until the timer is enabled and active, the boot service is enabled, and the planned UTC deadline is scheduled.

For direct OpenSSH, use the dedicated private key and user `ubuntu`. `gcloud compute ssh` is an alternative, but it may manage keys itself and is not the direct command required by this workflow.

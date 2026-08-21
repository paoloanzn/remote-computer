# AWS EC2 workflow

Use this reference only after the user selects AWS. Commands are templates: resolve placeholders into explicit values, show the final resource summary, and obtain confirmation before any create/import/authorize/run command.

## Official sources

Fetched from official AWS documentation on 2026-08-21:

- [AWS CLI configuration and credential files](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html)
- [`sts get-caller-identity`](https://docs.aws.amazon.com/cli/latest/reference/sts/get-caller-identity.html)
- [`ec2 import-key-pair`](https://docs.aws.amazon.com/cli/latest/reference/ec2/import-key-pair.html)
- [`ec2 run-instances`](https://docs.aws.amazon.com/cli/latest/reference/ec2/run-instances.html)
- [`ec2 describe-instance-status`](https://docs.aws.amazon.com/cli/latest/reference/ec2/describe-instance-status.html)
- [Run commands with EC2 user data](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/user-data.html)
- [EC2 instance state changes and shutdown behavior](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-lifecycle.html)
- [EC2 On-Demand pricing](https://aws.amazon.com/ec2/pricing/on-demand/)
- [EC2 On-Demand instance billing model](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-on-demand-instances.html)

Re-fetch these pages if current syntax, regional availability, pricing, or quotas matter. CLI references establish command behavior, not price.

## Resolve the compute cost control

Immediately before confirmation, obtain the selected instance type's current Linux on-demand hourly compute rate for the exact region from an official AWS pricing source. Do not use a rate copied from this repository, another region, a Spot quote, or a Free Tier assumption. Record the source label and UTC check time, then run `vm_bookkeeper.py plan-cost` and `render-cost-guard` as required by `SKILL.md`.

Linux On-Demand usage is billed per second with a 60-second minimum for each billing lifecycle. The helper models one single uninterrupted provider billing lifecycle and applies one minimum even when the requested runtime is shorter. Starting a stopped instance begins another lifecycle and another minimum, so obtain a fresh admission before every start. Repeated or out-of-band starts can exceed the recorded budget because the guest guard cannot prevent AWS from charging for a start attempt. The projection covers EC2 instance compute at the supplied rate for the admitted lifecycle. It does not cap the total cloud bill. EBS volumes and provisioned performance, snapshots, data transfer, Elastic IP or public IPv4 charges, premium software, taxes, discounts, credits, and other services remain outside it. Stopping the instance ends instance compute charges, but retained EBS and other resources can continue to incur charges.

## Verify context

```bash
aws --version
aws configure list
aws sts get-caller-identity --output json
aws ec2 describe-regions --region <region> --query 'Regions[?RegionName==`<region>`].OptInStatus' --output text
aws ec2 describe-vpcs --region <region> --vpc-ids <vpc-id> --output json
aws ec2 describe-subnets --region <region> --subnet-ids <subnet-id> --output json
```

Use `--profile <profile>` consistently when the user selected a non-default profile. Confirm the returned AWS account and ARN with the user. Confirm the subnet maps public IPs or explicitly request a public IP at launch. Verification errors do not authorize credential reconfiguration.

## Resolve an Ubuntu image

Use Canonical owner ID `099720109477`, require HVM/EBS/amd64, and select the newest matching Ubuntu 24.04 LTS image in the chosen region:

```bash
aws ec2 describe-images \
  --region <region> --owners 099720109477 \
  --filters 'Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*' 'Name=state,Values=available' \
  --query 'sort_by(Images,&CreationDate)[-1].[ImageId,Name,CreationDate]' --output json
```

Show the resolved AMI ID and name. Do not use an AMI copied from another region.

## Create narrowly scoped access

Derive unique names such as `remote-computer-<vm-name>`. Detect collisions first:

```bash
aws ec2 describe-key-pairs --region <region> --key-names <key-name> --output json
aws ec2 describe-security-groups --region <region> --filters Name=group-name,Values=<security-group-name> Name=vpc-id,Values=<vpc-id> --output json
```

A “not found” response is expected for a new key. Do not overwrite or reuse an unrelated cloud key.

Import only the public key:

```bash
aws ec2 import-key-pair --region <region> --key-name <key-name> --public-key-material fileb://<absolute-public-key-path> --output json
```

Create a security group in the selected VPC and restrict SSH to the confirmed client CIDR:

```bash
aws ec2 create-security-group --region <region> --vpc-id <vpc-id> --group-name <security-group-name> --description 'SSH for remote-computer <vm-name>' --output json
aws ec2 authorize-security-group-ingress --region <region> --group-id <security-group-id> --protocol tcp --port 22 --cidr <confirmed-ip/32>
```

Record the key name and security-group ID immediately. If a later step fails, report these residual resources; do not delete them without approval.

## Launch and verify

Use a client token to make retries idempotent. Require IMDSv2, an encrypted gp3 boot volume, an explicit subnet/security group, and tags:

```bash
aws ec2 run-instances \
  --region <region> --client-token <stable-unique-token> \
  --image-id <ami-id> --instance-type <machine-type> \
  --key-name <key-name> --subnet-id <subnet-id> \
  --security-group-ids <security-group-id> --associate-public-ip-address \
  <for-t-family-only: --credit-specification CpuCredits=standard> \
  --instance-initiated-shutdown-behavior stop \
  --user-data file://<cost-guard-script> \
  --metadata-options HttpTokens=required,HttpEndpoint=enabled \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=30,VolumeType=gp3,Encrypted=true,DeleteOnTermination=true}' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=<vm-name>},{Key=ManagedBy,Value=remote-computer}]' \
  --count 1 --output json
```

Capture `Instances[0].InstanceId`; never infer it by listing the newest instance. Then:

```bash
aws ec2 wait instance-running --region <region> --instance-ids <instance-id>
aws ec2 describe-instances --region <region> --instance-ids <instance-id> --query 'Reservations[0].Instances[0].{State:State.Name,IP:PublicIpAddress,Type:InstanceType,Image:ImageId}' --output json
aws ec2 describe-instance-status --region <region> --include-all-instances --instance-ids <instance-id> --output json
```

Wait for a public IP and running state. System/instance status checks may remain initializing briefly; report that honestly. The direct login for the Ubuntu image is usually `ubuntu`.

For the `t3` presets, use `--credit-specification CpuCredits=standard` so surplus CPU credits cannot create compute charges above the supplied instance rate. Omit that argument for machine families that do not support CPU credit specification. The explicit `--instance-initiated-shutdown-behavior stop` is required so the guard's guest-OS shutdown stops the EBS-backed instance rather than terminating it. After SSH is available, verify the timer, boot-time check, and deadline with the commands in `SKILL.md`. If the user-data run is incomplete, inspect `sudo journalctl -u cloud-final.service --no-pager` without exposing unrelated credential output. Do not claim cost-control success until the timer is enabled and active, the boot service is enabled, and the planned UTC deadline is scheduled.

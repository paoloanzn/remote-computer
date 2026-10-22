# Apple container workflow

Use this reference only after the user selects a local Apple container. [`apple/container`](https://github.com/apple/container) runs each Linux container in its own lightweight VM on an Apple silicon Mac. This workflow turns one container into an SSH development machine that behaves like the cloud VMs: the same access keys, registry record, GitHub/GPG porting, status reconciliation, and handoff. Commands are templates. Resolve placeholders into explicit values, show the final summary, and get confirmation before building images, creating volumes, or running containers.

## Official sources

Fetched from the official repositories on 2026-10-22 and checked against the `1.6.0` release source:

- [README: requirements, install, `container system start`](https://github.com/apple/container/blob/main/README.md)
- [Command reference](https://github.com/apple/container/blob/main/docs/command-reference.md): `run`, `build`, `list --format json`, `inspect`, `exec`, `volume create -s`, `system status --format json`
- [Resource usage](https://github.com/apple/container/blob/main/docs/resource-usage.md): `--cpus`, `--memory`, overcommit, and sparse disks
- [Technical overview](https://github.com/apple/container/blob/main/docs/technical-overview.md): memory ballooning limits
- [Networking](https://github.com/apple/container/blob/main/docs/networking.md): every container's IP is reachable from the host
- [Mounts and volumes](https://github.com/apple/container/blob/main/docs/volumes.md): named volumes are sparse ext4 images, 512 GiB unless sized
- [Container machine](https://github.com/apple/container/blob/main/docs/container-machine.md): why this workflow does not use it
- [`container-builder-shim`](https://github.com/apple/container-builder-shim): BuildKit `v0.29` Dockerfile frontend, which supports the heredocs used by the bundled Dockerfiles

Re-fetch these pages when a newer `container` major version is installed. The project states that CLI compatibility is generally preserved only within a major release.

## How a container maps to the VM workflow

| Registry field | Apple container value |
| --- | --- |
| `provider` | `apple-container` |
| `id` / `name` | Container name, which `container` uses as the container ID. It needs 2-63 letters, digits, `.`, `_`, or `-`. |
| `location` | `local/<network>`, normally `local/default` |
| `machine_type` | `<config>:<cpus>cpu-<memory>gib-<disk>gib`, for example `polyglot:4cpu-6gib-40gib` |
| `public_ip` | The container's IPv4 address on the vmnet network (`192.168.64.0/24` by default). Only this Mac can reach it. |
| `ssh_user` | `ubuntu` |
| `key_pair` | The dedicated key from `create-key`; `cloud_name` is the authorized-key comment `remote-computer:<name>` |
| `status` | `running`, `stopped`, `stopping`, `unknown`, or `missing` |

Every bundled or imported configuration ends with the managed SSH layer from `assets/containers/remote-computer-ssh-layer.dockerfile`. The layer:

- installs OpenSSH, sudo, Python 3, Git, GnuPG, `gpg-agent`, `pinentry-curses`, and `tini`;
- creates `ubuntu` with passwordless sudo and no usable password;
- allows public-key logins only, never root logins;
- reads the authorized public key from `REMOTE_COMPUTER_AUTHORIZED_KEY`;
- keeps SSH host keys on the `/home` volume, so a recreated container keeps its identity;
- exports the image's `ENV` (including `PATH`) into SSH sessions;
- runs executable hooks from `/etc/remote-computer/boot.d/` at boot, then `sshd` under `tini`.

This makes GitHub key porting and OpenPGP porting work unchanged, including the apt-based GPG setup.

Use regular containers, not `container machine`. A container machine mounts the whole macOS home directory read-write, forwards the host ssh-agent, and adds every Linux capability. That would bypass this skill's rule of copying only explicitly confirmed keys. For the same reason, never add `--ssh`, bind-mount `$HOME`, or `--publish` the SSH port. The container is reached only at its host-only vmnet address.

## 1. Verify the Mac and the CLI

Run the helper. It reads `hw.ncpu`, `hw.perflevel0.physicalcpu`, `hw.perflevel1.physicalcpu`, `hw.memsize`, and `machdep.cpu.brand_string` through `sysctl`, along with the macOS version and free disk space. It checks `container --version` and `container system status --format json`. It also installs or refreshes the default Dockerfiles under `~/Documents/Containers`:

```bash
python3 scripts/vm_bookkeeper.py container-plan
```

Requirements:

- Apple silicon;
- macOS 26 (`support.status` is `supported`); macOS 15 is `limited`, with an isolated default network and no custom networks;
- `container` 1.0.0 or newer (this workflow was validated against 1.6.0).

If the CLI is missing, point the user to the signed installer on the [release page](https://github.com/apple/container/releases). It needs an administrator password, so do not install it silently. If the service is not running, ask before starting it. The first start may ask to download the default Linux kernel:

```bash
container system start
```

Recommend that the user run it in their own terminal with `! container system start`, so they can answer the kernel prompt. Run `container system start --enable-kernel-install` yourself only after the user approves that download.

## 2. Choose a configuration and resources

Ask: "Do you have a Dockerfile you want this container to use?"

### The user has a Dockerfile

Save it as a custom configuration. The helper copies it to `~/Documents/Containers/<config>/Dockerfile`, appends the managed SSH layer (or refreshes it), records the build context, and analyzes the workload:

```bash
python3 scripts/vm_bookkeeper.py container-config-import --name <config> --dockerfile <path/to/Dockerfile>
python3 scripts/vm_bookkeeper.py container-plan --config <config>
```

The user's original file is never modified. If the Dockerfile uses `COPY` or `ADD` from its directory, that directory becomes the build context. Pass `--context <dir>` to override it. An existing, different custom configuration is never overwritten silently: confirm with the user, then pass `--replace`, or pick a new name.

Read the Dockerfile yourself as well as the helper's `analysis`. The tier rules are:

- **light**: a single interpreted language;
- **standard**: two or more languages, or one compiled toolchain (Rust, Java, .NET, C/C++, Haskell, or Swift);
- **heavy**: local services (databases, brokers), browser automation, ML stacks, or four or more languages.

Raise the tier if the project is clearly bigger than the Dockerfile suggests, for example a large monorepo build. Resolve these `warnings` with the user before you propose resources:

- a non-apt base (Alpine, Fedora, distroless): the SSH layer refuses to build; switch to a Debian or Ubuntu variant;
- `ENTRYPOINT`/`CMD` is replaced by the SSH entrypoint: move long-running services into an executable `/etc/remote-computer/boot.d/` hook, or start them over SSH;
- nested Docker or Podman: needs `--virtualization` plus a KVM-enabled custom kernel; it is not supported by default.

### No Dockerfile, or the user has nothing specific

`container-plan` without `--config` returns one sized plan for each maintained default. Offer all three:

| Config | Tier | Intended work | Contents |
| --- | --- | --- | --- |
| `essentials` | light | Scripting, editing, small single-language projects | Ubuntu 24.04, Git, Python 3, GCC/Make, common CLI tools (image about 1 GiB) |
| `polyglot` | standard | General multi-language development | Essentials plus Node.js LTS, Go, Rust, Java 21 + Maven, uv, CMake (about 3-4 GiB) |
| `fullstack` | heavy | Large native builds, multi-service stacks | Polyglot plus Clang/LLVM, Ninja, ccache, GDB, Valgrind, PostgreSQL, Redis (about 4-5 GiB) |

Node.js, Go, and Rust are downloaded over HTTPS when the image is built and checked against the SHA-256 sums their projects publish. `NODE_MAJOR` (default `24`) and `GO_VERSION` (default: latest stable) are build arguments. PostgreSQL and Redis are installed but not started: run `sudo service postgresql start` or `sudo service redis-server start`.

If the user wants changes to a default, never edit the default in place, because the helper maintains defaults. Copy it to a new custom name, edit the copy, then re-run the import on the edited file to refresh and validate the SSH layer:

```bash
python3 scripts/vm_bookkeeper.py container-config-import --name polyglot-data --dockerfile ~/Documents/Containers/polyglot/Dockerfile
# edit ~/Documents/Containers/polyglot-data/Dockerfile as requested
python3 scripts/vm_bookkeeper.py container-config-import --name polyglot-data --dockerfile ~/Documents/Containers/polyglot-data/Dockerfile
python3 scripts/vm_bookkeeper.py container-plan --config polyglot-data
```

`python3 scripts/vm_bookkeeper.py container-configs` lists every saved configuration, its tier, whether it was modified, and its SSH-layer state. If the user edited a default by hand, the helper reports `modified-preserved` and leaves the file alone. Offer to save those edits as a custom configuration.

### Propose resources and confirm them

Present the selected plan:

- **CPUs**: the recommendation and the `cpus_range` upper value. For example, an M4 MacBook Pro (10 cores, 16 GB) gets 4-6 CPUs for `polyglot`. `container` does not reject overcommitted CPU or memory; macOS schedules them like any other processes.
- **Memory**: `--memory` is a ceiling, not a reservation. The VM takes host memory only as the workload touches it, so 6 GiB on a 16 GB Mac is a safe default. Pages freed inside Linux are not returned to macOS until the container restarts, so suggest restarting after memory-heavy jobs.
- **Workspace disk**: a sparse named volume, capped at the chosen size and mounted at `/home`. It holds repositories, dotfiles, caches, and the SSH host keys, and survives container recreation. It uses host disk only as data is written. The root filesystem (image and system packages) is not capped per container; show `image_estimate_gib` and the free host disk.

Explain every plan warning. If the user adjusts the values, keep CPUs at or below the host's `cpus`, leave at least 4 GiB of host memory for macOS, and keep the volume well below free disk. Recompute `machine_type` from the final values.

## 3. Prepare SSH access

Use the same dedicated key as for cloud VMs:

```bash
python3 scripts/vm_bookkeeper.py create-key --name <container-name>
```

Detect collisions before confirming. `container-status` exits nonzero and reports `container not found` for an unused name:

```bash
python3 scripts/vm_bookkeeper.py container-status --id <container-name>
container volume list --format json
container image list --format json
```

Do not reuse or delete a container, volume, or image you did not create in this workflow.

## 4. Confirm

Present one summary:

- configuration name and Dockerfile path, build context, and image tag `local/remote-computer-<config>:latest`;
- container name, CPUs, memory ceiling, and workspace volume `remote-computer-<name>-home` with its size;
- network `default` (host-only, no published ports), SSH user `ubuntu`, and the public-key path;
- the resources to create (image, volume, container) and their effect on the Mac's disk and memory. There is no cloud billing.

Ask: "Create this local container now?" Continue only after an explicit yes.

## 5. Build, create, and verify

```bash
container build --tag local/remote-computer-<config>:latest --file ~/Documents/Containers/<config>/Dockerfile <context>
container volume create -s <disk>G --label managed-by=remote-computer remote-computer-<name>-home
container run --detach --name <name> --cpus <cpus> --memory <memory>G \
  --volume remote-computer-<name>-home:/home \
  --env "REMOTE_COMPUTER_AUTHORIZED_KEY=$(cat <absolute-public-key-path>)" \
  --label managed-by=remote-computer --label remote-computer.config=<config> \
  local/remote-computer-<config>:latest
python3 scripts/vm_bookkeeper.py container-status --id <name> --wait 90
python3 scripts/vm_bookkeeper.py container-trust-host-key --id <name>
```

Use the exact `build_command` from `container-plan`; it already has the right file and context. Build only when the plan's `action_required` list is empty. The builder VM defaults to 2 CPUs and 2 GiB. For very large custom builds, ask before replacing it: `container builder stop`, `container builder delete`, then `container builder start --cpus <n> --memory <m>g`.

`container-status --wait` exits 0 only when the container is `running` with an IP address. `container-trust-host-key` reads the host key through `container exec`, a local, authenticated channel, and records it in `~/.ssh/known_hosts` under the alias `remote-computer.apple-container.<name>`. First connections are then verified rather than trust-on-first-use. Vmnet reuses addresses, so the alias also keeps keys from colliding across containers. If the helper reports a different trusted key, stop and ask. Remove the old entry with `ssh-keygen -R remote-computer.apple-container.<name>` only after the user confirms the container was intentionally recreated without its volume.

If the container exits, inspect `container logs <name>` and `container logs --boot <name>`. A failed `sshd -t` or an invalid public key stops the entrypoint with a `remote-computer:` message.

## 6. Register, port, and hand off

Upsert with the actual values from `container-status`:

```bash
python3 scripts/vm_bookkeeper.py upsert \
  --provider apple-container --id <name> --name <name> \
  --location local/<network> --machine-type <config>:<cpus>cpu-<memory>gib-<disk>gib \
  --public-ip <container-ip> --ssh-user ubuntu --private-key <path> \
  --public-key <path.pub> --cloud-key-name remote-computer:<name> \
  --status running
```

Verify login with the command printed by `python3 scripts/vm_bookkeeper.py ssh-command --provider apple-container --id <name>`, adding `-o BatchMode=yes` and a trailing `true`.

Git identity, GitHub authentication, and commit signing use `references/github.md` without changes; pass `--provider apple-container --id <name>`. Because the container's home is a volume, ported keys and Git configuration survive restarts and recreation.

`sync` reconciles containers with one `container list --all --format json` call. A container absent from that successful listing becomes `missing`. CLI or service errors preserve the previous record. The IP can change after a restart, and `sync` updates it.

## Lifecycle

- **After a Mac restart or `container system stop`**: containers are stopped. Run `container system start`, then `container start <name>` and `sync`.
- **Resize CPUs or memory**: apple/container cannot change these on an existing container. With confirmation, run `container stop <name>`, `container delete <name>`, then repeat the `container run` command with the same name and volume and the new values. Home and SSH host keys persist. Upsert the new `machine_type` and IP.
- **Update the image** (new defaults or toolchains): rebuild, then recreate the container the same way.
- **Grow the workspace disk**: volumes cannot be resized. Create a larger volume, copy data with a temporary container that mounts both volumes, then recreate the container on the new volume. Delete the old volume only after the user confirms.
- **Cleanup**: only on explicit request and after confirming the resolved targets. Run `container stop <name>`, `container delete <name>`, `container volume delete remote-computer-<name>-home`, optionally `container image delete local/remote-computer-<config>:latest`, and `ssh-keygen -R remote-computer.apple-container.<name>`. Keep the registry record unless the user also asks to remove it.

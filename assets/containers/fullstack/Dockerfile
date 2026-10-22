# remote-computer default container configuration: fullstack
# Heavy workloads: large native builds and multi-service stacks. Everything in
# polyglot plus Clang/LLVM, Ninja, ccache, debuggers, PostgreSQL, and Redis.
# Services are installed but not started; run `sudo service postgresql start` or
# `sudo service redis-server start`, or add a hook to /etc/remote-computer/boot.d/.
FROM ubuntu:24.04

ENV LANG=C.UTF-8

RUN <<'PACKAGES'
set -eu
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  build-essential ca-certificates curl wget git git-lfs openssh-client openssh-server \
  sudo tini gnupg gpg-agent pinentry-curses less nano vim-tiny jq ripgrep fd-find tree \
  tmux htop unzip zip xz-utils file bash-completion iproute2 iputils-ping dnsutils \
  python3 python3-venv python3-pip python3-dev pipx pkg-config libssl-dev cmake \
  openjdk-21-jdk-headless maven sqlite3 clang lld lldb llvm ninja-build ccache gdb \
  valgrind strace postgresql postgresql-contrib redis-server
rm -rf /var/lib/apt/lists/*
ln -sf /usr/bin/fdfind /usr/local/bin/fd
PACKAGES

# Node.js (official tarball), Go (latest stable unless GO_VERSION is set), and Rust
# (rustup) are downloaded over HTTPS and checked against their published SHA-256 sums.
ARG NODE_MAJOR=24
ARG GO_VERSION=
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:/usr/local/go/bin:/usr/local/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN <<'TOOLCHAINS'
set -eu
arch="$(dpkg --print-architecture)"
case "$arch" in
  arm64) node_arch=arm64; rust_triple=aarch64-unknown-linux-gnu ;;
  amd64) node_arch=x64; rust_triple=x86_64-unknown-linux-gnu ;;
  *) echo "unsupported architecture: $arch" >&2; exit 1 ;;
esac
work="$(mktemp -d)"
cd "$work"

node_base="https://nodejs.org/dist/latest-v${NODE_MAJOR}.x"
curl -fsSLo SHASUMS256.txt "$node_base/SHASUMS256.txt"
node_file="$(awk -v suffix="-linux-${node_arch}.tar.xz" 'substr($2, length($2) - length(suffix) + 1) == suffix && $2 ~ /^node-v/ { print $2 }' SHASUMS256.txt)"
test -n "$node_file"
curl -fsSLo "$node_file" "$node_base/$node_file"
grep " $node_file\$" SHASUMS256.txt | sha256sum -c -
install -d /usr/local/node
tar -xJf "$node_file" -C /usr/local/node --strip-components=1
if command -v corepack >/dev/null 2>&1; then corepack enable; fi

curl -fsSLo go-releases.json 'https://go.dev/dl/?mode=json&include=all'
set -- $(python3 - "$arch" "${GO_VERSION:-}" go-releases.json <<'PY'
import json
import sys

arch, wanted, path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, encoding="utf-8") as handle:
    releases = json.load(handle)
if wanted:
    release = next(item for item in releases if item["version"] == f"go{wanted}")
else:
    release = next(item for item in releases if item["stable"])
archive = next(
    item for item in release["files"]
    if item["os"] == "linux" and item["arch"] == arch and item["kind"] == "archive"
)
print(archive["filename"], archive["sha256"])
PY
)
curl -fsSLo "$1" "https://go.dev/dl/$1"
echo "$2  $1" | sha256sum -c -
tar -C /usr/local -xzf "$1"

rustup_base="https://static.rust-lang.org/rustup/dist/${rust_triple}"
curl -fsSLo rustup-init "$rustup_base/rustup-init"
echo "$(curl -fsSL "$rustup_base/rustup-init.sha256" | awk '{print $1}')  rustup-init" | sha256sum -c -
chmod 0755 rustup-init
./rustup-init -y --no-modify-path --profile minimal --default-toolchain stable --component rustfmt,clippy
chown -R ubuntu:ubuntu "$RUSTUP_HOME" "$CARGO_HOME"

PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install uv
cd /
rm -rf "$work"
node --version
go version
rustc --version
uv --version
TOOLCHAINS

RUN <<'USER_PATHS'
set -eu
cat > /etc/profile.d/remote-computer-user-paths.sh <<'PROFILE'
# User-level tool directories for Go, npm, and pipx/uv installs.
export NPM_CONFIG_PREFIX="$HOME/.local"
for directory in "$HOME/go/bin" "$HOME/.local/bin"; do
  case ":$PATH:" in
    *":$directory:"*) ;;
    *) PATH="$directory:$PATH" ;;
  esac
done
export PATH
PROFILE
USER_PATHS

# >>> remote-computer managed SSH layer >>>
# Keep this block last. It provides key-only SSH for the remote-computer skill and
# requires a Debian/Ubuntu (apt-get) final stage. The skill refreshes it in place.
USER root
ARG REMOTE_COMPUTER_USER=ubuntu
ENV REMOTE_COMPUTER_USER=${REMOTE_COMPUTER_USER}
RUN <<'REMOTE_COMPUTER_SETUP'
set -eu
if ! command -v apt-get >/dev/null 2>&1; then
  echo 'remote-computer: the SSH layer requires a Debian/Ubuntu (apt-get) base image' >&2
  exit 1
fi
missing=''
for package in openssh-server openssh-client sudo python3 git gnupg gpg-agent pinentry-curses tini ca-certificates; do
  dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed' || missing="$missing $package"
done
if [ -n "$missing" ]; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $missing
  rm -rf /var/lib/apt/lists/*
fi
if ! getent passwd "$REMOTE_COMPUTER_USER" >/dev/null; then
  useradd --create-home --shell /bin/bash --user-group "$REMOTE_COMPUTER_USER"
fi
usermod --password '*' "$REMOTE_COMPUTER_USER"
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$REMOTE_COMPUTER_USER" > /etc/sudoers.d/90-remote-computer
chmod 0440 /etc/sudoers.d/90-remote-computer
visudo -cqf /etc/sudoers.d/90-remote-computer
install -d -m 0755 /etc/ssh/sshd_config.d /etc/remote-computer /etc/remote-computer/boot.d
cat > /etc/ssh/sshd_config.d/10-remote-computer.conf <<'SSHD'
# Managed by remote-computer.
HostKey /home/.remote-computer/ssh-host-keys/ssh_host_ed25519_key
HostKey /home/.remote-computer/ssh-host-keys/ssh_host_ecdsa_key
HostKey /home/.remote-computer/ssh-host-keys/ssh_host_rsa_key
PubkeyAuthentication yes
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
SSHD
if ! grep -q '^Include /etc/ssh/sshd_config.d/\*\.conf' /etc/ssh/sshd_config; then
  sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
fi
sed -i -E 's/^[[:space:]]*AcceptEnv[[:space:]]+LANG[[:space:]]+LC_\*/# &/' /etc/ssh/sshd_config
sed -i -E 's/^session[[:space:]]+required[[:space:]]+pam_loginuid\.so/session optional pam_loginuid.so/' /etc/pam.d/sshd
grep -q 'envfile=/etc/remote-computer/environment' /etc/pam.d/sshd \
  || printf '%s\n' 'session required pam_env.so readenv=1 envfile=/etc/remote-computer/environment' >> /etc/pam.d/sshd
grep -q '^LANG=' /etc/default/locale 2>/dev/null || printf '%s\n' 'LANG=C.UTF-8' >> /etc/default/locale
cat > /etc/profile.d/zz-remote-computer-environment.sh <<'PROFILE'
# Managed by remote-computer: restore the image environment for login shells.
if [ -r /etc/remote-computer/environment ]; then
  remote_computer_previous_path="${PATH:-}"
  set -a
  . /etc/remote-computer/environment
  set +a
  remote_computer_ifs="$IFS"
  IFS=:
  for remote_computer_directory in $remote_computer_previous_path; do
    case ":$PATH:" in
      *":$remote_computer_directory:"*) ;;
      *) PATH="$PATH:$remote_computer_directory" ;;
    esac
  done
  IFS="$remote_computer_ifs"
  unset remote_computer_previous_path remote_computer_ifs remote_computer_directory
  export PATH
fi
PROFILE
: > /etc/remote-computer/environment
REMOTE_COMPUTER_SETUP
COPY --chmod=0755 <<'REMOTE_COMPUTER_ENTRYPOINT' /usr/local/sbin/remote-computer-entrypoint
#!/bin/sh
# Managed by remote-computer: prepare key-only SSH access, then run sshd.
set -eu
user="${REMOTE_COMPUTER_USER:-ubuntu}"
state=/home/.remote-computer
host_keys="$state/ssh-host-keys"
entry="$(getent passwd "$user")" || { echo "remote-computer: missing user $user" >&2; exit 1; }
home="$(printf '%s\n' "$entry" | cut -d: -f6)"
group="$(id -gn "$user")"

# A fresh /home volume hides the image's home directory; seed it once from /etc/skel.
if [ ! -d "$home" ]; then
  install -d -m 0750 -o "$user" -g "$group" "$home"
  cp -a /etc/skel/. "$home/"
  chown -R "$user:$group" "$home"
fi
install -d -m 0700 -o "$user" -g "$group" "$home/.ssh"
keys="$home/.ssh/authorized_keys"
if [ -n "${REMOTE_COMPUTER_AUTHORIZED_KEY:-}" ]; then
  case "$REMOTE_COMPUTER_AUTHORIZED_KEY" in
    ssh-*|ecdsa-*|sk-*) ;;
    *) echo 'remote-computer: REMOTE_COMPUTER_AUTHORIZED_KEY is not an OpenSSH public key' >&2; exit 1 ;;
  esac
  touch "$keys"
  grep -qxF -- "$REMOTE_COMPUTER_AUTHORIZED_KEY" "$keys" \
    || printf '%s\n' "$REMOTE_COMPUTER_AUTHORIZED_KEY" >> "$keys"
fi
if [ -e "$keys" ]; then
  chown "$user:$group" "$keys"
  chmod 0600 "$keys"
fi

# Host keys live on the /home volume so a recreated container keeps its SSH identity.
install -d -m 0700 -o root -g root "$state" "$host_keys"
for type in ed25519 ecdsa rsa; do
  key="$host_keys/ssh_host_${type}_key"
  if [ ! -s "$key" ]; then
    rm -f "$key" "$key.pub"
    ssh-keygen -q -t "$type" -N '' -C remote-computer-host -f "$key"
  fi
done

# SSH sessions do not inherit the container environment; publish the image's ENV for PAM.
python3 - <<'PY'
import os
import re
import tempfile

skip = {"HOME", "HOSTNAME", "LOGNAME", "MAIL", "OLDPWD", "PWD", "SHELL", "SHLVL", "TERM", "USER", "_"}
lines = ["# Managed by remote-computer: image environment for SSH sessions."]
for name in sorted(os.environ):
    value = os.environ[name]
    if name in skip or name.startswith("REMOTE_COMPUTER_"):
        continue
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or "'" in value or "\n" in value:
        continue
    lines.append(f"{name}='{value}'")
fd, temporary = tempfile.mkstemp(dir="/etc/remote-computer")
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
os.chmod(temporary, 0o644)
os.replace(temporary, "/etc/remote-computer/environment")
PY

for hook in /etc/remote-computer/boot.d/*; do
  if [ -f "$hook" ] && [ -x "$hook" ]; then
    "$hook" || echo "remote-computer: boot hook failed: $hook" >&2
  fi
done

install -d -m 0755 /run/sshd
/usr/sbin/sshd -t
exec /usr/sbin/sshd -D -e
REMOTE_COMPUTER_ENTRYPOINT
EXPOSE 22
ENTRYPOINT ["/usr/bin/tini", "-s", "--", "/usr/local/sbin/remote-computer-entrypoint"]
CMD []
# <<< remote-computer managed SSH layer <<<

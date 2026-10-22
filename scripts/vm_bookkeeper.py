#!/usr/bin/env python3
"""Safe local bookkeeping helpers for the remote-computer skill."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast


class KeyPair(TypedDict):
    private_path: str
    public_path: str
    cloud_name: str


class VMRecord(TypedDict):
    provider: str
    id: str
    name: str
    location: str
    machine_type: str
    public_ip: str
    ssh_user: str
    key_pair: KeyPair
    status: str
    created_at: str
    checked_at: str


class GithubKeyPair(TypedDict):
    private_path: str
    public_path: str


class GithubInstall(TypedDict):
    provider: str
    id: str
    remote_private_path: str
    remote_public_path: str
    configured_at: str


class GithubAuthRecord(TypedDict):
    host: str
    account: str
    key_pair: GithubKeyPair
    source: str
    fingerprint: str
    title: str
    created_at: str
    installed_on: list[GithubInstall]


class RegistryRequired(TypedDict):
    version: int
    created_at: str
    updated_at: str
    vms: list[VMRecord]


class Registry(RegistryRequired, total=False):
    github_auth: list[GithubAuthRecord]


class ContainerHost(TypedDict):
    platform: str
    machine: str
    macos_version: str | None
    chip: str | None
    cpus: int
    performance_cores: int | None
    efficiency_cores: int | None
    memory_bytes: int
    disk_free_bytes: int
    disk_path: str


class TierProfile(TypedDict):
    memory_fraction: float
    memory_min_gib: int
    memory_max_gib: int
    disk_min_gib: int
    disk_gib: int
    disk_max_gib: int


class ResourcePlan(TypedDict):
    tier: str
    cpus: int
    cpus_range: list[int]
    memory_gib: int
    disk_gib: int
    disk_range_gib: list[int]
    image_estimate_gib: int
    warnings: list[str]


class DefaultContainerConfig(TypedDict):
    tier: str
    title: str
    workload: str
    toolchains: list[str]
    image_estimate_gib: int


class ContainerConfigEntry(TypedDict):
    kind: str
    tier: str
    source: str
    context: str
    sha256: str
    updated_at: str


class ContainerManifest(TypedDict):
    version: int
    configs: dict[str, ContainerConfigEntry]


class DockerfileAnalysis(TypedDict):
    final_base_image: str | None
    base_family: str
    languages: list[str]
    services: list[str]
    heavy_signals: list[str]
    tier: str
    reasons: list[str]
    warnings: list[str]
    uses_build_context: bool
    exposed_ports: list[str]
    ssh_layer: str


class ContainerState(TypedDict):
    id: str
    state: str
    ip: str | None
    network: str | None
    image: str | None
    cpus: int | None
    memory_bytes: int | None


SCHEMA_VERSION = 1
CONTAINER_PROVIDER = "apple-container"
VALID_PROVIDERS = {"aws", "gcp", CONTAINER_PROVIDER}
PROVIDER_CLIS = {"aws": "aws", "gcp": "gcloud", CONTAINER_PROVIDER: "container"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,62}$")
CONTAINER_MIN_VERSION = (1, 0, 0)
CONTAINER_VERSION_RE = re.compile(r"version (\d+)\.(\d+)\.(\d+)")
CONTAINER_MANIFEST_NAME = ".remote-computer.json"
CONTAINER_MANIFEST_VERSION = 1
CONTAINER_HOST_KEY = "/home/.remote-computer/ssh-host-keys/ssh_host_ed25519_key.pub"
SSH_LAYER_BEGIN = "# >>> remote-computer managed SSH layer >>>"
SSH_LAYER_END = "# <<< remote-computer managed SSH layer <<<"
GIB = 1024 ** 3
MEMORY_CEILING_NOTE = (
    "apple/container treats --memory as the VM's ceiling: the VM only takes host memory as the "
    "workload touches it. Pages freed inside Linux are not returned to macOS until the container "
    "restarts, so restart long-lived containers after memory-heavy jobs."
)
DISK_CEILING_NOTE = (
    "The workspace disk is a sparse, size-capped named volume mounted at /home; it consumes host "
    "disk only as data is written. The image and system packages live in the container root "
    "filesystem, which apple/container does not cap per container."
)
TIER_PROFILES: dict[str, TierProfile] = {
    "light": {
        "memory_fraction": 0.25, "memory_min_gib": 2, "memory_max_gib": 8,
        "disk_min_gib": 15, "disk_gib": 20, "disk_max_gib": 25,
    },
    "standard": {
        "memory_fraction": 0.375, "memory_min_gib": 3, "memory_max_gib": 16,
        "disk_min_gib": 30, "disk_gib": 40, "disk_max_gib": 40,
    },
    "heavy": {
        "memory_fraction": 0.5, "memory_min_gib": 4, "memory_max_gib": 32,
        "disk_min_gib": 60, "disk_gib": 80, "disk_max_gib": 100,
    },
}
DEFAULT_CONTAINER_CONFIGS: dict[str, DefaultContainerConfig] = {
    "essentials": {
        "tier": "light",
        "title": "Essentials",
        "workload": "Scripting, editing, and small single-language projects",
        "toolchains": ["Git", "Python 3", "GCC/Make", "common CLI tools"],
        "image_estimate_gib": 1,
    },
    "polyglot": {
        "tier": "standard",
        "title": "Polyglot",
        "workload": "General multi-language development and medium builds",
        "toolchains": ["Python 3 + uv", "Node.js LTS", "Go", "Rust", "Java 21 + Maven", "CMake"],
        "image_estimate_gib": 4,
    },
    "fullstack": {
        "tier": "heavy",
        "title": "Full stack",
        "workload": "Large native builds and multi-service stacks with local databases",
        "toolchains": [
            "everything in Polyglot", "Clang/LLVM", "Ninja", "ccache", "GDB/Valgrind",
            "PostgreSQL", "Redis",
        ],
        "image_estimate_gib": 5,
    },
}
CUSTOM_IMAGE_ESTIMATES_GIB = {"light": 1, "standard": 4, "heavy": 6}
LANGUAGE_PATTERNS: dict[str, re.Pattern[str]] = {
    "python": re.compile(r"\b(?:python[0-9.]*|pip3?|pipx|uv|poetry|conda|mamba)\b"),
    "node": re.compile(r"\b(?:node(?:js)?|npm|npx|yarn|pnpm|corepack|bun)\b"),
    "go": re.compile(r"\b(?:golang|go\.dev|/usr/local/go)\b|\bgo (?:build|install|mod)\b"),
    "rust": re.compile(r"\b(?:rust|rustup|rustc|cargo)\b"),
    "java": re.compile(r"\b(?:java|openjdk|jdk|jre|temurin|corretto|maven|mvn|gradle|kotlin|scala|sbt)\b"),
    "dotnet": re.compile(r"\b(?:dotnet|nuget)\b"),
    "ruby": re.compile(r"\b(?:ruby|bundler|rails)\b"),
    "php": re.compile(r"\b(?:php|composer)\b"),
    "c/c++": re.compile(r"\b(?:clang|cmake|ninja(?:-build)?|llvm|bazel)\b"),
    "elixir": re.compile(r"\b(?:elixir|erlang|rebar3)\b"),
    "haskell": re.compile(r"\b(?:ghc|ghcup|cabal|haskell)\b"),
    "swift": re.compile(r"\bswift\b"),
    "deno": re.compile(r"\bdeno\b"),
    "zig": re.compile(r"\bzig\b"),
}
COMPILED_LANGUAGES = {"rust", "java", "dotnet", "c/c++", "haskell", "swift"}
SERVICE_PATTERNS: dict[str, re.Pattern[str]] = {
    "postgresql": re.compile(r"\bpostgres(?:ql)?\b(?!-client)"),
    "mysql": re.compile(r"\b(?:mysql|mariadb)(?:-server)?\b(?!-client)"),
    "redis": re.compile(r"\bredis(?:-server)?\b(?!-tools)"),
    "mongodb": re.compile(r"\bmongo(?:db|d)?\b"),
    "search": re.compile(r"\b(?:elasticsearch|opensearch)\b"),
    "kafka": re.compile(r"\bkafka\b"),
    "rabbitmq": re.compile(r"\brabbitmq\b"),
    "nginx": re.compile(r"\bnginx\b"),
    "memcached": re.compile(r"\bmemcached\b"),
    "clickhouse": re.compile(r"\bclickhouse\b"),
}
HEAVY_PATTERNS: dict[str, re.Pattern[str]] = {
    "browser-automation": re.compile(r"\b(?:chromium|google-chrome|playwright|puppeteer|selenium|firefox)\b"),
    "machine-learning": re.compile(r"\b(?:cuda|torch|pytorch|tensorflow|jax|jupyter(?:lab)?|transformers)\b"),
    "android": re.compile(r"\bandroid\b"),
    "nested-containers": re.compile(r"\b(?:docker(?:-ce)?|podman|containerd)\b"),
}
APT_BASE_NAMES = {
    "ubuntu", "debian", "buildpack-deps", "python", "node", "golang", "rust", "openjdk",
    "eclipse-temurin", "maven", "gradle", "ruby", "php", "perl", "gcc", "haskell", "elixir",
    "erlang", "swift", "julia", "r-base", "sdk", "base", "cuda",
}
NON_APT_BASE_NAMES = {
    "fedora", "centos", "rockylinux", "almalinux", "amazonlinux", "oraclelinux", "ubi8", "ubi9",
    "ubi10", "opensuse", "leap", "tumbleweed", "archlinux", "photon", "amazoncorretto", "busybox",
    "distroless", "scratch", "wolfi-base", "chainguard",
}
NON_APT_TAG_MARKERS = ("alpine", "ubi", "rockylinux", "almalinux", "amazonlinux", "oraclelinux", "fedora", "distroless")
GITHUB_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
GITHUB_SOURCE_VALUES = {"discovered", "generated"}
GITHUB_SSH_GREETING_RE = re.compile(r"Hi ([A-Za-z0-9][A-Za-z0-9._-]{0,62})! You've successfully authenticated")
OPENPGP_FINGERPRINT_RE = re.compile(r"^(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})$")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def registry_path(value: str | None) -> Path:
    return Path(value).expanduser() if value else Path.home() / ".vms.json"


def empty_registry() -> Registry:
    timestamp = now()
    return {"version": SCHEMA_VERSION, "created_at": timestamp, "updated_at": timestamp, "vms": []}


def validate_registry(data: object) -> Registry:
    if not isinstance(data, dict):
        raise ValueError(f"registry must be an object with version {SCHEMA_VERSION}")
    raw = cast(dict[str, object], data)
    if raw.get("version") != SCHEMA_VERSION:
        raise ValueError(f"registry must be an object with version {SCHEMA_VERSION}")
    raw_vms = raw.get("vms")
    if not isinstance(raw_vms, list):
        raise ValueError("registry field 'vms' must be an array")
    for raw_item in cast(list[object], raw_vms):
        if not isinstance(raw_item, dict):
            raise ValueError("every VM record must be an object")
        item = cast(dict[str, object], raw_item)
        provider = item.get("provider")
        vm_id = item.get("id")
        if provider not in VALID_PROVIDERS or not isinstance(vm_id, str) or not vm_id:
            raise ValueError("every VM needs a valid provider and non-empty id")
    raw_github = raw.get("github_auth")
    if raw_github is not None:
        if not isinstance(raw_github, list):
            raise ValueError("registry field 'github_auth' must be an array when present")
        identities: set[tuple[str, str]] = set()
        for raw_item in cast(list[object], raw_github):
            if not isinstance(raw_item, dict):
                raise ValueError("every GitHub auth record must be an object")
            item = cast(dict[str, object], raw_item)
            host = item.get("host")
            account = item.get("account")
            source = item.get("source")
            fingerprint = item.get("fingerprint")
            pair = item.get("key_pair")
            installs = item.get("installed_on")
            if not isinstance(host, str) or not GITHUB_HOST_RE.fullmatch(host):
                raise ValueError("every GitHub auth record needs a valid host")
            if not isinstance(account, str) or not NAME_RE.fullmatch(account):
                raise ValueError("every GitHub auth record needs a valid account")
            if (host, account) in identities:
                raise ValueError("GitHub auth host/account pairs must be unique")
            identities.add((host, account))
            if source not in GITHUB_SOURCE_VALUES:
                raise ValueError("GitHub auth source must be discovered or generated")
            if not isinstance(fingerprint, str) or not fingerprint.startswith("SHA256:"):
                raise ValueError("every GitHub auth record needs an SHA256 fingerprint")
            if not isinstance(pair, dict):
                raise ValueError("every GitHub auth record needs a key_pair object")
            pair_dict = cast(dict[str, object], pair)
            if not all(isinstance(pair_dict.get(field), str) and pair_dict.get(field) for field in ("private_path", "public_path")):
                raise ValueError("GitHub key_pair paths must be non-empty strings")
            if not isinstance(installs, list):
                raise ValueError("every GitHub auth record needs an installed_on array")
    return cast(Registry, raw)


def load_registry(path: Path, create: bool = False) -> Registry:
    if not path.exists():
        if create:
            data = empty_registry()
            save_registry(path, data)
            return data
        raise FileNotFoundError(f"registry does not exist: {path}")
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
        return validate_registry(parsed)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in registry {path}: {exc}") from exc


def save_registry(path: Path, data: Registry) -> None:
    validate_registry(data)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data["updated_at"] = now()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def command_path(name: str) -> str | None:
    return shutil.which(name)


def run_text(command: list[str], timeout: int = 30, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def checked_text(command: list[str], timeout: int = 30, input_text: str | None = None) -> str:
    result = run_text(command, timeout=timeout, input_text=input_text)
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(message)
    return result.stdout.strip()


def ssh_fingerprint(path: Path) -> str:
    output = checked_text(["ssh-keygen", "-lf", str(path)])
    parts = output.split()
    if len(parts) < 2 or not parts[1].startswith("SHA256:"):
        raise RuntimeError(f"could not parse SSH fingerprint for {path}")
    return parts[1]


def ssh_text_fingerprint(public_key: str) -> str:
    output = checked_text(["ssh-keygen", "-lf", "-"], input_text=f"{public_key.strip()}\n")
    parts = output.split()
    if len(parts) < 2 or not parts[1].startswith("SHA256:"):
        raise RuntimeError("could not parse an SSH public-key fingerprint")
    return parts[1]


def validate_github_identity(host: str, account: str) -> None:
    if not GITHUB_HOST_RE.fullmatch(host):
        raise ValueError("GitHub host must be a valid DNS name")
    if not NAME_RE.fullmatch(account):
        raise ValueError("GitHub account must use 1-63 letters, digits, dots, underscores, or hyphens")


def github_records(data: Registry, create: bool = False) -> list[GithubAuthRecord]:
    records = data.get("github_auth")
    if records is None:
        if not create:
            return []
        records = []
        data["github_auth"] = records
    return records


def github_record(data: Registry, host: str, account: str) -> tuple[int, GithubAuthRecord] | None:
    for index, record in enumerate(github_records(data)):
        if record["host"] == host and record["account"] == account:
            return index, record
    return None


def git_config_value(name: str) -> str | None:
    if not command_path("git"):
        return None
    result = run_text(["git", "config", "--global", "--get", name], timeout=10)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def gh_status(host: str) -> dict[str, object]:
    executable = command_path("gh")
    result: dict[str, object] = {"installed": bool(executable), "path": executable, "accounts": []}
    if not executable:
        return result
    status = run_text([executable, "auth", "status", "--hostname", host, "--json", "hosts"], timeout=20)
    try:
        payload: object = json.loads(status.stdout) if status.stdout.strip() else {}
    except json.JSONDecodeError:
        result["error"] = status.stderr.strip() or "gh returned invalid status JSON"
        return result
    accounts: list[dict[str, object]] = []
    if isinstance(payload, dict):
        raw_hosts = cast(dict[str, object], payload).get("hosts")
        if isinstance(raw_hosts, dict):
            raw_accounts = cast(dict[str, object], raw_hosts).get(host, [])
            if isinstance(raw_accounts, list):
                for raw_account in cast(list[object], raw_accounts):
                    if not isinstance(raw_account, dict):
                        continue
                    account = cast(dict[str, object], raw_account)
                    accounts.append({
                        "login": account.get("login"),
                        "active": bool(account.get("active")),
                        "state": account.get("state"),
                        "git_protocol": account.get("gitProtocol"),
                    })
    result["accounts"] = accounts
    if status.returncode and not accounts:
        result["error"] = status.stderr.strip() or "gh authentication is unavailable"
    return result


def object_dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        cast(dict[str, object], item)
        for item in cast(list[object], value)
        if isinstance(item, dict)
    ]


def github_registered_fingerprints(host: str, status: dict[str, object]) -> tuple[set[str], str]:
    executable = status.get("path")
    accounts = object_dicts(status.get("accounts", []))
    if not isinstance(executable, str) or not executable:
        return set(), "gh-unavailable"
    if not any(item.get("state") == "success" for item in accounts):
        return set(), "gh-not-authenticated"
    response = run_text([
        executable, "api", "--hostname", host, "user/keys",
        "--paginate", "--jq", ".[].key",
    ], timeout=30)
    if response.returncode:
        return set(), "github-key-query-failed"
    fingerprints: set[str] = set()
    for key in response.stdout.splitlines():
        if not key.strip():
            continue
        try:
            fingerprints.add(ssh_text_fingerprint(key))
        except RuntimeError:
            continue
    return fingerprints, "ok"


def github_ssh_login(output: str) -> str | None:
    match = GITHUB_SSH_GREETING_RE.search(output)
    return match.group(1) if match else None


def verify_github_ssh(private: Path, host: str) -> tuple[str | None, str | None]:
    result = run_text([
        "ssh", "-T",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=yes",
        "-i", str(private),
        f"git@{host}",
    ], timeout=15)
    combined = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    login = github_ssh_login(combined)
    if login:
        return login, None
    return None, combined or f"ssh exited with status {result.returncode}"


def clipboard_tool() -> tuple[str, list[str]] | None:
    system = platform.system()
    if system == "Darwin" and command_path("pbcopy"):
        return "pbcopy", ["pbcopy"]
    if system == "Linux":
        if os.environ.get("WAYLAND_DISPLAY") and command_path("wl-copy"):
            return "wl-copy", ["wl-copy"]
        if os.environ.get("DISPLAY") and command_path("xclip"):
            return "xclip", ["xclip", "-selection", "clipboard"]
        if os.environ.get("DISPLAY") and command_path("xsel"):
            return "xsel", ["xsel", "--clipboard", "--input"]
    return None


def run_json_value(command: list[str]) -> object:
    result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=45)
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(message)
    try:
        value: object = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("provider CLI returned invalid JSON") from exc
    return value


def run_json(command: list[str]) -> dict[str, object]:
    value = run_json_value(command)
    if not isinstance(value, dict):
        raise RuntimeError("provider CLI returned a non-object JSON value")
    return cast(dict[str, object], value)


def object_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise RuntimeError(f"provider CLI field {field!r} is not an array")
    return cast(list[object], value)


def object_dict(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"provider CLI field {field!r} is not an object")
    return cast(dict[str, object], value)


def optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def aws_state(record: VMRecord) -> tuple[str, str | None]:
    payload = run_json([
        "aws", "ec2", "describe-instances", "--instance-ids", record["id"],
        "--region", record["location"], "--output", "json",
    ])
    reservations = object_list(payload.get("Reservations", []), "Reservations")
    if not reservations:
        return "missing", None
    reservation = object_dict(reservations[0], "Reservations[0]")
    instances = object_list(reservation.get("Instances", []), "Instances")
    if not instances:
        return "missing", None
    instance = object_dict(instances[0], "Instances[0]")
    state = object_dict(instance.get("State", {}), "State")
    state_name = optional_string(state.get("Name")) or "unknown"
    return state_name, optional_string(instance.get("PublicIpAddress"))


def gcp_state(record: VMRecord) -> tuple[str, str | None]:
    payload = run_json([
        "gcloud", "compute", "instances", "describe", record["id"],
        "--zone", record["location"], "--format=json",
    ])
    status = (optional_string(payload.get("status")) or "unknown").lower()
    interfaces = object_list(payload.get("networkInterfaces", []), "networkInterfaces")
    if not interfaces:
        return status, None
    interface = object_dict(interfaces[0], "networkInterfaces[0]")
    access_configs = object_list(interface.get("accessConfigs", []), "accessConfigs")
    if not access_configs:
        return status, None
    access_config = object_dict(access_configs[0], "accessConfigs[0]")
    return status, optional_string(access_config.get("natIP"))


def optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_container_list(payload: object) -> dict[str, ContainerState]:
    """Parse `container list --all --format json` from apple/container 1.x (and 0.x snapshots)."""
    states: dict[str, ContainerState] = {}
    for index, raw_item in enumerate(object_list(payload, "containers")):
        item = object_dict(raw_item, f"containers[{index}]")
        configuration = object_dict(item.get("configuration", {}), "configuration")
        container_id = optional_string(item.get("id")) or optional_string(configuration.get("id"))
        if not container_id:
            raise RuntimeError("container CLI returned an entry without an id")
        raw_status = item.get("status")
        if isinstance(raw_status, dict):
            status = cast(dict[str, object], raw_status)
            state = optional_string(status.get("state"))
            raw_networks = status.get("networks", [])
        else:
            state = optional_string(raw_status)
            raw_networks = item.get("networks", [])
        ip: str | None = None
        network: str | None = None
        networks = object_list(raw_networks, "networks")
        if networks:
            attachment = object_dict(networks[0], "networks[0]")
            address = optional_string(attachment.get("ipv4Address")) or optional_string(attachment.get("address"))
            ip = address.split("/", 1)[0] if address else None
            network = optional_string(attachment.get("network"))
        image = configuration.get("image")
        resources = configuration.get("resources")
        image_reference = (
            optional_string(cast(dict[str, object], image).get("reference")) if isinstance(image, dict) else None
        )
        resource_values = cast(dict[str, object], resources) if isinstance(resources, dict) else {}
        states[container_id] = {
            "id": container_id,
            "state": (state or "unknown").lower(),
            "ip": ip,
            "network": network,
            "image": image_reference,
            "cpus": optional_int(resource_values.get("cpus")),
            "memory_bytes": optional_int(resource_values.get("memoryInBytes")),
        }
    return states


def container_states() -> dict[str, ContainerState]:
    return parse_container_list(run_json_value(["container", "list", "--all", "--format", "json"]))


def container_state(record: VMRecord, cache: dict[str, ContainerState] | None) -> tuple[str, str | None]:
    states = cache if cache is not None else container_states()
    found = states.get(record["id"])
    if found is None:
        return "missing", None
    return found["state"], found["ip"]


def host_key_alias(provider: str, vm_id: str) -> str | None:
    """Return a stable known_hosts alias for VMs whose address is reused across instances."""
    return f"remote-computer.{provider}.{vm_id}" if provider == CONTAINER_PROVIDER else None


def ssh_login_command(vm: VMRecord) -> list[str]:
    command = ["ssh", "-o", "IdentitiesOnly=yes"]
    alias = host_key_alias(vm["provider"], vm["id"])
    if alias:
        command.extend(["-o", f"HostKeyAlias={alias}"])
    command.extend(["-i", vm["key_pair"]["private_path"], f"{vm['ssh_user']}@{vm['public_ip']}"])
    return command


def validate_container_name(name: str) -> None:
    if not CONTAINER_NAME_RE.fullmatch(name):
        raise ValueError("container names must use 2-63 letters, digits, dots, underscores, or hyphens")


def clamp(value: int, low: int, high: int) -> int:
    low = min(low, high)
    return max(low, min(value, high))


def sysctl_value(name: str) -> str | None:
    if not command_path("sysctl"):
        return None
    result = run_text(["sysctl", "-n", name], timeout=10)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def sysctl_int(name: str) -> int | None:
    value = sysctl_value(name)
    return int(value) if value and value.isdigit() else None


def physical_memory_bytes() -> int:
    if platform.system() == "Darwin":
        return sysctl_int("hw.memsize") or 0
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return 0


def detect_container_host() -> ContainerHost:
    darwin = platform.system() == "Darwin"
    data_root = Path.home() / "Library" / "Application Support" / "com.apple.container"
    disk_path = data_root if data_root.is_dir() else Path.home()
    return {
        "platform": platform.system().lower(),
        "machine": platform.machine().lower(),
        "macos_version": platform.mac_ver()[0] or None,
        "chip": sysctl_value("machdep.cpu.brand_string") if darwin else None,
        "cpus": (sysctl_int("hw.ncpu") if darwin else None) or os.cpu_count() or 1,
        "performance_cores": sysctl_int("hw.perflevel0.physicalcpu") if darwin else None,
        "efficiency_cores": sysctl_int("hw.perflevel1.physicalcpu") if darwin else None,
        "memory_bytes": physical_memory_bytes(),
        "disk_free_bytes": shutil.disk_usage(disk_path).free,
        "disk_path": str(disk_path),
    }


def container_host_support(host: ContainerHost) -> tuple[str, list[str]]:
    if host["platform"] != "darwin" or host["machine"] != "arm64":
        return "unsupported", ["apple/container requires a Mac with Apple silicon."]
    version = host["macos_version"] or ""
    major = int(version.split(".", 1)[0]) if version.split(".", 1)[0].isdigit() else 0
    if major >= 26:
        return "supported", []
    if major == 15:
        return "limited", [
            "macOS 15 runs apple/container with limitations (isolated default network, no custom "
            "networks); upstream supports macOS 26 and does not fix issues it cannot reproduce there.",
        ]
    return "unsupported", ["apple/container requires macOS 26 (macOS 15 with limitations)."]


def container_resource_plan(host: ContainerHost, tier: str, image_estimate_gib: int) -> ResourcePlan:
    """Suggest CPU, memory ceiling, and workspace-volume size for one workload tier."""
    profile = TIER_PROFILES[tier]
    total_cpus = max(1, host["cpus"])
    usable_cpus = max(1, total_cpus - 2 if total_cpus > 4 else total_cpus - 1)
    half_cpus = max(1, total_cpus // 2)
    standard_cpus = clamp(min(host["performance_cores"] or half_cpus, half_cpus), 2, min(usable_cpus, 12))
    if tier == "light":
        cpus = clamp(2, 1, usable_cpus)
        cpus_high = clamp(min(4, half_cpus), cpus, usable_cpus)
    elif tier == "standard":
        cpus = standard_cpus
        cpus_high = clamp(cpus + 2, cpus, usable_cpus)
    else:
        cpus = clamp(total_cpus * 3 // 4, standard_cpus, usable_cpus)
        cpus_high = max(cpus, usable_cpus)

    warnings: list[str] = []
    total_memory = host["memory_bytes"] // GIB
    reserve = max(4, -(-total_memory // 4))
    budget = max(1, total_memory - reserve)
    memory = clamp(
        round(total_memory * profile["memory_fraction"]),
        min(profile["memory_min_gib"], budget),
        min(profile["memory_max_gib"], budget),
    )
    if total_memory and budget < profile["memory_min_gib"]:
        warnings.append(
            f"{total_memory} GiB of host memory leaves little headroom for macOS; expect swapping under load."
        )

    free_disk = host["disk_free_bytes"] // GIB
    capacity = max(0, (free_disk - image_estimate_gib) // 2)
    disk = min(profile["disk_gib"], capacity)
    disk_low = min(profile["disk_min_gib"], capacity)
    disk_high = min(profile["disk_max_gib"], capacity)
    if capacity < profile["disk_min_gib"]:
        warnings.append(
            f"Only {free_disk} GiB of host disk is free; the workspace volume is below the "
            f"{profile['disk_min_gib']} GiB recommended for this workload."
        )
    if capacity < 5:
        warnings.append("Free more host disk space before creating this container.")
    return {
        "tier": tier,
        "cpus": cpus,
        "cpus_range": [cpus, cpus_high],
        "memory_gib": memory,
        "disk_gib": disk,
        "disk_range_gib": [disk_low, disk_high],
        "image_estimate_gib": image_estimate_gib,
        "warnings": warnings,
    }


def container_machine_type(config: str, cpus: int, memory_gib: int, disk_gib: int) -> str:
    return f"{config}:{cpus}cpu-{memory_gib}gib-{disk_gib}gib"


def container_cli_status() -> dict[str, object]:
    executable = command_path("container")
    result: dict[str, object] = {
        "installed": bool(executable),
        "path": executable,
        "version": None,
        "version_supported": None,
        "service": None,
    }
    if not executable:
        return result
    version = run_text([executable, "--version"], timeout=10)
    match = CONTAINER_VERSION_RE.search(version.stdout)
    if match:
        parsed = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        result["version"] = ".".join(str(part) for part in parsed)
        result["version_supported"] = parsed >= CONTAINER_MIN_VERSION
    status = run_text([executable, "system", "status", "--format", "json"], timeout=30)
    try:
        payload: object = json.loads(status.stdout) if status.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    service = optional_string(cast(dict[str, object], payload).get("status")) if isinstance(payload, dict) else None
    result["service"] = service or ("running" if status.returncode == 0 else "unknown")
    return result


def bundled_container_assets() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "containers"


def ssh_layer_text() -> str:
    path = bundled_container_assets() / "remote-computer-ssh-layer.dockerfile"
    if not path.is_file():
        raise FileNotFoundError(f"bundled SSH layer does not exist: {path}")
    return path.read_text(encoding="utf-8")


def ssh_layer_bounds(text: str) -> tuple[int, int] | None:
    """Return the [start, end) character span of the managed SSH layer, validating placement."""
    begin_count = text.count(SSH_LAYER_BEGIN)
    end_count = text.count(SSH_LAYER_END)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise ValueError("the Dockerfile must contain exactly one remote-computer managed SSH layer")
    start = text.index(SSH_LAYER_BEGIN)
    end = text.index(SSH_LAYER_END) + len(SSH_LAYER_END)
    if end < start:
        raise ValueError("the remote-computer managed SSH layer markers are out of order")
    if end < len(text) and text[end] == "\n":
        end += 1
    for line in text[end:].splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            raise ValueError("the remote-computer managed SSH layer must be the last block in the Dockerfile")
    return start, end


def ssh_layer_state(text: str) -> str:
    try:
        bounds = ssh_layer_bounds(text)
    except ValueError:
        return "invalid"
    if bounds is None:
        return "missing"
    return "current" if text[bounds[0]:bounds[1]] == ssh_layer_text() else "outdated"


def apply_ssh_layer(text: str) -> str:
    layer = ssh_layer_text()
    bounds = ssh_layer_bounds(text)
    if bounds is None:
        return f"{text.rstrip()}\n\n{layer}"
    return f"{text[:bounds[0]]}{layer}{text[bounds[1]:]}"


def dockerfile_instructions(text: str) -> list[tuple[str, str]]:
    """Return (INSTRUCTION, arguments) pairs, folding continuations and heredoc bodies."""
    instructions: list[tuple[str, str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("#"):
            continue
        logical = line
        while logical.endswith("\\") and index < len(lines):
            logical = f"{logical[:-1].rstrip()} {lines[index].strip()}"
            index += 1
        keyword, _, arguments = logical.partition(" ")
        bodies: list[str] = []
        for strip_tabs, delimiter in re.findall(r"<<(-?)\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?", arguments):
            while index < len(lines):
                body_line = lines[index]
                index += 1
                if (body_line.lstrip("\t") if strip_tabs else body_line) == delimiter:
                    break
                bodies.append(body_line)
        instructions.append((keyword.upper(), " ".join([arguments, *bodies]).strip()))
    return instructions


def image_base_family(image: str | None, package_managers: set[str]) -> str:
    if image is None:
        return "unknown"
    reference = image.lower()
    name_and_tag = reference.rsplit("/", 1)[-1]
    name, _, tag = name_and_tag.partition(":")
    if "alpine" in reference:
        return "alpine"
    if name in NON_APT_BASE_NAMES or any(marker in tag for marker in NON_APT_TAG_MARKERS):
        return "non-apt"
    if name in APT_BASE_NAMES:
        return "debian"
    if "apt" in package_managers:
        return "debian"
    if "apk" in package_managers:
        return "alpine"
    if package_managers & {"dnf", "yum", "microdnf", "zypper", "pacman"}:
        return "non-apt"
    return "unknown"


def analyze_dockerfile(text: str) -> DockerfileAnalysis:
    """Heuristically size a Dockerfile; the agent reviews the result before proposing it."""
    layer_state = ssh_layer_state(text)
    user_text = text
    bounds = ssh_layer_bounds(text) if layer_state != "invalid" else None
    if bounds is not None:
        user_text = text[:bounds[0]] + text[bounds[1]:]
    instructions = dockerfile_instructions(user_text)
    searchable = "\n".join(f"{keyword.lower()} {arguments.lower()}" for keyword, arguments in instructions)

    stages: dict[str, str] = {}
    final_base: str | None = None
    for keyword, arguments in instructions:
        if keyword != "FROM":
            continue
        words = [word for word in arguments.split() if not word.startswith("--")]
        if not words:
            continue
        base = stages.get(words[0].lower(), words[0])
        if len(words) >= 3 and words[1].lower() == "as":
            stages[words[2].lower()] = base
        final_base = base

    package_managers = {
        manager for manager, pattern in (
            ("apt", r"\bapt(?:-get)?\s+install\b"),
            ("apk", r"\bapk\s+add\b"),
            ("dnf", r"\bdnf\s+install\b"),
            ("yum", r"\byum\s+install\b"),
            ("microdnf", r"\bmicrodnf\s+install\b"),
            ("zypper", r"\bzypper\s+(?:install|in)\b"),
            ("pacman", r"\bpacman\s+-S"),
        ) if re.search(pattern, searchable)
    }
    family = image_base_family(final_base, package_managers)
    languages = sorted(name for name, pattern in LANGUAGE_PATTERNS.items() if pattern.search(searchable))
    services = sorted(name for name, pattern in SERVICE_PATTERNS.items() if pattern.search(searchable))
    heavy = sorted(name for name, pattern in HEAVY_PATTERNS.items() if pattern.search(searchable))

    reasons: list[str] = []
    if languages:
        reasons.append(f"languages/toolchains: {', '.join(languages)}")
    if services:
        reasons.append(f"local services: {', '.join(services)}")
    if heavy:
        reasons.append(f"heavy workloads: {', '.join(heavy)}")
    if services or heavy or len(languages) >= 4:
        tier = "heavy"
    elif len(languages) >= 2 or COMPILED_LANGUAGES.intersection(languages):
        tier = "standard"
    else:
        tier = "light"
    reasons.append(f"inferred tier: {tier}")

    warnings: list[str] = []
    if family in {"alpine", "non-apt"}:
        warnings.append(
            "The final stage is not Debian/Ubuntu-based; the managed SSH layer and GPG porting require apt-get."
        )
    elif family == "unknown":
        warnings.append("Could not identify the base distribution; the SSH layer build fails clearly without apt-get.")
    replaced = sorted({keyword for keyword, _ in instructions if keyword in {"ENTRYPOINT", "CMD", "HEALTHCHECK"}})
    if replaced:
        warnings.append(
            f"{'/'.join(replaced)} will be replaced by the SSH entrypoint; start long-running services "
            "from an executable hook in /etc/remote-computer/boot.d/."
        )
    if any(keyword == "VOLUME" for keyword, _ in instructions):
        warnings.append("Image VOLUME declarations are not used; persistent data belongs under /home.")
    if "nested-containers" in heavy:
        warnings.append(
            "Docker/Podman inside the container needs --virtualization and a KVM-enabled kernel; "
            "the default apple/container kernel does not support it."
        )
    uses_context = any(
        keyword in {"COPY", "ADD"}
        and "--from=" not in arguments
        and "<<" not in arguments
        and not re.match(r"^(?:--\S+\s+)*https?://", arguments)
        for keyword, arguments in instructions
    )
    ports = sorted({
        port for keyword, arguments in instructions if keyword == "EXPOSE" for port in arguments.split()
    })
    return {
        "final_base_image": final_base,
        "base_family": family,
        "languages": languages,
        "services": services,
        "heavy_signals": heavy,
        "tier": tier,
        "reasons": reasons,
        "warnings": warnings,
        "uses_build_context": uses_context,
        "exposed_ports": ports,
        "ssh_layer": layer_state,
    }


def containers_dir(value: str | None) -> Path:
    return Path(value).expanduser() if value else Path.home() / "Documents" / "Containers"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_container_manifest(directory: Path) -> ContainerManifest:
    path = directory / CONTAINER_MANIFEST_NAME
    if not path.exists():
        return {"version": CONTAINER_MANIFEST_VERSION, "configs": {}}
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in container manifest {path}: {exc}") from exc
    if not isinstance(raw, dict) or cast(dict[str, object], raw).get("version") != CONTAINER_MANIFEST_VERSION:
        raise ValueError(f"container manifest must be an object with version {CONTAINER_MANIFEST_VERSION}")
    configs = cast(dict[str, object], raw).get("configs")
    if not isinstance(configs, dict):
        raise ValueError("container manifest field 'configs' must be an object")
    fields = ("kind", "tier", "source", "context", "sha256", "updated_at")
    for name, entry in cast(dict[str, object], configs).items():
        if not NAME_RE.fullmatch(name) or not isinstance(entry, dict):
            raise ValueError(f"invalid container manifest entry: {name}")
        if not all(isinstance(cast(dict[str, object], entry).get(field), str) for field in fields):
            raise ValueError(f"container manifest entry {name} is missing required string fields")
    return cast(ContainerManifest, raw)


def atomic_write_text(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_container_manifest(directory: Path, manifest: ContainerManifest) -> None:
    atomic_write_text(
        directory / CONTAINER_MANIFEST_NAME,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        0o644,
    )


def sync_default_configs(directory: Path, manifest: ContainerManifest) -> list[dict[str, object]]:
    """Install or refresh bundled defaults without overwriting a user's manual edits."""
    results: list[dict[str, object]] = []
    for name, metadata in DEFAULT_CONTAINER_CONFIGS.items():
        source = bundled_container_assets() / name / "Dockerfile"
        if not source.is_file():
            raise FileNotFoundError(f"bundled container configuration does not exist: {source}")
        bundled = source.read_text(encoding="utf-8")
        bundled_sha = sha256_text(bundled)
        target = directory / name / "Dockerfile"
        previous = manifest["configs"].get(name)
        if not target.exists():
            action = "created"
        else:
            current_sha = sha256_text(target.read_text(encoding="utf-8"))
            if current_sha == bundled_sha:
                action = "unchanged"
            elif previous is not None and previous["kind"] == "default" and previous["sha256"] == current_sha:
                action = "updated"
            else:
                action = "modified-preserved"
        if action in {"created", "updated"}:
            atomic_write_text(target, bundled, 0o644)
        if action != "modified-preserved":
            manifest["configs"][name] = {
                "kind": "default",
                "tier": metadata["tier"],
                "source": "bundled",
                "context": str(target.parent),
                "sha256": bundled_sha,
                "updated_at": now() if action != "unchanged" or previous is None else previous["updated_at"],
            }
        results.append({"name": name, "action": action, "dockerfile": str(target)})
    return results


def prepare_containers_dir(value: str | None) -> tuple[Path, ContainerManifest, list[dict[str, object]]]:
    directory = containers_dir(value)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = load_container_manifest(directory)
    defaults = sync_default_configs(directory, manifest)
    save_container_manifest(directory, manifest)
    return directory, manifest, defaults


def container_image_tag(config: str) -> str:
    return f"local/remote-computer-{config.lower()}:latest"


def container_config_plan(
    directory: Path,
    manifest: ContainerManifest,
    name: str,
    host: ContainerHost,
) -> dict[str, object]:
    entry = manifest["configs"].get(name)
    dockerfile = directory / name / "Dockerfile"
    if entry is None or not dockerfile.is_file():
        raise FileNotFoundError(f"container configuration is not registered: {dockerfile}")
    text = dockerfile.read_text(encoding="utf-8")
    analysis = analyze_dockerfile(text)
    default = DEFAULT_CONTAINER_CONFIGS.get(name) if entry["kind"] == "default" else None
    tier = default["tier"] if default else analysis["tier"]
    estimate = default["image_estimate_gib"] if default else CUSTOM_IMAGE_ESTIMATES_GIB[tier]
    resources = container_resource_plan(host, tier, estimate)
    context = Path(entry["context"])
    plan: dict[str, object] = {
        "config": name,
        "kind": entry["kind"],
        "tier": tier,
        "dockerfile": str(dockerfile),
        "context": str(context),
        "image": container_image_tag(name),
        "ssh_layer": analysis["ssh_layer"],
        "resources": resources,
        "machine_type": container_machine_type(
            name, resources["cpus"], resources["memory_gib"], resources["disk_gib"]
        ),
        "build_command": shlex.join([
            "container", "build", "--tag", container_image_tag(name), "--file", str(dockerfile), str(context),
        ]),
        "action_required": [] if analysis["ssh_layer"] == "current" else [
            f"The managed SSH layer is {analysis['ssh_layer']}; refresh it with container-config-import "
            "before building (save edited defaults under a new name).",
        ],
    }
    if default:
        plan.update({
            "title": default["title"],
            "workload": default["workload"],
            "toolchains": default["toolchains"],
        })
    else:
        plan["analysis"] = analysis
    return plan


def cmd_doctor(args: argparse.Namespace) -> int:
    clip = clipboard_tool()
    result = {
        "platform": platform.system().lower(),
        "aws": {"installed": bool(command_path("aws")), "path": command_path("aws")},
        "gcp": {"installed": bool(command_path("gcloud")), "path": command_path("gcloud")},
        "apple_container": {"installed": bool(command_path("container")), "path": command_path("container")},
        "git": {"installed": bool(command_path("git")), "path": command_path("git")},
        "gh": {"installed": bool(command_path("gh")), "path": command_path("gh")},
        "gpg": {"installed": bool(command_path("gpg")), "path": command_path("gpg")},
        "scp": {"installed": bool(command_path("scp")), "path": command_path("scp")},
        "ssh_keygen": {"installed": bool(command_path("ssh-keygen")), "path": command_path("ssh-keygen")},
        "clipboard": {"available": bool(clip), "tool": clip[0] if clip else None},
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = registry_path(args.registry)
    created = not path.exists()
    data = load_registry(path, create=True)
    print(json.dumps({"created": created, "path": str(path), "vm_count": len(data["vms"])}, sort_keys=True))
    return 0


def cmd_create_key(args: argparse.Namespace) -> int:
    if not NAME_RE.fullmatch(args.name):
        raise ValueError("key name must use 1-63 letters, digits, dots, underscores, or hyphens")
    if not command_path("ssh-keygen"):
        raise RuntimeError("ssh-keygen is required")
    private = Path(args.path).expanduser() if args.path else Path.home() / ".ssh" / "remote-computer" / args.name
    public = Path(f"{private}.pub")
    if private.exists() or public.exists():
        raise FileExistsError(f"refusing to overwrite existing key: {private}")
    private.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(private.parent, 0o700)
    result = subprocess.run([
        "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"remote-computer:{args.name}", "-f", str(private)
    ], text=True, capture_output=True, check=False, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "ssh-keygen failed")
    os.chmod(private, 0o600)
    os.chmod(public, 0o644)
    print(json.dumps({"private_key": str(private), "public_key": str(public)}, sort_keys=True))
    return 0


def cmd_github_discover(args: argparse.Namespace) -> int:
    host = str(args.host)
    if not GITHUB_HOST_RE.fullmatch(host):
        raise ValueError("GitHub host must be a valid DNS name")
    status = gh_status(host)
    registered_fingerprints, registration_check = github_registered_fingerprints(host, status)

    configured_identities: set[Path] = set()
    if command_path("ssh"):
        ssh_config = run_text(["ssh", "-G", f"git@{host}"], timeout=10)
        if ssh_config.returncode == 0:
            for line in ssh_config.stdout.splitlines():
                field, _, value = line.partition(" ")
                if field.lower() == "identityfile" and value:
                    configured_identities.add(Path(value).expanduser())

    agent_fingerprints: set[str] = set()
    if command_path("ssh-add"):
        agent = run_text(["ssh-add", "-l"], timeout=10)
        if agent.returncode == 0:
            for line in agent.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].startswith("SHA256:"):
                    agent_fingerprints.add(parts[1])

    candidates: list[dict[str, object]] = []
    ssh_dir = Path.home() / ".ssh"
    if command_path("ssh-keygen") and ssh_dir.is_dir():
        for public in sorted(ssh_dir.rglob("*.pub")):
            private = Path(str(public)[:-4])
            if not private.is_file():
                continue
            try:
                fingerprint = ssh_fingerprint(public)
            except RuntimeError:
                continue
            candidate: dict[str, object] = {
                "private_path": str(private.resolve()),
                "public_path": str(public.resolve()),
                "fingerprint": fingerprint,
                "configured_for_host": private.expanduser() in configured_identities,
                "loaded_in_agent": fingerprint in agent_fingerprints,
                "registered_with_github": fingerprint in registered_fingerprints if registration_check == "ok" else None,
                "verified_account": None,
            }
            if args.verify_ssh:
                login, error = verify_github_ssh(private, host)
                candidate["verified_account"] = login
                if error and "Host key verification failed" in error:
                    candidate["verification_error"] = "host-key-not-known"
                elif error:
                    candidate["verification_error"] = "authentication-failed"
            candidates.append(candidate)

    signing_key = git_config_value("user.signingkey")
    gpg_info: dict[str, object] = {
        "program": git_config_value("gpg.program"),
        "format": git_config_value("gpg.format") or "openpgp",
        "signing_key": signing_key,
        "commit_gpgsign": git_config_value("commit.gpgsign"),
        "secret_key_present": False,
        "fingerprint": None,
        "uid": None,
    }
    if signing_key and command_path("gpg"):
        secret = run_text(["gpg", "--batch", "--with-colons", "--list-secret-keys", signing_key], timeout=20)
        if secret.returncode == 0:
            for line in secret.stdout.splitlines():
                fields = line.split(":")
                if fields[0] == "sec":
                    gpg_info["secret_key_present"] = True
                elif fields[0] == "fpr" and not gpg_info["fingerprint"]:
                    gpg_info["fingerprint"] = fields[9]
                elif fields[0] == "uid" and not gpg_info["uid"]:
                    gpg_info["uid"] = fields[9]
        else:
            gpg_info["error"] = secret.stderr.strip() or "GPG secret-key discovery failed"

    shared: list[dict[str, object]] = []
    path = registry_path(args.registry)
    if path.exists():
        data = load_registry(path)
        shared = [
            {
                "host": record["host"],
                "account": record["account"],
                "fingerprint": record["fingerprint"],
                "private_path": record["key_pair"]["private_path"],
                "public_path": record["key_pair"]["public_path"],
                "installed_vm_count": len(record["installed_on"]),
            }
            for record in github_records(data)
        ]

    registered_candidates = [
        candidate for candidate in candidates
        if candidate["registered_with_github"] is True or candidate["verified_account"]
    ]
    if shared:
        recommendation = "reuse-registered-shared-key"
    elif registered_candidates:
        recommendation = "confirm-and-register-existing-key"
    elif any(item.get("state") == "success" for item in object_dicts(status.get("accounts", []))):
        recommendation = "create-shared-key-and-upload-with-local-gh"
    else:
        recommendation = "authenticate-gh-then-create-shared-key"

    result = {
        "host": host,
        "git": {
            "user_name": git_config_value("user.name"),
            "user_email": git_config_value("user.email"),
            "credential_helper": git_config_value("credential.helper"),
        },
        "gpg": gpg_info,
        "gh": status,
        "ssh": {
            "registration_check": registration_check,
            "candidates": candidates,
        },
        "shared_registry_keys": shared,
        "recommendation": recommendation,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def store_github_record(
    data: Registry,
    host: str,
    account: str,
    private: Path,
    public: Path,
    source: str,
    fingerprint: str,
    title: str,
) -> str:
    existing = github_record(data, host, account)
    if existing:
        _, record = existing
        if record["fingerprint"] != fingerprint:
            raise ValueError(f"refusing to replace the registered GitHub key for {account}@{host}")
        record["key_pair"] = {
            "private_path": str(private.resolve()),
            "public_path": str(public.resolve()),
        }
        record["source"] = source
        record["title"] = title
        return "updated"
    github_records(data, create=True).append({
        "host": host,
        "account": account,
        "key_pair": {
            "private_path": str(private.resolve()),
            "public_path": str(public.resolve()),
        },
        "source": source,
        "fingerprint": fingerprint,
        "title": title,
        "created_at": now(),
        "installed_on": [],
    })
    return "created"


def cmd_github_register_key(args: argparse.Namespace) -> int:
    host = str(args.host)
    account = str(args.account)
    validate_github_identity(host, account)
    private = Path(args.private_key).expanduser()
    public = Path(args.public_key).expanduser()
    if not private.is_file() or not public.is_file():
        raise FileNotFoundError("GitHub private and public key paths must both exist")
    private_fingerprint = ssh_fingerprint(private)
    public_fingerprint = ssh_fingerprint(public)
    if private_fingerprint != public_fingerprint:
        raise ValueError("GitHub private and public key fingerprints do not match")
    path = registry_path(args.registry)
    data = load_registry(path, create=True)
    action = store_github_record(
        data, host, account, private, public, "discovered", public_fingerprint, str(args.title)
    )
    save_registry(path, data)
    print(json.dumps({
        "action": action,
        "account": account,
        "host": host,
        "fingerprint": public_fingerprint,
        "path": str(path),
    }, sort_keys=True))
    return 0


def cmd_github_create_key(args: argparse.Namespace) -> int:
    host = str(args.host)
    account = str(args.account)
    validate_github_identity(host, account)
    if not command_path("ssh-keygen"):
        raise RuntimeError("ssh-keygen is required")
    path = registry_path(args.registry)
    data = load_registry(path, create=True)
    if github_record(data, host, account):
        raise FileExistsError(f"a shared GitHub key is already registered for {account}@{host}")
    private = (
        Path(args.path).expanduser()
        if args.path
        else Path.home() / ".ssh" / "remote-computer" / "github" / f"{host}-{account}"
    )
    public = Path(f"{private}.pub")
    if private.exists() or public.exists():
        raise FileExistsError(f"refusing to overwrite existing key: {private}")
    private.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(private.parent, 0o700)
    result = run_text([
        "ssh-keygen", "-q", "-t", "ed25519", "-N", "",
        "-C", f"remote-computer-github:{account}@{host}", "-f", str(private),
    ])
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "ssh-keygen failed")
    os.chmod(private, 0o600)
    os.chmod(public, 0o644)
    fingerprint = ssh_fingerprint(public)
    store_github_record(data, host, account, private, public, "generated", fingerprint, str(args.title))
    save_registry(path, data)
    print(json.dumps({
        "action": "created",
        "account": account,
        "host": host,
        "fingerprint": fingerprint,
        "private_key": str(private),
        "public_key": str(public),
        "registry": str(path),
    }, sort_keys=True))
    return 0


def cmd_github_upload_key(args: argparse.Namespace) -> int:
    host = str(args.host)
    account = str(args.account)
    validate_github_identity(host, account)
    path = registry_path(args.registry)
    data = load_registry(path, create=False)
    found = github_record(data, host, account)
    if not found:
        raise ValueError(f"no shared GitHub key is registered for {account}@{host}")
    _, auth = found
    public = Path(auth["key_pair"]["public_path"]).expanduser()
    if not public.is_file():
        raise FileNotFoundError(f"registered GitHub public key does not exist: {public}")
    if ssh_fingerprint(public) != auth["fingerprint"]:
        raise ValueError("registered GitHub public key fingerprint no longer matches its file")

    status = gh_status(host)
    executable = status.get("path")
    accounts = object_dicts(status.get("accounts", []))
    matching_account = any(
        item.get("login") == account
        and item.get("active") is True
        and item.get("state") == "success"
        for item in accounts
    )
    if not isinstance(executable, str) or not executable:
        raise RuntimeError("gh is required to upload the GitHub public key")
    if not matching_account:
        raise RuntimeError(f"gh must be actively authenticated as {account} on {host}")

    fingerprints, check = github_registered_fingerprints(host, status)
    if check != "ok":
        raise RuntimeError(f"could not inspect GitHub SSH keys: {check}")
    action = "already-present"
    if auth["fingerprint"] not in fingerprints:
        checked_text([
            executable, "ssh-key", "add", str(public),
            "--type", "authentication", "--title", auth["title"],
        ], timeout=45)
        action = "uploaded"
        fingerprints, check = github_registered_fingerprints(host, status)
        if check != "ok" or auth["fingerprint"] not in fingerprints:
            raise RuntimeError("GitHub did not return the uploaded SSH key fingerprint")
    print(json.dumps({
        "account": account,
        "action": action,
        "fingerprint": auth["fingerprint"],
        "host": host,
        "verified": True,
    }, sort_keys=True))
    return 0


def checked_subprocess(command: list[str], timeout: int = 45, input_text: str | None = None) -> str:
    return checked_text(command, timeout=timeout, input_text=input_text)


def local_gpg_program() -> str:
    configured = git_config_value("gpg.program")
    executable = command_path(configured) if configured else command_path("gpg")
    if not executable:
        raise RuntimeError("the locally configured OpenPGP gpg program is required")
    return executable


def openpgp_secret_fingerprints(gpg: str, selector: str) -> list[str]:
    result = run_text([
        gpg, "--batch", "--with-colons", "--fingerprint", "--list-secret-keys", selector,
    ], timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "OpenPGP secret-key lookup failed")
    fingerprints: list[str] = []
    awaiting_primary_fingerprint = False
    for line in result.stdout.splitlines():
        fields = line.split(":")
        record = fields[0] if fields else ""
        if record == "sec":
            awaiting_primary_fingerprint = True
        elif record == "ssb":
            awaiting_primary_fingerprint = False
        elif record == "fpr" and awaiting_primary_fingerprint and len(fields) > 9:
            fingerprints.append(fields[9].upper())
            awaiting_primary_fingerprint = False
    return fingerprints


def resolve_openpgp_secret_key(selector: str | None) -> tuple[str, str]:
    gpg = local_gpg_program()
    requested = selector or git_config_value("user.signingkey")
    if not requested:
        raise ValueError("an OpenPGP fingerprint or configured user.signingkey is required")
    fingerprints = openpgp_secret_fingerprints(gpg, requested)
    if len(fingerprints) != 1:
        raise ValueError("the signing-key selector must resolve to exactly one local OpenPGP secret key")
    fingerprint = fingerprints[0]
    if not OPENPGP_FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError("the resolved OpenPGP fingerprint must contain 40 or 64 hexadecimal characters")
    return gpg, fingerprint


def normalized_commit_gpgsign(requested: str | None) -> str | None:
    value = requested if requested is not None else git_config_value("commit.gpgsign")
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"true", "yes", "on", "1"}:
        return "true"
    if lowered in {"false", "no", "off", "0"}:
        return "false"
    raise ValueError(f"commit.gpgsign is not a valid boolean: {value}")


def registered_vm_ssh(
    data: Registry,
    provider: str,
    vm_id: str,
) -> tuple[VMRecord, str, list[str]]:
    vm = next((
        record for record in data["vms"]
        if record["provider"] == provider and record["id"] == vm_id
    ), None)
    if vm is None:
        raise ValueError(f"VM not found in registry: {provider}/{vm_id}")
    access_path = Path(vm["key_pair"]["private_path"]).expanduser()
    if not access_path.is_file():
        raise FileNotFoundError(f"VM access key does not exist: {access_path}")
    if not vm["public_ip"]:
        raise ValueError(f"VM has no public IP: {provider}/{vm_id}")
    if not command_path("ssh"):
        raise RuntimeError("ssh is required")
    target = f"{vm['ssh_user']}@{vm['public_ip']}"
    common = [
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    alias = host_key_alias(provider, vm_id)
    if alias:
        common.extend(["-o", f"HostKeyAlias={alias}"])
    common.extend(["-i", str(access_path)])
    return vm, target, common


def remote_gpg_package_command() -> str:
    return " ".join([
        "set -eu;",
        "if ! command -v apt-get >/dev/null 2>&1; then",
        "echo 'automatic GPG setup currently requires an apt-based Ubuntu/Debian VM' >&2; exit 1; fi;",
        "run_root() { if test \"$(id -u)\" -eq 0; then \"$@\"; else sudo -n \"$@\"; fi; };",
        "if ! command -v python3 >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1 ||",
        "! command -v gpg >/dev/null 2>&1 || ! command -v gpgconf >/dev/null 2>&1 ||",
        "! command -v gpg-connect-agent >/dev/null 2>&1 ||",
        "! command -v pinentry-curses >/dev/null 2>&1; then",
        "run_root env DEBIAN_FRONTEND=noninteractive apt-get update;",
        "run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 git gnupg gpg-agent pinentry-curses; fi;",
        "command -v python3 >/dev/null; command -v git >/dev/null;",
        "command -v gpg >/dev/null; command -v gpgconf >/dev/null;",
        "command -v gpg-connect-agent >/dev/null; command -v pinentry-curses >/dev/null",
    ])


def stream_openpgp_secret_key(gpg: str, fingerprint: str, ssh_command: list[str]) -> None:
    environment = os.environ.copy()
    if sys.stdin.isatty():
        try:
            environment["GPG_TTY"] = os.ttyname(sys.stdin.fileno())
        except OSError:
            pass
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as export_errors:
        exporter = subprocess.Popen(
            [gpg, "--export-secret-keys", fingerprint],
            stdout=subprocess.PIPE,
            stderr=export_errors,
            env=environment,
        )
        if exporter.stdout is None:
            exporter.kill()
            raise RuntimeError("could not open the protected OpenPGP export stream")
        importer = subprocess.Popen(
            ssh_command,
            stdin=exporter.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        exporter.stdout.close()
        try:
            _, import_errors = importer.communicate(timeout=180)
            export_status = exporter.wait(timeout=30)
        except subprocess.TimeoutExpired:
            importer.kill()
            exporter.kill()
            importer.wait()
            exporter.wait()
            raise
        export_errors.seek(0)
        export_error = export_errors.read().strip()
    import_error = import_errors.decode(errors="replace").strip()
    if export_status:
        raise RuntimeError(export_error or "local OpenPGP secret-key export failed")
    if importer.returncode:
        raise RuntimeError(import_error or "remote OpenPGP secret-key import failed")


def signing_verification_command(target: str, common: list[str], fingerprint: str) -> list[str]:
    remote = " ".join([
        "set -eu;",
        'export GPG_TTY="$(tty)";',
        "gpg-connect-agent updatestartuptty /bye >/dev/null;",
        "printf '%s\\n' remote-computer-signing-test |",
        f"gpg --local-user {fingerprint} --armor --detach-sign --output - >/dev/null;",
        "echo 'OpenPGP signing verified; gpg-agent has accepted the key unlock.'",
    ])
    return ["ssh", *common, "-tt", target, remote]


def interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def cmd_github_install_gpg(args: argparse.Namespace) -> int:
    path = registry_path(args.registry)
    data = load_registry(path, create=False)
    provider = str(args.provider)
    vm_id = str(args.id)
    _, target, common = registered_vm_ssh(data, provider, vm_id)
    signing_format = (git_config_value("gpg.format") or "openpgp").lower()
    if signing_format != "openpgp":
        raise ValueError(
            f"github-install-gpg only ports OpenPGP keys; configured gpg.format is {signing_format}"
        )
    gpg, fingerprint = resolve_openpgp_secret_key(args.fingerprint)
    commit_gpgsign = normalized_commit_gpgsign(args.commit_gpgsign)
    setup_script = Path(__file__).with_name("remote_gpg_setup.py")
    if not setup_script.is_file():
        raise FileNotFoundError(f"remote GPG setup helper does not exist: {setup_script}")
    setup_source = setup_script.read_text(encoding="utf-8")

    checked_subprocess(
        ["ssh", *common, target, remote_gpg_package_command()],
        timeout=300,
    )
    checked_subprocess([
        "ssh", *common, target, "python3", "-", "--fingerprint", fingerprint, "--preflight",
    ], timeout=60, input_text=setup_source)
    stream_openpgp_secret_key(
        gpg,
        fingerprint,
        [
            "ssh", *common, target,
            "umask 077; mkdir -p ~/.gnupg; chmod 700 ~/.gnupg; gpg --batch --import",
        ],
    )

    setup_command = [
        "ssh", *common, target, "python3", "-", "--fingerprint", fingerprint,
    ]
    if commit_gpgsign is not None:
        setup_command.extend(["--commit-gpgsign", commit_gpgsign])
    setup_output = checked_subprocess(
        setup_command,
        timeout=60,
        input_text=setup_source,
    )
    verify = [
        sys.executable,
        str(Path(__file__).resolve()),
        "github-verify-gpg",
        "--provider", provider,
        "--id", vm_id,
        "--fingerprint", fingerprint,
    ]
    if args.registry:
        verify.extend(["--registry", str(args.registry)])
    print(json.dumps({
        "fingerprint": fingerprint,
        "id": vm_id,
        "provider": provider,
        "remote_setup": json.loads(setup_output),
        "secret_transfer": "streamed-over-ssh",
        "verification_command": shlex.join(verify),
    }, indent=2, sort_keys=True))
    return 0


def cmd_github_verify_gpg(args: argparse.Namespace) -> int:
    path = registry_path(args.registry)
    data = load_registry(path, create=False)
    provider = str(args.provider)
    vm_id = str(args.id)
    _, target, common = registered_vm_ssh(data, provider, vm_id)
    _, fingerprint = resolve_openpgp_secret_key(args.fingerprint)
    command = signing_verification_command(target, common, fingerprint)
    if not interactive_terminal():
        raise RuntimeError(
            "interactive GPG verification requires a real terminal; run: " + shlex.join(command)
        )
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise RuntimeError(f"interactive remote OpenPGP signing test failed with exit {result.returncode}")
    return 0


def cmd_github_install_key(args: argparse.Namespace) -> int:
    host = str(args.host)
    account = str(args.account)
    validate_github_identity(host, account)
    path = registry_path(args.registry)
    data = load_registry(path, create=False)
    found = github_record(data, host, account)
    if not found:
        raise ValueError(f"no shared GitHub key is registered for {account}@{host}")
    _, auth = found
    provider = str(args.provider)
    vm_id = str(args.id)
    private = Path(auth["key_pair"]["private_path"]).expanduser()
    public = Path(auth["key_pair"]["public_path"]).expanduser()
    if not private.is_file() or not public.is_file():
        raise FileNotFoundError("registered GitHub key paths must both exist")
    if ssh_fingerprint(private) != auth["fingerprint"] or ssh_fingerprint(public) != auth["fingerprint"]:
        raise ValueError("registered GitHub key fingerprint no longer matches its files")
    _, target, common = registered_vm_ssh(data, provider, vm_id)
    if not command_path("scp"):
        raise RuntimeError("scp is required")
    fingerprint_slug = re.sub(r"[^A-Za-z0-9]", "", auth["fingerprint"].removeprefix("SHA256:"))[:16]
    remote_name = f"{host}-{account}-{fingerprint_slug}"
    remote_private = f".ssh/remote-computer/github/{remote_name}"
    remote_public = f"{remote_private}.pub"
    checked_subprocess(["ssh", *common, target, "install -d -m 700 ~/.ssh/remote-computer/github"])
    prior_install = next((
        item for item in auth["installed_on"]
        if item["provider"] == provider and item["id"] == vm_id
    ), None)
    if prior_install is None:
        preflight = (
            f"if test -e ~/{remote_private} || test -e ~/{remote_public}; "
            "then echo 'refusing to overwrite an untracked remote GitHub key' >&2; exit 1; fi"
        )
        checked_subprocess(["ssh", *common, target, preflight])
        checked_subprocess(["scp", *common, str(private), f"{target}:{remote_private}"])
        checked_subprocess(["scp", *common, str(public), f"{target}:{remote_public}"])
    else:
        remote_private = prior_install["remote_private_path"].removeprefix("~/")
        remote_public = prior_install["remote_public_path"].removeprefix("~/")

    setup_script = Path(__file__).with_name("remote_github_setup.py")
    if not setup_script.is_file():
        raise FileNotFoundError(f"remote setup helper does not exist: {setup_script}")
    setup_output = checked_subprocess([
        "ssh", *common, target, "python3", "-",
        "--host", host,
        "--account", account,
        "--private-key", remote_private,
        "--public-key", remote_public,
        "--fingerprint", auth["fingerprint"],
    ], input_text=setup_script.read_text(encoding="utf-8"))

    configured_at = now()
    installation: GithubInstall = {
        "provider": provider,
        "id": vm_id,
        "remote_private_path": f"~/{remote_private}",
        "remote_public_path": f"~/{remote_public}",
        "configured_at": configured_at,
    }
    install_index = next((
        index for index, item in enumerate(auth["installed_on"])
        if item["provider"] == provider and item["id"] == vm_id
    ), None)
    if install_index is None:
        auth["installed_on"].append(installation)
    else:
        auth["installed_on"][install_index] = installation
    save_registry(path, data)
    print(json.dumps({
        "account": account,
        "host": host,
        "fingerprint": auth["fingerprint"],
        "provider": provider,
        "id": vm_id,
        "remote_setup": json.loads(setup_output),
        "registry": str(path),
    }, indent=2, sort_keys=True))
    return 0


def cmd_upsert(args: argparse.Namespace) -> int:
    path = registry_path(args.registry)
    data = load_registry(path, create=True)
    private = Path(args.private_key).expanduser()
    public = Path(args.public_key).expanduser()
    if not private.is_file() or not public.is_file():
        raise FileNotFoundError("private and public key paths must both exist")
    timestamp = now()
    provider = str(args.provider)
    vm_id = str(args.id)
    index = next((i for i, vm in enumerate(data["vms"]) if vm["provider"] == provider and vm["id"] == vm_id), None)
    created_at = timestamp if index is None else data["vms"][index]["created_at"]
    record: VMRecord = {
        "provider": provider,
        "id": vm_id,
        "name": str(args.name),
        "location": str(args.location),
        "machine_type": str(args.machine_type),
        "public_ip": str(args.public_ip),
        "ssh_user": str(args.ssh_user),
        "key_pair": {
            "private_path": str(private.resolve()),
            "public_path": str(public.resolve()),
            "cloud_name": str(args.cloud_key_name),
        },
        "status": str(args.status),
        "created_at": created_at,
        "checked_at": timestamp,
    }
    if index is None:
        data["vms"].append(record)
        action = "created"
    else:
        data["vms"][index] = record
        action = "updated"
    save_registry(path, data)
    print(json.dumps({"action": action, "provider": provider, "id": vm_id, "path": str(path)}, sort_keys=True))
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    path = registry_path(args.registry)
    data = load_registry(path, create=False)
    results: list[dict[str, object]] = []
    changed = False
    containers: dict[str, ContainerState] | None = None
    for record in data["vms"]:
        provider = record["provider"]
        executable = PROVIDER_CLIS[provider]
        if not command_path(executable):
            results.append({"provider": provider, "id": record["id"], "result": "skipped", "reason": f"{executable} not installed"})
            continue
        try:
            if provider == CONTAINER_PROVIDER:
                if containers is None:
                    containers = container_states()
                status, public_ip = container_state(record, containers)
            elif provider == "aws":
                status, public_ip = aws_state(record)
            else:
                status, public_ip = gcp_state(record)
            record["status"] = status
            if public_ip:
                record["public_ip"] = public_ip
            record["checked_at"] = now()
            results.append({"provider": provider, "id": record["id"], "result": "updated", "status": status})
            changed = True
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            results.append({"provider": provider, "id": record["id"], "result": "error", "reason": str(exc)})
    if changed:
        save_registry(path, data)
    print(json.dumps({"changed": changed, "path": str(path), "results": results}, indent=2, sort_keys=True))
    return 1 if any(item["result"] == "error" for item in results) else 0


def cmd_ssh_command(args: argparse.Namespace) -> int:
    data = load_registry(registry_path(args.registry), create=False)
    provider = str(args.provider)
    vm_id = str(args.id)
    vm = next((
        record for record in data["vms"]
        if record["provider"] == provider and record["id"] == vm_id
    ), None)
    if vm is None:
        raise ValueError(f"VM not found in registry: {provider}/{vm_id}")
    if not vm["public_ip"]:
        raise ValueError(f"VM has no reachable IP: {provider}/{vm_id}")
    print(json.dumps({
        "command": shlex.join(ssh_login_command(vm)),
        "host_key_alias": host_key_alias(provider, vm_id),
        "id": vm_id,
        "provider": provider,
    }, sort_keys=True))
    return 0


def cmd_container_plan(args: argparse.Namespace) -> int:
    directory, manifest, defaults = prepare_containers_dir(args.dir)
    host = detect_container_host()
    support, notes = container_host_support(host)
    names = [str(args.config)] if args.config else list(DEFAULT_CONTAINER_CONFIGS)
    print(json.dumps({
        "configs_dir": str(directory),
        "container_cli": container_cli_status(),
        "default_configs": defaults,
        "disk_note": DISK_CEILING_NOTE,
        "host": {
            **host,
            "memory_gib": host["memory_bytes"] // GIB,
            "disk_free_gib": host["disk_free_bytes"] // GIB,
        },
        "memory_note": MEMORY_CEILING_NOTE,
        "plans": [container_config_plan(directory, manifest, name, host) for name in names],
        "support": {"status": support, "notes": notes},
    }, indent=2, sort_keys=True))
    return 0


def cmd_container_configs(args: argparse.Namespace) -> int:
    directory, manifest, defaults = prepare_containers_dir(args.dir)
    configs: list[dict[str, object]] = []
    for dockerfile in sorted(directory.glob("*/Dockerfile")):
        name = dockerfile.parent.name
        text = dockerfile.read_text(encoding="utf-8")
        entry = manifest["configs"].get(name)
        configs.append({
            "name": name,
            "kind": entry["kind"] if entry else "untracked",
            "tier": entry["tier"] if entry else None,
            "dockerfile": str(dockerfile),
            "context": entry["context"] if entry else None,
            "modified": entry["sha256"] != sha256_text(text) if entry else None,
            "ssh_layer": ssh_layer_state(text),
        })
    print(json.dumps({
        "configs": configs,
        "configs_dir": str(directory),
        "default_configs": defaults,
    }, indent=2, sort_keys=True))
    return 0


def cmd_container_config_import(args: argparse.Namespace) -> int:
    name = str(args.name)
    if not NAME_RE.fullmatch(name) or name.startswith("."):
        raise ValueError("configuration names must use 1-63 letters, digits, dots, underscores, or hyphens")
    if name in DEFAULT_CONTAINER_CONFIGS:
        raise ValueError(f"{name} is a maintained default; save customizations under a new name")
    directory, manifest, _ = prepare_containers_dir(args.dir)
    source = Path(args.dockerfile).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Dockerfile does not exist: {source}")
    target = directory / name / "Dockerfile"
    original = source.read_text(encoding="utf-8")
    text = apply_ssh_layer(original)
    in_place = target.exists() and target.resolve() == source
    if in_place:
        action = "refreshed" if text != original else "unchanged"
    elif not target.exists():
        action = "created"
    elif target.read_text(encoding="utf-8") == text:
        action = "unchanged"
    elif args.replace:
        action = "replaced"
    else:
        raise FileExistsError(
            f"refusing to overwrite custom configuration {target}; confirm with the user and pass --replace"
        )
    analysis = analyze_dockerfile(text)
    previous = manifest["configs"].get(name)
    if args.context:
        context = Path(args.context).expanduser().resolve()
    elif previous is not None:
        context = Path(previous["context"])
    elif analysis["uses_build_context"] and not in_place:
        context = source.parent
    else:
        context = target.parent
    if context != target.parent and not context.is_dir():
        raise FileNotFoundError(f"build context directory does not exist: {context}")
    if action != "unchanged":
        atomic_write_text(target, text, 0o644)
    manifest["configs"][name] = {
        "kind": "custom",
        "tier": analysis["tier"],
        "source": str(previous["source"] if in_place and previous else source),
        "context": str(context),
        "sha256": sha256_text(text),
        "updated_at": now(),
    }
    save_container_manifest(directory, manifest)
    print(json.dumps({
        "action": action,
        "analysis": analysis,
        "context": str(context),
        "dockerfile": str(target),
        "name": name,
    }, indent=2, sort_keys=True))
    return 0


def cmd_container_status(args: argparse.Namespace) -> int:
    name = str(args.id)
    validate_container_name(name)
    if not command_path("container"):
        raise RuntimeError("the apple/container CLI is not installed")
    deadline = time.monotonic() + max(0, int(args.wait))
    while True:
        found = container_states().get(name)
        ready = found is not None and found["state"] == "running" and bool(found["ip"])
        if ready or time.monotonic() >= deadline:
            break
        time.sleep(2)
    if found is None:
        raise ValueError(f"container not found: {name}")
    address = ipaddress.ip_address(found["ip"]) if found["ip"] else None
    print(json.dumps({
        **found,
        "private_address": address.is_private if address else None,
        "ready": ready,
    }, indent=2, sort_keys=True))
    return 0 if ready else 1


def known_host_entries(known_hosts: Path, alias: str) -> list[tuple[str, str]]:
    if not known_hosts.exists():
        return []
    result = run_text(["ssh-keygen", "-F", alias, "-f", str(known_hosts)], timeout=10)
    entries: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and not line.startswith("#"):
            entries.append((fields[1], fields[2]))
    return entries


def cmd_container_trust_host_key(args: argparse.Namespace) -> int:
    name = str(args.id)
    validate_container_name(name)
    for executable in ("container", "ssh-keygen"):
        if not command_path(executable):
            raise RuntimeError(f"{executable} is required")
    public_key = checked_text(["container", "exec", name, "cat", CONTAINER_HOST_KEY], timeout=30)
    fields = public_key.split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise RuntimeError("the container did not return an Ed25519 SSH host key")
    alias = host_key_alias(CONTAINER_PROVIDER, name)
    if alias is None:
        raise RuntimeError("could not derive the container host-key alias")
    known_hosts = Path(args.known_hosts).expanduser() if args.known_hosts else Path.home() / ".ssh" / "known_hosts"
    entries = known_host_entries(known_hosts, alias)
    if (fields[0], fields[1]) in entries:
        action = "already-trusted"
    elif any(key_type == fields[0] for key_type, _ in entries):
        raise ValueError(
            f"{known_hosts} already trusts a different {fields[0]} key for {alias}; confirm the container "
            f"was recreated without its home volume, then remove it with: ssh-keygen -R {alias}"
        )
    else:
        known_hosts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        created = not known_hosts.exists()
        with known_hosts.open("a", encoding="utf-8") as handle:
            handle.write(f"{alias} {fields[0]} {fields[1]}\n")
        if created:
            os.chmod(known_hosts, 0o600)
        action = "trusted"
    print(json.dumps({
        "action": action,
        "alias": alias,
        "fingerprint": ssh_text_fingerprint(f"{fields[0]} {fields[1]}"),
        "known_hosts": str(known_hosts),
    }, sort_keys=True))
    return 0


def cmd_copy_ssh(args: argparse.Namespace) -> int:
    tool = clipboard_tool()
    if not tool:
        print(args.command)
        print("Clipboard unavailable; copy the SSH command printed above.", file=sys.stderr)
        return 2
    result = subprocess.run(tool[1], input=args.command, text=True, capture_output=True, check=False, timeout=5)
    if result.returncode:
        print(args.command)
        print(f"{tool[0]} failed; copy the SSH command printed above.", file=sys.stderr)
        return 2
    print(json.dumps({"copied": True, "tool": tool[0]}, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="subcommand", required=True)
    doctor = sub.add_parser("doctor", help="detect cloud, container, GitHub, SSH, GPG, and clipboard tooling")
    doctor.set_defaults(func=cmd_doctor)

    init = sub.add_parser("init", help="create or validate the VM registry")
    init.add_argument("--registry")
    init.set_defaults(func=cmd_init)

    key = sub.add_parser("create-key", help="create a dedicated Ed25519 key without a passphrase")
    key.add_argument("--name", required=True)
    key.add_argument("--path")
    key.set_defaults(func=cmd_create_key)

    github_discover = sub.add_parser(
        "github-discover",
        help="inspect local Git, GPG, gh, and SSH configuration without exposing credentials",
    )
    github_discover.add_argument("--registry")
    github_discover.add_argument("--host", default="github.com")
    github_discover.add_argument(
        "--verify-ssh",
        action="store_true",
        help="probe candidate keys with strict host verification and report the authenticated account",
    )
    github_discover.set_defaults(func=cmd_github_discover)

    github_register = sub.add_parser(
        "github-register-key",
        help="register an existing shared GitHub SSH key pair by path and fingerprint",
    )
    github_register.add_argument("--registry")
    github_register.add_argument("--host", default="github.com")
    github_register.add_argument("--account", required=True)
    github_register.add_argument("--private-key", required=True)
    github_register.add_argument("--public-key", required=True)
    github_register.add_argument("--title", required=True)
    github_register.set_defaults(func=cmd_github_register_key)

    github_create = sub.add_parser(
        "github-create-key",
        help="create and register a shared GitHub Ed25519 authentication key",
    )
    github_create.add_argument("--registry")
    github_create.add_argument("--host", default="github.com")
    github_create.add_argument("--account", required=True)
    github_create.add_argument("--title", required=True)
    github_create.add_argument("--path")
    github_create.set_defaults(func=cmd_github_create_key)

    github_upload = sub.add_parser(
        "github-upload-key",
        help="upload a registered public key with gh and verify its fingerprint",
    )
    github_upload.add_argument("--registry")
    github_upload.add_argument("--host", default="github.com")
    github_upload.add_argument("--account", required=True)
    github_upload.set_defaults(func=cmd_github_upload_key)

    github_install = sub.add_parser(
        "github-install-key",
        help="securely install a registered shared GitHub key on one registered VM",
    )
    github_install.add_argument("--registry")
    github_install.add_argument("--host", default="github.com")
    github_install.add_argument("--account", required=True)
    github_install.add_argument("--provider", choices=sorted(VALID_PROVIDERS), required=True)
    github_install.add_argument("--id", required=True)
    github_install.set_defaults(func=cmd_github_install_key)

    github_install_gpg = sub.add_parser(
        "github-install-gpg",
        help="stream an OpenPGP signing key to a VM and configure terminal pinentry",
    )
    github_install_gpg.add_argument("--registry")
    github_install_gpg.add_argument("--provider", choices=sorted(VALID_PROVIDERS), required=True)
    github_install_gpg.add_argument("--id", required=True)
    github_install_gpg.add_argument(
        "--fingerprint",
        help="local OpenPGP secret-key fingerprint; defaults to user.signingkey",
    )
    github_install_gpg.add_argument(
        "--commit-gpgsign",
        choices=("true", "false"),
        help="override the local commit.gpgsign value on the VM",
    )
    github_install_gpg.set_defaults(func=cmd_github_install_gpg)

    github_verify_gpg = sub.add_parser(
        "github-verify-gpg",
        help="unlock and test an installed OpenPGP signing key through an interactive remote PTY",
    )
    github_verify_gpg.add_argument("--registry")
    github_verify_gpg.add_argument("--provider", choices=sorted(VALID_PROVIDERS), required=True)
    github_verify_gpg.add_argument("--id", required=True)
    github_verify_gpg.add_argument(
        "--fingerprint",
        help="local OpenPGP secret-key fingerprint; defaults to user.signingkey",
    )
    github_verify_gpg.set_defaults(func=cmd_github_verify_gpg)

    upsert = sub.add_parser("upsert", help="create or replace one registry record")
    upsert.add_argument("--registry")
    upsert.add_argument("--provider", choices=sorted(VALID_PROVIDERS), required=True)
    for option in ("id", "name", "location", "machine-type", "public-ip", "ssh-user", "private-key", "public-key", "cloud-key-name", "status"):
        upsert.add_argument(f"--{option}", required=True)
    upsert.set_defaults(func=cmd_upsert)

    sync = sub.add_parser("sync", help="query providers and update all registered VM states")
    sync.add_argument("--registry")
    sync.set_defaults(func=cmd_sync)

    ssh_command = sub.add_parser("ssh-command", help="print the direct SSH command for one registered VM")
    ssh_command.add_argument("--registry")
    ssh_command.add_argument("--provider", choices=sorted(VALID_PROVIDERS), required=True)
    ssh_command.add_argument("--id", required=True)
    ssh_command.set_defaults(func=cmd_ssh_command)

    container_plan = sub.add_parser(
        "container-plan",
        help="detect Mac resources and suggest apple/container sizes for default or saved configurations",
    )
    container_plan.add_argument("--dir", help="configuration directory; defaults to ~/Documents/Containers")
    container_plan.add_argument("--config", help="plan one saved configuration instead of every default")
    container_plan.set_defaults(func=cmd_container_plan)

    container_configs = sub.add_parser(
        "container-configs",
        help="install or refresh default Dockerfiles and list saved container configurations",
    )
    container_configs.add_argument("--dir", help="configuration directory; defaults to ~/Documents/Containers")
    container_configs.set_defaults(func=cmd_container_configs)

    container_import = sub.add_parser(
        "container-config-import",
        help="save a custom Dockerfile as a configuration and add or refresh the managed SSH layer",
    )
    container_import.add_argument("--dir", help="configuration directory; defaults to ~/Documents/Containers")
    container_import.add_argument("--name", required=True)
    container_import.add_argument("--dockerfile", required=True)
    container_import.add_argument("--context", help="build context directory for COPY/ADD instructions")
    container_import.add_argument(
        "--replace",
        action="store_true",
        help="overwrite a different saved custom configuration after user confirmation",
    )
    container_import.set_defaults(func=cmd_container_config_import)

    container_status = sub.add_parser(
        "container-status",
        help="report one apple/container container's state, IP, image, and resources",
    )
    container_status.add_argument("--id", required=True)
    container_status.add_argument(
        "--wait",
        type=int,
        default=0,
        help="seconds to wait for the running state and an IP address",
    )
    container_status.set_defaults(func=cmd_container_status)

    container_trust = sub.add_parser(
        "container-trust-host-key",
        help="read a container's SSH host key through container exec and trust it under its alias",
    )
    container_trust.add_argument("--id", required=True)
    container_trust.add_argument("--known-hosts", help="known_hosts file; defaults to ~/.ssh/known_hosts")
    container_trust.set_defaults(func=cmd_container_trust_host_key)

    copy_ssh = sub.add_parser("copy-ssh", help="copy a direct SSH command or print it as fallback")
    copy_ssh.add_argument("--command", required=True)
    copy_ssh.set_defaults(func=cmd_copy_ssh)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.func(args))
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

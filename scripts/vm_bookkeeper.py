#!/usr/bin/env python3
"""Safe local bookkeeping helpers for the remote-computer skill."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path
from typing import TypedDict, cast


class KeyPair(TypedDict):
    private_path: str
    public_path: str
    cloud_name: str


class CostControl(TypedDict):
    hourly_rate_usd: str
    max_runtime_seconds: str
    max_compute_usd: str
    projected_compute_usd: str
    stop_at: str
    pricing_source: str
    pricing_checked_at: str
    guard: str


class VMRecordRequired(TypedDict):
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


class VMRecord(VMRecordRequired, total=False):
    cost_control: CostControl


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


SCHEMA_VERSION = 1
VALID_PROVIDERS = {"aws", "gcp"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
GITHUB_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
GITHUB_SOURCE_VALUES = {"discovered", "generated"}
GITHUB_SSH_GREETING_RE = re.compile(r"Hi ([A-Za-z0-9][A-Za-z0-9._-]{0,62})! You've successfully authenticated")
DECIMAL_INPUT_RE = re.compile(r"^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
COST_CONTROL_FIELDS = (
    "hourly_rate_usd",
    "max_runtime_seconds",
    "max_compute_usd",
    "stop_at",
    "pricing_source",
    "pricing_checked_at",
)
COST_GUARD = "instance-systemd-stop"
COST_INCURRING_STATES = {
    "pending",
    "pending_stop",
    "provisioning",
    "running",
    "staging",
    "suspended",
    "suspending",
}
MINIMUM_BILLABLE_SECONDS = 60
REPEATING_PROJECTION_PLACES = 28
MAX_DECIMAL_INPUT_CHARACTERS = 512
MAX_DECIMAL_DIGITS = 128
MAX_DECIMAL_EXPONENT = 128
MAX_PROJECTED_DECIMAL_DIGITS = (
    MAX_DECIMAL_DIGITS + MAX_DECIMAL_EXPONENT + REPEATING_PROJECTION_PLACES
)
MAX_PROJECTED_DECIMAL_EXPONENT = MAX_DECIMAL_EXPONENT + REPEATING_PROJECTION_PLACES
MAX_RUNTIME_SECONDS_DIGITS = 20
MAX_PRICING_AGE = timedelta(hours=24)
MAX_CLOCK_SKEW = timedelta(minutes=5)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now() -> str:
    return format_utc(utc_now())


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_utc(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def positive_decimal(
    value: object,
    field: str,
    max_digits: int = MAX_DECIMAL_DIGITS,
    max_exponent: int = MAX_DECIMAL_EXPONENT,
) -> Decimal:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a positive decimal string")
    text = value
    if len(text) > MAX_DECIMAL_INPUT_CHARACTERS:
        raise ValueError(
            f"{field} exceeds supported precision of {max_digits} digits"
        )
    if not DECIMAL_INPUT_RE.fullmatch(text):
        raise ValueError(f"{field} must be a positive decimal string")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a positive decimal string") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} must be a positive decimal string")
    decimal_tuple = parsed.as_tuple()
    significant_digits = list(decimal_tuple.digits)
    exponent = int(decimal_tuple.exponent)
    while len(significant_digits) > 1 and significant_digits[-1] == 0:
        significant_digits.pop()
        exponent += 1
    if len(significant_digits) > max_digits or abs(exponent) > max_exponent:
        raise ValueError(
            f"{field} exceeds supported precision of {max_digits} digits "
            f"and exponent magnitude {max_exponent}"
        )
    return parsed


def positive_integer(value: object, field: str) -> int:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not re.fullmatch(r"[0-9]+", value)
    ):
        raise ValueError(f"{field} must be a positive whole-second string")
    text = value
    if len(text) > MAX_RUNTIME_SECONDS_DIGITS:
        raise ValueError(f"{field} is outside the supported runtime range")
    parsed = int(text)
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive whole-second string")
    return parsed


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def exact_decimal_product(left: Decimal, right: Decimal) -> Decimal:
    required_precision = max(1, len(left.as_tuple().digits) + len(right.as_tuple().digits))
    with localcontext() as context:
        context.prec = required_precision
        return left * right


def finite_fraction_text(value: Fraction) -> str:
    if value < 0:
        raise ValueError("finite decimal value must not be negative")
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError("fraction does not have a finite decimal expansion")
    places = max(twos, fives)
    scale = 10**places
    scaled = value.numerator * scale // value.denominator
    if places == 0:
        return str(scaled)
    integer, fraction = divmod(scaled, scale)
    fractional_text = f"{fraction:0{places}d}".rstrip("0")
    return str(integer) if not fractional_text else f"{integer}.{fractional_text}"


def conservative_fraction_text(value: Fraction) -> str:
    try:
        return finite_fraction_text(value)
    except ValueError:
        scale = 10**REPEATING_PROJECTION_PLACES
        scaled, remainder = divmod(value.numerator * scale, value.denominator)
        if remainder:
            scaled += 1
        integer, fraction = divmod(scaled, scale)
        fractional_text = f"{fraction:0{REPEATING_PROJECTION_PLACES}d}".rstrip("0")
        return str(integer) if not fractional_text else f"{integer}.{fractional_text}"


def projected_compute(rate: Decimal, runtime_seconds: int) -> Decimal:
    billable_seconds = max(runtime_seconds, MINIMUM_BILLABLE_SECONDS)
    charge = Fraction(rate) * Fraction(billable_seconds, 3600)
    return Decimal(conservative_fraction_text(charge))


def runtime_seconds_from_hours(value: object, field: str = "max_runtime_hours") -> int:
    runtime = positive_decimal(value, field)
    seconds = exact_decimal_product(runtime, Decimal(3600))
    if seconds != seconds.to_integral_value():
        raise ValueError(
            f"{field} must resolve exactly to whole seconds; use max_runtime_seconds otherwise"
        )
    return int(seconds)


def validate_pricing_time(value: datetime, current: datetime | None = None) -> None:
    checked_at = value.astimezone(timezone.utc)
    reference = (current or utc_now()).astimezone(timezone.utc)
    if checked_at > reference + MAX_CLOCK_SKEW:
        raise ValueError("pricing check time is in the future")
    if reference - checked_at > MAX_PRICING_AGE:
        raise ValueError("pricing check is stale; refresh the official provider rate")


def cost_plan(
    hourly_rate_usd: str,
    max_runtime_seconds: str,
    max_compute_usd: str,
    start_at: str,
) -> dict[str, object]:
    rate = positive_decimal(hourly_rate_usd, "hourly_rate_usd")
    runtime_seconds = positive_integer(max_runtime_seconds, "max_runtime_seconds")
    budget = positive_decimal(max_compute_usd, "max_compute_usd")
    billable_seconds = max(runtime_seconds, MINIMUM_BILLABLE_SECONDS)
    projected = projected_compute(rate, runtime_seconds)
    if projected > budget:
        raise ValueError(
            f"projected compute cost {decimal_text(projected)} USD exceeds the compute budget "
            f"{decimal_text(budget)} USD"
        )
    start = parse_utc(start_at, "start_at")
    try:
        stop = start + timedelta(seconds=runtime_seconds)
    except OverflowError as exc:
        raise ValueError("max_runtime_seconds is outside the supported timestamp range") from exc
    return {
        "hourly_rate_usd": decimal_text(rate),
        "max_runtime_seconds": str(runtime_seconds),
        "max_compute_usd": decimal_text(budget),
        "projected_compute_usd": decimal_text(projected),
        "remaining_compute_usd": finite_fraction_text(Fraction(budget) - Fraction(projected)),
        "billable_compute_seconds": billable_seconds,
        "minimum_billable_seconds": MINIMUM_BILLABLE_SECONDS,
        "start_at": format_utc(start),
        "stop_at": format_utc(stop),
        "within_budget": True,
        "scope": "compute-only",
    }


def validate_cost_control(value: object) -> CostControl:
    if not isinstance(value, dict):
        raise ValueError("cost_control must be an object")
    policy = cast(dict[str, object], value)
    required = (*COST_CONTROL_FIELDS, "projected_compute_usd", "guard")
    missing = [field for field in required if field not in policy]
    if missing:
        raise ValueError(f"cost_control is missing required fields: {', '.join(missing)}")
    rate = positive_decimal(policy["hourly_rate_usd"], "cost_control.hourly_rate_usd")
    runtime_seconds = positive_integer(
        policy["max_runtime_seconds"], "cost_control.max_runtime_seconds"
    )
    budget = positive_decimal(policy["max_compute_usd"], "cost_control.max_compute_usd")
    projected = positive_decimal(
        policy["projected_compute_usd"],
        "cost_control.projected_compute_usd",
        MAX_PROJECTED_DECIMAL_DIGITS,
        MAX_PROJECTED_DECIMAL_EXPONENT,
    )
    expected_projection = projected_compute(rate, runtime_seconds)
    if projected != expected_projection:
        raise ValueError(
            "cost_control.projected_compute_usd does not match rate times provider-billable duration"
        )
    if projected > budget:
        raise ValueError("cost_control projected compute exceeds its compute budget")
    stop_at = parse_utc(str(policy["stop_at"]), "cost_control.stop_at")
    pricing_checked_at = parse_utc(str(policy["pricing_checked_at"]), "cost_control.pricing_checked_at")
    try:
        expected_stop = pricing_checked_at + timedelta(seconds=runtime_seconds)
    except OverflowError as exc:
        raise ValueError("cost_control.max_runtime_seconds is outside the supported timestamp range") from exc
    if stop_at != expected_stop:
        raise ValueError("cost_control.stop_at does not match pricing check time plus runtime")
    source = policy["pricing_source"]
    if not isinstance(source, str) or not source.strip() or len(source) > 1024:
        raise ValueError("cost_control.pricing_source must be a non-empty string of at most 1024 characters")
    if policy["guard"] != COST_GUARD:
        raise ValueError(f"cost_control.guard must be {COST_GUARD}")
    return cast(CostControl, policy)


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
        if "cost_control" in item:
            validate_cost_control(item["cost_control"])
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


def run_json(command: list[str]) -> dict[str, object]:
    result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=45)
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(message)
    try:
        value: object = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("provider CLI returned invalid JSON") from exc
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


def cmd_doctor(args: argparse.Namespace) -> int:
    clip = clipboard_tool()
    result = {
        "platform": platform.system().lower(),
        "aws": {"installed": bool(command_path("aws")), "path": command_path("aws")},
        "gcp": {"installed": bool(command_path("gcloud")), "path": command_path("gcloud")},
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
    vm = next((record for record in data["vms"] if record["provider"] == provider and record["id"] == vm_id), None)
    if vm is None:
        raise ValueError(f"VM not found in registry: {provider}/{vm_id}")
    private = Path(auth["key_pair"]["private_path"]).expanduser()
    public = Path(auth["key_pair"]["public_path"]).expanduser()
    if not private.is_file() or not public.is_file():
        raise FileNotFoundError("registered GitHub key paths must both exist")
    if ssh_fingerprint(private) != auth["fingerprint"] or ssh_fingerprint(public) != auth["fingerprint"]:
        raise ValueError("registered GitHub key fingerprint no longer matches its files")
    for executable in ("ssh", "scp"):
        if not command_path(executable):
            raise RuntimeError(f"{executable} is required")

    access_path = Path(vm["key_pair"]["private_path"]).expanduser()
    if not access_path.is_file():
        raise FileNotFoundError(f"VM access key does not exist: {access_path}")
    if not vm["public_ip"]:
        raise ValueError(f"VM has no public IP: {provider}/{vm_id}")
    access_key = str(access_path)
    target = f"{vm['ssh_user']}@{vm['public_ip']}"
    common = [
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", "StrictHostKeyChecking=accept-new",
        "-i", access_key,
    ]
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


def cmd_plan_cost(args: argparse.Namespace) -> int:
    start_at = str(args.start_at) if args.start_at else now()
    validate_pricing_time(parse_utc(start_at, "start_at"))
    seconds_value = getattr(args, "max_runtime_seconds", None)
    hours_value = getattr(args, "max_runtime_hours", None)
    if seconds_value is not None and hours_value is not None:
        raise ValueError("supply exactly one of max_runtime_seconds or max_runtime_hours")
    if seconds_value is not None:
        runtime_seconds = positive_integer(str(seconds_value), "max_runtime_seconds")
    elif hours_value is not None:
        runtime_seconds = runtime_seconds_from_hours(str(hours_value))
    else:
        raise ValueError("supply exactly one of max_runtime_seconds or max_runtime_hours")
    plan = cost_plan(
        str(args.hourly_rate_usd),
        str(runtime_seconds),
        str(args.max_compute_usd),
        start_at,
    )
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


def cost_control_from_args(args: argparse.Namespace) -> CostControl | None:
    values = {field: getattr(args, field, None) for field in COST_CONTROL_FIELDS}
    supplied = [field for field, value in values.items() if value is not None]
    if not supplied:
        return None
    if len(supplied) != len(COST_CONTROL_FIELDS):
        missing = [field for field in COST_CONTROL_FIELDS if values[field] is None]
        raise ValueError(
            "cost-control fields must be supplied together; missing: " + ", ".join(missing)
        )
    pricing_checked_at = format_utc(parse_utc(str(values["pricing_checked_at"]), "pricing_checked_at"))
    validate_pricing_time(parse_utc(pricing_checked_at, "pricing_checked_at"))
    plan = cost_plan(
        str(values["hourly_rate_usd"]),
        str(values["max_runtime_seconds"]),
        str(values["max_compute_usd"]),
        pricing_checked_at,
    )
    stop_at = format_utc(parse_utc(str(values["stop_at"]), "stop_at"))
    if stop_at != plan["stop_at"]:
        raise ValueError("stop_at must equal pricing_checked_at plus max_runtime_seconds")
    source = str(values["pricing_source"]).strip()
    policy: CostControl = {
        "hourly_rate_usd": str(plan["hourly_rate_usd"]),
        "max_runtime_seconds": str(plan["max_runtime_seconds"]),
        "max_compute_usd": str(plan["max_compute_usd"]),
        "projected_compute_usd": str(plan["projected_compute_usd"]),
        "stop_at": stop_at,
        "pricing_source": source,
        "pricing_checked_at": pricing_checked_at,
        "guard": COST_GUARD,
    }
    return validate_cost_control(policy)


def cost_guard_script(stop_at: str) -> str:
    deadline = parse_utc(stop_at, "stop_at")
    on_calendar = deadline.strftime("%Y-%m-%d %H:%M:%S UTC")
    deadline_epoch = int(deadline.timestamp())
    return f"""#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "remote-computer cost guard must run as root" >&2
  exit 1
fi

cat > /usr/local/sbin/remote-computer-cost-guard-check <<'REMOTE_COMPUTER_COST_CHECK'
#!/bin/sh
set -eu
deadline_epoch={deadline_epoch}
current_epoch="$(date -u +%s)"
if [ "$current_epoch" -lt "$deadline_epoch" ]; then
  exit 0
fi
exec /sbin/shutdown -h now
REMOTE_COMPUTER_COST_CHECK
chmod 0755 /usr/local/sbin/remote-computer-cost-guard-check

cat > /etc/systemd/system/remote-computer-cost-guard.service <<'REMOTE_COMPUTER_COST_SERVICE'
[Unit]
Description=Stop remote-computer VM at its approved compute deadline
After=time-sync.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/remote-computer-cost-guard-check

[Install]
WantedBy=multi-user.target
REMOTE_COMPUTER_COST_SERVICE

cat > /etc/systemd/system/remote-computer-cost-guard.timer <<'REMOTE_COMPUTER_COST_TIMER'
[Unit]
Description=Absolute compute deadline for remote-computer VM

[Timer]
OnCalendar={on_calendar}
Persistent=true
AccuracySec=1s
Unit=remote-computer-cost-guard.service

[Install]
WantedBy=timers.target
REMOTE_COMPUTER_COST_TIMER

systemctl daemon-reload
systemctl enable remote-computer-cost-guard.service
systemctl enable --now remote-computer-cost-guard.timer
systemctl start remote-computer-cost-guard.service
"""


def cmd_render_cost_guard(args: argparse.Namespace) -> int:
    output = Path(str(args.output)).expanduser()
    rendered = cost_guard_script(str(args.stop_at))
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(output, flags, 0o600)
    except FileExistsError:
        raise FileExistsError(f"refusing to overwrite existing cost guard: {output}") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(rendered)
    print(json.dumps({"output": str(output), "stop_at": format_utc(parse_utc(str(args.stop_at), "stop_at"))}, sort_keys=True))
    return 0


def cost_status(record: VMRecord, checked_at: datetime) -> dict[str, object]:
    policy = record.get("cost_control")
    base: dict[str, object] = {
        "provider": record["provider"],
        "id": record["id"],
        "name": record["name"],
        "provider_status": record["status"],
    }
    if policy is None:
        return {**base, "cost_status": "uncontrolled"}
    deadline = parse_utc(policy["stop_at"], "cost_control.stop_at")
    running = record["status"].lower() in COST_INCURRING_STATES
    expired = checked_at >= deadline
    if expired:
        state = "expired-running" if running else "expired-stopped"
    else:
        state = "active" if running else "stopped"
    remaining_seconds = max(0, int((deadline - checked_at).total_seconds()))
    return {
        **base,
        "cost_status": state,
        "stop_at": policy["stop_at"],
        "seconds_remaining": remaining_seconds,
        "hourly_rate_usd": policy["hourly_rate_usd"],
        "max_runtime_seconds": policy["max_runtime_seconds"],
        "max_compute_usd": policy["max_compute_usd"],
        "projected_compute_usd": policy["projected_compute_usd"],
        "pricing_source": policy["pricing_source"],
        "pricing_checked_at": policy["pricing_checked_at"],
        "guard": policy["guard"],
    }


def cmd_cost_status(args: argparse.Namespace) -> int:
    path = registry_path(args.registry)
    data = load_registry(path, create=False)
    checked_at = utc_now().replace(microsecond=0)
    result = {
        "checked_at": format_utc(checked_at),
        "path": str(path),
        "vms": [cost_status(record, checked_at) for record in data["vms"]],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
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
    policy = cost_control_from_args(args)
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
    if policy is not None:
        record["cost_control"] = policy
    elif index is not None:
        existing_policy = data["vms"][index].get("cost_control")
        if existing_policy is not None:
            record["cost_control"] = existing_policy
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
    for record in data["vms"]:
        provider = record["provider"]
        executable = "aws" if provider == "aws" else "gcloud"
        if not command_path(executable):
            results.append({"provider": provider, "id": record["id"], "result": "skipped", "reason": f"{executable} not installed"})
            continue
        try:
            status, public_ip = aws_state(record) if provider == "aws" else gcp_state(record)
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
    doctor = sub.add_parser("doctor", help="detect cloud, GitHub, SSH, GPG, and clipboard tooling")
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

    plan_cost = sub.add_parser(
        "plan-cost",
        help="validate a compute budget and calculate an absolute stop deadline",
    )
    plan_cost.add_argument("--hourly-rate-usd", required=True)
    runtime_group = plan_cost.add_mutually_exclusive_group(required=True)
    runtime_group.add_argument("--max-runtime-hours")
    runtime_group.add_argument("--max-runtime-seconds")
    plan_cost.add_argument("--max-compute-usd", required=True)
    plan_cost.add_argument("--start-at")
    plan_cost.set_defaults(func=cmd_plan_cost)

    render_cost_guard = sub.add_parser(
        "render-cost-guard",
        help="write cloud-init-compatible systemd auto-stop configuration",
    )
    render_cost_guard.add_argument("--stop-at", required=True)
    render_cost_guard.add_argument("--output", required=True)
    render_cost_guard.set_defaults(func=cmd_render_cost_guard)

    cost_status_parser = sub.add_parser(
        "cost-status",
        help="audit recorded compute budgets and stop deadlines without provider calls",
    )
    cost_status_parser.add_argument("--registry")
    cost_status_parser.set_defaults(func=cmd_cost_status)

    upsert = sub.add_parser("upsert", help="create or replace one registry record")
    upsert.add_argument("--registry")
    upsert.add_argument("--provider", choices=sorted(VALID_PROVIDERS), required=True)
    for option in ("id", "name", "location", "machine-type", "public-ip", "ssh-user", "private-key", "public-key", "cloud-key-name", "status"):
        upsert.add_argument(f"--{option}", required=True)
    upsert.add_argument("--hourly-rate-usd")
    upsert.add_argument("--max-runtime-seconds")
    upsert.add_argument("--max-compute-usd")
    upsert.add_argument("--stop-at")
    upsert.add_argument("--pricing-source")
    upsert.add_argument("--pricing-checked-at")
    upsert.set_defaults(func=cmd_upsert)

    sync = sub.add_parser("sync", help="query providers and update all registered VM states")
    sync.add_argument("--registry")
    sync.set_defaults(func=cmd_sync)

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

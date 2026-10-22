from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
ASSETS = SKILL_ROOT / "assets" / "containers"
GIB = 1024 ** 3


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


book = load_module("vm_bookkeeper", SKILL_ROOT / "scripts" / "vm_bookkeeper.py")


def make_host(
    cpus: int,
    performance: int | None,
    memory_gib: int,
    free_gib: int,
    system: str = "darwin",
    machine: str = "arm64",
    version: str | None = "26.0",
) -> dict[str, object]:
    return {
        "platform": system,
        "machine": machine,
        "macos_version": version,
        "chip": "Apple M4",
        "cpus": cpus,
        "performance_cores": performance,
        "efficiency_cores": None if performance is None else cpus - performance,
        "memory_bytes": memory_gib * GIB,
        "disk_free_bytes": free_gib * GIB,
        "disk_path": "/Users/dev",
    }


def run_json_command(function: Any, args: argparse.Namespace) -> tuple[int, Any]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = function(args)
    return rc, json.loads(output.getvalue())


def managed_container(
    name: str,
    state: str,
    address: str | None,
    image: str = "local/remote-computer-polyglot:latest",
) -> dict[str, object]:
    networks: list[dict[str, object]] = []
    if address:
        networks.append({
            "network": "default",
            "hostname": f"{name}.",
            "ipv4Address": f"{address}/24",
            "ipv4Gateway": "192.168.64.1",
        })
    return {
        "id": name,
        "configuration": {
            "id": name,
            "image": {"reference": image},
            "resources": {"cpus": 4, "memoryInBytes": 6 * GIB},
        },
        "status": {"state": state, "networks": networks},
    }


class ResourcePlanTests(unittest.TestCase):
    def test_m4_with_16_gib_matches_multi_language_guidance(self) -> None:
        host = make_host(cpus=10, performance=4, memory_gib=16, free_gib=250)
        plan = book.container_resource_plan(host, "standard", 4)
        self.assertEqual(plan["cpus"], 4)
        self.assertEqual(plan["cpus_range"], [4, 6])
        self.assertEqual(plan["memory_gib"], 6)
        self.assertEqual(plan["disk_gib"], 40)
        self.assertEqual(plan["disk_range_gib"], [30, 40])
        self.assertEqual(plan["warnings"], [])

    def test_m4_tiers_scale_with_workload(self) -> None:
        host = make_host(cpus=10, performance=4, memory_gib=16, free_gib=250)
        light = book.container_resource_plan(host, "light", 1)
        heavy = book.container_resource_plan(host, "heavy", 5)
        self.assertEqual((light["cpus"], light["memory_gib"], light["disk_gib"]), (2, 4, 20))
        self.assertEqual((heavy["cpus"], heavy["memory_gib"], heavy["disk_gib"]), (7, 8, 80))
        self.assertEqual(heavy["cpus_range"], [7, 8])

    def test_small_host_leaves_headroom_and_warns_about_disk(self) -> None:
        host = make_host(cpus=8, performance=4, memory_gib=8, free_gib=60)
        plan = book.container_resource_plan(host, "standard", 4)
        self.assertEqual(plan["cpus"], 4)
        self.assertEqual(plan["memory_gib"], 3)
        self.assertEqual(plan["disk_gib"], 28)
        self.assertTrue(any("below the 30 GiB" in warning for warning in plan["warnings"]))

    def test_large_host_caps_standard_and_heavy_tiers(self) -> None:
        host = make_host(cpus=16, performance=12, memory_gib=128, free_gib=2000)
        standard = book.container_resource_plan(host, "standard", 4)
        heavy = book.container_resource_plan(host, "heavy", 5)
        self.assertEqual((standard["cpus"], standard["memory_gib"]), (8, 16))
        self.assertEqual((heavy["cpus"], heavy["memory_gib"], heavy["disk_gib"]), (12, 32, 80))

    def test_unknown_performance_cores_fall_back_to_half_the_cpus(self) -> None:
        host = make_host(cpus=12, performance=None, memory_gib=32, free_gib=500)
        self.assertEqual(book.container_resource_plan(host, "standard", 4)["cpus"], 6)

    def test_plans_never_exceed_host_capacity(self) -> None:
        for cpus, memory, free in ((8, 8, 30), (10, 16, 250), (14, 24, 100), (24, 192, 4000)):
            host = make_host(cpus=cpus, performance=cpus // 2, memory_gib=memory, free_gib=free)
            for tier in ("light", "standard", "heavy"):
                with self.subTest(cpus=cpus, memory=memory, tier=tier):
                    plan = book.container_resource_plan(host, tier, 4)
                    self.assertLessEqual(plan["cpus_range"][1], cpus)
                    self.assertLess(plan["memory_gib"], memory)
                    self.assertLessEqual(plan["disk_range_gib"][1] + 4, free)

    def test_exhausted_disk_is_reported(self) -> None:
        host = make_host(cpus=10, performance=4, memory_gib=16, free_gib=10)
        plan = book.container_resource_plan(host, "standard", 4)
        self.assertEqual(plan["disk_gib"], 3)
        self.assertIn("Free more host disk space before creating this container.", plan["warnings"])

    def test_machine_type_encodes_config_and_resources(self) -> None:
        self.assertEqual(book.container_machine_type("polyglot", 4, 6, 40), "polyglot:4cpu-6gib-40gib")


class HostDetectionTests(unittest.TestCase):
    def test_support_matrix(self) -> None:
        cases = (
            (make_host(10, 4, 16, 250, version="26.1"), "supported"),
            (make_host(10, 4, 16, 250, version="15.6"), "limited"),
            (make_host(10, 4, 16, 250, version="14.7"), "unsupported"),
            (make_host(10, 4, 16, 250, machine="x86_64"), "unsupported"),
            (make_host(10, 4, 16, 250, system="linux", version=None), "unsupported"),
        )
        for host, expected in cases:
            with self.subTest(expected=expected, host=host["macos_version"]):
                self.assertEqual(book.container_host_support(host)[0], expected)

    def test_darwin_host_reads_sysctl_values(self) -> None:
        values = {
            "hw.ncpu": "10",
            "hw.perflevel0.physicalcpu": "4",
            "hw.perflevel1.physicalcpu": "6",
            "hw.memsize": str(16 * GIB),
            "machdep.cpu.brand_string": "Apple M4",
        }

        def fake_run(
            command: list[str], timeout: int = 30, input_text: str | None = None,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, f"{values[command[-1]]}\n", "")

        usage = mock.Mock(free=250 * GIB)
        with mock.patch.object(book.platform, "system", return_value="Darwin"), \
                mock.patch.object(book.platform, "machine", return_value="arm64"), \
                mock.patch.object(book.platform, "mac_ver", return_value=("26.0.1", ("", "", ""), "arm64")), \
                mock.patch.object(book, "command_path", return_value="/usr/sbin/sysctl"), \
                mock.patch.object(book, "run_text", side_effect=fake_run), \
                mock.patch.object(book.shutil, "disk_usage", return_value=usage):
            host = book.detect_container_host()
        self.assertEqual(host["cpus"], 10)
        self.assertEqual(host["performance_cores"], 4)
        self.assertEqual(host["efficiency_cores"], 6)
        self.assertEqual(host["memory_bytes"], 16 * GIB)
        self.assertEqual(host["disk_free_bytes"], 250 * GIB)
        self.assertEqual(host["chip"], "Apple M4")
        self.assertEqual(book.container_host_support(host)[0], "supported")

    def test_cli_status_parses_version_and_service(self) -> None:
        version = "container CLI version 1.6.0 (build: release, commit: abc1234)\n"
        responses = {
            "--version": subprocess.CompletedProcess(["container"], 0, version, ""),
            "status": subprocess.CompletedProcess(["container"], 1, '{"status": "not running"}\n', ""),
        }

        def fake_run(
            command: list[str], timeout: int = 30, input_text: str | None = None,
        ) -> subprocess.CompletedProcess[str]:
            return responses["--version" if "--version" in command else "status"]

        with mock.patch.object(book, "command_path", return_value="/usr/local/bin/container"), \
                mock.patch.object(book, "run_text", side_effect=fake_run):
            status = book.container_cli_status()
        self.assertEqual(status["version"], "1.6.0")
        self.assertIs(status["version_supported"], True)
        self.assertEqual(status["service"], "not running")

    def test_cli_status_reports_missing_cli(self) -> None:
        with mock.patch.object(book, "command_path", return_value=None):
            status = book.container_cli_status()
        self.assertFalse(status["installed"])
        self.assertIsNone(status["service"])


class ContainerListTests(unittest.TestCase):
    def test_parses_managed_container_json(self) -> None:
        payload = [
            managed_container("dev", "running", "192.168.64.5"),
            managed_container("old", "stopped", None),
        ]
        states = book.parse_container_list(payload)
        self.assertEqual(states["dev"]["state"], "running")
        self.assertEqual(states["dev"]["ip"], "192.168.64.5")
        self.assertEqual(states["dev"]["network"], "default")
        self.assertEqual(states["dev"]["cpus"], 4)
        self.assertEqual(states["dev"]["memory_bytes"], 6 * GIB)
        self.assertEqual(states["dev"]["image"], "local/remote-computer-polyglot:latest")
        self.assertEqual(states["old"]["state"], "stopped")
        self.assertIsNone(states["old"]["ip"])

    def test_parses_legacy_snapshot_json(self) -> None:
        payload = [{
            "configuration": {"id": "legacy"},
            "status": "running",
            "networks": [{"address": "192.168.64.9/24", "network": "default"}],
        }]
        state = book.parse_container_list(payload)["legacy"]
        self.assertEqual((state["state"], state["ip"]), ("running", "192.168.64.9"))

    def test_rejects_malformed_output(self) -> None:
        with self.assertRaises(RuntimeError):
            book.parse_container_list({"not": "a list"})
        with self.assertRaises(RuntimeError):
            book.parse_container_list([{"configuration": {}, "status": {"state": "running"}}])


class ContainerRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = self.root / "vms.json"
        self.private = self.root / "dev"
        self.public = self.root / "dev.pub"
        self.private.write_text("test-private-placeholder")
        self.public.write_text("ssh-ed25519 AAAATEST remote-computer:dev")

    def upsert(self, provider: str, vm_id: str, ip: str) -> None:
        args = argparse.Namespace(
            registry=str(self.registry), provider=provider, id=vm_id, name=vm_id,
            location="local/default" if provider == "apple-container" else "us-east-1",
            machine_type="polyglot:4cpu-6gib-40gib", public_ip=ip, ssh_user="ubuntu",
            private_key=str(self.private), public_key=str(self.public),
            cloud_key_name=f"remote-computer:{vm_id}", status="running",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(book.cmd_upsert(args), 0)

    def records(self) -> dict[tuple[str, str], dict[str, Any]]:
        data = json.loads(self.registry.read_text())
        return {(item["provider"], item["id"]): item for item in data["vms"]}

    def test_container_records_share_the_vm_schema(self) -> None:
        self.upsert("apple-container", "dev", "192.168.64.5")
        self.upsert("aws", "i-test", "203.0.113.10")
        records = self.records()
        self.assertEqual(set(records[("apple-container", "dev")]), set(records[("aws", "i-test")]))
        book.validate_registry(json.loads(self.registry.read_text()))

    def test_sync_reconciles_containers_with_one_cli_call(self) -> None:
        self.upsert("apple-container", "dev", "192.168.64.5")
        self.upsert("apple-container", "stopped-box", "192.168.64.6")
        self.upsert("apple-container", "gone", "192.168.64.7")
        payload = [
            managed_container("dev", "running", "192.168.64.8"),
            managed_container("stopped-box", "stopped", None),
        ]
        with mock.patch.object(book, "command_path", return_value="/usr/local/bin/container"), \
                mock.patch.object(book, "run_json_value", return_value=payload) as listing:
            rc, _ = run_json_command(book.cmd_sync, argparse.Namespace(registry=str(self.registry)))
        self.assertEqual(rc, 0)
        listing.assert_called_once_with(["container", "list", "--all", "--format", "json"])
        records = self.records()
        self.assertEqual(records[("apple-container", "dev")]["status"], "running")
        self.assertEqual(records[("apple-container", "dev")]["public_ip"], "192.168.64.8")
        self.assertEqual(records[("apple-container", "stopped-box")]["status"], "stopped")
        self.assertEqual(records[("apple-container", "stopped-box")]["public_ip"], "192.168.64.6")
        self.assertEqual(records[("apple-container", "gone")]["status"], "missing")

    def test_sync_error_preserves_container_record(self) -> None:
        self.upsert("apple-container", "dev", "192.168.64.5")
        before = self.records()[("apple-container", "dev")]
        with mock.patch.object(book, "command_path", return_value="/usr/local/bin/container"), \
                mock.patch.object(book, "run_json_value", side_effect=RuntimeError("apiserver is not running")):
            rc, output = run_json_command(book.cmd_sync, argparse.Namespace(registry=str(self.registry)))
        self.assertEqual(rc, 1)
        self.assertEqual(output["results"][0]["reason"], "apiserver is not running")
        self.assertEqual(self.records()[("apple-container", "dev")], before)

    def test_sync_skips_containers_without_cli(self) -> None:
        self.upsert("apple-container", "dev", "192.168.64.5")
        with mock.patch.object(book, "command_path", return_value=None):
            rc, output = run_json_command(book.cmd_sync, argparse.Namespace(registry=str(self.registry)))
        self.assertEqual(rc, 0)
        self.assertEqual(output["results"][0]["reason"], "container not installed")

    def test_container_ssh_uses_a_stable_host_key_alias(self) -> None:
        self.upsert("apple-container", "dev", "192.168.64.5")
        self.upsert("aws", "i-test", "203.0.113.10")
        data = book.load_registry(self.registry)
        with mock.patch.object(book, "command_path", return_value="/usr/bin/ssh"):
            _, target, common = book.registered_vm_ssh(data, "apple-container", "dev")
            _, _, cloud_common = book.registered_vm_ssh(data, "aws", "i-test")
        self.assertEqual(target, "ubuntu@192.168.64.5")
        self.assertIn("HostKeyAlias=remote-computer.apple-container.dev", common)
        self.assertEqual(common[-2:], ["-i", str(self.private.resolve())])
        self.assertFalse(any(option.startswith("HostKeyAlias=") for option in cloud_common))

    def test_ssh_command_matches_provider(self) -> None:
        self.upsert("apple-container", "dev", "192.168.64.5")
        self.upsert("aws", "i-test", "203.0.113.10")
        rc, container = run_json_command(book.cmd_ssh_command, argparse.Namespace(
            registry=str(self.registry), provider="apple-container", id="dev",
        ))
        self.assertEqual(rc, 0)
        self.assertEqual(
            container["command"],
            "ssh -o IdentitiesOnly=yes -o HostKeyAlias=remote-computer.apple-container.dev "
            f"-i {self.private.resolve()} ubuntu@192.168.64.5",
        )
        _, cloud = run_json_command(book.cmd_ssh_command, argparse.Namespace(
            registry=str(self.registry), provider="aws", id="i-test",
        ))
        self.assertEqual(cloud["command"], f"ssh -o IdentitiesOnly=yes -i {self.private.resolve()} ubuntu@203.0.113.10")
        self.assertIsNone(cloud["host_key_alias"])

    def test_container_status_waits_for_an_address(self) -> None:
        listings = [
            [managed_container("dev", "running", None)],
            [managed_container("dev", "running", "192.168.64.5")],
        ]
        with mock.patch.object(book, "command_path", return_value="/usr/local/bin/container"), \
                mock.patch.object(book, "run_json_value", side_effect=listings), \
                mock.patch.object(book.time, "sleep"):
            rc, status = run_json_command(book.cmd_container_status, argparse.Namespace(id="dev", wait=30))
        self.assertEqual(rc, 0)
        self.assertTrue(status["ready"])
        self.assertTrue(status["private_address"])
        self.assertEqual(status["ip"], "192.168.64.5")

    def test_container_status_rejects_invalid_names(self) -> None:
        with self.assertRaises(ValueError):
            book.cmd_container_status(argparse.Namespace(id="x", wait=0))


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen unavailable")
class HostKeyTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.known_hosts = self.root / "ssh" / "known_hosts"

    def host_key(self, name: str) -> str:
        path = self.root / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
        return (path.with_suffix(".pub")).read_text().strip()

    def trust(self, public_key: str) -> tuple[int, Any]:
        real_checked = book.checked_text

        def fake_checked(command: list[str], timeout: int = 30, input_text: str | None = None) -> str:
            if command[0] == "container":
                self.assertEqual(command, ["container", "exec", "dev", "cat", book.CONTAINER_HOST_KEY])
                return public_key
            return real_checked(command, timeout=timeout, input_text=input_text)

        def fake_command_path(name: str) -> str:
            return shutil.which(name) or f"/mock/{name}"

        with mock.patch.object(book, "checked_text", side_effect=fake_checked), \
                mock.patch.object(book, "command_path", side_effect=fake_command_path):
            return run_json_command(
                book.cmd_container_trust_host_key,
                argparse.Namespace(id="dev", known_hosts=str(self.known_hosts)),
            )

    def test_trusts_once_and_refuses_silent_replacement(self) -> None:
        first = self.host_key("first")
        rc, result = self.trust(first)
        self.assertEqual((rc, result["action"]), (0, "trusted"))
        self.assertEqual(result["alias"], "remote-computer.apple-container.dev")
        self.assertEqual(oct(self.known_hosts.stat().st_mode & 0o777), oct(0o600))
        self.assertIn(f"remote-computer.apple-container.dev {' '.join(first.split()[:2])}", self.known_hosts.read_text())
        self.assertEqual(self.trust(first)[1]["action"], "already-trusted")
        with self.assertRaisesRegex(ValueError, "different ssh-ed25519 key"):
            self.trust(self.host_key("second"))

    def test_rejects_non_ed25519_output(self) -> None:
        with self.assertRaises(RuntimeError):
            self.trust("not a key")


class SshLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.layer = (ASSETS / "remote-computer-ssh-layer.dockerfile").read_text()

    def test_default_configs_embed_the_current_layer_last(self) -> None:
        self.assertEqual(set(book.DEFAULT_CONTAINER_CONFIGS), {"essentials", "polyglot", "fullstack"})
        for name in book.DEFAULT_CONTAINER_CONFIGS:
            with self.subTest(name=name):
                text = (ASSETS / name / "Dockerfile").read_text()
                self.assertTrue(text.endswith(self.layer))
                self.assertEqual(book.ssh_layer_state(text), "current")
                self.assertTrue(text.startswith(f"# remote-computer default container configuration: {name}\n"))

    def test_layer_scripts_have_valid_shell_syntax(self) -> None:
        scripts = {
            "setup": r"RUN <<'REMOTE_COMPUTER_SETUP'\n(.*?)\nREMOTE_COMPUTER_SETUP\n",
            "entrypoint": r"/usr/local/sbin/remote-computer-entrypoint\n(.*?)\nREMOTE_COMPUTER_ENTRYPOINT\n",
        }
        for name, pattern in scripts.items():
            with self.subTest(script=name):
                match = re.search(pattern, self.layer, re.DOTALL)
                self.assertIsNotNone(match)
                assert match is not None
                result = subprocess.run(["sh", "-n"], input=match.group(1), text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_layer_enforces_key_only_ssh_and_tooling(self) -> None:
        for required in (
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "PermitRootLogin no",
            "AuthenticationMethods publickey",
            "REMOTE_COMPUTER_AUTHORIZED_KEY",
            "NOPASSWD:ALL",
            "pinentry-curses",
            "gpg-agent",
            "python3",
            "/home/.remote-computer/ssh-host-keys",
            'ENTRYPOINT ["/usr/bin/tini", "-s", "--", "/usr/local/sbin/remote-computer-entrypoint"]',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.layer)
        self.assertIn(book.CONTAINER_HOST_KEY.removesuffix(".pub"), self.layer)

    def test_apply_appends_refreshes_and_rejects_misplaced_layers(self) -> None:
        user = "FROM ubuntu:24.04\nRUN echo hi\n"
        applied = book.apply_ssh_layer(user)
        self.assertEqual(applied, f"FROM ubuntu:24.04\nRUN echo hi\n\n{self.layer}")
        self.assertEqual(book.apply_ssh_layer(applied), applied)
        outdated = applied.replace("PermitRootLogin no", "PermitRootLogin yes")
        self.assertEqual(book.ssh_layer_state(outdated), "outdated")
        self.assertEqual(book.apply_ssh_layer(outdated), applied)
        self.assertEqual(book.ssh_layer_state(user), "missing")
        with self.assertRaisesRegex(ValueError, "last block"):
            book.apply_ssh_layer(f"{applied}CMD [\"bash\"]\n")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            book.apply_ssh_layer(applied + self.layer)
        self.assertEqual(book.ssh_layer_state(applied + self.layer), "invalid")


class DockerfileAnalysisTests(unittest.TestCase):
    def test_single_language_is_light(self) -> None:
        analysis = book.analyze_dockerfile("FROM python:3.12-slim\nRUN pip install fastapi\nCMD [\"uvicorn\", \"app:app\"]\n")
        self.assertEqual(analysis["languages"], ["python"])
        self.assertEqual(analysis["tier"], "light")
        self.assertEqual(analysis["base_family"], "debian")
        self.assertEqual(analysis["ssh_layer"], "missing")
        self.assertTrue(any("CMD" in warning for warning in analysis["warnings"]))

    def test_compiled_or_multiple_languages_are_standard(self) -> None:
        rust = book.analyze_dockerfile("FROM rust:1-bookworm\nRUN cargo install ripgrep\n")
        multi = book.analyze_dockerfile("FROM ubuntu:24.04\nRUN apt-get install -y nodejs python3\n")
        self.assertEqual(rust["tier"], "standard")
        self.assertEqual(multi["languages"], ["node", "python"])
        self.assertEqual(multi["tier"], "standard")

    def test_services_and_browsers_are_heavy_but_clients_are_not(self) -> None:
        client = book.analyze_dockerfile("FROM node:24\nRUN apt-get install -y postgresql-client redis-tools\n")
        self.assertEqual(client["services"], [])
        service = book.analyze_dockerfile("FROM node:24\nRUN apt-get install -y postgresql\nRUN npx playwright install\n")
        self.assertEqual(service["services"], ["postgresql"])
        self.assertEqual(service["heavy_signals"], ["browser-automation"])
        self.assertEqual(service["tier"], "heavy")

    def test_multi_stage_final_base_and_context_detection(self) -> None:
        text = (
            "FROM golang:1.27 AS build\nCOPY . /src\nRUN go build ./...\n"
            "FROM build AS dev\nCOPY --from=build /src/bin /usr/local/bin\nEXPOSE 8080 9090\n"
        )
        analysis = book.analyze_dockerfile(text)
        self.assertEqual(analysis["final_base_image"], "golang:1.27")
        self.assertEqual(analysis["base_family"], "debian")
        self.assertTrue(analysis["uses_build_context"])
        self.assertEqual(analysis["exposed_ports"], ["8080", "9090"])
        no_context = book.analyze_dockerfile("FROM ubuntu:24.04\nCOPY --from=golang:1.27 /usr/local/go /usr/local/go\n")
        self.assertFalse(no_context["uses_build_context"])

    def test_non_apt_bases_are_flagged(self) -> None:
        for base, family in (
            ("alpine:3.22", "alpine"),
            ("node:24-alpine", "alpine"),
            ("fedora:42", "non-apt"),
            ("registry.example.com/team/dev:1", "unknown"),
        ):
            with self.subTest(base=base):
                analysis = book.analyze_dockerfile(f"FROM {base}\n")
                self.assertEqual(analysis["base_family"], family)
                self.assertTrue(analysis["warnings"])
        hinted = book.analyze_dockerfile("FROM registry.example.com/team/dev:1\nRUN apt-get install -y git\n")
        self.assertEqual(hinted["base_family"], "debian")

    def test_heredocs_continuations_and_managed_layer_are_handled(self) -> None:
        text = (
            "FROM ubuntu:24.04\n"
            "RUN <<'EOF'\napt-get update\napt-get install -y \\\n  openjdk-21-jdk maven\nEOF\n"
            "RUN apt-get install -y \\\n    golang\n"
        )
        analysis = book.analyze_dockerfile(book.apply_ssh_layer(text))
        self.assertEqual(analysis["languages"], ["go", "java"])
        self.assertEqual(analysis["ssh_layer"], "current")
        self.assertNotIn("python", analysis["languages"])

    def test_nested_containers_warn_about_virtualization(self) -> None:
        analysis = book.analyze_dockerfile("FROM ubuntu:24.04\nRUN apt-get install -y docker.io\n")
        self.assertIn("nested-containers", analysis["heavy_signals"])
        self.assertTrue(any("--virtualization" in warning for warning in analysis["warnings"]))


class ContainerConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.configs = self.root / "Documents" / "Containers"

    def configs_command(self) -> dict[str, Any]:
        rc, output = run_json_command(book.cmd_container_configs, argparse.Namespace(dir=str(self.configs)))
        self.assertEqual(rc, 0)
        return cast(dict[str, Any], output)

    def actions(self, output: dict[str, Any]) -> dict[str, str]:
        return {item["name"]: item["action"] for item in output["default_configs"]}

    def test_defaults_are_created_then_maintained(self) -> None:
        first = self.configs_command()
        self.assertEqual(set(self.actions(first).values()), {"created"})
        for name in book.DEFAULT_CONTAINER_CONFIGS:
            self.assertEqual(
                (self.configs / name / "Dockerfile").read_text(),
                (ASSETS / name / "Dockerfile").read_text(),
            )
        manifest = json.loads((self.configs / ".remote-computer.json").read_text())
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["configs"]["polyglot"]["tier"], "standard")
        self.assertEqual(set(self.actions(self.configs_command()).values()), {"unchanged"})

    def test_untouched_old_defaults_update_but_user_edits_are_preserved(self) -> None:
        self.configs_command()
        manifest_path = self.configs / ".remote-computer.json"
        manifest = json.loads(manifest_path.read_text())
        old = "# previous bundled version\nFROM ubuntu:22.04\n"
        (self.configs / "essentials" / "Dockerfile").write_text(old)
        manifest["configs"]["essentials"]["sha256"] = book.sha256_text(old)
        manifest_path.write_text(json.dumps(manifest))
        (self.configs / "polyglot" / "Dockerfile").write_text("FROM ubuntu:24.04\n# my edit\n")
        output = self.configs_command()
        actions = self.actions(output)
        self.assertEqual(actions["essentials"], "updated")
        self.assertEqual(actions["polyglot"], "modified-preserved")
        self.assertEqual(actions["fullstack"], "unchanged")
        self.assertEqual((self.configs / "polyglot" / "Dockerfile").read_text(), "FROM ubuntu:24.04\n# my edit\n")
        listed = {item["name"]: item for item in output["configs"]}
        self.assertTrue(listed["polyglot"]["modified"])
        self.assertEqual(listed["polyglot"]["ssh_layer"], "missing")

    def import_config(self, name: str, dockerfile: Path, replace: bool = False, context: str | None = None) -> dict[str, Any]:
        rc, output = run_json_command(book.cmd_container_config_import, argparse.Namespace(
            dir=str(self.configs), name=name, dockerfile=str(dockerfile), context=context, replace=replace,
        ))
        self.assertEqual(rc, 0)
        return cast(dict[str, Any], output)

    def test_import_saves_custom_config_with_layer_and_context(self) -> None:
        project = self.root / "project"
        project.mkdir()
        source = project / "Dockerfile"
        source.write_text("FROM node:24-bookworm\nCOPY package.json /app/\nCMD [\"node\", \"server.js\"]\n")
        result = self.import_config("webapp", source)
        saved = self.configs / "webapp" / "Dockerfile"
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["context"], str(project.resolve()))
        self.assertEqual(book.ssh_layer_state(saved.read_text()), "current")
        self.assertEqual(source.read_text().count("remote-computer"), 0)
        self.assertEqual(self.import_config("webapp", source)["action"], "unchanged")
        manifest = json.loads((self.configs / ".remote-computer.json").read_text())
        self.assertEqual(manifest["configs"]["webapp"]["kind"], "custom")
        self.assertEqual(manifest["configs"]["webapp"]["tier"], "light")

    def test_import_refuses_silent_overwrite_and_default_names(self) -> None:
        source = self.root / "Dockerfile"
        source.write_text("FROM ubuntu:24.04\n")
        self.import_config("tools", source)
        source.write_text("FROM ubuntu:24.04\nRUN apt-get install -y golang\n")
        with self.assertRaises(FileExistsError):
            book.cmd_container_config_import(argparse.Namespace(
                dir=str(self.configs), name="tools", dockerfile=str(source), context=None, replace=False,
            ))
        self.assertEqual(self.import_config("tools", source, replace=True)["action"], "replaced")
        self.assertEqual(self.import_config("tools", source)["context"], str(self.configs / "tools"))
        with self.assertRaisesRegex(ValueError, "maintained default"):
            book.cmd_container_config_import(argparse.Namespace(
                dir=str(self.configs), name="polyglot", dockerfile=str(source), context=None, replace=False,
            ))

    def test_customizing_a_default_copies_then_refreshes_in_place(self) -> None:
        self.configs_command()
        copied = self.import_config("polyglot-data", self.configs / "polyglot" / "Dockerfile")
        self.assertEqual(copied["action"], "created")
        saved = self.configs / "polyglot-data" / "Dockerfile"
        edited = saved.read_text().replace(
            "openjdk-21-jdk-headless maven sqlite3", "openjdk-21-jdk-headless maven sqlite3 postgresql",
        ).replace("PermitRootLogin no", "PermitRootLogin prohibit-password")
        saved.write_text(edited)
        refreshed = self.import_config("polyglot-data", saved)
        self.assertEqual(refreshed["action"], "refreshed")
        self.assertEqual(refreshed["analysis"]["tier"], "heavy")
        self.assertIn("PermitRootLogin no", saved.read_text())
        self.assertIn("sqlite3 postgresql", saved.read_text())

    def test_plan_covers_defaults_and_saved_configs(self) -> None:
        host = make_host(cpus=10, performance=4, memory_gib=16, free_gib=250)
        with mock.patch.object(book, "detect_container_host", return_value=host), \
                mock.patch.object(book, "container_cli_status", return_value={"installed": False}):
            rc, output = run_json_command(book.cmd_container_plan, argparse.Namespace(dir=str(self.configs), config=None))
        self.assertEqual(rc, 0)
        plans = {plan["config"]: plan for plan in output["plans"]}
        self.assertEqual(list(plans), ["essentials", "polyglot", "fullstack"])
        self.assertEqual(plans["polyglot"]["machine_type"], "polyglot:4cpu-6gib-40gib")
        self.assertEqual(plans["polyglot"]["image"], "local/remote-computer-polyglot:latest")
        self.assertIn("--file", plans["polyglot"]["build_command"])
        self.assertEqual(output["support"]["status"], "supported")
        self.assertIn("ceiling", output["memory_note"])
        self.assertEqual(plans["polyglot"]["action_required"], [])

        (self.configs / "essentials" / "Dockerfile").write_text("FROM ubuntu:24.04\n")
        with mock.patch.object(book, "detect_container_host", return_value=host), \
                mock.patch.object(book, "container_cli_status", return_value={"installed": False}):
            _, edited = run_json_command(book.cmd_container_plan, argparse.Namespace(dir=str(self.configs), config="essentials"))
        self.assertIn("missing", edited["plans"][0]["action_required"][0])

        source = self.root / "Dockerfile"
        source.write_text("FROM ubuntu:24.04\nRUN apt-get install -y postgresql nodejs\n")
        self.import_config("services", source)
        with mock.patch.object(book, "detect_container_host", return_value=host), \
                mock.patch.object(book, "container_cli_status", return_value={"installed": False}):
            _, custom = run_json_command(book.cmd_container_plan, argparse.Namespace(dir=str(self.configs), config="services"))
        plan = custom["plans"][0]
        self.assertEqual(plan["kind"], "custom")
        self.assertEqual(plan["tier"], "heavy")
        self.assertEqual(plan["resources"]["memory_gib"], 8)
        self.assertIn("analysis", plan)

    def test_plan_rejects_unknown_config(self) -> None:
        with mock.patch.object(book, "detect_container_host", return_value=make_host(10, 4, 16, 250)), \
                mock.patch.object(book, "container_cli_status", return_value={"installed": False}):
            with self.assertRaises(FileNotFoundError):
                book.cmd_container_plan(argparse.Namespace(dir=str(self.configs), config="missing"))

    def test_invalid_manifest_is_not_overwritten(self) -> None:
        self.configs.mkdir(parents=True)
        manifest = self.configs / ".remote-computer.json"
        manifest.write_text("not json")
        with self.assertRaises(ValueError):
            book.cmd_container_configs(argparse.Namespace(dir=str(self.configs)))
        self.assertEqual(manifest.read_text(), "not json")


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_apple_container(self) -> None:
        def fake_command_path(name: str) -> str | None:
            return "/usr/local/bin/container" if name == "container" else None

        with mock.patch.object(book, "command_path", side_effect=fake_command_path):
            rc, output = run_json_command(book.cmd_doctor, argparse.Namespace())
        self.assertEqual(rc, 0)
        self.assertEqual(output["apple_container"], {"installed": True, "path": "/usr/local/bin/container"})


@unittest.skipUnless(
    os.environ.get("REMOTE_COMPUTER_DOCKER_TESTS") == "1" and shutil.which("docker"),
    "set REMOTE_COMPUTER_DOCKER_TESTS=1 with Docker available to build the essentials image",
)
class EssentialsImageIntegrationTests(unittest.TestCase):
    """Build the essentials Dockerfile with BuildKit and verify key-only SSH like apple/container."""

    def test_essentials_image_serves_key_only_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = root / "id"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
            key.chmod(0o600)
            tag = "remote-computer-test/essentials:latest"
            name = f"remote-computer-test-{os.getpid()}"
            subprocess.run(["docker", "build", "-q", "-t", tag, str(ASSETS / "essentials")], check=True, capture_output=True)
            self.addCleanup(subprocess.run, ["docker", "volume", "rm", "-f", f"{name}-home"], capture_output=True)
            self.addCleanup(subprocess.run, ["docker", "rm", "-f", name], capture_output=True)
            subprocess.run([
                "docker", "run", "-d", "--name", name, "-v", f"{name}-home:/home",
                "-e", f"REMOTE_COMPUTER_AUTHORIZED_KEY={key.with_suffix('.pub').read_text().strip()}", tag,
            ], check=True, capture_output=True)
            address = subprocess.run(
                ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            command = [
                "ssh", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={root / 'known_hosts'}",
                "-i", str(key), f"ubuntu@{address}", "sudo -n true && python3 -c 'print(42)'",
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            for _ in range(30):
                if result.returncode == 0:
                    break
                time.sleep(1)
                result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(result.stdout.strip(), "42", result.stderr)


if __name__ == "__main__":
    unittest.main()

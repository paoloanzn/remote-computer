import importlib.util
import io
import json
import stat
import subprocess
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vm_bookkeeper", ROOT / "scripts" / "vm_bookkeeper.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load vm_bookkeeper module")
vm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vm)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "vms.json"
        self.private = self.root / "id_ed25519"
        self.public = self.root / "id_ed25519.pub"
        self.private.write_text("test-private-placeholder")
        self.public.write_text("ssh-ed25519 AAAATEST test")
        self.pricing_now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)

    def upsert_args(self, provider: str = "aws", vm_id: str = "i-test", location: str = "us-east-1") -> Namespace:
        return Namespace(
            registry=str(self.registry), provider=provider, id=vm_id, name="dev",
            location=location, machine_type="test-type", public_ip="192.0.2.1",
            ssh_user="ubuntu", private_key=str(self.private), public_key=str(self.public),
            cloud_key_name="remote-computer-dev", status="running",
        )

    def cost_args(self, **overrides: object) -> Namespace:
        values: dict[str, object] = {
            "hourly_rate_usd": "0.25",
            "max_runtime_seconds": "14400",
            "max_compute_usd": "1.50",
            "stop_at": "2026-08-21T16:00:00+00:00",
            "pricing_source": "AWS Price List API us-east-1 on-demand",
            "pricing_checked_at": "2026-08-21T12:00:00+00:00",
        }
        values.update(overrides)
        return Namespace(**values)

    def test_init_creates_private_valid_registry(self):
        rc = vm.cmd_init(Namespace(registry=str(self.registry)))
        self.assertEqual(rc, 0)
        data = json.loads(self.registry.read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["vms"], [])
        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o600)

    def test_upsert_preserves_created_at(self):
        vm.cmd_upsert(self.upsert_args())
        first = json.loads(self.registry.read_text())["vms"][0]
        args = self.upsert_args()
        args.status = "stopped"
        vm.cmd_upsert(args)
        records = json.loads(self.registry.read_text())["vms"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["created_at"], first["created_at"])
        self.assertEqual(records[0]["status"], "stopped")
        self.assertNotIn("test-private-placeholder", self.registry.read_text())

    def test_plan_cost_uses_exact_decimal_math(self):
        output = io.StringIO()
        args = Namespace(
            hourly_rate_usd="0.0832",
            max_runtime_hours="8",
            max_compute_usd="1.00",
            start_at="2026-08-21T12:00:00+00:00",
        )
        with mock.patch.object(vm, "utc_now", return_value=self.pricing_now), redirect_stdout(output):
            rc = vm.cmd_plan_cost(args)
        self.assertEqual(rc, 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["projected_compute_usd"], "0.6656")
        self.assertEqual(plan["remaining_compute_usd"], "0.3344")
        self.assertEqual(plan["stop_at"], "2026-08-21T20:00:00+00:00")
        self.assertTrue(plan["within_budget"])

    def test_plan_cost_rejects_projection_over_budget(self):
        args = Namespace(
            hourly_rate_usd="0.25",
            max_runtime_hours="8",
            max_compute_usd="1.00",
            start_at="2026-08-21T12:00:00+00:00",
        )
        with mock.patch.object(vm, "utc_now", return_value=self.pricing_now):
            with self.assertRaisesRegex(ValueError, "exceeds the compute budget"):
                vm.cmd_plan_cost(args)

    def test_plan_cost_honors_sixty_second_minimum_billing(self):
        args = Namespace(
            hourly_rate_usd="3.6",
            max_runtime_hours="0.0025",
            max_compute_usd="0.01",
            start_at="2026-08-21T12:00:00+00:00",
        )
        with mock.patch.object(vm, "utc_now", return_value=self.pricing_now):
            with self.assertRaisesRegex(ValueError, "exceeds the compute budget"):
                vm.cmd_plan_cost(args)

    def test_plan_cost_accepts_exact_common_runtime_seconds(self):
        for seconds, expected_stop in (
            ("60", "2026-08-21T12:01:00+00:00"),
            ("300", "2026-08-21T12:05:00+00:00"),
        ):
            with self.subTest(seconds=seconds):
                output = io.StringIO()
                args = Namespace(
                    hourly_rate_usd="0.60",
                    max_runtime_hours=None,
                    max_runtime_seconds=seconds,
                    max_compute_usd="0.05",
                    start_at="2026-08-21T12:00:00+00:00",
                )
                with mock.patch.object(vm, "utc_now", return_value=self.pricing_now), redirect_stdout(output):
                    rc = vm.cmd_plan_cost(args)
                plan = json.loads(output.getvalue())
                self.assertEqual(rc, 0)
                self.assertEqual(plan["max_runtime_seconds"], seconds)
                self.assertEqual(plan["stop_at"], expected_stop)

    def test_plan_cost_never_rounds_a_repeating_minimum_charge_down(self):
        args = Namespace(
            hourly_rate_usd="2",
            max_runtime_hours="0.0025",
            max_compute_usd="0.03333333333333333333333333333",
            start_at="2026-08-21T12:00:00+00:00",
        )
        with mock.patch.object(vm, "utc_now", return_value=self.pricing_now):
            with self.assertRaisesRegex(ValueError, "exceeds the compute budget"):
                vm.cmd_plan_cost(args)

    def test_plan_cost_rejects_unsupported_decimal_precision_cleanly(self):
        args = Namespace(
            hourly_rate_usd="0." + ("1" * 4301),
            max_runtime_hours="1",
            max_runtime_seconds=None,
            max_compute_usd="1",
            start_at="2026-08-21T12:00:00+00:00",
        )
        with mock.patch.object(vm, "utc_now", return_value=self.pricing_now):
            with self.assertRaisesRegex(ValueError, "supported precision"):
                vm.cmd_plan_cost(args)

    def test_plan_cost_rejects_malformed_decimal_underscores(self):
        for malformed in ("1__0", "_1", "1_", " 1", "1 "):
            with self.subTest(malformed=malformed):
                args = Namespace(
                    hourly_rate_usd=malformed,
                    max_runtime_hours=None,
                    max_runtime_seconds="60",
                    max_compute_usd="100",
                    start_at="2026-08-21T12:00:00+00:00",
                )
                with mock.patch.object(vm, "utc_now", return_value=self.pricing_now):
                    with self.assertRaisesRegex(ValueError, "positive decimal string"):
                        vm.cmd_plan_cost(args)

    def test_supported_precision_plan_round_trips_into_registry_policy(self):
        cases = (
            ("1e128", "9e127"),
            ("0." + ("9" * 128), "1"),
            (("9" * 127) + "8e128", ("9" * 127) + "8e128"),
        )
        for rate, budget in cases:
            with self.subTest(rate=rate):
                plan = vm.cost_plan(
                    rate,
                    "60",
                    budget,
                    "2026-08-21T12:00:00+00:00",
                )
                policy = {
                    "hourly_rate_usd": plan["hourly_rate_usd"],
                    "max_runtime_seconds": plan["max_runtime_seconds"],
                    "max_compute_usd": plan["max_compute_usd"],
                    "projected_compute_usd": plan["projected_compute_usd"],
                    "stop_at": plan["stop_at"],
                    "pricing_source": "official provider pricing",
                    "pricing_checked_at": plan["start_at"],
                    "guard": vm.COST_GUARD,
                }
                self.assertEqual(vm.validate_cost_control(policy), policy)

    def test_plan_cost_rejects_stale_pricing_time(self):
        args = Namespace(
            hourly_rate_usd="0.25",
            max_runtime_hours="1",
            max_compute_usd="1.00",
            start_at="2026-08-20T11:59:59+00:00",
        )
        with mock.patch.object(vm, "utc_now", return_value=self.pricing_now):
            with self.assertRaisesRegex(ValueError, "pricing check is stale"):
                vm.cmd_plan_cost(args)

    def test_cost_control_round_trip_is_additive_and_legacy_compatible(self):
        data = vm.empty_registry()
        data["github_auth"] = [{
            "host": "github.com",
            "account": "octocat",
            "key_pair": {
                "private_path": "/tmp/github-key",
                "public_path": "/tmp/github-key.pub",
            },
            "source": "generated",
            "fingerprint": "SHA256:test-fingerprint",
            "title": "remote-computer shared octocat",
            "created_at": "2026-08-21T12:00:00+00:00",
            "installed_on": [],
        }]
        vm.save_registry(self.registry, data)
        legacy_args = self.upsert_args(vm_id="legacy")
        vm.cmd_upsert(legacy_args)
        controlled_args = self.upsert_args(vm_id="controlled")
        for key, value in vars(self.cost_args()).items():
            setattr(controlled_args, key, value)
        with mock.patch.object(vm, "utc_now", return_value=self.pricing_now):
            vm.cmd_upsert(controlled_args)
        data = vm.load_registry(self.registry)
        records = {record["id"]: record for record in data["vms"]}
        self.assertNotIn("cost_control", records["legacy"])
        policy = records["controlled"]["cost_control"]
        self.assertEqual(policy["hourly_rate_usd"], "0.25")
        self.assertEqual(policy["projected_compute_usd"], "1")
        self.assertEqual(policy["stop_at"], "2026-08-21T16:00:00+00:00")
        self.assertEqual(data["github_auth"][0]["account"], "octocat")
        self.assertEqual(data["version"], 1)

    def test_partial_cost_control_is_rejected_without_overwriting_registry(self):
        vm.cmd_upsert(self.upsert_args())
        before = self.registry.read_text()
        args = self.upsert_args()
        args.hourly_rate_usd = "0.25"
        with self.assertRaisesRegex(ValueError, "cost-control fields must be supplied together"):
            vm.cmd_upsert(args)
        self.assertEqual(self.registry.read_text(), before)

    def test_stale_cost_control_is_rejected_without_overwriting_registry(self):
        vm.cmd_upsert(self.upsert_args())
        before = self.registry.read_text()
        args = self.upsert_args()
        stale = self.cost_args(
            pricing_checked_at="2026-08-20T11:59:59+00:00",
            stop_at="2026-08-20T15:59:59+00:00",
        )
        for key, value in vars(stale).items():
            setattr(args, key, value)
        with mock.patch.object(vm, "utc_now", return_value=self.pricing_now):
            with self.assertRaisesRegex(ValueError, "pricing check is stale"):
                vm.cmd_upsert(args)
        self.assertEqual(self.registry.read_text(), before)

    def test_inconsistent_stop_deadline_is_rejected_without_overwriting_registry(self):
        vm.cmd_upsert(self.upsert_args())
        before = self.registry.read_text()
        args = self.upsert_args()
        for key, value in vars(self.cost_args(stop_at="2026-08-21T16:00:01+00:00")).items():
            setattr(args, key, value)
        with mock.patch.object(vm, "utc_now", return_value=self.pricing_now):
            with self.assertRaisesRegex(ValueError, "must equal pricing_checked_at"):
                vm.cmd_upsert(args)
        self.assertEqual(self.registry.read_text(), before)

    def test_registry_rejects_invalid_cost_control(self):
        data = vm.empty_registry()
        args = self.upsert_args()
        for key, value in vars(self.cost_args()).items():
            setattr(args, key, value)
        with mock.patch.object(vm, "utc_now", return_value=self.pricing_now):
            vm.cmd_upsert(args)
        data = json.loads(self.registry.read_text())
        data["vms"][0]["cost_control"]["max_compute_usd"] = "-1"
        with self.assertRaisesRegex(ValueError, "positive decimal"):
            vm.validate_registry(data)

    def test_render_cost_guard_writes_absolute_systemd_timer(self):
        output_path = self.root / "cost-guard.sh"
        rc = vm.cmd_render_cost_guard(Namespace(
            stop_at="2026-08-21T20:00:00+00:00",
            output=str(output_path),
        ))
        self.assertEqual(rc, 0)
        rendered = output_path.read_text()
        self.assertIn("OnCalendar=2026-08-21 20:00:00 UTC", rendered)
        self.assertIn("Persistent=true", rendered)
        self.assertIn("deadline_epoch=1787342400", rendered)
        self.assertIn("ExecStart=/usr/local/sbin/remote-computer-cost-guard-check", rendered)
        self.assertIn("WantedBy=multi-user.target", rendered)
        self.assertIn("systemctl enable remote-computer-cost-guard.service", rendered)
        self.assertIn("systemctl enable --now remote-computer-cost-guard.timer", rendered)
        self.assertIn("exec /sbin/shutdown -h now", rendered)
        syntax = subprocess.run(["sh", "-n", str(output_path)], capture_output=True, text=True, check=False)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        with self.assertRaises(FileExistsError):
            vm.cmd_render_cost_guard(Namespace(
                stop_at="2026-08-21T20:00:00+00:00",
                output=str(output_path),
            ))
        invalid_path = self.root / "invalid-cost-guard.sh"
        with self.assertRaisesRegex(ValueError, "must include a UTC offset"):
            vm.cmd_render_cost_guard(Namespace(
                stop_at="2026-08-21T20:00:00",
                output=str(invalid_path),
            ))
        self.assertFalse(invalid_path.exists())

    def test_render_cost_guard_refuses_a_dangling_output_symlink(self):
        target = self.root / "unexpected-created.sh"
        output_path = self.root / "requested-output.sh"
        output_path.symlink_to(target)
        with self.assertRaises(FileExistsError):
            vm.cmd_render_cost_guard(Namespace(
                stop_at="2026-08-21T20:00:00+00:00",
                output=str(output_path),
            ))
        self.assertFalse(target.exists())

    def test_historical_cost_policy_remains_a_valid_audit_record(self):
        data = vm.empty_registry()
        args = self.upsert_args()
        for key, value in vars(self.cost_args()).items():
            setattr(args, key, value)
        with mock.patch.object(vm, "utc_now", return_value=self.pricing_now):
            vm.cmd_upsert(args)
        data = json.loads(self.registry.read_text())
        data["vms"][0]["cost_control"]["pricing_checked_at"] = "2020-01-01T00:00:00+00:00"
        data["vms"][0]["cost_control"]["stop_at"] = "2020-01-01T04:00:00+00:00"
        vm.validate_registry(data)

    def test_cost_status_reports_active_expired_stopped_and_uncontrolled(self):
        controlled = self.upsert_args(vm_id="active")
        for key, value in vars(self.cost_args()).items():
            setattr(controlled, key, value)
        output = io.StringIO()
        with mock.patch.object(
            vm,
            "utc_now",
            return_value=self.pricing_now,
        ):
            vm.cmd_upsert(controlled)
            expired = self.upsert_args(vm_id="expired")
            expired_policy = self.cost_args(
                pricing_checked_at="2026-08-21T07:00:00+00:00",
                stop_at="2026-08-21T11:00:00+00:00",
            )
            for key, value in vars(expired_policy).items():
                setattr(expired, key, value)
            vm.cmd_upsert(expired)
            stopped = self.upsert_args(vm_id="stopped")
            stopped.status = "stopped"
            for key, value in vars(expired_policy).items():
                setattr(stopped, key, value)
            vm.cmd_upsert(stopped)
            stopped_active = self.upsert_args(vm_id="stopped-active")
            stopped_active.status = "stopped"
            for key, value in vars(self.cost_args()).items():
                setattr(stopped_active, key, value)
            vm.cmd_upsert(stopped_active)
            vm.cmd_upsert(self.upsert_args(vm_id="uncontrolled"))
            before_status = self.registry.read_text()
            with redirect_stdout(output):
                rc = vm.cmd_cost_status(Namespace(registry=str(self.registry)))
            self.assertEqual(self.registry.read_text(), before_status)
        self.assertEqual(rc, 0)
        statuses = {item["id"]: item["cost_status"] for item in json.loads(output.getvalue())["vms"]}
        self.assertEqual(statuses, {
            "active": "active",
            "expired": "expired-running",
            "stopped": "expired-stopped",
            "stopped-active": "stopped",
            "uncontrolled": "uncontrolled",
        })

    def test_cost_status_treats_gcp_pending_stop_as_cost_incurring(self):
        record = self.upsert_args(provider="gcp", vm_id="pending-stop", location="us-central1-a")
        record.status = "PENDING_STOP"
        for key, value in vars(self.cost_args(
            pricing_checked_at="2026-08-21T07:00:00+00:00",
            stop_at="2026-08-21T11:00:00+00:00",
        )).items():
            setattr(record, key, value)
        with mock.patch.object(vm, "utc_now", return_value=self.pricing_now):
            vm.cmd_upsert(record)
            data = vm.load_registry(self.registry)
            status = vm.cost_status(data["vms"][0], self.pricing_now)
        self.assertEqual(status["cost_status"], "expired-running")

    def test_invalid_registry_is_not_overwritten(self):
        self.registry.write_text("not json")
        with self.assertRaises(ValueError):
            vm.load_registry(self.registry)
        self.assertEqual(self.registry.read_text(), "not json")

    def test_sync_updates_aws_and_gcp_from_mocked_clis(self):
        vm.cmd_upsert(self.upsert_args())
        vm.cmd_upsert(self.upsert_args(provider="gcp", vm_id="gcp-dev", location="us-central1-a"))
        aws_payload = {"Reservations": [{"Instances": [{"State": {"Name": "stopped"}, "PublicIpAddress": "192.0.2.10"}]}]}
        gcp_payload = {"status": "RUNNING", "networkInterfaces": [{"accessConfigs": [{"natIP": "192.0.2.20"}]}]}
        with mock.patch.object(vm, "command_path", return_value="/mock/bin"), mock.patch.object(vm, "run_json", side_effect=[aws_payload, gcp_payload]):
            rc = vm.cmd_sync(Namespace(registry=str(self.registry)))
        self.assertEqual(rc, 0)
        records = {(x["provider"], x["id"]): x for x in json.loads(self.registry.read_text())["vms"]}
        self.assertEqual(records[("aws", "i-test")]["status"], "stopped")
        self.assertEqual(records[("aws", "i-test")]["public_ip"], "192.0.2.10")
        self.assertEqual(records[("gcp", "gcp-dev")]["status"], "running")
        self.assertEqual(records[("gcp", "gcp-dev")]["public_ip"], "192.0.2.20")

    def test_sync_error_preserves_previous_record(self):
        vm.cmd_upsert(self.upsert_args())
        before = json.loads(self.registry.read_text())["vms"][0]
        with mock.patch.object(vm, "command_path", return_value="/mock/aws"), mock.patch.object(vm, "run_json", side_effect=RuntimeError("denied")):
            rc = vm.cmd_sync(Namespace(registry=str(self.registry)))
        after = json.loads(self.registry.read_text())["vms"][0]
        self.assertEqual(rc, 1)
        self.assertEqual(after, before)

    def test_clipboard_fallback_prints_command(self):
        with mock.patch.object(vm, "clipboard_tool", return_value=None), mock.patch("builtins.print") as output:
            rc = vm.cmd_copy_ssh(Namespace(command="ssh example"))
        self.assertEqual(rc, 2)
        self.assertTrue(any(call.args and call.args[0] == "ssh example" for call in output.call_args_list))

    @unittest.skipUnless(Path("/usr/bin/ssh-keygen").exists() or Path("/bin/ssh-keygen").exists(), "ssh-keygen unavailable")
    def test_create_key_generates_unencrypted_pair_without_overwrite(self):
        key_path = self.root / "keys" / "dev"
        rc = vm.cmd_create_key(Namespace(name="dev", path=str(key_path)))
        self.assertEqual(rc, 0)
        self.assertTrue(key_path.is_file())
        self.assertTrue(Path(f"{key_path}.pub").is_file())
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            vm.cmd_create_key(Namespace(name="dev", path=str(key_path)))


if __name__ == "__main__":
    unittest.main()

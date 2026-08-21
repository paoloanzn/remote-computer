# Task: Bound VM compute cost before and after provisioning

- ID: `cost-controls-20260821`
- Status: `ACCEPTED`
- Baseline: `docs/agentic/BASELINE.md` at `6f109bc2cd9849306dd12217b6665eeb47f48ab9`
- Base commit: `6f109bc2cd9849306dd12217b6665eeb47f48ab9`
- Risk: `HIGH`

## Objective

A user can set an explicit USD compute budget and maximum runtime for one uninterrupted provider billing lifecycle before provisioning, reject a plan that exceeds that single-lifecycle budget, install an absolute instance-side auto-stop deadline, record the policy with the VM, and audit the recorded deadline without relying on remembered prices or an open local session.

## Non-goals

- Claim a cap on the total cloud bill.
- Claim that a guest-side guard can cap compute charges after a provider stop/start, repeated restart attempt, instance-type change, or other out-of-band lifecycle mutation.
- Hard-code provider prices or Free Tier eligibility.
- Query AWS Cost Explorer, AWS Budgets, Google Cloud Billing, or organization billing exports.
- Terminate instances, delete disks, delete IPs, or clean up provider resources.
- Add support for non-systemd images.
- Provision or mutate a real cloud resource while developing this feature.

## Assumptions and unknowns

- **ASSUMED:** "Cost control" means a conservative compute-spend admission gate plus an unattended stop deadline. Provider billing reconciliation can be added separately if needed.
- **ASSUMED:** The modeled run is one uninterrupted provider billing lifecycle. Every stop/start begins another provider minimum and requires a fresh admission before start. Repeated out-of-band starts can add unbounded provider minimums that a guest-side guard cannot prevent.
- **ASSUMED:** The operator supplies a current on-demand hourly compute rate from an official provider pricing source. The skill records the source and check time instead of treating a repository constant as current.
- **ASSUMED:** The declared Ubuntu 24.04 LTS default has systemd. Custom images must prove systemd compatibility or proceed without claiming the auto-stop guard is installed.
- **UNKNOWN:** Provider discounts, credits, taxes, disks, snapshots, network transfer, public IPs, premium images, and other services are not computable from the selected instance rate alone.

## Invariants and protected surfaces

- Existing schema-version-1 registries remain valid without migration.
- Existing VM records retain their shape unless cost control is explicitly supplied.
- Dollar arithmetic uses decimal values, never binary floating point, and a displayed single-lifecycle projection never rounds below the exact rational provider charge.
- Projected compute honors the providers' 60-second minimum billable duration even when the requested runtime is shorter.
- Runtime can be supplied as exact whole seconds, including common 60-second and five-minute deadlines. Hours remain a convenience for values that convert exactly to whole seconds.
- Decimal inputs have an explicit supported precision bound and fail with a controlled validation error outside it.
- A partial cost policy is rejected. Rate, runtime, budget, deadline, pricing source, and pricing check time travel together.
- The calculated single-lifecycle runtime projection must fit within the compute budget before provider creation is authorized.
- The shutdown deadline is absolute UTC. An enabled systemd timer stops the first run at the deadline, and an enabled boot-time check stops any later restart after the deadline.
- Generated startup data contains no credential or private-key material, and its local output is created exclusively without following an existing final-path symlink.
- The feature stops a VM only. It never terminates or deletes cloud resources.
- Existing explicit confirmation immediately before billable resource creation remains mandatory.
- Acceptance tests and the unified quality gate are protected after this contract reaches `READY`.

## Affected surfaces

- `scripts/vm_bookkeeper.py`: decimal plan validation, cost policy registry metadata, guard renderer, and status reporting.
- `tests/test_vm_bookkeeper.py`: deterministic plan, validation, legacy compatibility, renderer, and reporting tests.
- `tests/test_skill_content.py`: workflow and provider-reference contract checks.
- `SKILL.md`, `README.md`, `references/aws.md`, `references/gcp.md`, `references/registry.md`: user workflow, limitations, provider wiring, and audit documentation.
- `check`: one repository-local command matching the CI gate. It stays outside the installed skill package.
- `.github/workflows/ci.yml`: packaging assertions matching the root gate's runtime-only script boundary.

## Acceptance contract

| Criterion | Evidence | Initial state |
|---|---|---|
| A valid rate, exact whole-second runtime or exactly convertible hour runtime, and compute budget produce a non-underestimating single-lifecycle decimal projection, remaining headroom, and absolute UTC stop time, including one AWS or GCP 60-second minimum. Repeating provider charges must not round down into an approval. Documentation must state that every stop/start is a new billing lifecycle requiring fresh admission. | Unit test plus `plan-cost` CLI smoke and protected content check. | `RED` |
| A projected compute amount above the requested budget exits nonzero before provisioning. | Unit test plus over-budget CLI smoke. | `RED` |
| Missing, negative, zero, malformed, inconsistent, unsupported-precision, or stale admission fields are rejected without overwriting the registry. Historical persisted pricing timestamps remain valid audit records after their freshness window passes. | Unit tests against `validate_registry` and `upsert`. | `RED` |
| A cost-controlled VM record preserves all existing VM and GitHub metadata while adding one optional schema-version-1 `cost_control` object. Legacy registries still validate. | Round-trip unit tests and fixture inspection. | `RED` |
| The generated cloud-init-compatible shell installs an enabled systemd timer with the exact absolute UTC deadline and an enabled boot-time deadline check, so a manual restart after expiry stops again. It refuses invalid deadlines and creates the output exclusively without following an existing final-path symlink. | Renderer unit tests plus shell syntax smoke on generated output. | `RED` |
| `cost-status` distinguishes active, expired, stopped, and uncontrolled records without calling a provider or mutating the registry, and treats GCP `PENDING_STOP` as still incurring compute charges. | Unit tests plus CLI smoke on a temporary registry. | `RED` |
| AWS and GCP workflows require a fresh official hourly rate, show compute scope and excluded cost classes, pass the generated guard at creation, verify the timer, and include the policy in final handoff. | Protected content tests and documentation inspection. | `RED` |
| One local gate reproduces CI checks and the packaged skill contains every runtime file needed for cost control without development-only files, including `scripts/quick_validate.py`. | `./check` and installer test. | `RED` |

## Validation commands

- `python3 -m unittest tests.test_vm_bookkeeper -v`
- `python3 -m unittest discover -s tests -v`
- `python3 scripts/quick_validate.py .`
- `npx --yes pyright@1.1.413 --level error`
- `sh -n install-skill.sh`
- `./check`
- Temporary-registry CLI journey covering `plan-cost`, `render-cost-guard`, `upsert`, and `cost-status`

## Rollback

Revert the accepted feature commit. Existing registries remain schema version 1 and legacy records are unchanged. Cost-controlled records contain only additive metadata, so an older helper will ignore those fields when loading and will preserve them only if it does not upsert that VM. Cloud instances already created with the generated timer must have that timer disabled explicitly on the VM if the operator intends to remove the deadline. Rollback never deletes provider resources.

## Progress and decisions

| Time or commit | State | Evidence or decision |
|---|---|---|
| 2026-08-21T11:49:38+05:00 | `READY` | Baseline complete. Scope fixed to compute admission plus absolute auto-stop, with total-bill costs explicitly excluded. |
| 2026-08-21T12:03:49+05:00 | `VERIFYING` | Protected tests are green. The unified gate passed 35 tests, strict Pyright, skill validation, shell syntax, installer packaging, and source-to-package diffs. A local CLI journey proved admission, rejection, rendering, registry round-trip, and status output without cloud calls. |
| 2026-08-21T12:12:00+05:00 | `DRAFT` | Reopened the protected guard surface after review found a restart bypass: EC2 user data normally runs once, so an expired stopped VM could be started after the one-shot timer had already fired. The contract now requires an enabled boot-time deadline check as well as the timer. |
| 2026-08-21T12:13:00+05:00 | `READY` | Added and demonstrated the failing restart-protection test. The revised criterion is falsifiable and implementation may resume. |
| 2026-08-21T12:15:00+05:00 | `VERIFYING` | Implemented the persistent boot-time expiry check and kept the absolute timer. All 37 local tests pass, including the restart-protection contract. |
| 2026-08-21T12:17:00+05:00 | `VERIFYING` | Reclassified risk from medium to high because the feature adds a public CLI and future billable-infrastructure control. No live cloud mutation occurred. Added an independent cloud-safety specialist review. |
| 2026-08-21T12:27:00+05:00 | `DRAFT` | Reopened the protected billing model after specialist review found two provider edge cases: AWS and GCP have a 60-second minimum billable duration, and GCP continues CPU and memory charges in `PENDING_STOP`. Official provider documentation confirmed both findings. |
| 2026-08-21T12:30:00+05:00 | `READY` | Added and demonstrated failing tests for minimum billing, `PENDING_STOP` classification, and provider-reference coverage. The revised protected criteria are ready for implementation. |
| 2026-08-21T12:33:00+05:00 | `VERIFYING` | Implemented provider-minimum billing math, cost-incurring GCP transition-state classification, and official provider documentation. All three new targeted tests pass. |
| 2026-08-21T12:46:00+05:00 | `DRAFT` | Reopened after fresh evaluation proved a repeating 60-second charge could round down under the default decimal context and the installed skill still contained the development-only quick validator. Both findings are in scope and reproducible. |
| 2026-08-21T12:49:00+05:00 | `READY` | Added and demonstrated failing tests for non-underestimating repeating-charge admission and exclusion of the quick validator from the installed runtime package. |
| 2026-08-21T12:53:00+05:00 | `VERIFYING` | Implemented exact finite multiplication, conservative upward rendering for repeating minimum charges, and explicit runtime-script packaging. All five focused math, registry, and installer tests pass. |
| 2026-08-21T13:05:00+05:00 | `DRAFT` | Reopened after final review found that common whole-second deadlines lacked an exact input path, dangling output symlinks bypassed overwrite refusal, oversized decimal input leaked an interpreter limit, and CI still expected the intentionally excluded validator. Clarified that pricing freshness governs admission, not the validity of historical audit records. |
| 2026-08-21T13:09:00+05:00 | `READY` | Added and demonstrated failing tests for exact 60-second and five-minute inputs, controlled precision rejection, dangling-symlink refusal, and CI runtime-package parity. Added a passing regression proving historical records remain valid audit data. |
| 2026-08-21T13:15:00+05:00 | `VERIFYING` | Added canonical whole-second runtime metadata with hours as an exactly convertible convenience, bounded decimal precision, exclusive no-follow guard creation, and CI parity with runtime-only packaging. All 45 tests pass. |
| 2026-08-21T13:27:00+05:00 | `DRAFT` | Reopened after release review found that repeated provider stop/start cycles add a fresh minimum that a guest-only guard cannot bound, Decimal accepted underscore syntax, and the advertised maximum precision did not round-trip through expanded registry strings. Narrowed the cost claim to one uninterrupted billing lifecycle and made fresh admission mandatory before any later start. |
| 2026-08-21T13:30:00+05:00 | `READY` | Added and demonstrated failing tests for strict decimal grammar, maximum-precision plan-to-registry round-trip, and the single-lifecycle boundary across the skill, README, both provider references, and registry documentation. |
| 2026-08-21T13:35:00+05:00 | `VERIFYING` | Enforced a plain decimal grammar, normalized precision accounting with extra generated-projection capacity, and explicit single-lifecycle limits across every user and provider surface. All three focused regressions pass. |
| 2026-08-21T13:44:00+05:00 | `DRAFT` | Reopened after final acceptance review found that a supported 128-place fractional rate can generate a valid 130-place projection rejected by the input exponent bound, and numeric strings with surrounding whitespace were normalized rather than rejected. |
| 2026-08-21T13:46:00+05:00 | `READY` | Extended the protected numeric tests to the negative-exponent round-trip boundary and leading/trailing whitespace. Both defects reproduce before implementation. |
| 2026-08-21T13:48:00+05:00 | `VERIFYING` | Made numeric grammar whitespace-strict and separated admission exponent limits from derived projection capacity. Both final numeric regressions pass. |
| 2026-08-21T13:54:00+05:00 | `DRAFT` | Reopened after acceptance review combined the maximum 128-digit coefficient with exponent +128 and produced a valid 284-digit conservative projection that exceeded the separately guessed generated-field digit allowance. |
| 2026-08-21T13:55:00+05:00 | `READY` | Added and demonstrated the exact maximum-coefficient and maximum-exponent plan-to-registry regression. |
| 2026-08-21T13:56:00+05:00 | `VERIFYING` | Derived generated projection capacity from coefficient digits, exponent magnitude, and conservative rounding places. All supported precision-boundary plans now round-trip. |
| 2026-08-21T14:13:00+05:00 | `ACCEPTED` | Unified gate passed 48 tests, strict Pyright, metadata validation, shell syntax, CI parity, and runtime-only packaging. Fresh release evaluator and cloud-safety specialist both returned ACCEPT. |

## Completion receipt

- Accepted checkpoint: `the commit containing this receipt`
- Unified gate: `REMOTE_COMPUTER_PYTHON=/private/tmp/remote-computer-check-venv/bin/python ./check` passed 48 tests, strict Pyright with zero errors, skill metadata validation, shell syntax, installer execution, and source-to-package comparisons.
- Runtime demonstration: `plan-cost` projected `0.6656` USD with `0.3344` USD headroom for 28,800 seconds at `0.0832` USD/hour. A repeating 60-second charge rounded upward to `0.0333333333333333333333333334` USD and rejected a lower budget. `/private/tmp/rc-cost-guard-release-20260821.sh` was created with mode `0600` and passed `sh -n`.
- Evaluator verdict: `ACCEPT`. The final public-CLI precision repro planned and upserted successfully, a 600-case supported input sweep round-tripped, and the required gate passed.
- Cloud-safety verdict: `ACCEPT`. Provider billing and lifecycle claims, the single-lifecycle boundary, stop behavior, boot protection, GCP cost-incurring states, T3 credit control, startup wiring, secret handling, and package boundaries passed review.
- Reconciled findings: added boot-time expiry protection after the original timer-only restart bypass; honored provider 60-second minimums; classified GCP `PENDING_STOP`, `SUSPENDING`, and `SUSPENDED` conservatively; made repeating projections non-underestimating; excluded the development validator from runtime packaging and aligned CI; added exact seconds; made guard creation exclusive and symlink-safe; bounded and strictly parsed decimal inputs; derived generated projection capacity from supported input bounds; and narrowed the budget claim to one uninterrupted provider billing lifecycle.
- Residual risk: live provider behavior is not exercised locally. A provider stop/start begins another billing lifecycle and minimum charge, so every later start requires a fresh admission. Repeated or out-of-band starts, disks, IPs, network transfer, premium images, taxes, credits, discounts, and other services can exceed the recorded projection.
- Untested surfaces: live AWS and GCP creation, real cloud-init and Google startup-script execution, actual systemd deadline firing, and observed provider stop and billing results.
- Next action: none.

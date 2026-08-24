#!/usr/bin/env python3
"""Configure an imported OpenPGP signing key for interactive use on a VM."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


FINGERPRINT_RE = re.compile(r"^(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})$")
AGENT_START = "# BEGIN remote-computer gpg-agent"
AGENT_END = "# END remote-computer gpg-agent"
TTY_START = "# BEGIN remote-computer gpg-tty"
TTY_END = "# END remote-computer gpg-tty"


def run(command: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(message)
    return result.stdout.strip()


def replace_managed_block(
    path: Path,
    start: str,
    end: str,
    body: list[str],
    mode: int | None = None,
) -> None:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    target_mode = mode if mode is not None else ((path.stat().st_mode & 0o777) if path.exists() else 0o600)
    kept: list[str] = []
    inside = False
    for line in original.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == start:
            if inside:
                raise RuntimeError(f"nested managed block in {path}")
            inside = True
            continue
        if stripped == end:
            if not inside:
                raise RuntimeError(f"unexpected managed block terminator in {path}")
            inside = False
            continue
        if not inside:
            kept.append(line)
    if inside:
        raise RuntimeError(f"unterminated managed block in {path}")

    prefix = "".join(kept).rstrip("\n")
    block = "\n".join([start, *body, end])
    updated = f"{prefix}\n\n{block}\n" if prefix else f"{block}\n"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.remote-computer.tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, target_mode)
    os.replace(temporary, path)


def check_pinentry_config(agent_config: Path, pinentry: str) -> None:
    original = agent_config.read_text(encoding="utf-8") if agent_config.exists() else ""
    inside = False
    for line in original.splitlines():
        stripped = line.strip()
        if stripped == AGENT_START:
            if inside:
                raise RuntimeError(f"nested managed block in {agent_config}")
            inside = True
            continue
        if stripped == AGENT_END:
            if not inside:
                raise RuntimeError(f"unexpected managed block terminator in {agent_config}")
            inside = False
            continue
        if inside or not stripped or stripped.startswith("#"):
            continue
        field, _, value = stripped.partition(" ")
        if field == "pinentry-program" and value.strip() != pinentry:
            raise RuntimeError(
                f"refusing to override unmanaged pinentry-program in {agent_config}: {value.strip()}"
            )
    if inside:
        raise RuntimeError(f"unterminated managed block in {agent_config}")


def ensure_pinentry_config(agent_config: Path, pinentry: str) -> None:
    check_pinentry_config(agent_config, pinentry)
    replace_managed_block(
        agent_config,
        AGENT_START,
        AGENT_END,
        [f"pinentry-program {pinentry}"],
        mode=0o600,
    )


def configure_tty_startup(path: Path) -> None:
    replace_managed_block(path, TTY_START, TTY_END, [
        'if command -v tty >/dev/null 2>&1 && test -t 0; then',
        '  export GPG_TTY="$(tty)"',
        '  if command -v gpg-connect-agent >/dev/null 2>&1; then',
        '    gpg-connect-agent updatestartuptty /bye >/dev/null 2>&1 || true',
        '  fi',
        'fi',
    ])


def secret_fingerprints(gpg: str, selector: str) -> list[str]:
    output = run([gpg, "--batch", "--with-colons", "--fingerprint", "--list-secret-keys", selector])
    fingerprints: list[str] = []
    awaiting_primary_fingerprint = False
    for line in output.splitlines():
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


def configure_git(git: str, gpg: str, fingerprint: str, commit_gpgsign: str | None) -> None:
    run([git, "config", "--global", "gpg.program", gpg])
    run([git, "config", "--global", "gpg.format", "openpgp"])
    run([git, "config", "--global", "user.signingkey", fingerprint])
    if commit_gpgsign is not None:
        run([git, "config", "--global", "commit.gpgsign", commit_gpgsign])


def git_config(git: str, key: str) -> str | None:
    result = subprocess.run(
        [git, "config", "--global", "--get", key],
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or f"could not inspect remote Git config {key}")
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def check_existing_signing_config(git: str, fingerprint: str) -> None:
    signing_format = git_config(git, "gpg.format")
    if signing_format and signing_format.lower() != "openpgp":
        raise RuntimeError(f"refusing to replace remote gpg.format={signing_format}")
    signing_key = git_config(git, "user.signingkey")
    if signing_key:
        selector = signing_key.upper().removeprefix("0X").removesuffix("!")
        if not fingerprint.endswith(selector):
            raise RuntimeError(f"refusing to replace remote user.signingkey={signing_key}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--fingerprint", required=True)
    root.add_argument("--commit-gpgsign", choices=("true", "false"))
    root.add_argument("--preflight", action="store_true")
    return root


def main() -> int:
    args = parser().parse_args()
    fingerprint = args.fingerprint.upper()
    if not FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError("OpenPGP fingerprint must be 40 or 64 hexadecimal characters")

    gpg = shutil.which("gpg")
    gpgconf = shutil.which("gpgconf")
    gpg_connect_agent = shutil.which("gpg-connect-agent")
    git = shutil.which("git")
    pinentry = shutil.which("pinentry-curses")
    if not gpg or not gpgconf or not gpg_connect_agent or not git or not pinentry:
        raise RuntimeError(
            "git, gpg, gpgconf, gpg-connect-agent, and pinentry-curses must be installed before GPG setup"
        )

    gnupg = Path.home() / ".gnupg"
    agent_config = gnupg / "gpg-agent.conf"
    check_pinentry_config(agent_config, pinentry)
    check_existing_signing_config(git, fingerprint)
    if args.preflight:
        print(json.dumps({
            "fingerprint": fingerprint,
            "pinentry_program": pinentry,
            "preflight": True,
        }, sort_keys=True))
        return 0

    gnupg.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(gnupg, 0o700)
    fingerprints = secret_fingerprints(gpg, fingerprint)
    if fingerprints != [fingerprint]:
        raise RuntimeError("the imported OpenPGP secret-key fingerprint could not be verified exactly")

    ensure_pinentry_config(agent_config, pinentry)
    startup_files = [Path.home() / ".profile", Path.home() / ".bashrc"]
    for startup_file in startup_files:
        configure_tty_startup(startup_file)
    configure_git(git, gpg, fingerprint, args.commit_gpgsign)
    run([gpgconf, "--kill", "gpg-agent"])

    print(json.dumps({
        "fingerprint": fingerprint,
        "git_program": git,
        "gpg_agent_config": str(agent_config),
        "gpg_connect_agent": gpg_connect_agent,
        "gpg_program": gpg,
        "pinentry_program": pinentry,
        "startup_files": [str(path) for path in startup_files],
        "verified": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)

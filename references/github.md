# GitHub configuration porting

Use this reference only when the user asks to port Git identity, GitHub authentication, or commit signing to a VM. These are three independent concerns:

- Git identity: `user.name` and `user.email` in Git configuration.
- GitHub authentication: an SSH authentication key or HTTPS token used for fetch/push.
- Commit signing: OpenPGP or SSH signing configured through `user.signingkey`, `gpg.format`, and `commit.gpgsign`.

Never replace or remove signing configuration while adding an authentication key.

## 1. Discover without exposing credentials

Run locally:

```bash
python3 scripts/vm_bookkeeper.py github-discover --host github.com --verify-ssh
```

The helper inspects non-secret Git configuration, matching GPG fingerprints, `gh auth status`, effective SSH identity paths, SSH-agent fingerprints, and public GitHub key fingerprints when authenticated `gh` access is available. With `--verify-ssh`, it also probes candidate keys using strict host-key verification and reports GitHub's authenticated login greeting. It never reads key contents, tokens, credential-store values, or `~/.git-credentials`. If the GitHub host key is not already trusted locally, the probe reports that condition rather than disabling verification or modifying `known_hosts`.

Show the user:

- intended GitHub host/account;
- Git name/email;
- signing format, configured key ID/fingerprint, and whether the secret signing key is locally available;
- candidate SSH paths/fingerprints and whether GitHub returned a matching public fingerprint;
- any shared GitHub key already present in the registry;
- which private paths would be copied.

Ask for explicit confirmation before copying a private SSH key or GPG secret key.

Do not copy HTTPS tokens or platform credential stores. If local authentication is HTTPS-only, create/use the shared SSH flow below or run an interactive target-side login when the user explicitly chooses HTTPS.

## 2. Select or create one shared GitHub authentication key

### Existing verified SSH key

Use an existing pair only when its public fingerprint matches a key returned for the intended active GitHub account and the user confirms reuse across VMs:

```bash
python3 scripts/vm_bookkeeper.py github-register-key \
  --host github.com --account <login> \
  --private-key <private-path> --public-key <public-path> \
  --title 'remote-computer shared <login>'
```

The helper verifies that the private/public fingerprints match. It refuses silent key rotation for an existing host/account record.

### No suitable SSH key

Create one shared local Ed25519 key, separate from all VM-access and signing keys:

```bash
python3 scripts/vm_bookkeeper.py github-create-key \
  --host github.com --account <login> \
  --title 'remote-computer shared <login>'
```

The default private path is `~/.ssh/remote-computer/github/github.com-<login>`, mode `0600`. The key has an empty passphrase for unattended Git operations. Warn that compromise of any VM receiving this shared account-level key exposes the key's GitHub access until it is removed from GitHub. Offer a repository-scoped deploy key instead when account-wide, cross-VM access is unnecessary.

## 3. Upload only the public key with local GitHub CLI

Prefer an already authenticated local `gh` session. If unavailable, authenticate locally through the web/device flow:

```bash
gh auth login --hostname github.com --git-protocol ssh \
  --web --skip-ssh-key --scopes admin:public_key
```

`--skip-ssh-key` prevents `gh` from generating a second key. Then upload and verify the registered shared key:

```bash
python3 scripts/vm_bookkeeper.py github-upload-key \
  --host github.com --account <login>
```

The helper requires the active `gh` account to match, uploads only the `.pub` file, and re-queries GitHub for the exact fingerprint. If the session existed before this workflow, preserve it. If the agent created a temporary `gh` session solely for upload, log it out after verification:

```bash
gh auth logout --hostname github.com --user <login>
```

Do not install or authenticate `gh` on every VM merely to use Git. GitHub CLI is a bootstrap/API client here; after public-key registration, SSH performs Git authentication. A remote `gh` login is a fallback only when local `gh` cannot be used. If used, warn when credentials fall back to plaintext storage and log out after the verified upload unless the user asks to keep the session.

## 4. Install the shared key on a registered VM

After user confirmation:

```bash
python3 scripts/vm_bookkeeper.py github-install-key \
  --host github.com --account <login> \
  --provider <aws|gcp> --id <provider-native-vm-id>
```

The helper:

1. Verifies local private/public fingerprints against the registry.
2. Connects with the VM-specific access key from the VM record.
3. Refuses to overwrite an untracked remote key path.
4. Copies the shared key to `~/.ssh/remote-computer/github/` with modes `0600`/`0644`.
5. Runs `remote_github_setup.py` on the VM to re-verify both fingerprints.
6. Adds a managed `Host github.com` block without overwriting an unmanaged block.
7. Configures Git to rewrite `https://github.com/` remotes to SSH.
8. Adds an `installed_on` record only after setup succeeds.

If the VM already has an unmanaged `Host github.com` block, stop and ask before merging. Do not overwrite it.

Verify from the VM:

```bash
ssh -T -o BatchMode=yes git@github.com
git -C <repo> remote -v
git -C <repo> ls-remote origin HEAD
```

GitHub normally returns a successful authentication greeting with exit status 1 because it offers no interactive shell. Match the intended login in the greeting. Use `git push --dry-run` only when write-access verification is useful; do not perform a real push unless requested.

## 5. Port Git identity and commit signing separately

Set the discovered name/email on the VM only after user confirmation. Preserve absent values rather than inventing them.

For OpenPGP signing, first show the resolved full fingerprint and obtain explicit confirmation to copy that secret key. Then run:

```bash
python3 scripts/vm_bookkeeper.py github-install-gpg \
  --provider <aws|gcp> --id <provider-native-vm-id> \
  --fingerprint <full-openpgp-fingerprint>
```

The command is intentionally limited to apt-based Ubuntu/Debian VMs, including the default Ubuntu 24.04 image. It:

1. Verifies that the selector resolves to exactly one local OpenPGP secret-key fingerprint.
2. Installs `python3`, `git`, `gnupg`, `gpg-agent`, and `pinentry-curses` remotely as root or with non-interactive, passwordless `sudo`; it stops if the VM is not apt-based or requires an interactive sudo password.
3. Preflights the remote pinentry and Git signing configuration before transferring secret material. It refuses to replace a conflicting unmanaged `pinentry-program`, `gpg.format`, or `user.signingkey`.
4. Pipes `gpg --export-secret-keys` directly into remote `gpg --import` over the encrypted SSH connection. It never creates a plaintext export file, copies `~/.gnupg`, or includes a passphrase in an argument, log, or registry record. The local GPG agent may display the user's existing local pinentry while exporting; the user enters the passphrase there, never into the agent conversation.
5. Verifies the imported primary fingerprint exactly and preserves the key's existing passphrase protection.
6. Configures the VM's native GPG path, `user.signingkey`, `gpg.format=openpgp`, and the existing local `commit.gpgsign` value. `--commit-gpgsign true|false` may explicitly override that copied boolean.
7. Adds a managed `pinentry-program` entry for `pinentry-curses` to `~/.gnupg/gpg-agent.conf`, without deleting other agent settings or overriding a conflicting unmanaged pinentry choice.
8. Adds idempotent managed `GPG_TTY` and `gpg-connect-agent updatestartuptty` blocks to `~/.profile` and `~/.bashrc`, preserving their prior contents and file modes, then restarts `gpg-agent`.
9. Prints the separate interactive verification command.

Run the printed command in a real local terminal:

```bash
python3 scripts/vm_bookkeeper.py github-verify-gpg \
  --provider <aws|gcp> --id <provider-native-vm-id> \
  --fingerprint <full-openpgp-fingerprint>
```

Verification refuses redirected/non-terminal input, forces an SSH PTY with `-tt`, refreshes the agent's startup TTY, and creates a disposable detached signature entirely through standard input/output. For a protected key, `pinentry-curses` displays in that terminal and the user enters the existing passphrase directly. A successful test confirms that `gpg-agent` accepted the unlock and can cache it according to the agent's existing cache policy. The workflow never asks the user to send the passphrase to the agent.

For SSH commit signing (`gpg.format=ssh`), treat the signing key path as independent from GitHub authentication. Reuse the same key for both roles only when the user explicitly confirms that choice and GitHub has the public key registered with both appropriate key types.

Never unset or delete the user's existing GPG key while adding GitHub SSH authentication. Never run `github-install-gpg` for `gpg.format=ssh`; use the separate SSH signing-key procedure instead.

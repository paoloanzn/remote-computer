#!/bin/sh
set -eu

# Install/update: curl -fsSL https://raw.githubusercontent.com/aidvgg/remote-computer/main/install-skill.sh | sh
REPO="${REPO:-aidvgg/remote-computer}"
REF="${REF:-main}"
SKILL_NAME="${SKILL_NAME:-remote-computer}"
REQUESTED_AGENT="${REMOTE_COMPUTER_SKILL_AGENT:-${AGENT:-auto}}"
TEMPORARY=""
STAGE=""
BACKUP=""
DESTINATION=""

cleanup() {
  if [ -n "$STAGE" ] && [ -e "$STAGE" ]; then rm -rf "$STAGE"; fi
  if [ -n "$BACKUP" ] && [ -e "$BACKUP" ]; then
    if [ -n "$DESTINATION" ] && [ ! -e "$DESTINATION" ]; then
      mv "$BACKUP" "$DESTINATION"
    else
      rm -rf "$BACKUP"
    fi
  fi
  if [ -n "$TEMPORARY" ] && [ -e "$TEMPORARY" ]; then rm -rf "$TEMPORARY"; fi
}
trap cleanup EXIT INT TERM

case "$REQUESTED_AGENT" in
  auto|claude|codex) AGENT="$REQUESTED_AGENT" ;;
  *) echo "Error: AGENT must be auto, claude, or codex" >&2; exit 1 ;;
esac

download() {
  url="$1"
  output="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$output"
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$url" -O "$output"
  else
    echo "Error: curl or wget is required" >&2
    exit 1
  fi
}

resolve_skills_dir() {
  if [ -n "${SKILLS_DIR:-}" ]; then
    printf '%s\n' "$SKILLS_DIR"
    return
  fi
  case "$AGENT" in
    claude) printf '%s\n' "${CLAUDE_CODE_SKILLS_DIR:-$HOME/.claude/skills}" ;;
    codex) printf '%s\n' "${CODEX_HOME:-$HOME/.codex}/skills" ;;
    auto)
      if [ -n "${CLAUDE_CODE_SKILLS_DIR:-}" ]; then
        printf '%s\n' "$CLAUDE_CODE_SKILLS_DIR"
      elif [ -n "${CODEX_HOME:-}" ]; then
        printf '%s\n' "$CODEX_HOME/skills"
      elif [ -d "$HOME/.codex" ]; then
        printf '%s\n' "$HOME/.codex/skills"
      elif [ -d "$HOME/.claude" ]; then
        printf '%s\n' "$HOME/.claude/skills"
      else
        printf '%s\n' "$HOME/.codex/skills"
      fi
      ;;
  esac
}

install_from() {
  source_root="$1"
  destination_root="$(resolve_skills_dir)"
  DESTINATION="${destination_root%/}/${SKILL_NAME}"
  STAGE="${destination_root%/}/.${SKILL_NAME}.new.$$"
  BACKUP="${destination_root%/}/.${SKILL_NAME}.old.$$"

  for required in SKILL.md agents references scripts/vm_bookkeeper.py scripts/remote_github_setup.py; do
    if [ ! -e "$source_root/$required" ]; then
      echo "Error: missing skill component: $source_root/$required" >&2
      exit 1
    fi
  done

  mkdir -p "$destination_root"
  mkdir "$STAGE"
  cp "$source_root/SKILL.md" "$STAGE/SKILL.md"
  cp -R "$source_root/agents" "$STAGE/agents"
  mkdir "$STAGE/scripts"
  cp "$source_root/scripts/vm_bookkeeper.py" "$STAGE/scripts/vm_bookkeeper.py"
  cp "$source_root/scripts/remote_github_setup.py" "$STAGE/scripts/remote_github_setup.py"
  cp -R "$source_root/references" "$STAGE/references"

  if [ -e "$DESTINATION" ]; then
    mv "$DESTINATION" "$BACKUP"
  fi
  if ! mv "$STAGE" "$DESTINATION"; then
    echo "Error: installation failed" >&2
    exit 1
  fi
  STAGE=""
  if [ -e "$BACKUP" ]; then rm -rf "$BACKUP"; fi
  BACKUP=""
  printf 'Installed or updated %s at %s\n' "$SKILL_NAME" "$DESTINATION"
}

if [ -n "${SOURCE_DIR:-}" ]; then
  install_from "${SOURCE_DIR%/}"
  exit 0
fi

TEMPORARY="$(mktemp -d)"
archive="$TEMPORARY/repository.tar.gz"
download "https://codeload.github.com/${REPO}/tar.gz/refs/heads/${REF}" "$archive"
tar -xzf "$archive" -C "$TEMPORARY"
source_root=""
for candidate in "$TEMPORARY"/*; do
  if [ -d "$candidate" ]; then source_root="$candidate"; break; fi
done
if [ -z "$source_root" ]; then
  echo "Error: downloaded archive did not contain a repository" >&2
  exit 1
fi
install_from "$source_root"

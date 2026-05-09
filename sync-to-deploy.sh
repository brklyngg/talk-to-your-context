#!/usr/bin/env bash
# Sync the public repo into Gary's deployed Hermes Mini sidecar.
#
# Symlinks server.py, web/, events.py, transcripts.py, auth.py from this repo
# to ~/.hermes-custom/hermes-mini/ so future code changes propagate without
# manual copying. Updates the deployed .env with the new AGENT_* var names
# (aliased to existing HERMES_* values so the old names keep working too).
# Then kicks the launchd service.
#
# Idempotent. Safe to re-run after every code change.
#
# Usage: ./sync-to-deploy.sh

set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.hermes-custom/hermes-mini"
LAUNCHD_LABEL="ai.hermes.mini"

if [[ ! -d "$DEST" ]]; then
  echo "deployed sidecar not found at $DEST" >&2
  exit 1
fi

backup="$DEST/.backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
echo "backing up old files to $backup/"
for f in server.py events.py transcripts.py auth.py; do
  if [[ -f "$DEST/$f" && ! -L "$DEST/$f" ]]; then
    cp "$DEST/$f" "$backup/$f"
  fi
done
if [[ -d "$DEST/web" && ! -L "$DEST/web" ]]; then
  cp -R "$DEST/web" "$backup/web"
fi

echo "symlinking source files from $SRC"
for f in server.py events.py transcripts.py auth.py; do
  rm -f "$DEST/$f"
  ln -s "$SRC/$f" "$DEST/$f"
done
rm -rf "$DEST/web"
ln -s "$SRC/web" "$DEST/web"

# Make the existing .hermes-api-key visible under the new name the new
# server.py looks for. (Symlink, not copy -- single source of truth.)
if [[ -f "$DEST/.hermes-api-key" && ! -e "$DEST/.agent-api-key" ]]; then
  ln -s "$DEST/.hermes-api-key" "$DEST/.agent-api-key"
fi

# Append AGENT_* aliases to .env if not already present. We don't rewrite the
# existing HERMES_* vars (other tools may read them).
env_file="$DEST/.env"
touch "$env_file"
add_if_missing() {
  local key="$1" value="$2"
  if ! grep -q "^${key}=" "$env_file"; then
    echo "$key=$value" >> "$env_file"
    echo "  + $key"
  fi
}
echo "patching $env_file"
add_if_missing AGENT_API_BASE '${HERMES_BASE:-http://127.0.0.1:8642}'
add_if_missing ASK_AGENT_TIMEOUT_SEC '90'
add_if_missing AGENT_DEPLOYMENT_NOTE 'You run on Gary'\''s Mac Mini. The agent backend (Hermes) has full filesystem access -- it can write files anywhere including ~/Desktop and the Obsidian vault. The diagnostic log lives at ~/.hermes-custom/hermes-mini/logs/calls/<conv_id>.ndjson. Voice = gpt-realtime-2. Brain = gpt-5.5 via Codex OAuth, with Ollama Gemma-4-E4B and OpenRouter Gemini-3.1-Pro fallbacks. If asked anything about logs, models, file capabilities, or where data lives -- call ask_agent. Do not improvise.'

# Bump model to gpt-realtime-2 if still pinned to the legacy name (May 2026
# cutover). Idempotent — does nothing if already gpt-realtime-2 or unset.
if grep -q '^OPENAI_REALTIME_MODEL=gpt-realtime$' "$env_file"; then
  sed -i.bak 's/^OPENAI_REALTIME_MODEL=gpt-realtime$/OPENAI_REALTIME_MODEL=gpt-realtime-2/' "$env_file"
  rm -f "$env_file.bak"
  echo "  ~ OPENAI_REALTIME_MODEL → gpt-realtime-2 (May 2026 GA cutover)"
fi

# Drop the obsolete idle-watchdog var if present (replaced by ASK_AGENT_TIMEOUT_SEC).
if grep -q '^ASK_AGENT_IDLE_TIMEOUT=' "$env_file"; then
  sed -i.bak '/^ASK_AGENT_IDLE_TIMEOUT=/d' "$env_file"
  rm -f "$env_file.bak"
  echo "  - ASK_AGENT_IDLE_TIMEOUT (obsolete; superseded by ASK_AGENT_TIMEOUT_SEC)"
fi

echo "kicking launchd service"
launchctl kickstart -k "gui/$(id -u)/${LAUNCHD_LABEL}" || \
  echo "launchctl kickstart failed (service may not be loaded)" >&2

echo "done"
echo "logs:  tail -f $DEST/logs/server.log"
echo "calls: ls -lat $DEST/logs/calls/"

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON:-python3}"

normalize_path() {
  "$PYTHON_BIN" -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$1"
}

install_launcher() {
  local use_source="$1"
  local resolved_python scripts_dir wrapper_dir wrapper_path
  resolved_python="$("$PYTHON_BIN" -c 'import sys; print(sys.executable)')"
  scripts_dir="$("$PYTHON_BIN" -c 'import sysconfig; print(sysconfig.get_path("scripts"))')"
  if [[ -w "$scripts_dir" ]]; then
    wrapper_dir="$scripts_dir"
  else
    wrapper_dir="$TELEAGENT_HOME/.local/bin"
    install -d -m 755 "$wrapper_dir"
  fi
  wrapper_path="$wrapper_dir/teleagent"
  {
    printf '#!/usr/bin/env bash\n'
    printf 'export TELEAGENT_HOME=%q\n' "$TELEAGENT_HOME"
    if [[ -n "${TELEAGENT_CONFIG_DIR:-}" ]]; then
      printf 'export TELEAGENT_CONFIG_DIR=%q\n' "$TELEAGENT_CONFIG_DIR"
    fi
    if [[ "$use_source" == "yes" ]]; then
      printf 'PYTHONPATH=%q${PYTHONPATH:+:$PYTHONPATH} exec %q -m teleagent "$@"\n' "$ROOT_DIR" "$resolved_python"
    else
      printf 'exec %q -m teleagent "$@"\n' "$resolved_python"
    fi
  } > "$wrapper_path"
  chmod +x "$wrapper_path"
  echo "Installed teleagent launcher: $wrapper_path"
}

cd "$ROOT_DIR"

default_teleagent_home="${TELEAGENT_HOME:-$HOME}"
read -r -p "TeleAgent data home [$default_teleagent_home]: " teleagent_home
teleagent_home="${teleagent_home:-$default_teleagent_home}"
TELEAGENT_HOME="$(normalize_path "$teleagent_home")"
export TELEAGENT_HOME

if [[ -n "${TELEAGENT_CONFIG_DIR:-}" ]]; then
  TELEAGENT_CONFIG_DIR="$(normalize_path "$TELEAGENT_CONFIG_DIR")"
  export TELEAGENT_CONFIG_DIR
  teleagent_config_dir="$TELEAGENT_CONFIG_DIR"
else
  teleagent_config_dir="$TELEAGENT_HOME/.config/teleagent"
fi
if [[ -z "${PIP_CACHE_DIR:-}" ]]; then
  PIP_CACHE_DIR="$TELEAGENT_HOME/.cache/pip"
  export PIP_CACHE_DIR
fi

custom_data_home="no"
if [[ "$TELEAGENT_HOME" != "$(normalize_path "$HOME")" || -n "${TELEAGENT_CONFIG_DIR:-}" ]]; then
  custom_data_home="yes"
fi

echo "TeleAgent config directory: $teleagent_config_dir"
echo "Installing TeleAgent from: $ROOT_DIR"
pip_log="$(mktemp -t teleagent-pip-install.XXXXXX.log)"
if "$PYTHON_BIN" -m pip install -e . --no-build-isolation >"$pip_log" 2>&1; then
  echo "Installed TeleAgent with pip."
  rm -f "$pip_log"
  if [[ "$custom_data_home" == "yes" ]]; then
    install_launcher "no"
  fi
else
  echo "pip install failed; installing a source wrapper instead."
  echo "pip log: $pip_log"
  install_launcher "yes"
fi

read -r -p "Default wrapped CLI command [codex]: " default_command
default_command="${default_command:-codex}"

init_args=(--init-global --global-default-command "$default_command")

token=""
read -r -p "Configure Telegram now? [y/N]: " setup_telegram
case "$setup_telegram" in
  y|Y|yes|YES|Yes)
    read -r -s -p "Telegram bot token (input hidden, leave empty to skip): " token
    echo
    read -r -p "Telegram chat id: " chat_id
    if [[ -n "${chat_id//[[:space:]]/}" ]]; then
      init_args+=(--enable-telegram --telegram-chat-id "$chat_id")
    else
      echo "No chat id provided; Telegram will remain disabled in the generated config."
    fi
    ;;
esac

"$PYTHON_BIN" -m teleagent "${init_args[@]}"

if [[ -n "$token" ]]; then
  install -d -m 700 "$teleagent_config_dir"
  umask 077
  printf '%s\n' "$token" > "$teleagent_config_dir/telegram-token"
  chmod 600 "$teleagent_config_dir/telegram-token"
  echo "Wrote Telegram token file: $teleagent_config_dir/telegram-token"
fi

cat <<'EOF'

Install complete.

Run from any project directory:
  teleagent

Or override the wrapped command:
  teleagent -- codex
  teleagent -- kimi

Global config:
EOF
printf '  %s/teleagent.toml\n' "$teleagent_config_dir"

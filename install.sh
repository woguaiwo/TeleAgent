#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON:-python3}"

cd "$ROOT_DIR"

echo "Installing TeleAgent from: $ROOT_DIR"
pip_log="$(mktemp -t teleagent-pip-install.XXXXXX.log)"
if "$PYTHON_BIN" -m pip install -e . --no-build-isolation >"$pip_log" 2>&1; then
  echo "Installed TeleAgent with pip."
  rm -f "$pip_log"
else
  echo "pip install failed; installing a source wrapper instead."
  echo "pip log: $pip_log"
  resolved_python="$("$PYTHON_BIN" -c 'import sys; print(sys.executable)')"
  python_bin_dir="$(dirname "$resolved_python")"
  if [[ -w "$python_bin_dir" ]]; then
    wrapper_dir="$python_bin_dir"
  else
    wrapper_dir="$HOME/.local/bin"
    install -d -m 755 "$wrapper_dir"
  fi
  wrapper_path="$wrapper_dir/teleagent"
  cat > "$wrapper_path" <<EOF
#!/usr/bin/env bash
PYTHONPATH="$ROOT_DIR\${PYTHONPATH:+:\$PYTHONPATH}" exec "$resolved_python" -m teleagent "\$@"
EOF
  chmod +x "$wrapper_path"
  echo "Installed source wrapper: $wrapper_path"
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
  install -d -m 700 "$HOME/.config/teleagent"
  umask 077
  printf '%s\n' "$token" > "$HOME/.config/teleagent/telegram-token"
  chmod 600 "$HOME/.config/teleagent/telegram-token"
  echo "Wrote Telegram token file: $HOME/.config/teleagent/telegram-token"
fi

cat <<'EOF'

Install complete.

Run from any project directory:
  teleagent

Or override the wrapped command:
  teleagent -- codex
  teleagent -- kimi

Global config:
  ~/.config/teleagent/teleagent.toml
EOF

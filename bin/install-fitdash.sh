#!/bin/sh
# Install the `fitdash` command: a tiny wrapper in ~/.local/bin that runs this checkout's dashboard
# with uv (which fetches its one dependency, rich, on first run). Safe to re-run after moving the repo.
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BIN="${FITDASH_BIN_DIR:-$HOME/.local/bin}"
command -v uv >/dev/null 2>&1 || { echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2; exit 1; }
mkdir -p "$BIN"
cat > "$BIN/fitdash" <<WRAP
#!/bin/sh
# fitdash: terminal health dashboard from $REPO
exec uv run --quiet --script "$REPO/.claude/skills/stats/scripts/dashboard.py" "\$@"
WRAP
chmod +x "$BIN/fitdash"
echo "installed $BIN/fitdash → $REPO"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "add $BIN to your PATH (e.g. in ~/.zshrc: export PATH=\"$BIN:\$PATH\")";; esac

#!/usr/bin/env bash
# Set up the ETROC i2c measurement environment.
#
# Recommended usage:
#   source ~/ETL/i2c_gui/helpers/setup_i2c_measurement_env.sh
#
# If executed directly, this script starts a new shell with the environment active.

# Do not use `set -e` here: this script is meant to be sourced, and leaking
# errexit into an interactive shell can make normal bash completion failures
# exit the shell and close SSH.

HELPERS_DIR="$HOME/ETL/i2c_gui/helpers"
VENV_DIR="${I2C_ETROC_VENV:-$HOME/i2c_etroc}"
LOCAL_FIGURE_ROOT="${ETROC_LOCAL_FIGURE_ROOT:-$HOME/ETL/i2c_gui/ETROC-figures}"
OUTPUT_DIR="${ETROC_SQLITE_OUTPUT_DIR:-$HOME/ETL/i2c_gui/helpers/output}"
FIGURE_ROOT="${ETROC_FIGURE_ROOT:-$HOME/ETL/cernbox-www-plots/ETROC-figures}"

if [ ! -d "$HELPERS_DIR" ]; then
  echo "ERROR: helpers directory not found: $HELPERS_DIR" >&2
  return 1 2>/dev/null || exit 1
fi

if [ ! -f "$VENV_DIR/bin/activate" ]; then
  echo "ERROR: virtual environment activate script not found: $VENV_DIR/bin/activate" >&2
  echo "Set I2C_ETROC_VENV=/path/to/i2c_etroc if it lives elsewhere." >&2
  return 1 2>/dev/null || exit 1
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
export ETROC_LOCAL_FIGURE_ROOT="$LOCAL_FIGURE_ROOT"
export ETROC_SQLITE_OUTPUT_DIR="$OUTPUT_DIR"
export ETROC_FIGURE_ROOT="$FIGURE_ROOT"
if ! mkdir -p "$ETROC_LOCAL_FIGURE_ROOT/by_hybrid_name" "$ETROC_SQLITE_OUTPUT_DIR"; then
  echo "ERROR: could not create local figure or SQLite output directory" >&2
  return 1 2>/dev/null || exit 1
fi
cd "$HELPERS_DIR" || return 1 2>/dev/null || exit 1

echo "i2c measurement environment ready"
echo "  venv:          $VENV_DIR"
echo "  local figures: $ETROC_LOCAL_FIGURE_ROOT/by_hybrid_name"
echo "  SQLite/logs:   $ETROC_SQLITE_OUTPUT_DIR"
echo "  pending plots: $ETROC_SQLITE_OUTPUT_DIR/pending_cernbox_plots.txt"
echo "  sync target:   $ETROC_FIGURE_ROOT/by_hybrid_name"
echo "  working dir:   $PWD"

# If the script was executed instead of sourced, keep the configured environment
# alive in a child interactive shell.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo
  echo "Tip: source this script to apply the setup to your current shell:"
  echo "  source ~/ETL/i2c_gui/helpers/setup_i2c_measurement_env.sh"
  exec "${SHELL:-/bin/bash}"
fi

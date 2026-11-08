#!/bin/zsh
# Sets up LOCKED IN on an Apple Silicon Mac. One repository, two profiles:
#   16 GB (M4 Air): Qwen3 on MLX, voice, camera, dashboard, Code Quest.
#   24 GB or more (M5 Pro): the same, and room for Nemotron Bolt through Ollama.
#
#   scripts/setup_mac.sh            install pinned dependencies and the pinned models, then run the checks
#   scripts/setup_mac.sh --check    look only: install nothing, download nothing
#   scripts/setup_mac.sh --no-models     dependencies only
#   scripts/setup_mac.sh --relay    also install NeMo Relay's Python binding (12 MB, pinned)
#   scripts/setup_mac.sh --bolt     also pull nemotron-3.5-bolt:e4b (7.9 GB). Asks first, refuses on a small disk.
#
# Model weights stay in backend/models and Ollama's own folder. Neither is in git, and neither are API keys.
set -u
cd "${0:A:h}/.."

CHECK=0 MODELS=1 BOLT=0 RELAY=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --no-models) MODELS=0 ;;
    --bolt) BOLT=1 ;;
    --relay) RELAY=1 ;;
    *) print -u2 "unknown option: $arg"; exit 2 ;;
  esac
done

BOLT_TAG="nemotron-3.5-bolt:e4b"   # Never the bare name: that is a 25 GB model with no Apple Silicon support.
BOLT_GB=8
RELAY_PIN="nemo-relay==0.11.1"
fail=0
ok()   { print "  [ok]   $*" }
warn() { print "  [WARN] $*" }
bad()  { print "  [FAIL] $*"; fail=1 }
need_tool() { # name, how to get it
  if command -v "$1" >/dev/null 2>&1; then ok "$1: $("$1" --version 2>&1 | head -1)"; else bad "$1 is missing. Install it: $2"; fi
}

print "THIS MAC"
arch_name=$(uname -m)
[[ "$(uname -s)" == Darwin && "$arch_name" == arm64 ]] && ok "macOS on Apple Silicon" || bad "needs macOS on Apple Silicon (found $(uname -s) $arch_name): the models run on MLX and Metal"
chip=$(sysctl -n machdep.cpu.brand_string 2>/dev/null)
ram_gb=$(( $(sysctl -n hw.memsize 2>/dev/null || print 0) / 1073741824 ))
free_gb=$(df -g . | awk 'NR==2 {print $4}')
ok "$chip, $ram_gb GB memory, $free_gb GB free on this disk"
swap_mb=$(sysctl vm.swapusage 2>/dev/null | sed -E 's/.*used = ([0-9]+).*/\1/')
[[ "${swap_mb:-0}" -gt 3000 ]] && warn "$swap_mb MB of swap in use: close other apps before a live demo"
[[ "$(pmset -g 2>/dev/null | awk '/lowpowermode/ {print $2}')" == 1 ]] && warn "Low Power Mode is on: the local model runs about 4x slower"

if (( ram_gb >= 24 )); then
  profile="24 GB or more: Qwen3 by default, Nemotron Bolt can run alongside voice and camera (measure it: make agent-bench LLM=bolt)"
else
  profile="$ram_gb GB: Qwen3 with voice, camera and dashboard. Nemotron Bolt is not recommended here: it takes about $BOLT_GB GB of disk and memory"
fi
print "\nPROFILE\n  $profile"

print "\nTOOLS"
need_tool uv "curl -LsSf https://astral.sh/uv/install.sh | sh"
need_tool node "https://nodejs.org or: brew install node"
need_tool npm "comes with node"

if (( BOLT )); then
  print "\nNEMOTRON BOLT"
  if ! command -v ollama >/dev/null 2>&1; then
    bad "Ollama is missing: https://ollama.com/download"
  elif ollama list 2>/dev/null | grep -q "^$BOLT_TAG"; then
    ok "$BOLT_TAG is already downloaded"
  elif (( CHECK )); then
    print "  not downloaded: $BOLT_TAG ($BOLT_GB GB). Nothing fetched in --check."
  elif (( free_gb < BOLT_GB + 8 )); then
    bad "$free_gb GB free is not enough: $BOLT_TAG needs $BOLT_GB GB and the Mac needs room to breathe. Nothing was deleted or downloaded."
  else
    print -n "  Download $BOLT_TAG, $BOLT_GB GB, to Ollama's folder? Type yes: "
    read -r answer
    if [[ "$answer" == yes ]]; then ollama pull "$BOLT_TAG" || bad "the download failed"; else print "  skipped"; fi
  fi
fi

if (( CHECK )); then
  print "\nINSTALLED"
  [[ -x backend/.venv/bin/python ]] && ok "backend environment" || warn "backend environment not installed: run scripts/setup_mac.sh"
  [[ -d frontend/node_modules ]] && ok "dashboard packages" || warn "dashboard packages not installed: run scripts/setup_mac.sh"
  [[ -f .env ]] && ok ".env exists (not in git)" || print "  [info] no .env: Fish Audio generation is off; cached clips and the local voice still work"
  if [[ -x backend/.venv/bin/python ]]; then
    backend/.venv/bin/python -c "import nemo_relay" 2>/dev/null && ok "NeMo Relay binding installed" || print "  [info] NeMo Relay binding not installed (optional: --relay)"
    print "\nREADINESS"
    (cd backend && .venv/bin/python scripts/offline_check.py --no-smoke) || fail=1
  fi
  (( fail == 0 )) && print "\nChecks passed." || print "\nSome checks failed: see FAIL above."
  exit $fail
fi

(( fail )) && { print "\nFix the FAIL lines above first. Nothing was installed."; exit 1 }

print "\nINSTALLING pinned dependencies"
(cd backend && uv sync --frozen --all-extras --python 3.12) || { print "backend install failed"; exit 1 }
(cd frontend && npm ci --no-fund --no-audit) || { print "dashboard install failed"; exit 1 }
(( RELAY )) && { (cd backend && uv pip install --python .venv/bin/python "$RELAY_PIN") || warn "NeMo Relay did not install; everything else works without it" }

if (( MODELS )); then
  print "\nMODELS (pinned, checked against backend/models.sha256)"
  (cd backend && .venv/bin/python scripts/download_models.py) || { print "model download failed: run it again on a good connection"; exit 1 }
fi

print "\nBUILDING the dashboard"
(cd frontend && npm run build >/dev/null) || { print "dashboard build failed"; exit 1 }

print "\nCHECKS"
(cd backend && .venv/bin/python scripts/offline_check.py) || { print "\nInstalled, but some checks failed: see FAIL above."; exit 1 }
print "\nReady. Robot: join the Zeus_Car Wi-Fi, then: make hardware SPEAKER=\"JLab GO Party\". No robot: make demo."

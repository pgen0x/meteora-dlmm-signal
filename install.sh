#!/usr/bin/env bash
# Installs the DLMM signal system into a Hermes profile:
#   1. copies the solana-dlmm skill + safety scripts, rewriting absolute paths
#   2. installs the dlmm-signal webhook subscription (agent decision logic)
#   3. builds the Go discovery daemon
# It never touches your wallet keys — those live in the profile .env you create.
set -euo pipefail

PROFILE="${1:-$HOME/.hermes/profiles/dlmm}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "→ Target Hermes profile: $PROFILE"
mkdir -p "$PROFILE/skills/solana-dlmm" "$PROFILE/skills/solana-web3/scripts"

echo "→ Symlinking solana-dlmm scripts"
# scripts/ is symlinked (not copied) so edits in the repo take effect immediately in
# every installed profile without re-running install.sh. The scripts resolve their own
# profile dir at runtime (3 levels up from their own file location), so no path rewrite
# is needed here.
if [ -e "$PROFILE/skills/solana-dlmm/scripts" ] && [ ! -L "$PROFILE/skills/solana-dlmm/scripts" ]; then
  echo "   ⚠ $PROFILE/skills/solana-dlmm/scripts exists as a real directory (pre-symlink install)."
  echo "     Back it up / diff it against $REPO/assets/skill/scripts, then remove it before re-running."
  exit 1
fi
ln -sfn "$REPO/assets/skill/scripts" "$PROFILE/skills/solana-dlmm/scripts"
cp "$REPO/assets/skill/SKILL.md" "$REPO/assets/skill/package.json" "$PROFILE/skills/solana-dlmm/"
cp "$REPO/assets/skill/solana-web3-scripts/"*.py "$PROFILE/skills/solana-web3/scripts/"

echo "→ Installing webhook subscription (merges into existing file if present)"
SUB_SRC="$REPO/assets/hermes/webhook_subscriptions.json"
SUB_DST="$PROFILE/webhook_subscriptions.json"
if [ -f "$SUB_DST" ]; then
  python3 - "$SUB_DST" "$SUB_SRC" "$PROFILE" <<'PY'
import json,sys
dst,src,profile=sys.argv[1],sys.argv[2],sys.argv[3]
d=json.load(open(dst))
s=json.loads(open(src).read().replace("__PROFILE__",profile))
d.update(s)
json.dump(d,open(dst,'w'),indent=2)
print("   merged dlmm-signal into existing webhook_subscriptions.json")
PY
else
  sed "s#__PROFILE__#$PROFILE#g" "$SUB_SRC" > "$SUB_DST"
  echo "   created $SUB_DST"
fi

echo "→ npm install (Meteora SDK) in skill"
( cd "$PROFILE/skills/solana-dlmm" && npm install --no-audit --no-fund >/dev/null 2>&1 ) && echo "   ok" || echo "   ⚠ npm install failed — run manually in $PROFILE/skills/solana-dlmm"

echo "→ Building Go daemon"
( cd "$REPO" && go build -o mds . ) && echo "   built $REPO/mds"

cat <<DONE

✅ Install complete.

Next steps:
  1. Create $PROFILE/.env with:
       SOLANA_PUBLIC_KEY=...
       SOLANA_PRIVATE_KEY=...          # base58, used by dlmm_executor.js
  2. Edit $SUB_DST:
       - set "secret" (match HERMES_WEBHOOK_SECRET below)
       - set deliver_extra.chat_id to your channel
  3. Enable the webhook platform in $PROFILE/config.yaml (port 8646).
  4. Add a cron job to run dlmm_monitor.py every 5m (position management).
  5. Configure + run the daemon:
       cp $REPO/.env.example $REPO/.env   # edit secret to match step 2
       cd $REPO && set -a && . ./.env && set +a && ./mds
DONE

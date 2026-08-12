#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
cargo build --release --target wasm32-wasip1
DEST="${HOME}/.dotfiles/zellij/plugins/zellij-auto-name.wasm"
mkdir -p "$(dirname "$DEST")"
cp -f target/wasm32-wasip1/release/zellij-auto-name.wasm "$DEST"
echo "installed → $DEST ($(wc -c < "$DEST") bytes)"

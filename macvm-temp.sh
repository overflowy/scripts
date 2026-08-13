#!/bin/bash
set -e

BASE="macvm"
VM="macos-run-$$"

cleanup() {
    tart delete "$VM" 2>/dev/null || true
    echo 'Cleanup complete'
}
trap cleanup EXIT

tart clone "$BASE" "$VM"
tart run "$VM"

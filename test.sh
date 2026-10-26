#!/usr/bin/env bash
# test.sh — offline checks for AgentHands skills (unit tests + CLI contract).
# No network, no cookies, no browser.
set -uo pipefail

REPO_ROOT=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
SKILL="$REPO_ROOT/skills/feishu"
VENV_PY="${FEISHU_HOME:-$HOME/.local/share/feishu}/venv/bin/python"

fail=0
note() { printf '%s\n' "$*"; }

# 1. pick an interpreter with the deps (venv preferred, system python fallback)
PY=""
if [ -x "$VENV_PY" ]; then
    PY="$VENV_PY"
elif python3 -c "import httpx, websockets, cryptography; from google.protobuf import runtime_version" 2>/dev/null; then
    PY=python3
fi
if [ -z "$PY" ]; then
    note "feishu: 没有可用的 python（venv 未建且系统 python 缺依赖）——先跑一次 skills/feishu/scripts/feishu.py 让它自举"
    exit 3
fi

# 2. compile everything
if ! "$PY" -m compileall -q "$SKILL/scripts" "$SKILL/tests"; then
    note "feishu: 编译失败"
    fail=1
fi

# 3. unit tests
TEST_HOME=$(mktemp -d)
trap 'rm -rf "$TEST_HOME"' EXIT
if ! FEISHU_HOME="$TEST_HOME" FEISHU_NO_BOOTSTRAP=1 FEISHU_EPHEMERAL_KEY=1 \
        "$PY" -m unittest discover -s "$SKILL/tests" 2>&1; then
    note "feishu: 单测失败"
    fail=1
fi

# 4. CLI static contract: --help works on bare system python (no venv hop,
#    no network), and an unknown command exits 2 (argparse).
if ! FEISHU_NO_BOOTSTRAP=1 python3 "$SKILL/scripts/feishu.py" --help >/dev/null 2>&1; then
    note "feishu: --help 失败"
    fail=1
fi
FEISHU_NO_BOOTSTRAP=1 python3 "$SKILL/scripts/feishu.py" 不存在的命令 >/dev/null 2>&1
rc=$?
if [ "$rc" -ne 2 ]; then
    note "feishu: 未知命令退出码应为 2，实际 $rc"
    fail=1
fi

# 5. secrets must not appear in any default output path
out=$(FEISHU_NO_NETWORK=1 FEISHU_NO_BOOTSTRAP=1 "$PY" "$SKILL/scripts/feishu.py" whoami 2>&1 || true)
case "$out" in
    *session=*|*passport_web_did=*) note "feishu: whoami 输出疑似泄露 cookie"; fail=1 ;;
esac

[ "$fail" -eq 0 ] && note "AgentHands (feishu): 全部通过"
exit "$fail"

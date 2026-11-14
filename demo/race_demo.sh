#!/usr/bin/env bash
# 并发可控演示：int 写端 / long 读端的链路上，
# 同时把 writer 改为 long（发往 web1）、把 reader 改为 int（发往 web2）。
# 两端各带演示延迟，预检都能通过；若都提交成功将留下 writer=long / reader=int
# 的不兼容组合。提交时按当时状态重新裁决 —— 后提交者必然 409，旧契约保留。
#
# 用法:  demo/race_demo.sh            （默认打 http://localhost:8001 与 :8002）
#        BASE1=... BASE2=... DELAY_MS=800 demo/race_demo.sh
set -euo pipefail

BASE1="${BASE1:-http://localhost:8001}"
BASE2="${BASE2:-http://localhost:8002}"
LINK="demo-int-long"
DELAY_MS="${DELAY_MS:-800}"

rec() { printf '{"type":"record","name":"Demo","fields":[{"name":"v","type":"%s"}]}' "$1"; }
pp() { if command -v python3 >/dev/null; then python3 -m json.tool --no-ensure-ascii 2>/dev/null || python3 -m json.tool; else cat; fi }

echo "== 0. 重置演示链路（writer=int, reader=long，经 web1 创建） =="
curl -s -X DELETE "$BASE1/api/links/$LINK" > /dev/null || true
curl -s -X POST "$BASE1/api/links" -H 'Content-Type: application/json' \
  -d "{\"name\":\"$LINK\",\"writer\":$(rec int),\"reader\":$(rec long)}" | pp

echo
echo "== 1. 并发提交：writer->long (web1) 与 reader->int (web2)，各延迟 ${DELAY_MS}ms =="
TMP="$(mktemp -d)"
curl -s -X PUT "$BASE1/api/links/$LINK/writer" -H 'Content-Type: application/json' \
  -d "{\"schema\":$(rec long),\"demo_delay_ms\":$DELAY_MS}" \
  -w '%{http_code}' -o "$TMP/writer.body" > "$TMP/writer.code" &
P1=$!
curl -s -X PUT "$BASE2/api/links/$LINK/reader" -H 'Content-Type: application/json' \
  -d "{\"schema\":$(rec int),\"demo_delay_ms\":$DELAY_MS}" \
  -w '%{http_code}' -o "$TMP/reader.body" > "$TMP/reader.code" &
P2=$!
wait $P1 $P2

for side in writer reader; do
  code="$(cat "$TMP/$side.code")"
  if [ "$side" = writer ]; then desc="writer -> long (web1)"; else desc="reader -> int (web2)"; fi
  echo "--- $desc : HTTP $code ---"
  pp < "$TMP/$side.body"
done

echo
echo "== 2. 最终契约（分别从两个进程读取，应一致且必然兼容） =="
echo "--- web1 视角 ---"; curl -s "$BASE1/api/links/$LINK" | pp
echo "--- web2 视角 ---"; curl -s "$BASE2/api/links/$LINK" | pp
rm -rf "$TMP"

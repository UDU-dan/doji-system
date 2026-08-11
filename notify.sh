#!/usr/bin/env bash
# 텔레그램 발송. 사용: bash notify.sh <market> <prefix>
set -u
MK="${1:-kr}"
PREFIX="${2:-watchlist}"

if [ -z "${TG_TOKEN:-}" ]; then
  echo "토큰 없음 - 건너뜀"
  exit 0
fi

send() {
  curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TG_CHAT}" \
    --data-urlencode "text=$1" > /dev/null
}

F="results/${PREFIX}_${MK}.txt"

if [ -s "$F" ]; then
  split -b 3500 "$F" /tmp/part_
  for p in /tmp/part_*; do
    send "$(cat "$p")"
    sleep 1
  done
  rm -f /tmp/part_*
  echo "발송 완료"
else
  LOG=$(ls results/*_log.txt 2>/dev/null | head -1)
  TAIL=$(tail -c 900 "$LOG" 2>/dev/null | tr '\n' ' ')
  [ -z "$TAIL" ] && TAIL="로그 없음"
  send "[${MK}] 결과 0건 또는 실행 실패 / ${TAIL}"
  echo "0건 알림 발송"
fi

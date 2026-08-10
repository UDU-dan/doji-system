#!/usr/bin/env bash
# 결과 파일을 저장소에 커밋한다.
set -u
BR="${1:-main}"
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git add -f results/ || true
if git diff --staged --quiet; then
  echo "변경 없음"
  exit 0
fi
git commit -m "결과 업데이트 $(date +'%Y-%m-%d %H:%M')"
git pull --rebase --autostash origin "$BR" || true
git push || true
echo "커밋 완료"

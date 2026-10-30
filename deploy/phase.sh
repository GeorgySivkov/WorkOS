#!/usr/bin/env bash
# Включение по фазам плана. Правило 02: следующая фаза не начинается, пока
# предыдущая не проработала неделю без ручного вмешательства.
#   sudo bash deploy/phase.sh 1          # включить
#   sudo bash deploy/phase.sh status     # что включено
#   sudo bash deploy/phase.sh off 5      # выключить фазу
set -euo pipefail

declare -A UNITS=(
  [1]="workos-bot.service workos-app.service"
  [persona]="workos-award.timer workos-classify.timer workos-weekly.timer workos-garmin.timer"
  [2]="workos-collect.timer workos-eas.service workos-exec-calendar.service workos-dayplan.timer workos-plan.timer workos-health.timer"
  [3]="workos-people.timer workos-embed.timer workos-brief.timer workos-cards.timer"
  [4]="workos-extract.timer workos-triage.timer"
  [5]="workos-exec-queue.service workos-draft.timer"
  [6]="workos-review.timer workos-autonomy.timer"
  [7]="workos-ingest.service"
  [8]="workos-audit.timer workos-backup.timer"
)

if [ "${1:-}" = "status" ]; then
  for k in 1 persona 2 3 4 5 6 7 8; do
    for u in ${UNITS[$k]}; do printf '%-8s %-34s %s\n' "$k" "$u" "$(systemctl is-enabled "$u" 2>/dev/null || true)/$(systemctl is-active "$u" 2>/dev/null || true)"; done
  done
  exit 0
fi

if [ "${1:-}" = "off" ]; then
  for u in ${UNITS[${2:?какую фазу}]}; do systemctl disable --now "$u" || true; done
  exit 0
fi

phase="${1:?укажи фазу: 1, persona, 2..8}"
[ -n "${UNITS[$phase]:-}" ] || { echo "нет фазы $phase"; exit 1; }
for u in ${UNITS[$phase]}; do
  systemctl enable --now "$u"
  echo "  включено: $u"
done

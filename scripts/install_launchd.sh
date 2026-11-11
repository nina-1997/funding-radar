#!/bin/bash
# Автозапуск сервиса сбора на macOS через launchd: стартует при входе в систему
# и перезапускается, если процесс упал.
#
#   bash scripts/install_launchd.sh              установить и запустить
#   bash scripts/install_launchd.sh --uninstall  остановить и удалить
#
# Ограничение: пока Mac спит, сервис не работает (после пробуждения продолжит сам).
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.fundingradar.scheduler"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Автозапуск удалён."
    exit 0
fi

# macOS не даёт фоновым службам читать Загрузки, Рабочий стол и Документы без отдельного разрешения
case "$DIR" in
    "$HOME/Downloads"*|"$HOME/Desktop"*|"$HOME/Documents"*)
        echo "Проект лежит в $DIR."
        echo "macOS не пустит фоновую службу в эту папку. Перенеси проект в домашнюю папку:"
        echo "  mv \"$DIR\" ~/funding-radar && cd ~/funding-radar"
        echo "  rm -rf .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
        echo "и запусти этот скрипт снова."
        exit 1;;
esac

PY="$DIR/.venv/bin/python"
[[ -x "$PY" ]] || { echo "Нет $PY — сначала создай окружение: python3 -m venv .venv && pip install -r requirements.txt"; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string><string>-m</string><string>pipeline.scheduler</string>
    </array>
    <key>WorkingDirectory</key><string>$DIR</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>EnvironmentVariables</key>
    <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
    <key>StandardOutPath</key><string>$DIR/logs/launchd.out.log</string>
    <key>StandardErrorPath</key><string>$DIR/logs/launchd.err.log</string>
</dict>
</plist>
PLIST

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
sleep 3
if launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -q "state = running"; then
    echo "Сервис запущен и будет перезапускаться сам."
else
    echo "Служба установлена, но пока не работает. Смотри $DIR/logs/launchd.err.log"
fi
echo "Лог работы:   tail -f $DIR/logs/funding_radar.log"
echo "Состояние:    launchctl print $DOMAIN/$LABEL | grep state"
echo "Удалить:      bash scripts/install_launchd.sh --uninstall"

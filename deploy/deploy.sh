#!/usr/bin/env bash
#
# Управление ботом на VPS. Настройки подключения — в deploy/deploy.env.
#
#   ./deploy/deploy.sh install     первая установка: Docker, код, запуск
#   ./deploy/deploy.sh update      залить текущий код и перезапустить
#   ./deploy/deploy.sh start       включить (в том числе снять стоп-фразу)
#   ./deploy/deploy.sh stop        остановить
#   ./deploy/deploy.sh restart     перезапустить
#   ./deploy/deploy.sh logs        живой лог
#   ./deploy/deploy.sh status      состояние контейнера
#   ./deploy/deploy.sh shell       шелл внутри контейнера
#   ./deploy/deploy.sh uninstall   снести бота с сервера
#
# На Windows запускать из Git Bash.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$SCRIPT_DIR/deploy.env"

# Должен совпадать с uid пользователя bot из Dockerfile.
CONTAINER_UID=10001

die() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }
step() { printf '\n=== %s\n' "$*"; }
# Справка = шапка этого файла: печатаем комментарии до первой строки кода.
usage() { awk 'NR>2 && /^#/ { sub(/^# ?/, ""); print; next } NR>2 { exit }' "${BASH_SOURCE[0]}"; }

# Справка не должна требовать настроенного deploy.env.
case "${1:-}" in
    -h|--help|help) usage; exit 0 ;;
esac

[[ -f "$CONFIG" ]] || die "нет $CONFIG
Скопируй deploy/deploy.env.example в deploy/deploy.env и укажи адрес сервера."

set -a
# shellcheck disable=SC1090
. "$CONFIG"
set +a

: "${VPS_HOST:?не задан VPS_HOST в deploy.env}"
VPS_USER="${VPS_USER:-root}"
VPS_PORT="${VPS_PORT:-22}"
REMOTE_DIR="${REMOTE_DIR:-/opt/vk_claude_bot}"

SSH_OPTS=(-p "$VPS_PORT" -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
if [[ -n "${SSH_KEY:-}" ]]; then
    SSH_OPTS+=(-i "${SSH_KEY/#\~/$HOME}")
fi

# Не-root пользователю нужен sudo для Docker и записи в /opt.
if [[ "$VPS_USER" == "root" ]]; then SUDO=""; else SUDO="sudo "; fi

# -n обязателен: без него ssh вычитывает наш stdin и съедает ответы на
# последующие вопросы read. Для передачи данных на сервер есть remote_in.
remote()     { ssh -n "${SSH_OPTS[@]}" "$VPS_USER@$VPS_HOST" "$@"; }
remote_in()  { ssh "${SSH_OPTS[@]}" "$VPS_USER@$VPS_HOST" "$@"; }
remote_tty() { ssh -t "${SSH_OPTS[@]}" "$VPS_USER@$VPS_HOST" "$@"; }
compose()    { remote "cd '$REMOTE_DIR' && ${SUDO}docker compose $*"; }

check_ssh() {
    step "Проверяю доступ к $VPS_USER@$VPS_HOST:$VPS_PORT"
    remote true || die "не подключиться по ssh. Проверь адрес, порт и ключ в deploy.env."
    echo "ssh работает"
}

check_env() {
    [[ -f "$PROJECT_DIR/.env" ]] || die "в проекте нет .env — заполни его перед деплоем."
    if grep -qi 'xxxx' "$PROJECT_DIR/.env"; then
        printf '\nВнимание: в .env остались шаблонные значения (иксы):\n' >&2
        grep -in 'xxxx' "$PROJECT_DIR/.env" | cut -d= -f1 >&2
        printf 'На сервере нет твоих локальных кредов Claude, там нужен настоящий\n' >&2
        printf 'CLAUDE_CODE_OAUTH_TOKEN (получить: claude setup-token).\n\n' >&2
        read -r -p "Всё равно продолжить? [y/N] " answer
        [[ "$answer" == [yY] ]] || die "отменено"
    fi
}

ensure_docker() {
    step "Проверяю Docker на сервере"
    if remote "command -v docker >/dev/null 2>&1"; then
        remote "${SUDO}docker compose version >/dev/null 2>&1" \
            || die "Docker есть, но нет плагина compose v2. Поставь docker-compose-plugin."
        echo "Docker уже установлен — систему не трогаю"
        return
    fi

    printf '\nНа сервере нет Docker. Установка (get.docker.com) изменит систему:\n'
    printf 'добавит репозиторий, пакеты и systemd-сервис docker.\n'
    read -r -p "Ставить? [y/N] " answer
    [[ "$answer" == [yY] ]] || die "отменено. Поставь Docker вручную и запусти снова."
    remote "curl -fsSL https://get.docker.com | ${SUDO}sh"
}

sync_code() {
    step "Заливаю код в $REMOTE_DIR"
    remote "${SUDO}mkdir -p '$REMOTE_DIR'"

    tar -czf - -C "$PROJECT_DIR" \
        --exclude=.git \
        --exclude=.venv \
        --exclude=venv \
        --exclude=workspace \
        --exclude=state.json \
        --exclude=.env \
        --exclude=deploy/deploy.env \
        --exclude=__pycache__ \
        --exclude='*.pyc' \
        . | remote_in "${SUDO}tar -xzf - -C '$REMOTE_DIR'"

    step "Заливаю .env (права 600)"
    remote_in "${SUDO}tee '$REMOTE_DIR/.env' >/dev/null && ${SUDO}chmod 600 '$REMOTE_DIR/.env'" \
        < "$PROJECT_DIR/.env"

    # Контейнер работает под uid 10001 и должен писать в примонтированную папку.
    remote "${SUDO}mkdir -p '$REMOTE_DIR/workspace' \
        && ${SUDO}chown -R $CONTAINER_UID:$CONTAINER_UID '$REMOTE_DIR/workspace'"
    echo "код на месте"
}

clear_kill_flag() {
    remote "${SUDO}rm -f '$REMOTE_DIR/workspace/.killed'"
}

cmd_install() {
    check_env
    check_ssh
    ensure_docker
    sync_code
    step "Собираю образ и запускаю (первый раз это несколько минут)"
    clear_kill_flag
    compose "up -d --build"
    cmd_status
    printf '\nГотово. Живой лог: ./deploy/deploy.sh logs\n'
}

cmd_update() {
    check_env
    check_ssh
    sync_code
    step "Пересобираю и перезапускаю"
    compose "up -d --build"
    cmd_status
}

cmd_start() {
    step "Запускаю (снимаю метку стоп-фразы, если она была)"
    clear_kill_flag
    compose "up -d"
    cmd_status
}

cmd_stop() {
    step "Останавливаю"
    compose "stop"
}

cmd_restart() {
    step "Перезапускаю"
    clear_kill_flag
    compose "restart"
    cmd_status
}

cmd_logs()   { remote_tty "cd '$REMOTE_DIR' && ${SUDO}docker compose logs -f --tail=100"; }
cmd_shell()  { remote_tty "cd '$REMOTE_DIR' && ${SUDO}docker compose exec bot bash"; }

cmd_status() {
    step "Состояние"
    compose "ps"
    if remote "test -f '$REMOTE_DIR/workspace/.killed'"; then
        printf '\nБот выключен стоп-фразой. Включить: ./deploy/deploy.sh start\n'
    fi
}

cmd_uninstall() {
    printf '\nЭто удалит контейнер, образ, тома и папку %s на сервере.\n' "$REMOTE_DIR"
    read -r -p "Точно? [y/N] " answer
    [[ "$answer" == [yY] ]] || die "отменено"
    compose "down -v --rmi local" || true
    remote "${SUDO}rm -rf '$REMOTE_DIR'"
    echo "снесено"
}

case "${1:-install}" in
    install)   cmd_install ;;
    update)    cmd_update ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    logs)      cmd_logs ;;
    status)    cmd_status ;;
    shell)     cmd_shell ;;
    uninstall) cmd_uninstall ;;
    *)
        printf 'Неизвестная команда: %s\n' "$1" >&2
        usage >&2
        exit 1
        ;;
esac

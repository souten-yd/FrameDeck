#!/bin/sh
# FrameDeck QPKG service controller for QNAP QTS.
# Application/runtime files live under the QPKG install directory. Mutable
# settings/database/cache/logs live on the containing data volume so an app
# upgrade can safely replace the QPKG directory.

QPKG_NAME="FrameDeck"
QPKG_CONF="/etc/config/qpkg.conf"
QPKG_ROOT="$(/sbin/getcfg "$QPKG_NAME" Install_Path -d "" -f "$QPKG_CONF")"

if [ -z "$QPKG_ROOT" ] || [ ! -d "$QPKG_ROOT" ]; then
    echo "FrameDeck: unable to resolve QPKG install path" >&2
    exit 1
fi

APP_DIR="$QPKG_ROOT/app"
PYTHON="$QPKG_ROOT/runtime/python/bin/python3"

resolve_volume_root() {
    volume_root="$(/sbin/getcfg SHARE_DEF defVolMP -d "" -f /etc/config/def_share.info 2>/dev/null)"
    if [ -n "$volume_root" ] && [ -d "$volume_root" ]; then
        printf '%s\n' "$volume_root"
        return 0
    fi

    # /share/<volume>/.qpkg/FrameDeck -> /share/<volume>
    volume_root="$(dirname "$(dirname "$QPKG_ROOT")")"
    if [ -n "$volume_root" ] && [ -d "$volume_root" ]; then
        printf '%s\n' "$volume_root"
        return 0
    fi
    return 1
}

VOLUME_ROOT="$(resolve_volume_root)" || {
    echo "FrameDeck: unable to resolve QNAP data volume" >&2
    exit 1
}
VAR_DIR="${FRAMEDECK_HOME:-$VOLUME_ROOT/.framedeck}"
PID_FILE="$VAR_DIR/runtime/qpkg/framedeck.pid"
SERVICE_LOG="$VAR_DIR/logs/qpkg-service.log"
TMPDIR="$VAR_DIR/runtime/tmp"

export FRAMEDECK_HOME="$VAR_DIR"
export FRAMEDECK_MODE="web"
export FRAMEDECK_HOST="${FRAMEDECK_HOST:-0.0.0.0}"
export FRAMEDECK_PORT="${FRAMEDECK_PORT:-9000}"
export FRAMEDECK_OPEN_BROWSER="0"
export PATH="$QPKG_ROOT/bin:$QPKG_ROOT/runtime/python/bin:$PATH"
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
export HOME="$VAR_DIR"
export TMPDIR
export QNAP_QPKG="$QPKG_NAME"

migrate_legacy_data() {
    legacy="$QPKG_ROOT/var"
    [ -d "$legacy" ] || return 0
    if [ ! -e "$VAR_DIR/config/settings.json" ] && \
       [ ! -e "$VAR_DIR/data/framedeck.db" ]; then
        mkdir -p "$VAR_DIR"
        cp -a "$legacy"/. "$VAR_DIR"/ 2>/dev/null || true
    fi
}

mkdir_runtime() {
    mkdir -p "$VAR_DIR/config" "$VAR_DIR/data" "$VAR_DIR/cache" \
        "$VAR_DIR/logs" "$VAR_DIR/runtime/qpkg" "$TMPDIR"
}

is_running() {
    [ -f "$PID_FILE" ] || return 1
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

start_service() {
    if is_running; then
        echo "FrameDeck is already running (PID $(cat "$PID_FILE"))"
        return 0
    fi

    if [ ! -x "$PYTHON" ]; then
        echo "FrameDeck: bundled Python runtime not found: $PYTHON" >&2
        return 1
    fi
    if [ ! -d "$APP_DIR/framedeck" ]; then
        echo "FrameDeck: application files not found: $APP_DIR" >&2
        return 1
    fi

    migrate_legacy_data
    mkdir_runtime
    rm -f "$PID_FILE"

    cd "$APP_DIR" || return 1
    nohup "$PYTHON" -m framedeck \
        --mode web \
        --host "$FRAMEDECK_HOST" \
        --port "$FRAMEDECK_PORT" \
        --home "$FRAMEDECK_HOME" \
        >>"$SERVICE_LOG" 2>&1 &
    pid=$!
    echo "$pid" >"$PID_FILE"
    sleep 1

    if ! kill -0 "$pid" 2>/dev/null; then
        echo "FrameDeck failed to start. See $SERVICE_LOG" >&2
        rm -f "$PID_FILE"
        return 1
    fi
    echo "FrameDeck started (PID $pid, port $FRAMEDECK_PORT)"
}

stop_service() {
    if ! is_running; then
        rm -f "$PID_FILE"
        echo "FrameDeck is not running"
        return 0
    fi
    pid="$(cat "$PID_FILE")"
    kill "$pid" 2>/dev/null || true

    count=0
    while kill -0 "$pid" 2>/dev/null && [ "$count" -lt 20 ]; do
        sleep 1
        count=$((count + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "FrameDeck stopped"
}

case "$1" in
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        stop_service
        start_service
        ;;
    status)
        if is_running; then
            echo "FrameDeck is running (PID $(cat "$PID_FILE"))"
            exit 0
        fi
        echo "FrameDeck is stopped"
        exit 1
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}" >&2
        exit 2
        ;;
esac

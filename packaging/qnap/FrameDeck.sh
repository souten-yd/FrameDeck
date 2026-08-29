#!/bin/sh

QPKG_NAME="FrameDeck"
QPKG_CONF="/etc/config/qpkg.conf"
QPKG_ROOT="$(/sbin/getcfg "$QPKG_NAME" Install_Path -d "" -f "$QPKG_CONF")"

if [ -z "$QPKG_ROOT" ] || [ ! -d "$QPKG_ROOT" ]; then
    echo "FrameDeck: unable to resolve QPKG install path" >&2
    exit 1
fi

APP_DIR="$QPKG_ROOT/app"
VAR_DIR="$QPKG_ROOT/var"
PYTHON="$QPKG_ROOT/runtime/python/bin/python3"
PID_FILE="$VAR_DIR/runtime/framedeck.pid"
SERVICE_LOG="$VAR_DIR/logs/service.log"

export FRAMEDECK_HOME="$VAR_DIR"
export FRAMEDECK_MODE="web"
export FRAMEDECK_HOST="0.0.0.0"
export FRAMEDECK_PORT="${FRAMEDECK_PORT:-9000}"
export FRAMEDECK_OPEN_BROWSER="0"
export PATH="$QPKG_ROOT/bin:$QPKG_ROOT/runtime/python/bin:$PATH"
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
export HOME="$VAR_DIR"
export TMPDIR="$VAR_DIR/runtime/tmp"

mkdir -p "$VAR_DIR/config" "$VAR_DIR/data" "$VAR_DIR/cache" \
    "$VAR_DIR/logs" "$VAR_DIR/runtime" "$TMPDIR"

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
    rm -f "$PID_FILE"
    if [ ! -x "$PYTHON" ]; then
        echo "FrameDeck: bundled Python runtime not found: $PYTHON" >&2
        return 1
    fi
    if [ ! -d "$APP_DIR/framedeck" ]; then
        echo "FrameDeck: application files not found: $APP_DIR" >&2
        return 1
    fi

    cd "$APP_DIR" || return 1
    nohup "$PYTHON" -m framedeck >>"$SERVICE_LOG" 2>&1 &
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

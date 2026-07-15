#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/compose.yaml"
BACKUP_HELPER="$ROOT_DIR/deploy/sqlite_backup.py"
SERVICE="mail-sync"
IMAGE_NAME="lantian-mail-sync:latest"
ROLLBACK_IMAGE="lantian-mail-sync:rollback"
HEALTH_PATH="/api/live"
ROLLBACK_HEALTH_PATH="/login.html"
HEALTH_TIMEOUT_SECONDS="${DEPLOY_HEALTH_TIMEOUT_SECONDS:-180}"
BACKUPS_TO_KEEP="${MAIL_SYNC_DEPLOY_BACKUP_KEEP:-10}"

OLD_COMMIT=""
OLD_IMAGE_ID=""
OLD_IMAGE_REF=""
OLD_CONTAINER_ID=""
ROLLBACK_READY=0
REPLACEMENT_STARTED=0

log() {
    printf '[deploy] %s\n' "$*"
}

compose() {
    docker compose -f "$COMPOSE_FILE" "$@"
}

service_container_id() {
    compose ps -q "$SERVICE"
}

container_is_running() {
    local container_id="$1"

    [[ -n "$container_id" ]] \
        && [[ "$(docker inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null)" == "true" ]]
}

wait_for_health() {
    local container_id
    local deadline
    local last_status=""
    local running
    local status

    container_id="$(service_container_id)"
    if [[ -z "$container_id" ]]; then
        log "service container was not created"
        return 1
    fi

    deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
    while (( SECONDS < deadline )); do
        running="$(docker inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null || true)"
        status="$(
            docker inspect \
                --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
                "$container_id" 2>/dev/null || true
        )"

        if [[ "$running" != "true" ]]; then
            log "container stopped before becoming healthy"
            compose logs --tail 120 "$SERVICE" || true
            return 1
        fi
        if [[ "$status" == "healthy" ]]; then
            log "container health check passed"
            return 0
        fi
        if [[ "$status" != "$last_status" ]]; then
            log "container health status: ${status:-unknown}"
            last_status="$status"
        fi
        sleep 2
    done

    log "health check timed out after ${HEALTH_TIMEOUT_SECONDS}s"
    compose logs --tail 120 "$SERVICE" || true
    return 1
}

verify_http() {
    local health_path="$1"
    local container_id
    local internal_port
    local http_status
    local published_address
    local published_port
    local url

    container_id="$(service_container_id)"
    [[ -n "$container_id" ]] || return 1

    internal_port="$(
        docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container_id" \
            | sed -n 's/^MAIL_SYNC_WEB_PORT=//p' \
            | tail -n 1
    )"
    internal_port="${internal_port:-24000}"
    published_address="$(docker port "$container_id" "${internal_port}/tcp" 2>/dev/null | sed -n '1p')"

    if [[ -n "$published_address" ]] && command -v curl >/dev/null 2>&1; then
        published_port="${published_address##*:}"
        url="http://127.0.0.1:${published_port}${health_path}"
        http_status="$(
            curl --silent --show-error --max-time 10 \
                --output /dev/null --write-out '%{http_code}' "$url"
        )"
        [[ "$http_status" =~ ^2[0-9][0-9]$ ]]
        log "HTTP verification passed: $url"
        return 0
    fi

    docker exec "$container_id" python3 -c \
        'import http.client, os, sys; connection = http.client.HTTPConnection("127.0.0.1", int(os.environ.get("MAIL_SYNC_WEB_PORT", "24000")), timeout=10); connection.request("GET", sys.argv[1]); response = connection.getresponse(); response.read(1); connection.close(); raise SystemExit(0 if 200 <= response.status < 300 else 1)' \
        "$health_path"
    log "HTTP verification passed inside the container: $health_path"
}

backup_database() {
    local backup_name="$1"
    local -a backup_environment=(
        -e "MAIL_SYNC_BACKUP_NAME=$backup_name"
        -e "MAIL_SYNC_DEPLOY_OLD_COMMIT=$OLD_COMMIT"
        -e "MAIL_SYNC_DEPLOY_OLD_IMAGE_ID=$OLD_IMAGE_ID"
        -e "MAIL_SYNC_DEPLOY_OLD_IMAGE_REF=$OLD_IMAGE_REF"
        -e "MAIL_SYNC_DEPLOY_BACKUP_KEEP=$BACKUPS_TO_KEEP"
    )

    if container_is_running "$OLD_CONTAINER_ID"; then
        compose exec -T "${backup_environment[@]}" "$SERVICE" python3 - \
            < "$BACKUP_HELPER"
        return 0
    fi

    if [[ -n "$OLD_IMAGE_ID" && -d "$ROOT_DIR/data" ]]; then
        docker run --rm -i \
            --read-only \
            --tmpfs /tmp:size=32m,mode=1777 \
            -e MAIL_SYNC_STATE_DB=/app/data/mail_sync_state.sqlite3 \
            "${backup_environment[@]}" \
            -v "$ROOT_DIR/data:/app/data" \
            --entrypoint python3 \
            "$OLD_IMAGE_ID" - \
            < "$BACKUP_HELPER"
        return 0
    fi

    log "no existing container/database image found; deployment backup skipped"
}

rollback() {
    local original_exit_code="$1"
    local failed_line="$2"

    if (( BASH_SUBSHELL > 0 )); then
        return "$original_exit_code"
    fi

    trap - ERR INT TERM
    set +e
    log "deployment failed near line $failed_line (exit $original_exit_code)"

    if (( ROLLBACK_READY )); then
        docker image tag "$ROLLBACK_IMAGE" "$IMAGE_NAME"
        if (( REPLACEMENT_STARTED )); then
            log "restoring previous image: $OLD_IMAGE_ID"
            MAIL_SYNC_HEALTH_PATH="$ROLLBACK_HEALTH_PATH" \
                compose up -d --no-build --force-recreate "$SERVICE"
            if wait_for_health && verify_http "$ROLLBACK_HEALTH_PATH"; then
                log "previous image restored successfully"
            else
                log "automatic rollback could not be fully verified"
                compose logs --tail 120 "$SERVICE"
            fi
        else
            log "previous container was not replaced; restored the latest image tag only"
        fi
    else
        log "no previous image was available for automatic rollback"
    fi

    compose ps
    exit "$original_exit_code"
}

if ! [[ "$HEALTH_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
    printf 'DEPLOY_HEALTH_TIMEOUT_SECONDS must be a positive integer.\n' >&2
    exit 2
fi
if ! [[ "$BACKUPS_TO_KEEP" =~ ^[1-9][0-9]*$ ]]; then
    printf 'MAIL_SYNC_DEPLOY_BACKUP_KEEP must be a positive integer.\n' >&2
    exit 2
fi

cd "$ROOT_DIR"

OLD_COMMIT="$(git rev-parse HEAD)"
OLD_CONTAINER_ID="$(service_container_id 2>/dev/null || true)"
if [[ -n "$OLD_CONTAINER_ID" ]]; then
    OLD_IMAGE_ID="$(docker inspect --format '{{.Image}}' "$OLD_CONTAINER_ID")"
    OLD_IMAGE_REF="$(docker inspect --format '{{.Config.Image}}' "$OLD_CONTAINER_ID")"
else
    OLD_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE_NAME" 2>/dev/null || true)"
    OLD_IMAGE_REF="$IMAGE_NAME"
fi

log "previous commit: $OLD_COMMIT"
log "previous image: ${OLD_IMAGE_ID:-none} (${OLD_IMAGE_REF:-unknown})"

if [[ -n "$OLD_IMAGE_ID" ]]; then
    docker image tag "$OLD_IMAGE_ID" "$ROLLBACK_IMAGE"
    ROLLBACK_READY=1
fi

trap 'rollback $? "$LINENO"' ERR
trap 'rollback 130 "$LINENO"' INT
trap 'rollback 143 "$LINENO"' TERM

backup_database "deploy-$(date -u +%Y%m%dT%H%M%SZ)-${OLD_COMMIT:0:12}.sqlite3"

git pull --ff-only
NEW_COMMIT="$(git rev-parse HEAD)"
log "building commit: $NEW_COMMIT"
compose build "$SERVICE"
NEW_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE_NAME")"
log "built image: $NEW_IMAGE_ID"

REPLACEMENT_STARTED=1
MAIL_SYNC_HEALTH_PATH="$HEALTH_PATH" compose up -d --no-build "$SERVICE"
wait_for_health
verify_http "$HEALTH_PATH"

trap - ERR INT TERM
log "deployment completed: $OLD_COMMIT -> $NEW_COMMIT"
compose ps

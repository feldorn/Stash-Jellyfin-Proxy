#!/bin/bash
# Dev helper for stash-jellyfin-proxy. Run from repo root.
set -e

COMPOSE_FILE="docker-compose.dev.yml"
CONTAINER_PROXY="sjp-proxy-dev"
DATA_DIR="./dev-data"
CONF_FILE="${DATA_DIR}/stash_jellyfin_proxy.conf"
EXAMPLE_FILE="./stash_jellyfin_proxy.dev.conf.example"
LAN_IP="192.168.0.200"

cd "$(dirname "$0")"

cmd="${1:-help}"
shift || true

require_conf() {
  if [ ! -f "$CONF_FILE" ]; then
    echo "No $CONF_FILE. Run: ./dev.sh init" >&2
    exit 1
  fi
}

case "$cmd" in
  init)
    mkdir -p "$DATA_DIR"
    if [ -f "$CONF_FILE" ]; then
      echo "Dev conf already exists at $CONF_FILE - not overwriting."
    else
      cp "$EXAMPLE_FILE" "$CONF_FILE"
      SERVER_ID=$(python3 -c "import uuid; print(uuid.uuid4().hex)")
      sed -i "s/^SERVER_ID =$/SERVER_ID = ${SERVER_ID}/" "$CONF_FILE"
      echo "Created $CONF_FILE with fresh SERVER_ID=${SERVER_ID}"
      echo
      echo "Now edit $CONF_FILE and fill in:"
      echo "  STASH_URL, STASH_API_KEY, SJS_USER, SJS_PASSWORD"
    fi
    ;;
  up)
    require_conf
    docker compose -f "$COMPOSE_FILE" up -d "$@"
    echo
    echo "Endpoints:"
    echo "  http://${LAN_IP}:18096   proxy Jellyfin API (point clients here)"
    echo "  http://${LAN_IP}:18097   proxy Web UI"
    echo "  http://${LAN_IP}:18098   jellyfin-web browser UI"
    ;;
  down)
    docker compose -f "$COMPOSE_FILE" down "$@"
    ;;
  restart)
    docker compose -f "$COMPOSE_FILE" restart "$@"
    ;;
  rebuild)
    docker compose -f "$COMPOSE_FILE" build --no-cache "$@"
    docker compose -f "$COMPOSE_FILE" up -d "$@"
    ;;
  logs)
    docker compose -f "$COMPOSE_FILE" logs -f "${@:-proxy-dev}"
    ;;
  tail)
    tail -F "${DATA_DIR}/stash_jellyfin_proxy.log"
    ;;
  shell)
    docker exec -it "$CONTAINER_PROXY" /bin/bash
    ;;
  ps)
    docker compose -f "$COMPOSE_FILE" ps
    ;;
  status)
    echo "=== compose ==="
    docker compose -f "$COMPOSE_FILE" ps
    echo
    echo "=== endpoint health ==="
    curl -sk -o /dev/null -w "  proxy  18096 : HTTP %{http_code}\n" --max-time 3 "http://localhost:18096/System/Info/Public" || echo "  proxy  18096 : DOWN"
    curl -sk -o /dev/null -w "  webui  18097 : HTTP %{http_code}\n" --max-time 3 "http://localhost:18097/" || echo "  webui  18097 : DOWN"
    curl -sk -o /dev/null -w "  jfweb  18098 : HTTP %{http_code}\n" --max-time 3 "http://localhost:18098/" || echo "  jfweb  18098 : DOWN"
    ;;
  curl)
    path="${1:-/System/Info/Public}"
    shift || true
    curl -sk "http://localhost:18096${path}" "$@"
    ;;
  help|*)
    cat <<EOF
Usage: ./dev.sh <command> [args]

  init           Create ./dev-data/stash_jellyfin_proxy.conf from example (with fresh SERVER_ID)
  up             Start proxy-dev + jellyfin-web
  down           Stop and remove containers
  restart [svc]  Restart services
  rebuild        No-cache rebuild of proxy-dev image and restart
  logs [svc]     docker compose logs -f (default: proxy-dev)
  tail           tail -F the proxy log file on host
  shell          bash shell inside proxy-dev container
  ps             compose ps
  status         one-shot endpoint health check
  curl PATH      curl http://localhost:18096<PATH>

Ports:
  18096  proxy Jellyfin API    (point Swiftfin / jellyfin-web here)
  18097  proxy Web UI
  18098  jellyfin-web browser UI
EOF
    ;;
esac

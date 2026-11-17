#!/usr/bin/env bash
# Build the production image + test image, start Redis and the API on an isolated
# Docker network, run the test suite, then tear everything down.
#
#   tests/run.sh            run everything
#   KEEP=1 tests/run.sh     leave containers running afterwards (inspect `docker logs eventapi-test-app`)
#
# The private `events` dependency is fetched with your `gh` token, passed as a BuildKit
# secret (never stored in an image layer), same as CI does with DB_REPO_TOKEN.
set -euo pipefail
cd "$(dirname "$0")/.."

NET=eventapi-test
REDIS=eventapi-test-redis
APP=eventapi-test-app
REDIS_PASSWORD=test-redis-pass
SECRET=pdl_ntfset_test_secret
# Same as tests/conftest.py: two origins, one with a trailing slash (normalized by config.parse_origins)
WEBSITE_ORIGIN="https://website.test, https://www.website.test/"

teardown() {
  docker rm -f "$APP" "$REDIS" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then teardown; fi
}
trap cleanup EXIT
teardown

echo "==> Building production image (event-api:local)"
GIT_CREDENTIALS="https://x-access-token:$(gh auth token)@github.com" \
  docker build --secret id=git_credentials,env=GIT_CREDENTIALS -t event-api:local .

echo "==> Building test image (event-api:test)"
docker build -f tests/Dockerfile -t event-api:test .

echo "==> Starting Redis + API"
docker network create "$NET" >/dev/null
docker run -d --name "$REDIS" --network "$NET" redis:7.4 redis-server --requirepass "$REDIS_PASSWORD" >/dev/null
docker run -d --name "$APP" --network "$NET" -p 127.0.0.1:18000:8000 \
  -e REDIS_HOST="$REDIS" -e REDIS_PORT=6379 -e REDIS_PASSWORD="$REDIS_PASSWORD" \
  -e PADDLE_WEBHOOK_SECRET="$SECRET" -e WEBSITE_ORIGIN="$WEBSITE_ORIGIN" \
  event-api:local >/dev/null

for _ in $(seq 1 30); do
  curl -sf http://127.0.0.1:18000/events/v1/openapi.json >/dev/null && break
  sleep 1
done

echo "==> Running tests"
docker run --rm --network "$NET" \
  -e REDIS_HOST="$REDIS" -e REDIS_PORT=6379 -e REDIS_PASSWORD="$REDIS_PASSWORD" \
  -e PADDLE_WEBHOOK_SECRET="$SECRET" -e WEBSITE_ORIGIN="$WEBSITE_ORIGIN" \
  -e E2E_BASE_URL="http://$APP:8000" \
  event-api:test python -m pytest tests -v -p no:cacheprovider -rxXs "$@"

#!/usr/bin/env bash
# Build both images with Cloud Build and deploy them to Cloud Run.
#   PROJECT=my-project ./deploy.sh v2              # tag to build and deploy
#   PROJECT=my-project SKIP_BUILD=1 ./deploy.sh v1
set -euo pipefail

TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
PROJECT="${PROJECT:?set PROJECT to your Google Cloud project id}"
REGION="${REGION:-us-central1}"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/commentary-box"
API_SA="commentary-box-api@${PROJECT}.iam.gserviceaccount.com"
GCLOUD="${GCLOUD:-gcloud}"

cd "$(dirname "$0")"

if [[ -z "${SKIP_BUILD:-}" ]]; then
  "$GCLOUD" builds submit --project "$PROJECT" --region "$REGION" --config cloudbuild.yaml --substitutions "_TAG=${TAG}" .
fi

# API: holds the Gemini Live relay sessions. One instance keeps uploads and the session that uses them
# on the same machine; WebSockets need the 60-minute request timeout and CPU outside requests for downloads.
"$GCLOUD" run deploy commentary-box-api --project "$PROJECT" --region "$REGION" \
  --image "${REPO}/api:${TAG}" \
  --service-account "$API_SA" \
  --cpu 4 --memory 4Gi --concurrency 20 --min-instances 0 --max-instances 1 \
  --timeout 3600 --no-cpu-throttling --session-affinity --execution-environment gen2 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=${REGION},CACHE_BUCKET=${PROJECT}-commentary-box-cache" \
  --allow-unauthenticated --quiet

API_URL="$("$GCLOUD" run services describe commentary-box-api --project "$PROJECT" --region "$REGION" --format 'value(status.url)')"
API_HOST="${API_URL#https://}"

# Web: static UI + same-origin proxy to the API (keeps WebSocket and upload URLs relative).
"$GCLOUD" run deploy commentary-box-web --project "$PROJECT" --region "$REGION" \
  --image "${REPO}/web:${TAG}" \
  --cpu 1 --memory 512Mi --concurrency 200 --min-instances 0 --max-instances 3 --timeout 3600 \
  --set-env-vars "BACKEND_URL=${API_URL},BACKEND_HOST=${API_HOST}" \
  --allow-unauthenticated --quiet

echo "API: ${API_URL}"
echo "App: $("$GCLOUD" run services describe commentary-box-web --project "$PROJECT" --region "$REGION" --format 'value(status.url)')"

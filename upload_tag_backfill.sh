#!/usr/bin/env bash
set -euo pipefail

URL='https://arr-source-tagger.your.domain.tld/backfill/uploading'
SECRET='YOUR_SECRET_HERE'
PAYLOAD='{"arr":"both","dry_run":false}' # put your desired payload here

resp_file=""
code_file=""
err_file=""

cleanup() {
  [[ -n "${resp_file-}" ]] && rm -f "$resp_file" || true
  [[ -n "${code_file-}" ]] && rm -f "$code_file" || true
  [[ -n "${err_file-}"  ]] && rm -f "$err_file"  || true

  if [[ -t 1 ]]; then
    tput cnorm 2>/dev/null || true
  fi
}
trap cleanup EXIT

spinner() {
  local pid="$1"
  local msg="${2:-Working...}"
  local frames='|/-\'
  local i=0

  [[ -t 1 ]] || return 0

  tput civis 2>/dev/null || true
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r%s %c' "$msg" "${frames:i++%${#frames}:1}"
    sleep 0.1
  done
  printf '\r\033[2K'
  tput cnorm 2>/dev/null || true
}

main() {
  echo "Tagging recent uploads in the Arrs now"
  sleep 1
  echo

  resp_file="$(mktemp)"
  code_file="$(mktemp)"
  err_file="$(mktemp)"

  curl -sS \
    -X POST "$URL" \
    -H 'Content-Type: application/json' \
    -H "X-Webhook-Secret: $SECRET" \
    -d "$PAYLOAD" \
    -o "$resp_file" \
    -w '%{http_code}' >"$code_file" 2>"$err_file" &
  local curl_pid=$!

  spinner "$curl_pid" "Calling API - this may take a while... but as long as you see the spinner, it's working 🤓"
  wait "$curl_pid" || true

  local http_code
  http_code="$(cat "$code_file" 2>/dev/null || true)"

  echo "HTTP: ${http_code:-unknown}"
  echo

  if [[ -s "$err_file" ]]; then
    echo "curl stderr:"
    sed 's/^/  /' "$err_file"
    echo
  fi

  if jq -e . >/dev/null 2>&1 <"$resp_file"; then
    # FIX: show dry_run even if it's false (jq's // treats false as fallback)
    jq -r '
      def shown(v):
        if v == null then "n/a" else (v|tostring) end;

      [
        "Status: \(.status // "n/a")",
        "Category: \(.category // "n/a")",
        "Dry-run: \(shown(.dry_run))",
        "Processed torrents: \(.processed_torrents // 0)",
        "Matched: Radarr=\(.matched_radarr // 0), Sonarr(full season)=\(.matched_sonarr_full_season // 0)",
        "Tagged: Radarr=\(.tagged_radarr // 0), Sonarr=\(.tagged_sonarr // 0)",
        "Skipped: \(.skipped // 0)",
        "Tag: \(.uploading_tag // "n/a")"
      ] | .[]' <"$resp_file"

    echo
    echo "Full JSON:"
    jq . <"$resp_file"

    # Fail if HTTP is not 2xx or API status is not ok
    if [[ -n "${http_code:-}" && ! "${http_code}" =~ ^2 ]]; then
      echo "ERROR: Non-2xx HTTP status: ${http_code}" >&2
      exit 1
    fi

    if [[ "$(jq -r '.status // empty' <"$resp_file")" != "ok" ]]; then
      echo "ERROR: API returned non-ok status" >&2
      exit 1
    fi
  else
    echo "Response is not valid JSON; raw output:"
    echo "----------------------------------------"
    cat "$resp_file"

    if [[ -n "${http_code:-}" && ! "${http_code}" =~ ^2 ]]; then
      exit 1
    fi
  fi

  echo
  echo "Done!"
}

main "$@"

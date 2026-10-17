#!/usr/bin/env bash
# svg2png.sh <file.svg> [scale]  -- rasterize an SVG (and its -dark sibling) with rsvg-convert.
set -euo pipefail

RSVG="${RSVG_CONVERT:-$(command -v rsvg-convert || true)}"
[ -z "$RSVG" ] && [ -x /opt/homebrew/bin/rsvg-convert ] && RSVG=/opt/homebrew/bin/rsvg-convert
if [ -z "$RSVG" ]; then
  echo "svg2png: missing required tool: rsvg-convert" >&2
  exit 1
fi
if ! command -v sips >/dev/null 2>&1; then
  echo "svg2png: missing required tool: sips" >&2
  exit 1
fi

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: svg2png.sh <file.svg> [scale]" >&2
  exit 2
fi

SRC="$1"
SCALE="${2:-2}"
if [ ! -f "$SRC" ]; then
  echo "svg2png: file not found: $SRC" >&2
  exit 1
fi
case "$SCALE" in
  ''|*[!0-9.]*) echo "svg2png: scale must be a positive number, got: $SCALE" >&2; exit 2 ;;
esac

warn_unsupported() {
  local f="$1"
  grep -q '<foreignObject' "$f" \
    && echo "svg2png: WARNING $f contains <foreignObject>; rsvg will not render it" >&2 || true
  grep -q '@import' "$f" \
    && echo "svg2png: WARNING $f contains @import; rsvg will not load it" >&2 || true
  grep -Eq "(url\(|src=|href=)[[:space:]]*[\"']?https?://|font[^>]*https?://" "$f" \
    && echo "svg2png: WARNING $f references an http(s):// URL (remote font or resource); rsvg will not fetch it" >&2 || true
  grep -Eq "<image[[:space:]][^>]*(xlink:)?href=[\"'](https?:)?//" "$f" \
    && echo "svg2png: WARNING $f has <image> tags with external hrefs; rsvg will not render them" >&2 || true
  return 0
}

convert_one() {
  local svg="$1"
  local out="${svg%.svg}.png"
  warn_unsupported "$svg"
  if ! "$RSVG" -z "$SCALE" -o "$out" "$svg"; then
    echo "svg2png: rsvg-convert failed on $svg" >&2
    exit 1
  fi
  local w h
  w="$(sips -g pixelWidth "$out" | awk '/pixelWidth/ {print $2}')"
  h="$(sips -g pixelHeight "$out" | awk '/pixelHeight/ {print $2}')"
  echo "$out ${w}x${h}"
}

convert_one "$SRC"

base="${SRC%.svg}"
DARK="${base}-dark.svg"
# do not treat a dark file passed in as the main input as needing its own dark sibling
if [ -f "$DARK" ] && [ "$SRC" != "$DARK" ]; then
  convert_one "$DARK"
fi

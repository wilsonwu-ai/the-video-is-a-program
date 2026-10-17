#!/usr/bin/env bash
# make_video.sh <repo_dir> [--preview] [--gif-start S] [--gif-len N] [--voice NAME] [--rate WPM]
# Builds narration + frames + mp4/srt/gif/contact sheet from <repo_dir>/video/{beats.json,composition.html}.
# Progress goes to stderr; stdout carries exactly one JSON summary line.
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RENDER_MJS="${RENDER_MJS:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/render.mjs}"
export PATH="/opt/homebrew/bin:$PATH"

log() { echo "make_video: $*" >&2; }
die() { echo "make_video: $*" >&2; exit 1; }

usage() {
  echo "usage: make_video.sh <repo_dir> [--preview] [--gif-start S] [--gif-len N] [--voice NAME] [--rate WPM]" >&2
  exit 2
}

# ---------- dependencies (fail fast) ----------
for tool in ffmpeg ffprobe say node python3 sips stat; do
  command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool"
done
[ -f "$RENDER_MJS" ] || die "missing required file: $RENDER_MJS (render.mjs)"

# ---------- args ----------
[ "$#" -ge 1 ] || usage
REPO="$1"; shift
PREVIEW=0
OPT_VOICE=""; OPT_RATE=""; OPT_GIF_START=""; OPT_GIF_LEN=""
is_num() { case "$1" in ''|*[!0-9.]*|*.*.*) return 1 ;; *) return 0 ;; esac; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --preview)   PREVIEW=1; shift ;;
    --gif-start) [ "$#" -ge 2 ] || usage; OPT_GIF_START="$2"; shift 2 ;;
    --gif-len)   [ "$#" -ge 2 ] || usage; OPT_GIF_LEN="$2"; shift 2 ;;
    --voice)     [ "$#" -ge 2 ] || usage; OPT_VOICE="$2"; shift 2 ;;
    --rate)      [ "$#" -ge 2 ] || usage; OPT_RATE="$2"; shift 2 ;;
    -h|--help)   usage ;;
    *) echo "make_video: unknown argument: $1" >&2; usage ;;
  esac
done
[ -d "$REPO" ] || die "repo_dir not found: $REPO"
REPO="$(cd "$REPO" && pwd)"
VID="$REPO/video"
BEATS="$VID/beats.json"
COMP="$VID/composition.html"
[ -f "$BEATS" ] || die "missing $BEATS"
[ -f "$COMP" ] || die "missing $COMP"
for v in "$OPT_GIF_START" "$OPT_GIF_LEN" "$OPT_RATE"; do
  if [ -n "$v" ] && ! is_num "$v"; then die "numeric flag expected, got: $v"; fi
done

cfg() {
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
v = d.get(sys.argv[2])
print("" if v is None else v)
' "$BEATS" "$1"
}

SLUG="$(cfg slug)"
[ -n "$SLUG" ] || die "beats.json has no slug"
case "$SLUG" in */*|*..*) die "unsafe slug: $SLUG" ;; esac
VOICE="${OPT_VOICE:-$(cfg voice)}"; VOICE="${VOICE:-Samantha}"
RATE="${OPT_RATE:-$(cfg rate)}";   RATE="${RATE:-175}"
GIF_START="${OPT_GIF_START:-$(cfg gif_start)}"; GIF_START="${GIF_START:-0}"
GIF_LEN="${OPT_GIF_LEN:-$(cfg gif_len)}";       GIF_LEN="${GIF_LEN:-12}"
if [ "$PREVIEW" -eq 1 ]; then FPS=8; CRF=28; MP4="$VID/$SLUG.preview.mp4"; else FPS=30; CRF=23; MP4="$VID/$SLUG.mp4"; fi
SRT="$VID/$SLUG.srt"
log "slug=$SLUG voice=$VOICE rate=$RATE preview=$PREVIEW fps=$FPS"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/make_video.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$VID/audio"

# ---------- a. TTS ----------
log "a. TTS"
rm -f "$VID"/audio/*.aiff "$VID"/audio/*.txt
python3 - "$BEATS" "$VID/audio" "$TMP/ids.txt" <<'PY'
import json, re, sys
beats_path, audio_dir, ids_path = sys.argv[1:4]
d = json.load(open(beats_path))
beats = d.get("beats") or []
if not beats:
    sys.exit("make_video: beats.json has no beats")
ids = []
for b in beats:
    bid = str(b.get("id", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", bid):
        sys.exit("make_video: bad beat id: %r" % bid)
    if bid in ids:
        sys.exit("make_video: duplicate beat id: %s" % bid)
    text = str(b.get("narration", "")).strip()
    if not text:
        sys.exit("make_video: beat %s has empty narration" % bid)
    ids.append(bid)
    open("%s/%s.txt" % (audio_dir, bid), "w", encoding="utf-8").write(text)
open(ids_path, "w").write("\n".join(ids) + "\n")
PY

: > "$TMP/durations.tsv"
while IFS= read -r bid; do
  [ -n "$bid" ] || continue
  say -v "$VOICE" -r "$RATE" -o "$VID/audio/$bid.aiff" -f "$VID/audio/$bid.txt" \
    || die "say failed for beat $bid (voice '$VOICE' installed?)"
  dur="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$VID/audio/$bid.aiff")"
  [ -n "$dur" ] || die "ffprobe could not read duration of audio/$bid.aiff"
  printf '%s\t%s\n' "$bid" "$dur" >> "$TMP/durations.tsv"
done < "$TMP/ids.txt"

python3 - "$BEATS" "$TMP/durations.tsv" "$VID/timing.json" "$VID/timing.js" "$TMP/offsets.tsv" <<'PY'
import json, sys
beats_path, dur_path, tj, tjs, off_path = sys.argv[1:6]
beats = json.load(open(beats_path))["beats"]
dur = {}
for line in open(dur_path):
    k, v = line.rstrip("\n").split("\t")
    dur[k] = float(v)
out, cum = [], 0.0
offs = []
for b in beats:
    bid = str(b["id"])
    ln = round(max(float(b.get("min_s", 0) or 0), dur[bid] + 0.6), 3)
    start = round(cum, 3)
    out.append({"id": bid, "start": start, "len": ln, "caption": b.get("caption", "")})
    offs.append("%s\t%d" % (bid, round(start * 1000)))
    cum = start + ln
total = round(cum, 3)
t = {"beats": out, "total": total}
s = json.dumps(t, ensure_ascii=False)
open(tj, "w", encoding="utf-8").write(s + "\n")
open(tjs, "w", encoding="utf-8").write("window.TIMING = " + s + ";")
open(off_path, "w").write("\n".join(offs) + "\n")
PY
TOTAL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["total"])' "$VID/timing.json")"
log "timing total=${TOTAL}s"

# ---------- b. narration track ----------
log "b. narration.m4a"
FF_IN=(); FILTER=""; MIX_IN=""; N=0
while IFS=$'\t' read -r bid ms; do
  [ -n "$bid" ] || continue
  FF_IN+=(-i "$VID/audio/$bid.aiff")
  FILTER+="[$N:a]aresample=48000,aformat=channel_layouts=stereo,adelay=${ms}:all=1[a$N];"
  MIX_IN+="[a$N]"
  N=$((N + 1))
done < "$TMP/offsets.tsv"
[ "$N" -ge 1 ] || die "no beats to mix"
if [ "$N" -eq 1 ]; then
  FILTER+="${MIX_IN}apad[aout]"
else
  FILTER+="${MIX_IN}amix=inputs=${N}:normalize=0:duration=longest,apad[aout]"
fi
ffmpeg -hide_banner -loglevel error -nostdin -y "${FF_IN[@]}" \
  -filter_complex "$FILTER" -map "[aout]" -t "$TOTAL" -ar 48000 -c:a aac -b:a 160k \
  "$VID/narration.m4a"

# ---------- c. frames ----------
log "c. frames at ${FPS} fps"
mkdir -p "$VID/frames"
rm -f "$VID"/frames/f*.jpg
if ! node "$RENDER_MJS" "$COMP" "$VID/frames" --fps "$FPS" >"$TMP/render.out" 2>"$TMP/render.err"; then
  echo "make_video: render.mjs failed:" >&2
  cat "$TMP/render.err" >&2
  exit 1
fi
cat "$TMP/render.err" >&2 || true
RJSON="$(grep -E '^\{' "$TMP/render.out" | tail -n 1 || true)"
NFRAMES="$(find "$VID/frames" -maxdepth 1 -name 'f*.jpg' | wc -l | tr -d ' ')"
[ "$NFRAMES" -gt 0 ] || die "render.mjs produced no frames in $VID/frames"
if [ -n "$RJSON" ]; then
  log "render: $RJSON"
  python3 - "$RJSON" "$TOTAL" "$FPS" <<'PY' >&2 || true
import json, sys
r = json.loads(sys.argv[1]); total = float(sys.argv[2]); fps = float(sys.argv[3])
d = r.get("duration")
if d is not None and abs(float(d) - total) > 0.15:
    print("make_video: WARNING window.DURATION (%s) differs from timing total (%s)" % (d, total))
if r.get("fps") is not None and abs(float(r["fps"]) - fps) > 0.01:
    print("make_video: WARNING render fps (%s) differs from requested (%s)" % (r["fps"], fps))
PY
fi

# ---------- d. mux ----------
log "d. mux -> $(basename "$MP4")"
ffmpeg -hide_banner -loglevel error -nostdin -y \
  -framerate "$FPS" -i "$VID/frames/f%06d.jpg" -i "$VID/narration.m4a" \
  -c:v libx264 -preset medium -crf "$CRF" -pix_fmt yuv420p -movflags +faststart \
  -c:a aac -b:a 160k -shortest "$MP4"

# ---------- e. SRT ----------
log "e. SRT"
python3 - "$BEATS" "$VID/timing.json" "$SRT" <<'PY'
import json, sys
beats = json.load(open(sys.argv[1]))["beats"]
timing = json.load(open(sys.argv[2]))["beats"]
def ts(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)
lines = []
for i, (b, t) in enumerate(zip(beats, timing), 1):
    text = " ".join(str(b.get("narration", "")).split())
    lines += [str(i), "%s --> %s" % (ts(t["start"]), ts(t["start"] + t["len"])), text, ""]
open(sys.argv[3], "w", encoding="utf-8").write("\n".join(lines))
PY

# ---------- f. GIF ----------
GIF="$VID/preview.gif"
GIF_MB="null"
if [ "$PREVIEW" -eq 0 ]; then
  log "f. GIF"
  # clamp the window into [0, TOTAL]
  read -r GS GL < <(python3 - "$GIF_START" "$GIF_LEN" "$TOTAL" <<'PY'
import sys
s, n, t = (float(x) for x in sys.argv[1:4])
n = min(n, t)
s = max(0.0, min(s, t - n))
print("%.3f %.3f" % (s, n))
PY
)
  MAX_BYTES=$((8 * 1024 * 1024))
  for attempt in "12 720" "10 600" "8 540"; do
    set -- $attempt
    gfps="$1"; gw="$2"
    ffmpeg -hide_banner -loglevel error -nostdin -y -ss "$GS" -t "$GL" -i "$MP4" \
      -filter_complex "[0:v]fps=${gfps},scale=${gw}:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5" \
      -loop 0 "$GIF"
    bytes="$(stat -f%z "$GIF")"
    log "gif ${gfps}fps ${gw}px -> ${bytes} bytes"
    [ "$bytes" -le "$MAX_BYTES" ] && break
  done
  GIF_MB="$(python3 -c 'import sys; print(round(int(sys.argv[1])/1048576, 2))' "$bytes")"
  [ "$bytes" -le "$MAX_BYTES" ] || log "WARNING final GIF is still ${GIF_MB} MB (> 8 MB)"
else
  log "f. GIF skipped (preview)"
fi

# ---------- g. contact sheet ----------
log "g. contact sheet"
ROWS="$(python3 -c '
import math, sys
t = float(sys.argv[1])
n = max(1, math.ceil(t / 4.0))
print(max(1, math.ceil(n / 5)))
' "$TOTAL")"
ffmpeg -hide_banner -loglevel error -nostdin -y -i "$MP4" \
  -vf "fps=1/4,scale=384:-1,tile=5x${ROWS}" -frames:v 1 -update 1 "$VID/contact.png"

# ---------- h. probe + summary ----------
log "h. probe"
ffprobe -v quiet -print_format json -show_format -show_streams "$MP4" > "$VID/probe.json"
python3 - "$VID/probe.json" "$MP4" "$GIF_MB" "$SRT" "$VID/contact.png" "$TOTAL" <<'PY'
import json, os, sys
probe = json.load(open(sys.argv[1]))
mp4, gif_mb, srt, contact, total = sys.argv[2:7]
streams = probe.get("streams", [])
v = next((s for s in streams if s.get("codec_type") == "video"), {})
audio = any(s.get("codec_type") == "audio" for s in streams)
def rate(r):
    try:
        a, b = r.split("/")
        return round(float(a) / float(b), 3)
    except Exception:
        return None
out = {
    "mp4": mp4,
    "duration_s": round(float(probe["format"]["duration"]), 3),
    "width": v.get("width"),
    "height": v.get("height"),
    "fps": rate(v.get("r_frame_rate", "0/1")),
    "audio": audio,
    "size_mb": round(os.path.getsize(mp4) / 1048576, 2),
    "gif_mb": None if gif_mb == "null" else float(gif_mb),
    "srt": srt,
    "contact": contact,
    "timing_total": float(total),
}
print(json.dumps(out))
PY

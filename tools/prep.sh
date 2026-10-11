#!/usr/bin/env bash
# Get a video ready for the site without losing quality.
#
#   tools/prep.sh clip.mov             -> videos/clip.mp4   lossless remux (default)
#   tools/prep.sh --encode clip.mov    -> videos/clip.mp4   re-encode at near-lossless quality
#   tools/prep.sh clip.mov out.mp4     -> out.mp4
#
# Remux copies the exact video and audio streams, bit for bit, into a web
# MP4 with its index moved to the front (+faststart) so playback starts
# before the whole file has downloaded. Resolution, fps and bitrate are
# untouched. Only use --encode when the codec doesn't play in browsers
# (ProRes, DNxHD, ...) — this script switches to it automatically then.
set -euo pipefail

mode=remux
if [[ "${1:-}" == "--encode" ]]; then
  mode=encode
  shift
fi
in="${1:?usage: tools/prep.sh [--encode] input [output]}"
name="$(basename "${in%.*}")"
out="${2:-videos/${name}.mp4}"
mkdir -p "$(dirname "$out")"

stream() { ffprobe -v error -select_streams "$1" -show_entries "stream=$2" -of csv=p=0 "$in" | head -n1; }
vcodec="$(stream v:0 codec_name)"
pix_fmt="$(stream v:0 pix_fmt)"
acodec="$(stream a:0 codec_name || true)"
echo "Input: $(stream v:0 width)x$(stream v:0 height) @ $(stream v:0 avg_frame_rate) fps, $vcodec $pix_fmt, audio: ${acodec:-none}"

case "$vcodec" in
  h264 | hevc | av1 | vp9) ;;
  *)
    if [[ $mode == remux ]]; then
      echo "'$vcodec' doesn't play in browsers, so re-encoding instead of remuxing."
      mode=encode
    fi
    ;;
esac

case "$acodec" in
  "" | aac | mp3) audio=(-c:a copy) ;;
  *) audio=(-c:a aac -b:a 320k) ;;
esac

if [[ $mode == remux ]]; then
  video=(-c:v copy)
  # Safari only plays HEVC in MP4 when it's tagged hvc1
  [[ $vcodec == hevc ]] && video+=(-tag:v hvc1)
elif [[ $pix_fmt == *10le* || $pix_fmt == *10be* || $pix_fmt == *12le* ]]; then
  # keep 10-bit HDR as 10-bit HEVC; squeezing it into 8-bit H.264 would wash it out
  video=(-c:v libx265 -preset slow -crf 14 -pix_fmt yuv420p10le -tag:v hvc1)
else
  video=(-c:v libx264 -preset slow -crf 12 -pix_fmt yuv420p -profile:v high)
fi

# No -r, -s or -vf: frame rate and resolution stay exactly as recorded.
ffmpeg -hide_banner -loglevel warning -stats -y -i "$in" -map 0:v:0 -map '0:a:0?' "${video[@]}" "${audio[@]}" -movflags +faststart "$out"

size=$(stat -c %s "$out" 2>/dev/null || stat -f %z "$out")
duration=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$out")
mbps=$(awk -v s="$size" -v d="$duration" 'BEGIN { printf "%.0f", s * 8 / d / 1e6 }')
echo "Done ($mode): $out — $((size / 1024 / 1024)) MB, ~${mbps} Mbps"

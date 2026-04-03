#!/usr/bin/env bash
#
# Projects/run_workflow.sh
#
# Resumable pipeline runner with improved output layout:
#   All outputs for a run are placed in: ./output/<YYYYMMDD_HHMMSS>/
#     - merged.mp3
#     - merged.mp4
#     - script.txt  (copied from project root if exists)
#     - clips/      (copied snapshot of clips/)
#     - segments/   (copied snapshot of segments/)
#
# Features:
# - Steps: split -> tts -> merge -> move+collect -> video -> clean
# - Resumable markers kept in .workflow_state/
# - Can run only the video step with --only-video and --merged-path
# - Preserves original segments/clips into the run folder before running clean
#
# Usage examples:
#   ./run_workflow.sh
#   ./run_workflow.sh --resume
#   ./run_workflow.sh --only-video --merged-path output/20260101_123456/merged.mp3
#   ./run_workflow.sh --out-dir output --voice zh-CN-XiaoxiaoNeural
#
set -euo pipefail

# Defaults
VOICE="zh-CN-XiaoxiaoNeural"
SEGMENT_DIR="segments"
CLIPS_DIR="clips"
CLIPS_MERGED_REL="${CLIPS_DIR}/merged.mp3"
OUT_BASE="output"
DO_CLEAN=1
REENCODE=0
PYTHON_BIN="${PYTHON_BIN:-python3}"
VAAPI_DEVICE="/dev/dri/renderD128"
VIDEO_RES="1920:1080"
VIDEO_BITRATE="5M"
AUDIO_BITRATE="192k"
OVERWRITE_FLAG=""
RESUME=0
ONLY_VIDEO=0
MERGED_PATH_OVERRIDE=""
UPLOAD=1
DELETE=0

STATE_DIR=".workflow_state"
mkdir -p "${STATE_DIR}"

print_help() {
  cat <<EOF
Usage: $0 [options]

Options:
  --voice <voice>         Voice shortname (default: ${VOICE})
  --out-dir <dir>         Output base directory (default: ${OUT_BASE})
  --resume                Skip steps already marked completed in ${STATE_DIR}/
  --only-video            Only run video creation (requires --merged-path or existing merged_*.mp3 in --out-dir/<ts>)
  --merged-path <path>    Use this MP3 as the merged input for the video step (overrides auto detection)
  --no-clean              Do not run clean_all.py at the end
  --reencode              Pass --reencode to merge_clips.py
  --overwrite             Pass --overwrite to tts_batch.py
  --vaapi-device <dev>    VAAPI device to use (default: ${VAAPI_DEVICE})
  --upload                Upload video using biliup after creation (default: true, use --no-upload to disable)
  --no-upload             Do not upload the video
  --delete                Delete output files after successful upload (default: false)
  -h, --help              Show this help
EOF
}

# helpers
check_cmd() {
  command -v "$1" >/dev/null 2>&1
}

mark_done() {
  local step="$1"
  touch "${STATE_DIR}/${step}.done"
}

is_done() {
  local step="$1"
  [ -f "${STATE_DIR}/${step}.done" ]
}

run_python() {
  local script="$1"
  shift
  if [ ! -f "${script}" ]; then
    echo "ERROR: ${script} not found in project root." >&2
    return 2
  fi
  "${PYTHON_BIN}" "${script}" "$@"
}

# parse args
while [ $# -gt 0 ]; do
  case "$1" in
    --voice) VOICE="$2"; shift 2;;
    --out-dir) OUT_BASE="$2"; shift 2;;
    --resume) RESUME=1; shift;;
    --only-video) ONLY_VIDEO=1; shift;;
    --merged-path) MERGED_PATH_OVERRIDE="$2"; shift 2;;
    --no-clean) DO_CLEAN=0; shift;;
    --reencode) REENCODE=1; shift;;
    --overwrite) OVERWRITE_FLAG="--overwrite"; shift;;
    --vaapi-device) VAAPI_DEVICE="$2"; shift 2;;
    --upload) UPLOAD=1; shift;;
    --no-upload) UPLOAD=0; shift;;
    --delete) DELETE=1; shift;;
    -h|--help) print_help; exit 0;;
    *) echo "Unknown arg: $1"; print_help; exit 2;;
  esac
done

# normalize paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "[workflow] Project root: ${SCRIPT_DIR}"
echo "[workflow] Voice: ${VOICE}"
echo "[workflow] Output base dir: ${OUT_BASE}"
echo "[workflow] Resume: $([ "${RESUME}" -eq 1 ] && echo 'ON' || echo 'OFF')"
echo "[workflow] Only video: $([ "${ONLY_VIDEO}" -eq 1 ] && echo 'ON' || echo 'OFF')"
echo "[workflow] VAAPI device: ${VAAPI_DEVICE}"
echo "[workflow] Upload: $([ "${UPLOAD}" -eq 1 ] && echo 'ON' || echo 'OFF')"
echo "[workflow] Delete after upload: $([ "${DELETE}" -eq 1 ] && echo 'ON' || echo 'OFF')"
echo

# Step names
STEP_GENERATE="generate"
STEP_SPLIT="split"
STEP_TTS="tts"
STEP_MERGE="merge"
STEP_COLLECT="collect"
STEP_VIDEO="video"
STEP_CLEAN="clean"

# If we will run python steps, ensure python exists (skip if only-video)
if [ "${ONLY_VIDEO}" -eq 0 ]; then
  if ! check_cmd "${PYTHON_BIN}"; then
    echo "ERROR: python3 not found. Please install Python 3 or set PYTHON_BIN." >&2
    exit 3
  fi
fi

# Warn if ffmpeg missing; video step will fail without it
if ! check_cmd ffmpeg; then
  echo "WARN: ffmpeg not found on PATH. Video step will fail if requested." >&2
fi

# ---------- STEP: generate (create script) ----------
if [ "${ONLY_VIDEO}" -eq 0 ]; then
  if [ "${RESUME}" -eq 1 ] && is_done "${STEP_GENERATE}"; then
    echo "[workflow] Skipping generate (marked done)."
  else
    echo "[workflow] Step: generate_script.py"
    run_python generate_script.py || { echo "[workflow] generate_script.py failed"; exit 4; }
    mark_done "${STEP_GENERATE}"
  fi
fi

# ---------- STEP: split (create segments) ----------
if [ "${ONLY_VIDEO}" -eq 0 ]; then
  if [ "${RESUME}" -eq 1 ] && is_done "${STEP_SPLIT}"; then
    echo "[workflow] Skipping split (marked done)."
  else
    echo "[workflow] Step: split_segments.py"
    LATEST_SCRIPT_DIR="$(ls -1td scripts/*/ 2>/dev/null | head -n 1 || true)"
    if [ -n "${LATEST_SCRIPT_DIR}" ] && [ -f "${LATEST_SCRIPT_DIR}script.txt" ]; then
      SCRIPT_INPUT="${LATEST_SCRIPT_DIR}script.txt"
    else
      SCRIPT_INPUT="script.txt"
    fi
    echo "[workflow] Using script for split: ${SCRIPT_INPUT}"
    run_python split_segments.py --input "${SCRIPT_INPUT}" || { echo "[workflow] split_segments.py failed"; exit 5; }
    mark_done "${STEP_SPLIT}"
  fi
fi

# ---------- STEP: tts (synthesize clips) ----------
if [ "${ONLY_VIDEO}" -eq 0 ]; then
  if [ "${RESUME}" -eq 1 ] && is_done "${STEP_TTS}"; then
    echo "[workflow] Skipping tts (marked done)."
  else
    echo "[workflow] Step: tts_batch.py"
    if [ ! -f "tts_batch.py" ]; then
      echo "ERROR: tts_batch.py not found." >&2
      exit 6
    fi
    mkdir -p "${CLIPS_DIR}"
    TTS_ARGS=( --segment-dir "${SEGMENT_DIR}" --out-dir "${CLIPS_DIR}" --voice "${VOICE}" )
    if [ -n "${OVERWRITE_FLAG}" ]; then
      TTS_ARGS+=( ${OVERWRITE_FLAG} )
    fi
    if "${PYTHON_BIN}" tts_batch.py "${TTS_ARGS[@]}"; then
      mark_done "${STEP_TTS}"
    else
      echo "[workflow] tts_batch.py reported failure. Aborting." >&2
      exit 7
    fi
  fi
fi

# ---------- STEP: merge (concatenate clips) ----------
if [ "${ONLY_VIDEO}" -eq 0 ]; then
  if [ "${RESUME}" -eq 1 ] && is_done "${STEP_MERGE}"; then
    echo "[workflow] Skipping merge (marked done)."
  else
    echo "[workflow] Step: merge_clips.py"
    if [ ! -f "merge_clips.py" ]; then
      echo "ERROR: merge_clips.py not found." >&2
      exit 8
    fi
    MERGE_ARGS=( --input-dir "${CLIPS_DIR}" --output "${CLIPS_MERGED_REL}" )
    if [ "${REENCODE}" -eq 1 ]; then
      MERGE_ARGS+=( --reencode )
    fi
    if "${PYTHON_BIN}" merge_clips.py "${MERGE_ARGS[@]}"; then
      mark_done "${STEP_MERGE}"
    else
      echo "[workflow] merge_clips.py failed." >&2
      exit 9
    fi
  fi
fi

# ---------- STEP: collect / move outputs into timestamped run dir ----------
# Determine run timestamp and destination folder
RUN_TS="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${OUT_BASE}/${RUN_TS}"
mkdir -p "${RUN_DIR}"

# Determine source MP3 we will use for the video step
SOURCE_MP3=""
if [ -n "${MERGED_PATH_OVERRIDE}" ]; then
  if [ ! -f "${MERGED_PATH_OVERRIDE}" ]; then
    echo "ERROR: --merged-path specified but file does not exist: ${MERGED_PATH_OVERRIDE}" >&2
    exit 10
  fi
  SOURCE_MP3="${MERGED_PATH_OVERRIDE}"
  # copy override into run dir as merged.mp3
  cp -f "${SOURCE_MP3}" "${RUN_DIR}/merged.mp3"
  SOURCE_MP3="${RUN_DIR}/merged.mp3"
  echo "[workflow] Copied provided merged mp3 into run dir: ${SOURCE_MP3}"
else
  # If merge step produced CLIPS_MERGED_REL, move it into run dir
  if [ -f "${CLIPS_MERGED_REL}" ]; then
    mv -f "${CLIPS_MERGED_REL}" "${RUN_DIR}/merged.mp3"
    SOURCE_MP3="${RUN_DIR}/merged.mp3"
    echo "[workflow] Moved merged mp3 to run dir: ${SOURCE_MP3}"
  else
    # If ONLY_VIDEO, try to pick latest existing merged in OUT_BASE/*/merged_*.mp3
    if [ "${ONLY_VIDEO}" -eq 1 ]; then
      latest="$(ls -1t ${OUT_BASE}/*/merged.mp3 2>/dev/null | head -n1 || true)"
      if [ -z "${latest}" ]; then
        echo "ERROR: --only-video requested but no merged.mp3 found in ${OUT_BASE}/*/merged.mp3. Use --merged-path." >&2
        exit 11
      fi
      # copy it into run dir for consistent layout
      cp -f "${latest}" "${RUN_DIR}/merged.mp3"
      SOURCE_MP3="${RUN_DIR}/merged.mp3"
      echo "[workflow] Copied latest merged mp3 into run dir: ${SOURCE_MP3}"
    else
      echo "ERROR: Expected merged mp3 at ${CLIPS_MERGED_REL} but not found." >&2
      exit 12
    fi
  fi
fi

# Save related artifacts into run dir (snapshot)
# Copy segments and clips if they exist
if [ -d "${SEGMENT_DIR}" ]; then
  cp -r "${SEGMENT_DIR}" "${RUN_DIR}/segments"
fi
if [ -d "${CLIPS_DIR}" ]; then
  # If clips dir still exists, copy snapshot
  cp -r "${CLIPS_DIR}" "${RUN_DIR}/clips"
fi
# Copy cover.jpg if present
if [ -f "cover.jpg" ]; then
  cp -f "cover.jpg" "${RUN_DIR}/cover.jpg"
fi
# Copy original script.txt and biliup_config.json if present
LATEST_SCRIPT_DIR="$(ls -1td scripts/*/ 2>/dev/null | head -n 1 || true)"
if [ -n "${LATEST_SCRIPT_DIR}" ]; then
  if [ -f "${LATEST_SCRIPT_DIR}script.txt" ]; then
    cp -f "${LATEST_SCRIPT_DIR}script.txt" "${RUN_DIR}/script.txt"
  fi
  if [ -f "${LATEST_SCRIPT_DIR}biliup_config.json" ]; then
    cp -f "${LATEST_SCRIPT_DIR}biliup_config.json" "${RUN_DIR}/biliup_config.json"
  fi
elif [ -f "script.txt" ]; then
  cp -f "script.txt" "${RUN_DIR}/script.txt"
fi

mark_done "${STEP_COLLECT}"
echo "[workflow] Collected run artifacts into ${RUN_DIR}"

# ---------- STEP: video creation ----------
if [ -n "${SOURCE_MP3}" ] && [ -f "${SOURCE_MP3}" ]; then
  VIDEO_OUT="${RUN_DIR}/merged.mp4"
  echo "[workflow] Creating video: ${VIDEO_OUT} from ${RUN_DIR}/cover.jpg and ${SOURCE_MP3}"

  if [ ! -f "${RUN_DIR}/cover.jpg" ]; then
    echo "WARN: cover.jpg not found in run dir (${RUN_DIR}). Video will be generated with missing cover if ffmpeg allows." >&2
  fi

  # Choose VAAPI if device exists and ffmpeg available
  if [ -e "${VAAPI_DEVICE}" ] && command -v ffmpeg >/dev/null 2>&1; then
    echo "[workflow] Using VAAPI device: ${VAAPI_DEVICE}"
    set +e
    ffmpeg -vaapi_device "${VAAPI_DEVICE}" -y \
      -loop 1 -framerate 1 -i "${RUN_DIR}/cover.jpg" \
      -i "${SOURCE_MP3}" \
      -vf format=nv12,scale=${VIDEO_RES},hwupload \
      -c:v h264_vaapi -b:v ${VIDEO_BITRATE} \
      -c:a aac -b:a ${AUDIO_BITRATE} -ac 2 -ar 44100 \
      -shortest "${VIDEO_OUT}"
    RC=$?
    set -e
  else
    echo "[workflow] VAAPI not available or ffmpeg missing - using software encoding"
    ffmpeg -y -loop 1 -framerate 1 -i "${RUN_DIR}/cover.jpg" -i "${SOURCE_MP3}" \
      -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p \
      -c:a aac -b:a ${AUDIO_BITRATE} -ac 2 -ar 44100 \
      -shortest "${VIDEO_OUT}"
    RC=$?
  fi

  if [ ${RC} -ne 0 ]; then
    echo "[workflow] ffmpeg failed to create video (rc=${RC})." >&2
  else
    echo "[workflow] Video created: ${VIDEO_OUT}"
    mark_done "${STEP_VIDEO}"

    if [ "${UPLOAD}" -eq 1 ]; then
      echo "[workflow] Step: upload (biliup)"
      if [ -f "${RUN_DIR}/biliup_config.json" ]; then
        if [ -x "${SCRIPT_DIR}/biliup" ]; then
          BILIUP_CMD="${SCRIPT_DIR}/biliup"
        elif check_cmd biliup; then
          BILIUP_CMD="biliup"
        else
          BILIUP_CMD=""
        fi

        if [ -n "${BILIUP_CMD}" ]; then
          echo "[workflow] Uploading with biliup..."
          UPLOAD_SUCCESS=0
          if ( cd "${RUN_DIR}" && "${BILIUP_CMD}" upload -c biliup_config.json ); then
            UPLOAD_SUCCESS=1
          fi

          if [ "${UPLOAD_SUCCESS}" -eq 1 ]; then
            echo "[workflow] Upload successful."
            if [ "${DELETE}" -eq 1 ]; then
              echo "[workflow] Deleting run directory ${RUN_DIR} due to --delete flag..."
              rm -rf "${RUN_DIR}"
            fi
          else
            echo "[workflow] biliup upload failed." >&2
          fi
        else
          echo "[workflow] Cannot upload: biliup command not found." >&2
        fi
      else
        echo "[workflow] Cannot upload: biliup_config.json not found in ${RUN_DIR}." >&2
      fi
    fi
  fi
else
  echo "[workflow] No source MP3 available for video step; skipping." >&2
fi

# ---------- STEP: optional clean ----------
if [ "${DO_CLEAN}" -eq 1 ] && [ "${ONLY_VIDEO}" -eq 0 ]; then
  if [ "${RESUME}" -eq 1 ] && is_done "${STEP_CLEAN}"; then
    echo "[workflow] Skipping clean (marked done)."
  else
    if [ -f "clean_all.py" ]; then
      echo "[workflow] Running clean_all.py --yes ..."
      "${PYTHON_BIN}" clean_all.py --yes
      mark_done "${STEP_CLEAN}"
      echo "[workflow] clean done."
    else
      echo "[workflow] clean_all.py not found; skipping clean."
    fi
  fi
else
  if [ "${DO_CLEAN}" -eq 0 ]; then
    echo "[workflow] Skipping clean step (--no-clean)."
  fi
fi

echo
if [ "${UPLOAD}" -eq 1 ] && [ "${DELETE}" -eq 1 ] && [ "${UPLOAD_SUCCESS:-0}" -eq 1 ]; then
  echo "[workflow] Completed. Run folder ${RUN_DIR} was deleted after upload."
else
  echo "[workflow] Completed. Run folder: ${RUN_DIR}"
  if [ -f "${VIDEO_OUT:-}" ]; then
    echo "[workflow] Video: ${VIDEO_OUT}"
  fi
fi
echo "[workflow] State markers are in ${STATE_DIR}/ (remove them to force re-run of steps)."
exit 0

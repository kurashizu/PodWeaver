# PodWeaver: Autonomous AI Podcast Pipeline

## 📋 Overview

**PodWeaver** is an end-to-end automated pipeline for creating AI-hosted podcasts. It utilizes LangGraph to orchestrate a reliable multi-agent architecture for script generation, processes the script into high-quality Text-to-Speech (TTS) audio, renders it into a video, and automatically uploads the final product to Bilibili using Biliup.

Specially designed to run smoothly on local LLMs, it handles interruptions, features automatic retry mechanisms, and manages the entire lifecycle of content creation from a simple text prompt to a published video.

### ✨ Core Features

✅ **Multi-Agent Architecture**: A Supervisor agent coordinates multiple Worker agents for chapter-by-chapter script generation.
✅ **Dynamic Chapter Planning**: Automatically structures podcast chapters based on your topic.
✅ **Resilient Execution**: Saves progress via LangGraph checkpointer and automatically retries failed chapters.
✅ **Automated TTS & Video Rendering**: Converts scripts to speech, merges clips, and renders an MP4 using FFmpeg (with VAAPI hardware acceleration support).
✅ **One-Click Publishing**: Automatically generates metadata (including title and description extracted by AI) and uploads the finished video to Bilibili via `biliup`.
✅ **Self-Cleaning**: Optionally cleans up temporary files and caches after a successful upload to save disk space.

## 🏗️ Architecture & Workflow

The pipeline is unified under a single bash script (`run_workflow.sh`) which seamlessly connects the following stages:

```text
1. Prompt & Config ──> 2. LangGraph Script Generation ──> ./scripts/[timestamp]/script.txt
   (Planner Agent)     (Supervisor + Worker Agents)       ./scripts/[timestamp]/biliup_config.json
                                                                │
┌───────────────────────────────────────────────────────────────┘
│
▼
3. Split Segments ──> 4. Batch TTS ──> 5. Audio Merge ──> 6. Video Render (FFmpeg)
(Sentence chunks)     (TTS engine)     (Combined MP3)     (Combined MP4 with Cover)
                                                                │
┌───────────────────────────────────────────────────────────────┘
│
▼
7. Auto Upload (Biliup) ──> 8. Cleanup (--delete)
```

## 📦 Installation

```bash
# 1. Install Python dependencies
pip install -r requirements.txt
pip install -r requirements_langgraph.txt

# 2. Setup FFmpeg
# Ensure ffmpeg is installed and available in your system PATH.

# 3. Setup Biliup (for auto-upload)
# Download the biliup CLI tool and place the executable in the project root.
# Run `./biliup login` to generate `cookies.json`.
```

## ⚙️ Configuration

### 1. Main Config (`config.json`)
The system uses `config.json` for model settings, prompt paths, and Bilibili upload metadata:

```json
{
    "ollama": {
        "base_url": "http://localhost:11435/v1",
        "model": "gemma4:e4b-it-q8_0",
        "temperature": 0.7,
        "max_tokens": 8000
    },
    "podcast": {
        "user_prompt_file": "./prompts/user_prompt.txt",
        "planner_prompt_file": "./prompts/planner_prompt.txt",
        "supervisor_prompt_file": "./prompts/supervisor_prompt.txt",
        "worker_prompt_file": "./prompts/worker_prompt.txt"
    },
    "biliup_config_default": {
        "line": "kodo",
        "limit": 3,
        "streamers": {
            "视频patterns1*": {
                "copyright": 1,
                "source": "转载来源",
                "tid": 171,
                "cover": "",
                "title": "标题",
                "desc_format_id": 0,
                "desc": "简介",
                "open_subtitle": false
            }
        }
    }
}
```

### 2. Prompt Files (`./prompts/`)
- `user_prompt.txt`: Contains the main topic and instructions for the podcast.
- `planner_prompt.txt`: Instructs the LLM on how to generate the dynamic chapter outline and video description.
- `supervisor_prompt.txt`: The system prompt instructing the Supervisor agent.
- `worker_prompt.txt`: The system prompt instructing the Worker agents.

## 🚀 Usage

The entire pipeline is heavily automated. Just define your topic in `prompts/user_prompt.txt`, place a `cover.jpg` in the root folder, and run the workflow.

### Run Full Automated Pipeline
```bash
./run_workflow.sh
```
*This generates the script, synthesizes audio, creates the video, and automatically uploads to Bilibili (keeping local output files).*

### Run Locally Without Uploading
```bash
./run_workflow.sh --no-upload
```

### Fire and Forget (Upload & Delete Local Cache)
```bash
./run_workflow.sh --delete
```
*Uploads the final video and cleans up all temporary directories (`clips/`, `segments/`, and `output/[timestamp]/`) upon success to save disk space.*

### Handling Interruptions & Resuming
If the workflow is interrupted (e.g., via `Ctrl+C`), progress is automatically check-pointed.
```bash
./run_workflow.sh --resume
```
*This skips successfully completed steps.*

### Advanced Workflow Arguments
```bash
Usage: ./run_workflow.sh [options]

Options:
  --voice <voice>         Voice shortname (default: zh-CN-XiaoxiaoNeural)
  --out-dir <dir>         Output base directory (default: output)
  --resume                Skip steps already marked completed in .workflow_state/
  --only-video            Only run video creation
  --merged-path <path>    Use this MP3 as the merged input for the video step
  --vaapi-device <dev>    VAAPI device to use (default: /dev/dri/renderD128)
  --upload                Upload video using biliup after creation (default: true)
  --no-upload             Do not upload the video
  --delete                Delete output files after successful upload (default: false)
```

## 🔍 Monitoring and Debugging

### Output Folders
All final generated artifacts are collected in timestamped directories under `output/` (e.g., `output/20231027_153000/`).
Inside, you will find:
- `merged.mp4` (Final Video)
- `merged.mp3` (Final Audio)
- `script.txt` (Full generated script)
- `biliup_config.json` (Dynamic Biliup metadata)
- `cover.jpg` (Thumbnail used)

### Troubleshooting

**1. LLM Connection Refused:**
Ensure your local model service is running and `base_url` in `config.json` is correct.

**2. Biliup Upload Fails:**
Ensure you've executed `./biliup login` and that `cookies.json` is valid and present in the project root.

**3. FFmpeg VAAPI Errors:**
If hardware acceleration fails, omit or change the `--vaapi-device`. The script will gracefully fallback to software encoding (`libx264`) if VAAPI is unavailable.

## 📄 License

This project shares the same license as the main repository.
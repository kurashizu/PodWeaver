#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

# Add project root to sys.path so 'src' module can be found
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )
except ImportError:
    print("Error: The 'rich' library is required for the UI.")
    print("Please install it by running: pip install rich")
    sys.exit(1)

console = Console()


class WorkflowRunner:
    def __init__(self, args):
        from src.config import WORKSPACE_DIR

        self.args = args
        self.python_bin = args.python_bin
        self.state_dir = WORKSPACE_DIR / ".workflow_state"

    def run_cmd_live(
        self, cmd, step_id, progress, task_id, log_queue, live_display, cwd=None
    ):
        """
        Run a command and parse its stdout in real-time to update the Rich progress bar.
        This provides granular UI updates instead of just hanging at 0%.
        """
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            env=env,
        )

        output_log = []
        for line in iter(process.stdout.readline, ""):
            output_log.append(line)
            line_str = line.strip()

            if line_str:
                log_queue.append(line_str)
                live_display.update(
                    Group(
                        progress,
                        Panel(
                            "\n".join(log_queue), title="Live Logs", border_style="blue"
                        ),
                    )
                )

            # Real-time parsing based on step
            if step_id == "generate":
                match = re.search(r"Chapters:\s*(\d+)", line_str)
                if match:
                    progress.update(task_id, total=int(match.group(1)), completed=0)
                elif "✓ Chapter completed" in line_str:
                    progress.advance(task_id)
                elif "=== GENERATING DYNAMIC CHAPTERS ===" in line_str:
                    progress.update(
                        task_id, description="[yellow]AI Script Gen (Planning)"
                    )

            elif step_id == "tts":
                if "[DONE] Saved" in line_str or "[SKIP]" in line_str:
                    progress.advance(task_id)

            elif step_id == "video":
                match = re.search(r"time=(\d{2}:\d{2}:\d{2}\.\d{2})", line_str)
                if match:
                    # Update description to show current encoded time to the user
                    progress.update(
                        task_id, description=f"[yellow]Render Video ({match.group(1)})"
                    )

            elif step_id == "upload":
                if "Upload progress" in line_str or "%" in line_str:
                    # Optional: Could parse exact percentages if biliup outputs them predictably
                    pass

        process.wait()

        # Ensure the progress bar fills up fully upon completion
        task = next(t for t in progress.tasks if t.id == task_id)
        if task.total is not None:
            progress.update(task_id, completed=task.total)
        else:
            progress.update(task_id, total=1, completed=1)

        return process.returncode, "".join(output_log)

    def get_latest_script_dir(self):
        from src.config import SCRIPTS_DIR

        scripts_dir = SCRIPTS_DIR
        if not scripts_dir.exists():
            return None
        dirs = [d for d in scripts_dir.iterdir() if d.is_dir()]
        if not dirs:
            return None
        dirs.sort(key=lambda x: x.name, reverse=True)
        return dirs[0]

    def get_latest_merged_mp3(self):
        out_base = Path(self.args.out_dir)
        if not out_base.exists():
            return None
        mp3s = list(out_base.glob("*/merged.mp3"))
        if not mp3s:
            return None
        mp3s.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return mp3s[0]

    def execute_pipeline(self, prompt_idx=1, total_prompts=1, prompt_text=None):
        from src.config import (
            CLIPS_DIR,
            SEGMENTS_DIR,
            SRC_DIR,
            WORKSPACE_DIR,
            get_asset_path,
        )

        run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        # If running in queue mode, inject the current prompt into the designated file
        if prompt_text:
            from src.config import get_prompt_path

            prompt_file = get_prompt_path(
                "user_prompt_file", "conf/prompts/user_prompt.txt"
            )

            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(prompt_text)

            # Each queue item is independent, so clear previous state markers and outputs
            if self.state_dir.exists():
                shutil.rmtree(self.state_dir)
            if (SRC_DIR / "clean_all.py").exists():
                subprocess.run(
                    [self.python_bin, str(SRC_DIR / "clean_all.py"), "--yes"],
                    capture_output=True,
                )

        self.state_dir.mkdir(parents=True, exist_ok=True)

        steps = []
        if not self.args.only_video:
            steps.extend(
                [
                    {"id": "generate", "name": "AI Script Gen"},
                    {"id": "split", "name": "Split Segments"},
                    {"id": "tts", "name": "TTS Synthesis"},
                    {"id": "merge", "name": "Merge Audio"},
                ]
            )

        steps.append({"id": "collect", "name": "Collect Artifacts"})
        steps.append({"id": "video", "name": "Render Video"})

        if self.args.upload:
            steps.append({"id": "upload", "name": "Browser Upload"})

        if self.args.clean and not self.args.only_video:
            steps.append({"id": "clean", "name": "Cache Cleanup"})

        current_run_dir = Path(self.args.out_dir) / run_ts
        cached_source_mp3 = None
        upload_success = False

        # Keep descriptions short to prevent terminal wrapping which breaks Rich progress updates
        overall_title = (
            f"Queue [{prompt_idx}/{total_prompts}]" if total_prompts > 1 else "Pipeline"
        )

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
        )
        overall_task = progress.add_task(
            f"[bold cyan]{overall_title}", total=len(steps)
        )

        log_queue = deque(maxlen=15)

        with Live(
            Group(
                progress,
                Panel("Waiting for logs...", title="Live Logs", border_style="blue"),
            ),
            console=console,
            refresh_per_second=10,
        ) as live_display:
            for step in steps:
                step_id = step["id"]
                step_name = step["name"]

                step_task = progress.add_task(f"[yellow]{step_name}", total=None)

                # Check resume marker
                if self.args.resume and (self.state_dir / f"{step_id}.done").exists():
                    progress.update(
                        step_task,
                        total=1,
                        completed=1,
                        description=f"[green]✓ {step_name} (Skipped)",
                    )
                    progress.advance(overall_task)
                    continue

                success = False
                output_log = ""

                try:
                    if step_id == "generate":
                        code, out = self.run_cmd_live(
                            [self.python_bin, "src/generate_script.py"],
                            step_id,
                            progress,
                            step_task,
                            log_queue,
                            live_display,
                        )
                        output_log = out
                        if code == 0:
                            success = True

                    elif step_id == "split":
                        progress.update(step_task, total=1)
                        latest_dir = self.get_latest_script_dir()
                        script_input = str(WORKSPACE_DIR / "script.txt")
                        if latest_dir and (latest_dir / "script.txt").exists():
                            script_input = str(latest_dir / "script.txt")
                        code, out = self.run_cmd_live(
                            [
                                self.python_bin,
                                str(SRC_DIR / "split_segments.py"),
                                "--input",
                                script_input,
                                "--outdir",
                                str(SEGMENTS_DIR),
                            ],
                            step_id,
                            progress,
                            step_task,
                            log_queue,
                            live_display,
                        )
                        output_log = out
                        if code == 0:
                            success = True

                    elif step_id == "tts":
                        CLIPS_DIR.mkdir(parents=True, exist_ok=True)

                        # Pre-calculate total segments for accurate TTS progress
                        tts_total = 0
                        seg_dir = SEGMENTS_DIR
                        if seg_dir.exists():
                            tts_total = len(list(seg_dir.glob("*.txt")))

                        if tts_total > 0:
                            progress.update(step_task, total=tts_total, completed=0)

                        cmd = [
                            self.python_bin,
                            str(SRC_DIR / "tts_batch.py"),
                            "--segment-dir",
                            str(SEGMENTS_DIR),
                            "--out-dir",
                            str(CLIPS_DIR),
                            "--voice",
                            self.args.voice,
                        ]
                        if self.args.overwrite:
                            cmd.append("--overwrite")

                        code, out = self.run_cmd_live(
                            cmd, step_id, progress, step_task, log_queue, live_display
                        )
                        output_log = out
                        if code == 0:
                            success = True

                    elif step_id == "merge":
                        progress.update(step_task, total=1)
                        cmd = [
                            self.python_bin,
                            str(SRC_DIR / "merge_clips.py"),
                            "--input-dir",
                            str(CLIPS_DIR),
                            "--output",
                            str(CLIPS_DIR / "merged.mp3"),
                        ]
                        if self.args.reencode:
                            cmd.append("--reencode")
                        code, out = self.run_cmd_live(
                            cmd, step_id, progress, step_task, log_queue, live_display
                        )
                        output_log = out
                        if code == 0:
                            success = True

                    elif step_id == "collect":
                        progress.update(step_task, total=1)
                        current_run_dir.mkdir(parents=True, exist_ok=True)

                        source_mp3 = None
                        if (
                            self.args.merged_path
                            and Path(self.args.merged_path).exists()
                        ):
                            shutil.copy(
                                self.args.merged_path, current_run_dir / "merged.mp3"
                            )
                            source_mp3 = current_run_dir / "merged.mp3"
                        elif (CLIPS_DIR / "merged.mp3").exists():
                            shutil.move(
                                CLIPS_DIR / "merged.mp3",
                                current_run_dir / "merged.mp3",
                            )
                            source_mp3 = current_run_dir / "merged.mp3"
                        elif self.args.only_video:
                            latest_mp3 = self.get_latest_merged_mp3()
                            if latest_mp3:
                                shutil.copy(latest_mp3, current_run_dir / "merged.mp3")
                                source_mp3 = current_run_dir / "merged.mp3"

                        if source_mp3:
                            cached_source_mp3 = source_mp3
                            if SEGMENTS_DIR.exists():
                                shutil.copytree(
                                    SEGMENTS_DIR,
                                    current_run_dir / "segments",
                                    dirs_exist_ok=True,
                                )
                            if CLIPS_DIR.exists():
                                shutil.copytree(
                                    CLIPS_DIR,
                                    current_run_dir / "clips",
                                    dirs_exist_ok=True,
                                )
                            if get_asset_path("cover.jpg").exists():
                                shutil.copy(
                                    get_asset_path("cover.jpg"),
                                    current_run_dir / "cover.jpg",
                                )

                            latest_dir = self.get_latest_script_dir()
                            if latest_dir:
                                if (latest_dir / "script.txt").exists():
                                    shutil.copy(
                                        latest_dir / "script.txt",
                                        current_run_dir / "script.txt",
                                    )
                                if (latest_dir / "biliup_config.yaml").exists():
                                    shutil.copy(
                                        latest_dir / "biliup_config.yaml",
                                        current_run_dir / "biliup_config.yaml",
                                    )
                            elif (WORKSPACE_DIR / "script.txt").exists():
                                shutil.copy(
                                    WORKSPACE_DIR / "script.txt",
                                    current_run_dir / "script.txt",
                                )

                            success = True
                            progress.advance(step_task)
                        else:
                            output_log = "Error: merged.mp3 not found."

                    elif step_id == "video":
                        if cached_source_mp3:
                            video_out = current_run_dir / "merged.mp4"
                            cover = current_run_dir / "cover.jpg"
                            if (
                                not cover.exists()
                                and get_asset_path("cover.jpg").exists()
                            ):
                                cover = get_asset_path("cover.jpg")

                            cmd = []
                            if Path(self.args.vaapi_device).exists() and shutil.which(
                                "ffmpeg"
                            ):
                                cmd = [
                                    "ffmpeg",
                                    "-vaapi_device",
                                    self.args.vaapi_device,
                                    "-y",
                                    "-loop",
                                    "1",
                                    "-framerate",
                                    "1",
                                    "-i",
                                    str(cover),
                                    "-i",
                                    str(cached_source_mp3),
                                    "-vf",
                                    f"format=nv12,scale={self.args.video_res},hwupload",
                                    "-c:v",
                                    "h264_vaapi",
                                    "-b:v",
                                    self.args.video_bitrate,
                                    "-c:a",
                                    "aac",
                                    "-b:a",
                                    self.args.audio_bitrate,
                                    "-ac",
                                    "2",
                                    "-ar",
                                    "44100",
                                    "-shortest",
                                    str(video_out),
                                ]
                            else:
                                cmd = [
                                    "ffmpeg",
                                    "-y",
                                    "-loop",
                                    "1",
                                    "-framerate",
                                    "1",
                                    "-i",
                                    str(cover),
                                    "-i",
                                    str(cached_source_mp3),
                                    "-c:v",
                                    "libx264",
                                    "-preset",
                                    "veryfast",
                                    "-crf",
                                    "23",
                                    "-pix_fmt",
                                    "yuv420p",
                                    "-c:a",
                                    "aac",
                                    "-b:a",
                                    self.args.audio_bitrate,
                                    "-ac",
                                    "2",
                                    "-ar",
                                    "44100",
                                    "-shortest",
                                    str(video_out),
                                ]

                            code, out = self.run_cmd_live(
                                cmd,
                                step_id,
                                progress,
                                step_task,
                                log_queue,
                                live_display,
                            )
                            output_log = out
                            if code == 0:
                                success = True
                        else:
                            progress.update(step_task, total=1, completed=1)
                            success = True

                    elif step_id == "upload":
                        progress.update(step_task, total=None)  # Indeterminate
                        yaml_path = current_run_dir / "biliup_config.yaml"
                        video_path = current_run_dir / "merged.mp4"
                        cover_path = current_run_dir / "cover.jpg"

                        if not yaml_path.exists():
                            success = False
                            output_log = "Error: biliup_config.yaml not found."
                        elif not video_path.exists():
                            success = False
                            output_log = "Error: merged.mp4 not found in run dir."
                        else:
                            cmd = [
                                self.python_bin,
                                str(SRC_DIR / "upload_video.py"),
                                "--video",
                                str(video_path),
                                "--cover",
                                str(cover_path),
                                "--yaml",
                                str(yaml_path),
                            ]
                            code, out = self.run_cmd_live(
                                cmd,
                                step_id,
                                progress,
                                step_task,
                                log_queue,
                                live_display,
                            )
                            output_log = out
                            if code == 0:
                                success = True
                                upload_success = True

                    elif step_id == "clean":
                        progress.update(step_task, total=1)
                        if (SRC_DIR / "clean_all.py").exists():
                            code, out = self.run_cmd_live(
                                [
                                    self.python_bin,
                                    str(SRC_DIR / "clean_all.py"),
                                    "--yes",
                                ],
                                step_id,
                                progress,
                                step_task,
                                log_queue,
                                live_display,
                            )
                            output_log = out
                            if code == 0:
                                success = True
                        else:
                            progress.advance(step_task)
                            success = True

                except Exception as e:
                    output_log += f"\nException: {str(e)}"

                if success:
                    (self.state_dir / f"{step_id}.done").touch()
                    progress.update(
                        step_task,
                        description=f"[green]✓ {step_name}",
                    )
                else:
                    progress.stop_task(step_task)
                    progress.update(step_task, description=f"[red]✗ {step_name} Failed")
                    console.print(
                        Panel(
                            output_log[-1500:],
                            title=f"Error Log - {step_name}",
                            border_style="red",
                        )
                    )
                    return False

                progress.advance(overall_task)

            progress.update(
                overall_task, description=f"[bold green]✓ {overall_title} - Completed!"
            )

        if self.args.upload and self.args.delete and upload_success:
            try:
                shutil.rmtree(current_run_dir)
                console.print(
                    f"[green]Successfully uploaded and cleaned up directory: {current_run_dir}[/green]"
                )
            except Exception as e:
                console.print(f"[yellow]Failed to cleanup directory: {str(e)}[/yellow]")

        return True


def main():
    parser = argparse.ArgumentParser(description="PodWeaver Workflow Runner UI")
    parser.add_argument(
        "--voice", default="zh-CN-XiaoxiaoNeural", help="Voice shortname"
    )
    from src.config import OUTPUT_DIR

    parser.add_argument(
        "--out-dir", default=str(OUTPUT_DIR), help="Output base directory"
    )
    parser.add_argument(
        "--resume", action="store_true", help="Resume from last failed step"
    )
    parser.add_argument(
        "--only-video", action="store_true", help="Only run video creation"
    )
    parser.add_argument("--merged-path", help="Path to merged mp3")
    parser.add_argument(
        "--no-clean",
        action="store_false",
        dest="clean",
        help="Do not run clean_all.py at the end",
    )
    parser.add_argument(
        "--reencode", action="store_true", help="Pass --reencode to merge_clips.py"
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Pass --overwrite to tts_batch.py"
    )
    parser.add_argument(
        "--vaapi-device", default="/dev/dri/renderD128", help="VAAPI device"
    )
    parser.add_argument(
        "--no-upload", action="store_false", dest="upload", help="Do not upload"
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete output files after successful upload",
    )
    parser.add_argument(
        "--queue", action="store_true", help="Run prompts from queue file sequentially"
    )
    parser.add_argument("--python-bin", default="python3", help="Python binary")
    parser.add_argument("--video-res", default="1920:1080", help="Video resolution")
    parser.add_argument("--video-bitrate", default="5M", help="Video bitrate")
    parser.add_argument("--audio-bitrate", default="192k", help="Audio bitrate")

    args = parser.parse_args()

    console.clear()
    console.print(
        Panel(
            "[bold magenta]PodWeaver: Autonomous AI Podcast Pipeline[/bold magenta]\n"
            "[cyan]Initializing workflow...[/cyan]",
            border_style="blue",
        )
    )

    runner = WorkflowRunner(args)

    if args.queue:
        from src.config import get_prompt_path

        try:
            queue_file = get_prompt_path(
                "user_prompt_queue_file", "conf/prompts/user_prompt_queue.json"
            )
            with open(queue_file, "r", encoding="utf-8") as f:
                queue = json.load(f)
        except Exception as e:
            console.print(f"[red]Failed to load queue file: {str(e)}[/red]")
            sys.exit(1)

        if not isinstance(queue, list) or len(queue) == 0:
            console.print("[yellow]Queue is empty or malformed. Exiting.[/yellow]")
            sys.exit(0)

        total = len(queue)
        console.print(
            f"[bold green]Found {total} podcast topics in queue. Starting sequential processing.[/bold green]"
        )

        for idx, prompt_text in enumerate(queue):
            console.print(
                f"\n[bold cyan]─── Processing Queue Task {idx + 1}/{total} ───[/bold cyan]"
            )
            success = runner.execute_pipeline(idx + 1, total, prompt_text)
            if not success:
                console.print(
                    f"[bold red]Task {idx + 1} failed. Queue execution stopped.[/bold red]"
                )
                sys.exit(1)

        console.print(
            "\n[bold green]🎉 All queue tasks completed successfully![/bold green]"
        )

    else:
        success = runner.execute_pipeline()
        if not success:
            sys.exit(1)
        console.print("\n[bold green]🎉 Pipeline completed successfully![/bold green]")


if __name__ == "__main__":
    main()

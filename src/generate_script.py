#!/usr/bin/env python3
"""
LangGraph Supervisor System for Podcast Script Generation

这个系统使用LangGraph实现一个可靠的multi-agent架构：
- 主管agent负责任务分配和进度监督
- 多个worker agent分别负责不同章节的内容生成
- 自动重试机制处理本地模型的中断问题
- 状态持久化确保进度不丢失
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

# Add project root to sys.path so 'src' module can be found
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

# ==================== Configuration ====================

# 本地模型配置 (使用OpenAI-compatible API)
LOCAL_MODEL_CONFIG = {
    "base_url": "http://localhost:11435/v1",  # OpenAI兼容端点
    "api_key": "openai",  # 需要一个非空的api_key
    "model": "gemma4:e4b-it-q8_0",  # 本地可用的模型
    "temperature": 0.7,
    "max_tokens": 4000,
}

# 脚本配置
SCRIPT_CONFIG = {
    "topic": "",
    "target_duration_minutes": 30,
    "target_words": "6000-8000",
    "output_file": "./script.txt",
}

# 章节定义
CHAPTERS = []

# 重试配置
MAX_RETRIES_PER_CHAPTER = 3
CHECKPOINT_DIR = "./.langgraph_checkpoints"


# ==================== State Definitions ====================


class ChapterStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ChapterState:
    id: str
    title: str
    status: ChapterStatus = ChapterStatus.PENDING
    content: str = ""
    attempts: int = 0
    error_message: str = ""


class AgentState(TypedDict):
    """LangGraph state that flows through the graph"""

    messages: Annotated[list[BaseMessage], add_messages]
    chapters: dict[str, ChapterState]  # chapter_id -> ChapterState
    current_chapter: str  # Currently processing chapter ID
    supervisor_decision: str  # Next action: assign_work, check_progress, finalize
    final_script: str  # Assembled final script
    iteration: int  # Track iterations to prevent infinite loops


# ==================== Agent Prompts ====================

SUPERVISOR_SYSTEM_PROMPT = ""
WORKER_SYSTEM_PROMPT = ""


# ==================== Helper Functions ====================


def create_llm():
    """Create LLM instance with local model config"""
    return ChatOpenAI(
        base_url=LOCAL_MODEL_CONFIG["base_url"],
        api_key=LOCAL_MODEL_CONFIG["api_key"],
        model=LOCAL_MODEL_CONFIG["model"],
        temperature=LOCAL_MODEL_CONFIG["temperature"],
        max_tokens=LOCAL_MODEL_CONFIG["max_tokens"],
    )


def get_chapters_summary(chapters: dict[str, ChapterState]) -> dict:
    """Get summary statistics of chapters"""
    stats = {
        "total": len(chapters),
        "completed": 0,
        "in_progress": 0,
        "pending": 0,
        "failed": 0,
    }
    for ch in chapters.values():
        if ch.status == ChapterStatus.COMPLETED:
            stats["completed"] += 1
        elif ch.status == ChapterStatus.IN_PROGRESS:
            stats["in_progress"] += 1
        elif ch.status == ChapterStatus.PENDING:
            stats["pending"] += 1
        elif ch.status == ChapterStatus.FAILED:
            stats["failed"] += 1
    return stats


def format_chapters_list(chapters: dict[str, ChapterState]) -> str:
    """Format chapters list for display"""
    lines = []
    for ch_id, ch in chapters.items():
        lines.append(
            f"- {ch_id}: {ch.title} [{ch.status.value}] (attempts: {ch.attempts})"
        )
    return "\n".join(lines)


# ==================== Agent Nodes ====================


def supervisor_node(state: AgentState) -> AgentState:
    """Supervisor agent decides what to do next"""
    print("\n=== SUPERVISOR NODE ===")

    chapters = state["chapters"]
    stats = get_chapters_summary(chapters)

    # Check if all chapters are completed
    if stats["completed"] == stats["total"]:
        print("✓ All chapters completed! Moving to finalize.")
        state["supervisor_decision"] = "finalize"
        return state

    # Find next chapter to work on
    next_chapter_id = None

    # First, check for pending chapters
    for ch_id, ch in chapters.items():
        if ch.status == ChapterStatus.PENDING:
            next_chapter_id = ch_id
            break

    # If no pending, check for failed chapters that can be retried
    if not next_chapter_id:
        for ch_id, ch in chapters.items():
            if (
                ch.status == ChapterStatus.FAILED
                and ch.attempts < MAX_RETRIES_PER_CHAPTER
            ):
                next_chapter_id = ch_id
                print(f"! Retrying failed chapter: {ch_id}")
                break

    # If we found a chapter to work on
    if next_chapter_id:
        state["current_chapter"] = next_chapter_id
        state["supervisor_decision"] = "assign_work"
        chapters[next_chapter_id].status = ChapterStatus.IN_PROGRESS
        print(f"→ Assigning work: {next_chapter_id}")
    else:
        # Check if we have any failures
        if stats["failed"] > 0:
            print("✗ Some chapters failed and exceeded retry limit")
            state["supervisor_decision"] = "report_failure"
        else:
            # This shouldn't happen, but just in case
            print("? Unexpected state, moving to finalize")
            state["supervisor_decision"] = "finalize"

    state["iteration"] += 1
    print(f"Iteration: {state['iteration']}, Stats: {stats}")

    return state


def worker_node(state: AgentState) -> AgentState:
    """Worker agent generates content for a chapter"""
    print("\n=== WORKER NODE ===")

    chapter_id = state["current_chapter"]
    chapter = state["chapters"][chapter_id]
    chapter.attempts += 1

    print(
        f"Processing: {chapter_id} - {chapter.title} (attempt {chapter.attempts}/{MAX_RETRIES_PER_CHAPTER})"
    )

    # Find chapter config
    chapter_config = next((ch for ch in CHAPTERS if ch["id"] == chapter_id), None)
    if not chapter_config:
        chapter.status = ChapterStatus.FAILED
        chapter.error_message = f"Chapter config not found: {chapter_id}"
        return state

    # Create worker prompt
    worker_prompt = WORKER_SYSTEM_PROMPT.format(
        topic=SCRIPT_CONFIG["topic"],
        chapter_title=chapter.title,
        duration=chapter_config["duration_minutes"],
        target_words=chapter_config["target_words"],
        description=chapter_config["description"],
    )

    try:
        llm = create_llm()
        messages = [
            SystemMessage(content=worker_prompt),
            HumanMessage(content=WORKER_HUMAN_PROMPT),
        ]

        print(f"Calling LLM for chapter: {chapter_id}...")
        response = llm.invoke(messages)
        content = response.content.strip()

        if not content or len(content) < 100:
            raise ValueError(
                f"Generated content too short ({len(content)} chars), likely model failure"
            )

        chapter.content = content
        chapter.status = ChapterStatus.COMPLETED
        chapter.error_message = ""
        print(f"✓ Chapter completed: {chapter_id} ({len(content)} chars)")

    except Exception as e:
        print(f"✗ Error generating chapter {chapter_id}: {e}")
        chapter.error_message = str(e)
        if chapter.attempts >= MAX_RETRIES_PER_CHAPTER:
            chapter.status = ChapterStatus.FAILED
            print(f"✗ Chapter {chapter_id} marked as FAILED (max retries exceeded)")
        else:
            chapter.status = ChapterStatus.PENDING  # Will be retried
            print(f"! Chapter {chapter_id} will be retried")

    state["chapters"][chapter_id] = chapter
    return state


def finalize_node(state: AgentState) -> AgentState:
    """Combine all chapters into final script and save to file"""
    print("\n=== FINALIZE NODE ===")

    chapters = state["chapters"]
    final_parts = []

    # Assemble chapters in order
    for chapter_config in CHAPTERS:
        ch_id = chapter_config["id"]
        chapter = chapters.get(ch_id)
        if chapter and chapter.status == ChapterStatus.COMPLETED:
            final_parts.append(chapter.content)
        else:
            print(f"⚠ Warning: Chapter {ch_id} not completed, skipping")

    final_script = "\n\n".join(final_parts)
    state["final_script"] = final_script

    # Save to file
    output_path = Path(SCRIPT_CONFIG["output_file"])
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(final_script, encoding="utf-8")
        print(f"✓ Final script saved to: {output_path.resolve()}")
        print(f"✓ Total length: {len(final_script)} characters")
        print(f"✓ Total words (approx): {len(final_script.split())}")

        import copy

        import yaml

        biliup_config = copy.deepcopy(SCRIPT_CONFIG.get("biliup", {}))

        if "streamers" in biliup_config:
            new_streamers = {}
            for pattern in biliup_config["streamers"]:
                streamer_config = biliup_config["streamers"][pattern]
                streamer_config["title"] = SCRIPT_CONFIG.get(
                    "topic", streamer_config.get("title", "")
                )
                if "desc" in SCRIPT_CONFIG:
                    streamer_config["desc"] = SCRIPT_CONFIG["desc"]
                new_streamers["merged.mp4"] = streamer_config
            biliup_config["streamers"] = new_streamers
        biliup_config_path = output_path.parent / "biliup_config.yaml"
        with open(biliup_config_path, "w", encoding="utf-8") as f:
            yaml.dump(biliup_config, f, allow_unicode=True, sort_keys=False)
        print(f"✓ Biliup_config saved to: {biliup_config_path.resolve()}")
    except Exception as e:
        print(f"✗ Error saving script: {e}")

    state["supervisor_decision"] = "done"
    return state


# ==================== Routing Logic ====================


def should_continue(state: AgentState) -> Literal["worker", "finalize", "end"]:
    """Route based on supervisor's decision"""
    decision = state.get("supervisor_decision", "")

    if decision == "assign_work":
        return "worker"
    elif decision == "finalize":
        return "finalize"
    else:
        return "end"


# ==================== Graph Construction ====================


def create_workflow():
    """Create the LangGraph workflow"""

    workflow = StateGraph(AgentState)

    # Add nodes
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("worker", worker_node)
    workflow.add_node("finalize", finalize_node)

    # Define edges
    workflow.add_edge(START, "supervisor")
    workflow.add_conditional_edges(
        "supervisor",
        should_continue,
        {"worker": "worker", "finalize": "finalize", "end": END},
    )
    workflow.add_edge("worker", "supervisor")  # Loop back to supervisor
    workflow.add_edge("finalize", END)

    # Add checkpointer for persistence
    memory = MemorySaver()
    return workflow.compile(checkpointer=memory)


# ==================== Main Execution ====================


def initialize_state() -> AgentState:
    """Initialize the workflow state"""
    chapters = {}
    for ch_config in CHAPTERS:
        chapters[ch_config["id"]] = ChapterState(
            id=ch_config["id"],
            title=ch_config["title"],
            status=ChapterStatus.PENDING,
        )

    return {
        "messages": [],
        "chapters": chapters,
        "current_chapter": "",
        "supervisor_decision": "",
        "final_script": "",
        "iteration": 0,
    }


def init_config():
    """Initialize configuration from config.json and prompt files"""
    global \
        LOCAL_MODEL_CONFIG, \
        SCRIPT_CONFIG, \
        PLANNER_SYSTEM_PROMPT, \
        PLANNER_HUMAN_PROMPT, \
        SUPERVISOR_SYSTEM_PROMPT, \
        WORKER_SYSTEM_PROMPT, \
        WORKER_HUMAN_PROMPT

    from src.config import CONFIG, get_prompt_path

    if "biliup_config_default" in CONFIG:
        SCRIPT_CONFIG["biliup"] = CONFIG["biliup_config_default"]

    if "openai" in CONFIG:
        LOCAL_MODEL_CONFIG.update(CONFIG["openai"])
        if "api_key" not in LOCAL_MODEL_CONFIG:
            LOCAL_MODEL_CONFIG["api_key"] = "openai"

    user_prompt = get_prompt_path("user_prompt_file", "conf/prompts/user_prompt.txt")
    planner_prompt = get_prompt_path(
        "planner_prompt_file", "conf/prompts/planner_prompt.txt"
    )
    planner_human = get_prompt_path(
        "planner_human_file", "conf/prompts/planner_human.txt"
    )
    supervisor_prompt = get_prompt_path(
        "supervisor_prompt_file", "conf/prompts/supervisor_prompt.txt"
    )
    worker_prompt = get_prompt_path(
        "worker_prompt_file", "conf/prompts/worker_prompt.txt"
    )
    worker_human = get_prompt_path("worker_human_file", "conf/prompts/worker_human.txt")

    if user_prompt.exists():
        with open(user_prompt, "r", encoding="utf-8") as pf:
            SCRIPT_CONFIG["topic"] = pf.read().strip()
    else:
        raise FileNotFoundError(f"user_prompt_file not found: {user_prompt}")

    if planner_prompt.exists():
        with open(planner_prompt, "r", encoding="utf-8") as pf:
            PLANNER_SYSTEM_PROMPT = pf.read().strip()
    else:
        raise FileNotFoundError(f"planner_prompt_file not found: {planner_prompt}")

    if planner_human.exists():
        with open(planner_human, "r", encoding="utf-8") as pf:
            PLANNER_HUMAN_PROMPT = pf.read().strip()
    else:
        raise FileNotFoundError(f"planner_human_file not found: {planner_human}")

    if supervisor_prompt.exists():
        with open(supervisor_prompt, "r", encoding="utf-8") as pf:
            SUPERVISOR_SYSTEM_PROMPT = pf.read().strip()
    else:
        raise FileNotFoundError(
            f"supervisor_prompt_file not found: {supervisor_prompt}"
        )

    if worker_prompt.exists():
        with open(worker_prompt, "r", encoding="utf-8") as pf:
            WORKER_SYSTEM_PROMPT = pf.read().strip()
    else:
        raise FileNotFoundError(f"worker_prompt_file not found: {worker_prompt}")

    if worker_human.exists():
        with open(worker_human, "r", encoding="utf-8") as pf:
            WORKER_HUMAN_PROMPT = pf.read().strip()
    else:
        raise FileNotFoundError(f"worker_human_file not found: {worker_human}")


def generate_dynamic_chapters():
    """Dynamically generate chapters based on the topic using LLM"""
    global CHAPTERS

    print("\n=== GENERATING DYNAMIC CHAPTERS ===")

    llm = create_llm()

    human_prompt = PLANNER_HUMAN_PROMPT.format(topic=SCRIPT_CONFIG["topic"])
    messages = [
        SystemMessage(content=PLANNER_SYSTEM_PROMPT),
        HumanMessage(content=human_prompt),
    ]

    try:
        response = llm.invoke(messages)
        content = response.content.strip()

        start_idx = content.find("{")
        end_idx = content.rfind("}") + 1
        if start_idx != -1 and end_idx != -1:
            content = content[start_idx:end_idx]

        result = json.loads(content)
        if isinstance(result, dict) and "chapters" in result and "topic" in result:
            SCRIPT_CONFIG["topic"] = result["topic"]
            if "desc" in result:
                SCRIPT_CONFIG["desc"] = result["desc"]
            CHAPTERS.clear()
            CHAPTERS.extend(result["chapters"])
            print(
                f"✓ Successfully generated {len(CHAPTERS)} chapters dynamically for topic: {result['topic']}."
            )
        else:
            raise ValueError("Failed to parse chapters or topic: Invalid JSON format")
    except Exception as e:
        print(f"✗ Error generating chapters: {e}")
        raise e


def main():
    """Main entry point"""
    init_config()
    generate_dynamic_chapters()

    from src.config import SCRIPTS_DIR

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    SCRIPT_CONFIG["output_file"] = str(SCRIPTS_DIR / timestamp / "script.txt")

    print("=" * 60)
    print("LangGraph Supervisor System for Podcast Script Generation")
    print("=" * 60)
    topic_display = SCRIPT_CONFIG["topic"]
    if len(topic_display) > 50:
        topic_display = topic_display[:50] + "..."
    print(f"\nTopic: {topic_display}")
    print(f"Target: {SCRIPT_CONFIG['target_words']} words")
    print(f"Chapters: {len(CHAPTERS)}")
    print(f"Output: {SCRIPT_CONFIG['output_file']}")
    print(f"Local Model: {LOCAL_MODEL_CONFIG['base_url']}")
    print("\n" + "=" * 60)

    # Create workflow
    app = create_workflow()

    # Initialize state
    initial_state = initialize_state()

    # Run workflow with thread support for checkpointing
    config = {"configurable": {"thread_id": "podcast_generation_1"}}

    try:
        print("\n🚀 Starting workflow execution...\n")
        final_state = app.invoke(initial_state, config)

        print("\n" + "=" * 60)
        print("WORKFLOW COMPLETED")
        print("=" * 60)

        stats = get_chapters_summary(final_state["chapters"])
        print(f"\nFinal Statistics:")
        print(f"  Total chapters: {stats['total']}")
        print(f"  Completed: {stats['completed']}")
        print(f"  Failed: {stats['failed']}")
        print(f"  Total iterations: {final_state['iteration']}")

        if stats["completed"] == stats["total"]:
            print(f"\n✓ SUCCESS! Script saved to: {SCRIPT_CONFIG['output_file']}")
            return 0
        else:
            print(f"\n✗ PARTIAL FAILURE - some chapters could not be completed")
            return 1

    except KeyboardInterrupt:
        print("\n\n⚠ Workflow interrupted by user")
        print("Progress has been checkpointed and can be resumed later")
        return 2
    except Exception as e:
        print(f"\n\n✗ Workflow failed with error: {e}")
        import traceback

        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())

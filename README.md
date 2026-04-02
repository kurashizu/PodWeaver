# LangGraph Supervisor System for Podcast Script Generation

## 📋 Overview

This system uses LangGraph to implement a reliable multi-agent architecture for automatically generating podcast scripts. It is specifically designed to handle situations where local models may have limited performance, slow output, or potential interruptions.

### Core Features

✅ **Supervisor-Worker Architecture**: A supervisor agent coordinates multiple worker agents.
✅ **Automatic Retry Mechanism**: Handles local model interruptions by automatically retrying failed chapters.
✅ **State Persistence**: Uses LangGraph checkpointer to save progress.
✅ **Dynamic Chapter Generation**: Automatically plans and structures the podcast chapters based on user topics.
✅ **Modular Design**: Each chapter is generated independently; a failure in one does not affect the others.
✅ **Resumable Execution**: Can continue execution after a Ctrl+C interruption.

## 🏗️ Architecture Design

```
┌─────────────────────────────────────────────────┐
│          Supervisor Agent                       │
│  - Task Assignment                              │
│  - Progress Monitoring                          │
│  - Retry Strategy                               │
│  - Final Script Assembly                        │
└────────┬────────────────────────────────────────┘
         │
         ├─> Worker Agent 1 (Chapter 1)
         ├─> Worker Agent 2 (Chapter 2)
         ├─> Worker Agent 3 (Chapter 3)
         ├─> Worker Agent 4 (Chapter 4)
         └─> Worker Agent N (Chapter N)
                    │
                    ▼
            Final Output (./scripts/[date_time]/)
            ├── script.txt
            └── metadata.json
```

## 🔄 Workflow

1. **Initialization**: Reads configurations from `config.json` and loads prompts.
2. **Dynamic Planning**: Uses the user's topic to automatically generate a chapter outline using an LLM.
3. **Execution Loop**:
   - The Supervisor selects a PENDING chapter.
   - A Worker generates the content for that chapter.
   - If successful → marked as COMPLETED.
   - If failed → retried (up to a configured maximum).
   - Returns to Supervisor to assign the next task.
4. **Finalization**: Once all chapters are completed, the Finalize node assembles and saves the script to `./scripts/[date_time]/script.txt` along with a `metadata.json` for Biliup configuration.

## 📦 Installation

```bash
# 1. Install dependencies
pip install -r requirements_langgraph.txt

# 2. Configure the system
# Ensure config.json and prompt files are set up correctly
```

## ⚙️ Configuration

The system uses a `config.json` file for configuration, structured as follows:

```json
{
    "ollama": {
        "base_url": "http://localhost:11435/v1",
        "model": "gemma4:e4b-it-q8_0",
        "temperature": 0.7,
        "max_tokens": 8000
    },
    "podcast": {
        "user_prompt_file": "./user_prompt.txt",
        "planner_prompt_file": "./planner_prompt.txt",
        "supervisor_prompt_file": "./supervisor_prompt.txt",
        "worker_prompt_file": "./worker_prompt.txt"
    }
}
```

### Prompt Files

- `user_prompt.txt`: Contains the main topic and instructions for the podcast.
- `planner_prompt.txt`: The system prompt instructing the LLM on how to generate the dynamic chapter outline.
- `supervisor_prompt.txt`: The system prompt instructing the Supervisor agent.
- `worker_prompt.txt`: The system prompt instructing the Worker agents.

### Supported Local Model Services

- **LM Studio**: `http://localhost:1234/v1`
- **Ollama** (with OpenAI compatibility): `http://localhost:11434/v1`
- **vLLM**: `http://localhost:8000/v1`
- **text-generation-webui**: `http://localhost:5000/v1`

## 🚀 Usage

### Basic Execution

```bash
python3 generate_script.py
```

### Handling Interruptions

If the program is interrupted (e.g., via Ctrl+C):

```bash
# State is automatically saved.
# Failed or pending chapters will resume upon restarting.
python3 generate_script.py
```

## 🔍 Monitoring and Debugging

### Output Indicators

- `✓` Success
- `✗` Error
- `!` Warning / Retry
- `→` Progress Indicator

### Chapter Statuses

- `PENDING`: Waiting to be processed.
- `IN_PROGRESS`: Currently being generated.
- `COMPLETED`: Successfully finished.
- `FAILED`: Failed (exceeded maximum retry limit).

## 📊 Integration with Existing Workflows

```bash
# Full Podcast Generation Pipeline

# 1. Generate Script (Using LangGraph)
python3 generate_script.py

# 2. Run existing workflow (TTS + Video Generation)
./run_workflow.sh

# Or run step-by-step:
# python3 split_segments.py      # Split script
# python3 tts_batch.py            # Text-to-Speech
# python3 merge_clips.py          # Merge audio
# (run_workflow.sh generates the final video)
```

## 🔧 Troubleshooting

### Issue 1: Failed to connect to local model

```
Error: Connection refused
```

**Solution**:
- Ensure your local model service is running.
- Verify `base_url` in `config.json`.
- Test connection: `curl http://localhost:11435/v1/models`

### Issue 2: Generated content is too short

```
Generated content too short (45 chars), likely model failure
```

**Solution**:
- The local model might lack performance or context length.
- Increase `max_tokens`.
- Lower `temperature`.
- Try a more capable model.

### Issue 3: Content style is incorrect

**Solution**:
- Adjust the prompts in `worker_prompt.txt`.
- Modify the `temperature` parameter (e.g., between 0.7-1.0).

## 📈 Performance Optimization

### For Limited Local Models:

1. **Reduce Target Length**: Ask for fewer words in the user prompt.
2. **Increase Retries**: Modify `MAX_RETRIES_PER_CHAPTER` in the code.
3. **Use Quantized Models**: e.g., GGUF Q4_K_M formats.

### For Faster Generation:

1. **Parallel Execution**: (Future feature) Modify code to support multiple concurrent workers.
2. **Faster Models**: Use models like Llama 3 8B instead of larger variants.

## 📄 License

This project shares the same license as the main repository.


[![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/github/license/dovvnloading/Cortex?color=blue)](LICENSE)
[![Framework](https://img.shields.io/badge/Framework-PySide6-2496ED)](https://doc.qt.io/qtforpython-6/)
[![Platform](https://img.shields.io/badge/platform-windows%20%7C%20macos%20%7C%20linux-lightgrey)]()
[![Status](https://img.shields.io/badge/status-active-success)]()
[![Version](https://img.shields.io/badge/version-1.0.0-c75a28)]()

**Cortex** is a private, secure, and highly responsive desktop AI assistant designed for seamless interaction with **local Large Language Models (LLMs)** through the **Ollama** framework. All models and data stay on your device—no cloud, no third parties. Cortex keeps everything local for maximum privacy and speed.

Its rich feature set includes a robust **permanent memory system**, **auto-translation**, **response suggestions**, and deep customization options to tailor the AI's core behavior to your exact needs.

---

## **New in v1.0.0**

Cortex 1.0.0 introduces a major architecture overhaul with new cognitive capabilities:

*   **Real-Time Auto-Translation**: Instantly translate AI responses into your preferred language using a dedicated local translation model.
*   **Conversation Suggestions**: Intelligent, context-aware follow-up bubbles to help keep the conversation flowing.
*   **Vector Memory System**: A new embedding-based memory layer that helps the AI retrieve relevant context from past conversations more effectively.
*   **Integrated Persona Editor**: Edit the AI's system instructions and personality directly from the UI, with no need to touch configuration files.
*   **Automated Update Checks**: Cortex now discreetly checks for updates to ensure you are running the latest version.

---

## **Key Features**

### Core Principles
*   **100% Local & Private**: All processing happens on your machine via your Ollama instance. Nothing ever leaves your system.
*   **Powered by Ollama**: Seamlessly integrates with any model served by Ollama (DeepSeek R1, Qwen 3, Mistral, Llama 3, etc.).
*   **High-Performance Database**: Chat history is stored in a robust SQLite database for instantaneous loading and rock-solid data integrity.

### Advanced Conversational Tools
*   **Response Regeneration**: Not satisfied with an answer? A single click prompts the AI to rethink its last response, optionally with new instructions.
*   **Conversational Forking**: Explore different lines of thought. Split a conversation at any point to create a new, independent chat thread.
*   **Rich Code Rendering**: Code blocks are displayed in a professional container with syntax highlighting, a one-click copy button, and theme support.
*   **Conversation Suggestions**: Dynamic, clickable bubbles that suggest what you might want to say next.

### Deep AI Customization
*   **Permanent Memory**: Teach the AI key facts about you, your projects, or your preferences. It will subtly use this information to personalize future responses.
*   **Model Behavior Control**: Fine-tune the AI by adjusting **Temperature**, **Context Window Size**, and **Seed** for reproducible outputs.
*   **System Instructions UI**: Define a global persona or ruleset (e.g., "You are a Python expert") that the AI will follow in every chat.

### Professional User Experience
*   **Light & Dark Themes**: Choose a look that fits your workspace. The entire UI updates instantly.
*   **Keyboard Shortcuts**: High-velocity workflow with shortcuts like `Ctrl+N` (New Chat) and `Ctrl+L` (Focus Input).
*   **Asynchronous Processing**: The UI remains perfectly smooth and responsive while the AI is thinking.

---

## **Manual Setup & Requirements**

If you prefer to set up Cortex manually (without the installer), ensure you have the following models pulled in Ollama. Cortex v1.0.0 utilizes a multi-model architecture for specialized tasks.

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Pull Required Models
Run the following commands in your terminal to prepare the local AI engine:

**Primary Chat Model (Default):**
```bash
ollama pull qwen3:8b
```
*(Note: You can swap this for deepseek-r1, mistral, etc., inside the app settings)*

**Utility Models (Required for full functionality):**
```bash
# For generating smart chat titles
ollama pull granite4:tiny-h

# For the auto-translation feature
ollama pull translategemma:4b

# For vector memory embeddings
ollama pull nomic-embed-text
```

### 3. Run Cortex
```bash
python Chat_LLM.py
```

---

## **Architecture**

Cortex is built on a modern, modular architecture:

*   **Presentation Layer (UI)**: Built with **PySide6 (Qt)** for a native, high-performance look and feel.
*   **Orchestration Layer**: Manages state, threading, and the interaction between the database and the AI agents.
*   **Synthesis Agent**: A specialized agent that constructs complex prompts, handles memory retrieval, and manages the translation pipeline.
*   **Data Layer**:
    *   **SQLite**: Stores chat history and vector embeddings.
    *   **JSON**: Stores lightweight permanent memory "memos".

---

## **Summary**

Cortex is a locally-run, privacy-focused AI assistant that integrates tightly with Ollama to deliver fast, context-aware, and persistent interactions. With v1.0.0, it evolves from a simple chat interface into a comprehensive cognitive assistant with memory, translation, and proactive suggestions.


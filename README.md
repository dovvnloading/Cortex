# Cortex

![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)
![License](https://img.shields.io/github/license/dovvnloading/Cortex?color=blue)
![Framework](https://img.shields.io/badge/Framework-PySide6-2496ED)
![Platform](https://img.shields.io/badge/platform-windows%20%7C%20macos%20%7C%20linux-lightgrey)
![Status](https://img.shields.io/badge/status-active-success)

Cortex is a private, secure, and highly responsive desktop AI assistant designed for seamless interaction with local Large Language Models (LLMs) through the Ollama framework. It prioritizes data privacy and deep personalization by keeping all models and user data entirely on the local machine. Its core feature is a sophisticated permanent memory system, allowing the AI to build a persistent knowledge base tailored to the user's specific context.

![Untitled video - Made with Clipchamp (14)](https://github.com/user-attachments/assets/f4ae7753-ead0-4601-8664-17fa0629f236)
<img width="800" height="500" alt="Screenshot 2025-10-09 183932" src="https://github.com/user-attachments/assets/f21d2478-b27e-47fa-8ff2-c0ddc9afa264" />
<img width="800" height="500" alt="2" src="https://github.com/user-attachments/assets/a030bdd0-46c9-470d-8daf-276b8e167fc0" />
<img width="800" height="500" alt="1" src="https://github.com/user-attachments/assets/bff256df-d8f7-484e-afd2-c7c4b63befa4" />

---
---
[Download Cortex.exe (65 MB)](https://drive.google.com/file/d/1BF4O7Hy1o5H9nkPzUjmsVsdi1Pt-VEYa/view)

& 

[Quick Setup Walk-through](https://github.com/dovvnloading/Cortex/blob/main/Desktop-Quick-Setup-Guide.md)

---
---

## Core Philosophy

In an era of cloud-centric AI, Cortex champions a local-first approach. The guiding philosophy is that your data and your AI interactions should belong to you. By leveraging the power of locally-run models via Ollama, Cortex provides a powerful conversational experience without compromising on privacy or control. It is designed not as a service, but as a personal tool for thought, research, and development.

## Key Features

*   **Local-First AI Interaction:** All communication happens directly with your local Ollama instance. No data is ever sent to the cloud.
*   **Persistent Chat History:** Conversations are automatically saved locally and can be reloaded at any time, preserving the full context of your interactions.
*   **Permanent Memory System:** Go beyond simple chat history. Explicitly instruct the AI to remember key facts, preferences, or project details using simple in-chat tags.
    *   **Add Memories:** Tell the AI `<memo>My project 'Apollo' is written in Go.</memo>` and it will remember this context for future questions.
    *   **Forget Memories:** Full control to clear the AI's memory bank with a `<clear_memory />` command.
*   **Intuitive User Interface:** A clean, modern UI built with PySide6 (Qt) provides a fluid and responsive user experience.
*   **Theming Support:** Switch between focused light and dark themes to suit your preference.
*   **Model Flexibility:** Easily switch between different chat models available on your Ollama instance directly from the settings menu.
*   **Asynchronous Processing:** The UI remains perfectly responsive at all times, with AI query processing handled in a background thread.

## Technical Architecture

Cortex is built on a robust, multi-layered architecture founded on the principle of Separation of Concerns. This design ensures the application is maintainable, scalable, and easy to reason about.

```
+--------------------------------+
|         UI Layer (View)        |  (PySide6 Widgets, Dialogs, Styles)
|  Handles presentation & input. |
+--------------------------------+
               ^
               | (Signals & Slots)
               v
+--------------------------------+
|  Orchestration Layer (Control) |  (Orchestrator, Workers in Chat_LLM.py)
|   Manages state & async tasks. |
+--------------------------------+
               ^
               | (Method Calls)
               v
+--------------------------------+
| Service & Logic Layer (Model)  |  (SynthesisAgent, Memory Managers)
|  Handles business logic & data.|
+--------------------------------+
```

*   **UI Layer:** Responsible for rendering the interface and capturing user events. It is completely decoupled from the application's business logic.
*   **Orchestration Layer:** The `Orchestrator` class acts as the central nervous system, mediating communication between the UI and the backend services. It manages application state, chat threads, and offloads all blocking operations (like LLM requests) to dedicated `QThread` workers.
*   **Service & Logic Layer:** Contains the "brains" of the application. The `SynthesisAgent` is responsible for building prompts and communicating with the Ollama API. The various `MemoryManager` classes handle the rules for short-term context, long-term history persistence, and the permanent memory bank.

## Getting Started

Follow these steps to set up and run Cortex on your local machine.

### Prerequisites

1.  **Python:** Python 3.10 or newer is required.
2.  **Git:** Required to clone the repository.
3.  **Ollama:** Cortex is a client for Ollama. You must have [Ollama](https://ollama.com/) installed and running.

### Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/dovvnloading/Cortex.git
    cd Cortex
    ```

2.  **Create and activate a virtual environment:**
    ```bash
    # For Windows
    python -m venv venv
    .\venv\Scripts\activate

    # For macOS/Linux
    python3 -m venv venv
    source venv/bin/activate
    ```

3.  **Install the required dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Download the necessary Ollama models:**
    Cortex uses two models by default: a primary model for generation and a smaller, faster model for generating chat titles.
    ```bash
    ollama pull qwen3:8b
    ollama pull granite4:tiny-h
    ```
    *Note: You can configure Cortex to use other models after installation via the in-app settings.*

5.  **Run the application:**
    ```bash
    python Chat_LLM.py
    ```

## Usage Guide

*   **Chatting:** Type your message in the input box at the bottom and press Enter or click the "Send" button.
*   **New Chat:** Click the "+ New Chat" button in the top-left panel to start a new conversation.
*   **Managing Chats:** Right-click on any conversation in the history panel to access options for renaming or deleting it.

### Using the Permanent Memory System

You can control the AI's permanent memory directly from the chat.

*   **To save a memory:**
    Include a `<memo>` tag anywhere in your response. The AI will save the enclosed fact.
    > **User:** My project is called 'Apollo' and it is written in the Go programming language. From now on, remember that.
    > **AI:** Understood. I will remember that your project 'Apollo' is written in Go. `<memo>User's project is named 'Apollo' and is written in Go.</memo>`

*   **To clear all memories:**
    Ask the AI to forget everything. It will use the `<clear_memory />` tag.
    > **User:** Please forget everything you know about me.
    > **AI:** As you wish. I have cleared all permanent memories. `<clear_memory />`

*   **Managing Memories Manually:**
    Click the settings cog in the title bar, and in the "Permanent Memory" section, click "Manage..." to open a dialog where you can view, edit, add, or delete all stored facts.

## Development

This project includes a Visual Studio Code project file (`.vscode/settings.json`) with recommended settings for formatting and linting to maintain code consistency.

To contribute, please fork the repository and submit a pull request. For major changes, please open an issue first to discuss the proposed changes.

## Future Roadmap

Cortex is an actively developed project. Potential future enhancements include:
*   **Streaming Responses:** Displaying the AI's response token-by-token for a "typewriter" effect.
*   **Plugin System:** An architecture to allow for extensions, such as web search or local file access.
*   **Advanced Memory Management:** Exploring more sophisticated techniques for automatic memory retrieval and summarization.
*   **UI Enhancements:** Additional user experience improvements, such as global keyboard shortcuts.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.

## Credits

Developed by **Matt Wesney**.

This application is powered by exceptional open-source technologies, including:
*   [Ollama](https://ollama.com/)
*   [PySide6 (Qt for Python)](https://www.qt.io/qt-for-python)
*   [Python](https://www.python.org/)

Icon credits: Anthony Bossard

<div align="center">
<img width="890" height="419" alt="cortex_Banner_001" src="https://github.com/user-attachments/assets/d4225beb-ccae-4b96-bdcb-473cf004ae51" />


# **Cortex**

![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)
![License](https://img.shields.io/github/license/dovvnloading/Cortex?color=blue)
![Framework](https://img.shields.io/badge/Framework-PySide6-2496ED)
![Platform](https://img.shields.io/badge/platform-windows%20%7C%20macos%20%7C%20linux-lightgrey)
![Status](https://img.shields.io/badge/status-active-success)

</div> 

**Cortex** is a private, secure, and highly responsive desktop AI assistant designed for seamless interaction with **local Large Language Models (LLMs)** through the **Ollama** framework. All models and data stay on your device—no cloud, no third parties. Cortex keeps everything local for maximum privacy and speed.

Its rich feature set includes a robust **permanent memory system**, advanced conversational controls like **response regeneration** and **chat forking**, and deep customization options to tailor the AI's core behavior to your exact needs.

![Untitled video - Made with Clipchamp (17)](https://github.com/user-attachments/assets/96d509e9-d15b-4b41-ac0b-bd03459c6cc8)
![Untitled video - Made with Clipchamp (18)](https://github.com/user-attachments/assets/1a3e25a4-aaa2-4bb0-b49f-9970d6697a8c)


---

## **Download**

*   **Cortex v0.95.7 App**: [Download Cortex.exe (66.9 MB)](https://drive.google.com/file/d/1q9iFi04WmF6pvw5HGR_sTX8RTOx2PDcF/view?usp=sharing)
*   **Ollama & Model Installer**: [Download Cortex Setup.exe (65 MB)](https://drive.google.com/file/d/19mtempOGZKk1v7muxCsdcFLC1uZ5Dv3u/view?usp=sharing)
    <img width="844" height="598" alt="Screenshot 2025-10-12 152015" src="https://github.com/user-attachments/assets/769d7a0b-d3f7-4ed6-a0e2-f2ecf3e58119" />

*   **Manual Setup Guide**: [Desktop Quick Setup Walkthrough](https://github.com/dovvnloading/Cortex/blob/main/Desktop-Quick-Setup-Guide.md)

> Use the automated setup tool to install Ollama and pull the required models, or follow the manual guide linked above for more control.

---

## **Overview**

Cortex is built for users who demand full control and privacy from their AI tools. It runs entirely on your local machine, connecting directly to your Ollama models for fast, private, and reliable conversations. No cloud processing—just your hardware, your data, your control.

---

## **Key Features**

### Core Principles
*   **100% Local & Private**: All processing happens on your machine via your Ollama instance. Nothing ever leaves your system.
*   **Powered by Ollama**: Seamlessly integrates with any model served by Ollama, giving you the freedom to choose the right tool for the job.
*   **High-Performance Database**: Chat history is stored in a robust SQLite database for instantaneous loading and rock-solid data integrity.

### Advanced Conversational Tools
*   **Response Regeneration**: Not satisfied with an answer? A single click prompts the AI to rethink its last response, offering a new perspective or a more refined solution.
*   **Conversational Forking**: Explore different lines of thought without losing your place. Split a conversation at any point to create a new, independent chat thread that preserves the context up to that moment.
*   **Rich Code Rendering**: Code blocks are displayed in a professional, dedicated container with syntax highlighting, a one-click copy button, and full theme support.

### Deep AI Customization
*   **Permanent Memory System**: Teach the AI key facts about you, your projects, or your preferences. It will subtly use this information to personalize future responses.
*   **Custom System Instructions**: Set a persistent persona or define global behavioral rules for the AI through a dedicated settings dialog. Your instructions are given the highest priority.
*   **Advanced Model Controls**: Fine-tune the AI's core behavior by adjusting **Temperature**, **Context Window Size**, and **Seed** for reproducible outputs.
*   **Externalized AI Persona**: The AI's core instructions are located in external `.txt` files (`system_prompt.txt`, `memory_prompt.txt`), allowing advanced users to directly edit its personality and operational rules.

### Professional User Experience
*   **Light & Dark Themes**: Choose a look that fits your workspace. The entire UI updates instantly.
*   **Keyboard Shortcuts**: A full suite of shortcuts (`Ctrl+N` for New Chat, `Ctrl+L` to focus input, etc.) for a high-velocity workflow.
*   **Asynchronous Processing**: The UI remains perfectly smooth and responsive while the AI is thinking, thanks to a multi-threaded architecture.
*   **Non-Intrusive Updates**: The app checks for new versions in the background and notifies you discreetly within the Settings panel.
*   **First-Run User Agreement**: A one-time EULA screen on first launch ensures transparency and clarifies user liability when interacting with local models.



---
| | |
|:-------------------------:|:-------------------------:|
| <img width="1920" height="1032" alt="Screenshot 2025-10-14 170142" src="https://github.com/user-attachments/assets/d5817af5-db5d-4f4b-96c0-7a455e234b49" /> | <img width="1920" height="1032" alt="Screenshot 2025-10-14 170155" src="https://github.com/user-attachments/assets/3321e3c7-7f11-4f10-a337-a7c487bbc0fc" /> |
| <img width="1919" height="1032" alt="Screenshot 2025-10-14 170524" src="https://github.com/user-attachments/assets/2f311cc4-1c70-44bf-9a43-51ba0468c514" /> | <img width="1919" height="1032" alt="Screenshot 2025-10-14 170559" src="https://github.com/user-attachments/assets/cdb0a1bd-70dd-49cf-bcea-103cdf66fdc2" /> |
| <img width="1919" height="1030" alt="Screenshot 2025-10-14 170549" src="https://github.com/user-attachments/assets/a18386b0-bae1-4a2a-a50b-023ebb9f2a6e" /> | <img width="1919" height="1030" alt="Screenshot 2025-10-14 170535" src="https://github.com/user-attachments/assets/be24d21a-5641-4929-a6c0-7c0445cea291" /> |
---

## **Architecture**

Cortex is built on a modern, modular architecture that separates concerns for maintainability and performance.

*   **Presentation Layer (UI)**: A responsive and themeable interface built with **PySide6 (Qt)**. Custom widgets ensure a consistent and polished user experience.
*   **Orchestration Layer (Control)**: The `Orchestrator` manages application state, coordinates UI events, and dispatches long-running tasks to asynchronous workers to prevent the UI from freezing.
*   **Data & Agent Layer (Model)**:
    *   **SQLite Database**: All chat history is stored in a local `cortex_db.sqlite` file, providing fast, reliable, and scalable data persistence.
    *   **Synthesis Agent**: Interfaces with the Ollama client, builds complex prompts incorporating memory and user instructions, and parses the AI's response.
    *   **Externalized Prompts**: The AI's core identity and rules are loaded from external `.txt` files, decoupling the AI's "personality" from the application's code.

---

### **Summary**

Cortex is a locally-run, privacy-focused AI assistant that integrates tightly with Ollama to deliver fast, context-aware, and persistent interactions. With advanced tools for conversational control and deep customization, it empowers you to create a personalized AI assistant that operates entirely on your terms—all without ever sending your data to the cloud.

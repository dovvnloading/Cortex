# **Cortex**

![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)
![License](https://img.shields.io/github/license/dovvnloading/Cortex?color=blue)
![Framework](https://img.shields.io/badge/Framework-PySide6-2496ED)
![Platform](https://img.shields.io/badge/platform-windows%20%7C%20macos%20%7C%20linux-lightgrey)
![Status](https://img.shields.io/badge/status-active-success)

**Cortex** is a private, secure, and highly responsive desktop AI assistant designed for seamless interaction with **local Large Language Models (LLMs)** through the **Ollama** framework.
All models and data stay on your device — no cloud, no third parties. Cortex keeps everything local for maximum privacy and speed. Its built-in **permanent memory system** lets the AI learn and retain context over time, creating a more personal and consistent experience.

![Demo Video](https://github.com/user-attachments/assets/f4ae7753-ead0-4601-8664-17fa0629f236) <img width="800" height="500" alt="Screenshot 1" src="https://github.com/user-attachments/assets/f21d2478-b27e-47fa-8ff2-c0ddc9afa264" /> <img width="800" height="500" alt="Screenshot 2" src="https://github.com/user-attachments/assets/a030bdd0-46c9-470d-8daf-276b8e167fc0" /> <img width="800" height="500" alt="Screenshot 3" src="https://github.com/user-attachments/assets/bff256df-d8f7-484e-afd2-c7c4b63befa4" />

---

## **Download**

* **Cortex App**: [Download Cortex.exe (65 MB)](https://drive.google.com/file/d/1LFwnGOt0KiuQLtDWkSxEDpeGxRgF1C3z/view?usp=sharing)
* **Ollama & Model Installer**: [Download Cortex Setup.exe (65 MB)](https://drive.google.com/file/d/19mtempOGZKk1v7muxCsdcFLC1uZ5Dv3u/view?usp=sharing)
  <img width="844" height="598" alt="Screenshot 2025-10-12 152015" src="https://github.com/user-attachments/assets/769d7a0b-d3f7-4ed6-a0e2-f2ecf3e58119" />

* **Manual Setup Guide**: [Desktop Quick Setup Walkthrough](https://github.com/dovvnloading/Cortex/blob/main/Desktop-Quick-Setup-Guide.md)

> Use either the automated setup tool to install Ollama and pull models, or follow the manual guide linked above.

---

## **Overview**

Cortex is built for people who want full control over their AI tools.
It runs entirely on your local machine, connecting directly to your Ollama models for fast, private, and reliable conversations. No cloud processing — just your hardware, your data, your control.

---

## **Key Features**

* **Local-First AI Interaction** – All processing happens locally through your Ollama instance. Nothing leaves your system.
* **Persistent Chat History** – Conversations are stored locally and can be reloaded anytime, keeping full context intact.
* **Permanent Memory System** – Teach Cortex to remember details, facts, or project info using simple chat tags:

  * Add memory: `<memo>My project 'Apollo' is written in Go.</memo>`
  * Clear memory: `<clear_memory />`
* **Responsive UI** – Built with PySide6 (Qt) for a clean, modern, and fast interface.
* **Theming Support** – Choose between light or dark modes.
* **Model Switching** – Instantly change which Ollama model you’re using from the settings panel.
* **Asynchronous Processing** – The UI remains smooth and responsive while AI tasks run in the background.

---

## **Architecture**

Cortex follows a modular, layered design that keeps the system maintainable and scalable.

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
```

---

### **Summary**

Cortex is a locally-run, privacy-focused AI assistant that integrates tightly with Ollama to deliver fast, context-aware, and persistent interactions — all without ever sending your data to the cloud.

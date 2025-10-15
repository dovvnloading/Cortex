# **Cortex**

![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)
![License](https://img.shields.io/github/license/dovvnloading/Cortex?color=blue)
![Framework](https://img.shields.io/badge/Framework-PySide6-2496ED)
![Platform](https://img.shields.io/badge/platform-windows%20%7C%20macos%20%7C%20linux-lightgrey)
![Status](https://img.shields.io/badge/status-active-success)

**Cortex** is a private, secure, and highly responsive desktop AI assistant designed for seamless interaction with **local Large Language Models (LLMs)** through the **Ollama** framework.
All models and data stay on your device — no cloud, no third parties. Cortex keeps everything local for maximum privacy and speed. Its built-in **permanent memory system** lets the AI learn and retain context over time, creating a more personal and consistent experience.

![Untitled video - Made with Clipchamp (17)](https://github.com/user-attachments/assets/96d509e9-d15b-4b41-ac0b-bd03459c6cc8)
![Untitled video - Made with Clipchamp (18)](https://github.com/user-attachments/assets/1a3e25a4-aaa2-4bb0-b49f-9970d6697a8c)

<img width="1920" height="1032" alt="Screenshot 2025-10-14 170142" src="https://github.com/user-attachments/assets/d5817af5-db5d-4f4b-96c0-7a455e234b49" />
<img width="1920" height="1032" alt="Screenshot 2025-10-14 170155" src="https://github.com/user-attachments/assets/3321e3c7-7f11-4f10-a337-a7c487bbc0fc" />
<img width="1919" height="1032" alt="Screenshot 2025-10-14 170524" src="https://github.com/user-attachments/assets/2f311cc4-1c70-44bf-9a43-51ba0468c514" />
<img width="1919" height="1032" alt="Screenshot 2025-10-14 170559" src="https://github.com/user-attachments/assets/cdb0a1bd-70dd-49cf-bcea-103cdf66fdc2" />
<img width="1919" height="1030" alt="Screenshot 2025-10-14 170549" src="https://github.com/user-attachments/assets/a18386b0-bae1-4a2a-a50b-023ebb9f2a6e" />
<img width="1919" height="1030" alt="Screenshot 2025-10-14 170535" src="https://github.com/user-attachments/assets/be24d21a-5641-4929-a6c0-7c0445cea291" />



---

## **Download**

* **Cortex v0.95.5 App**: [Download Cortex.exe (66.9 MB)](https://drive.google.com/file/d/1gog73F-zPlUhI6mi2szdq04Y7ZyoL3x7/view?usp=sharing)
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

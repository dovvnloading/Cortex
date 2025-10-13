# Contributing to Cortex

First off, thank you for considering contributing to Cortex! We're thrilled you're interested in helping make Cortex a better private, local-first AI assistant. Your contributions, whether it's reporting a bug, suggesting a new feature, or writing code, are incredibly valuable.

This document provides guidelines to help you get started.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [How Can I Contribute?](#how-can-i-contribute)
  - [Reporting Bugs](#reporting-bugs)
  - [Suggesting Enhancements](#suggesting-enhancements)
  - [Submitting Pull Requests](#submitting-pull-requests)
- [Development Setup](#development-setup)
- [Style Guides](#style-guides)
  - [Git Commit Messages](#git-commit-messages)
  - [Python Code](#python-code)
  - [UI/UX Design](#uiux-design)

## Code of Conduct

This project and everyone participating in it is governed by our [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code. Please report unacceptable behavior.

*(Note: You will need to create a `CODE_OF_CONDUCT.md` file. A good template is the [Contributor Covenant](https://www.contributor-covenant.org/version/2/1/code_of_conduct.html).)*

## How Can I Contribute?

### Reporting Bugs

Bugs are tracked as [GitHub Issues](https://github.com/dovvnloading/Cortex/issues). Before creating a bug report, please check the existing issues to see if someone has already reported it.

When you create a bug report, please include as many details as possible:

- **A clear and descriptive title.**
- **Steps to reproduce the bug.** Be as specific as possible.
- **What you expected to happen.**
- **What actually happened.** Include screenshots, logs, or error messages if possible.
- **Your environment:**
  - Cortex version
  - Operating System (e.g., Windows 11, macOS Sonoma, Ubuntu 22.04)
  - Ollama version and the model you were using

### Suggesting Enhancements

We welcome new ideas for features and improvements! Enhancements are also tracked as [GitHub Issues](https://github.com/dovvnloading/Cortex/issues).

To suggest an enhancement:

1.  **Check for existing suggestions** to avoid duplicates.
2.  Create a new issue, providing a clear and descriptive title.
3.  In the body, describe the enhancement in detail:
    - **What problem does this solve?** Explain the use case or the pain point you're addressing.
    - **How do you envision it working?** Describe the proposed feature from a user's perspective.
    - **Provide examples or mockups** if possible.

### Submitting Pull Requests

If you're ready to contribute code, that's fantastic! Here’s the general workflow for submitting a pull request (PR):

1.  **Fork the repository** to your own GitHub account.
2.  **Clone your fork** to your local machine: `git clone https://github.com/YOUR-USERNAME/Cortex.git`
3.  **Create a new branch** for your changes. Use a descriptive name like `feature/permanent-memory-export` or `fix/ui-rendering-glitch`.
    ```bash
    git checkout -b your-branch-name
    ```
4.  **Set up your development environment** (see [Development Setup](#development-setup) below).
5.  **Make your changes.** Write clean, readable code and add comments where necessary.
6.  **Commit your changes** using our [commit message guidelines](#git-commit-messages).
7.  **Push your branch** to your fork on GitHub: `git push origin your-branch-name`
8.  **Open a Pull Request** from your branch to the `main` branch of the original Cortex repository.
9.  **Provide a clear description** of your PR, explaining the "what" and "why" of your changes. If your PR fixes an existing issue, link to it (e.g., `Fixes #123`).

## Development Setup

To get Cortex running locally for development, follow these steps.

#### Prerequisites

- [Git](https://git-scm.com/)
- [Python 3.10+](https://www.python.org/downloads/)
- [Ollama](https://ollama.com/) installed and running with at least one model pulled (e.g., `ollama run llama3`).

#### Installation Steps

1.  **Clone your forked repository:**
    ```bash
    git clone https://github.com/YOUR-USERNAME/Cortex.git
    cd Cortex
    ```

2.  **Create and activate a virtual environment:**
    ```bash
    # For macOS/Linux
    python3 -m venv venv
    source venv/bin/activate

    # For Windows
    python -m venv venv
    venv\Scripts\activate
    ```

3.  **Install the required dependencies:**
    *(Note: Ensure you have a `requirements.txt` file in your repository.)*
    ```bash
    pip install -r requirements.txt
    ```

4.  **Run the application:**
    ```bash
    python main.py  # Or whatever the main entry point script is named
    ```

You should now have the Cortex application running from the source code.

## Style Guides

### Git Commit Messages

We follow the [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) specification. This helps keep the commit history clean and readable. Each commit message should consist of a **type**, an optional **scope**, and a **subject**.

**Format:** `type(scope): subject`

**Common Types:**

- `feat`: A new feature
- `fix`: A bug fix
- `docs`: Documentation only changes
- `style`: Changes that do not affect the meaning of the code (white-space, formatting, etc.)
- `refactor`: A code change that neither fixes a bug nor adds a feature
- `ui`: Changes related to the user interface (PySide6 widgets, styling)
- `perf`: A code change that improves performance

**Example:**
```
feat: add model switching dropdown to settings panel
fix(memory): prevent duplicate entries in permanent memory
docs: update contributing guide with development setup
ui: change default theme to dark mode
```

### Python Code

- All Python code must follow the **PEP 8** style guide.
- We use `black` for code formatting and `flake8` for linting. We recommend setting up your editor to use these tools.
- Use type hints wherever possible to improve code clarity and maintainability.

### UI/UX Design

Since Cortex is a desktop application built with **PySide6**, maintaining a consistent and responsive UI is important.

- When adding new UI elements, try to match the existing style (e.g., colors, fonts, spacing).
- Ensure the UI remains responsive, especially by offloading long-running tasks (like LLM inference) to background threads/workers, as the current architecture does.
- Keep the interface clean and intuitive.

Thank you again for your interest in contributing to Cortex! We look forward to seeing your contributions.

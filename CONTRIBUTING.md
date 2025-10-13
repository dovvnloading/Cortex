# Contributing to the AI-Powered Mind Map Generator

Thank you for your interest in contributing to this project. Your help is greatly appreciated. Whether it's reporting a bug, discussing improvements, or contributing code, every contribution is valuable.

This document provides guidelines for contributing to the project. Please read it to ensure a smooth and effective collaboration process.

## How Can I Contribute?

There are several ways you can contribute to this project:

### Reporting Bugs

If you encounter a bug, please open an issue on our [GitHub Issues page](https://github.com/dovvnloading/Tree-Graph-MindMap/issues). A well-documented bug report helps us resolve the problem faster.

Please include the following information in your bug report:

-   A clear and descriptive title.
-   Steps to reproduce the bug, with a step-by-step description of how to trigger the issue.
-   The expected behavior versus the actual behavior, including any error messages.
-   Screenshots or GIFs to illustrate the problem.
-   Your system information, such as operating system, Python version, and PySide6 version.

### Suggesting Enhancements

If you have an idea for a new feature or an improvement to an existing one, please open an issue on our [GitHub Issues page](https://github.com/dovvnloading/Tree-Graph-MindMap/issues).

In your enhancement suggestion, please include:

-   A clear and descriptive title.
-   A detailed description of the proposed feature, explaining the problem it solves or the value it adds.
-   Any relevant mockups or examples of how the feature might look or work.

## Development Setup

To contribute code, you will need to set up a local development environment.

1.  **Fork the repository:** Click the "Fork" button on the top right of the repository page.

2.  **Clone your fork:**
    ```bash
    git clone https://github.com/YOUR_USERNAME/Tree-Graph-MindMap.git
    cd Tree-Graph-MindMap
    ```

3.  **Create and activate a virtual environment:**
    ```bash
    # Create the virtual environment
    python -m venv venv

    # Activate it
    # On Windows:
    .\venv\Scripts\activate
    # On macOS/Linux:
    source venv/bin/activate
    ```

4.  **Install dependencies:**
    ```bash
    pip install PySide6 ollama
    ```

5.  **Set up Ollama:** Ensure the Ollama service is running and you have pulled at least one model, as described in the `README.md`.

## Submitting Pull Requests

When you are ready to submit your changes, please follow these steps:

1.  **Create a new branch:** Work on a new branch with a descriptive name.
    ```bash
    # For a new feature:
    git checkout -b feature/your-feature-name

    # For a bug fix:
    git checkout -b fix/bug-description
    ```

2.  **Make your changes:** Write your code and ensure it is well-commented and follows the project's style guide.

3.  **Commit your changes:** Use clear and concise commit messages. Reference the issue number if your commit resolves an issue.
    ```bash
    git add .
    git commit -m "feat: Implement node snapping feature (closes #12)"
    ```

4.  **Push your branch to your fork:**
    ```bash
    git push origin feature/your-feature-name
    ```

5.  **Open a Pull Request (PR):**
    -   Navigate to the original repository on GitHub.
    -   Click the "New pull request" button.
    -   Provide a clear title and a detailed description of your changes.
    -   Link to the issue your PR is addressing (e.g., `Closes #12`).
    -   Submit the pull request for review.

A project maintainer will review your PR, provide feedback, and merge it if it meets the contribution standards.

## Style Guides

### Python Code
All Python code should adhere to the **[PEP 8 style guide](https://www.python.org/dev/peps/pep-0008/)**. We recommend using a linter like `flake8` or an auto-formatter like `black` to ensure compliance.

### Commit Messages
Commit messages should follow a conventional format to maintain a clear project history.
-   Start with a type (`feat`, `fix`, `docs`, `style`, `refactor`, `test`).
-   Use the imperative mood (e.g., "Add feature" not "Added feature").
-   Keep the subject line short and descriptive.

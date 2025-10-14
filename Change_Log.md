### **Version Update - 10/13/2025: Update Notification System & Stability Fixes**

This update introduces a new, non-intrusive update notification system to keep you informed of the latest features and fixes, alongside a crucial stability enhancement for the update-checking process itself.

#### **Alabaster: Smart Update Notifications**

Staying up-to-date should be effortless. This release introduces a new, intelligent update notification system designed to be informative without being disruptive. My philosophy is that you should be in control of your workspace, and this feature reflects that.

*   **Automatic & Asynchronous Checking:** When the application starts, it now performs a silent, one-time check in the background to see if a new version is available. This process is fully asynchronous, meaning it will never freeze or slow down your startup experience.
*   **Non-Intrusive Notification:** You won't be interrupted by pop-ups. Instead, if a new version is detected, a clean and clear notification will be waiting for you within the Settings dialog. This allows you to check for updates on your own terms.
*   **Clear Status, Always:** The Settings dialog now provides transparent feedback on the update check's status. You will always know if the application is up-to-date, if an update is available, or if the check encountered a network error.
*   **Robust Cache-Busting:** The web request has been engineered to be highly robust. It sends specific headers that instruct servers and proxies to bypass their caches, ensuring the check always fetches the latest, live version information and never gives a false negative due to stale, cached data.
---
---
### **Update Log: Critical Hotfix for AI Reasoning (Chain-of-Thought) Display**

**Date:** 2025-10-13
**Version:** Hotfix 0.94.3

#### **Summary (TL;DR)**

A critical hotfix has been deployed to address a bug where the AI's step-by-step reasoning was not being displayed in the chat interface. The "View Reasoning" button, which reveals the model's thought process, is now fully functional again for all compatible models. 

#### Expected .exe push update soon

---

#### **The Issue: Missing "View Reasoning" Button**

We identified a critical issue reported by our users where the "View Reasoning" button was consistently absent from the AI assistant's chat bubbles. This feature provides transparency into the model's Chain-of-Thought (CoT) process, allowing users to understand how an answer was formulated. Its absence was a significant regression in functionality and user experience.

#### **Root Cause Analysis**

After a thorough investigation, we determined the root cause was an upstream change in the Ollama API's response structure (specifically in versions 0.12.5 and newer).

In a welcome move to improve clarity, the Ollama API now separates the model's reasoning process into a dedicated `thinking` field within the response. Previously, this reasoning block was embedded directly inside the main `content` field.

Our application's data parsing logic was still programmed to look for the reasoning in the old, embedded location. When the new API structure was encountered, our system failed to find the `thinking` data, and as a result, the UI correctly determined there was no reasoning to display.

#### **The Solution: A Robust and Backward-Compatible Fix**

The `SynthesisAgent`, responsible for handling communication with the LLM, has been updated with smarter response-handling logic:

1.  **Primary Parsing Path:** The system now correctly looks for and extracts the reasoning from the new, dedicated `thinking` field provided by modern Ollama versions.
2.  **Fallback Mechanism:** To ensure full compatibility, we have retained the old parsing logic as a fallback. If the `thinking` field is not present in a response, the system will then scan the main content for the inline reasoning block.

This dual approach ensures that the "View Reasoning" feature works seamlessly for users running any version of the Ollama service, providing both forward compatibility with the latest updates and backward compatibility for those on older installations.

#### **Impact on Users**

With this fix deployed, the "View Reasoning" functionality is fully restored. You can once again gain valuable insight into the AI's problem-solving process. No action is required on your part.

We extend our sincere thanks to the community member who provided the detailed logs and API outputs that allowed for a swift diagnosis and resolution of this issue. Your feedback is invaluable in helping us maintain the quality and reliability of the application.

---
---
---
### **Version Update - 10/12/2025: Architectural Overhaul & Major New Features**

This is a landmark update that touches nearly every part of the application. I've focused on rebuilding core systems for performance and reliability, while also introducing powerful new features and quality-of-life improvements based on how I see the app evolving.

#### **Bedrock: Architectural Overhaul - Next-Generation Data Storage**

I've fundamentally rebuilt how conversational data is stored and managed, transitioning from a system of individual text files to a robust, high-performance SQLite database. This architectural shift moves the application to a professional-grade storage solution, delivering massive improvements in speed, reliability, and scalability.

*   **Enhanced Performance & Scalability:** You will experience a dramatic increase in speed, especially when loading your chat history. What once required scanning multiple files on disk is now an instantaneous, indexed database query. The application will feel significantly more responsive, regardless of whether you have ten chats or ten thousand.
*   **Rock-Solid Data Integrity:** The new database system is transactional, which protects your chat history against corruption from unexpected application crashes or power failures. This ensures your conversations are always saved safely.
*   **Seamless, Automatic Migration:** For existing users, this transition is completely effortless. On its first run after the update, the application will automatically detect your old chat files and migrate them into the new database. All of your history will be preserved with no action required from you.
*   **Foundation for Future Features:** This new architecture isn't just about improving what's here; it's about unlocking what's next. It lays the groundwork for powerful future capabilities, such as a full-text search across all of your past conversations.

#### **Flint: Automated Installer & Onboarding**

Getting started previously required manually finding, downloading, and installing Ollama, followed by using command-line tools to pull AI models. This process could be a significant barrier.

The new Corted Startup utility completely transforms this experience into a simple, guided workflow. My goal is to remove technical barriers and empower every user to get up and running in minutes.

*   **Guided Ollama Installation:** The utility now presents direct download links for Windows and macOS and a one-click copy command for Linux. There's no more need to search for installation instructions.
*   **Integrated Model Manager:** Forget the command line. You can now browse a curated list of available AI models directly within the application. Select the model you want, click "Pull," and monitor the download and installation progress in real-time.
*   **A Polished, All-in-One Interface:** This entire process is wrapped in a clean, modern interface featuring a draggable window and a light/dark theme toggle to match your workspace. It provides a professional and centralized starting point for the entire Corted experience.

#### **Quartz: AI Customization - User-Defined System Instructions**

This update introduces a powerful new dimension of control over the AI assistant. You can now define a persistent persona and set global behavioral rules through the new System Instructions feature, allowing you to tailor the AI's core identity to your specific needs.

This feature was engineered with extreme care. The underlying AI prompt has been restructured to create an unmistakable hierarchy, ensuring the model perfectly understands its core function, your custom instructions, and the context of your conversation.

*   **Set a Persistent Persona:** Accessed via the Settings menu, the new System Instructions dialog allows you to provide high-level directives that apply to every conversation.
*   **Precision Control Over AI Behavior:** Your custom instructions are given the highest priority, giving you unprecedented influence over the tone, style, and structure of the AI's responses.
*   **Robust & Unambiguous Prompting:** The AI's core prompt has been meticulously re-architected to ensure your instructions are integrated without conflicting with its primary system functions, leading to a more predictable and reliable output.

#### **Obsidian: Advanced Model Controls**

Building upon the foundation of AI customization, this update provides granular control over the core parameters of the language model itself. These advanced settings have been moved to their own dedicated dialog, ensuring a clean, uncluttered interface that gives power users the tools they need to fine-tune the AI's performance.

*   **Dedicated Control Panel:** A new "Advanced Model Settings" dialog provides a focused workspace for adjusting the AI's inner workings without cluttering the main settings panel.
*   **Temperature Control:** An intuitive slider allows you to precisely manage the AI's creativity. Lower the temperature for more deterministic, factual responses, or raise it to encourage more novel and imaginative output.
*   **Context Window Management:** Directly set the size of the model's conversational memory (context window). Increase it for longer-term context retention in complex conversations, or decrease it to optimize performance and memory usage.
*   **Reproducible Outputs with Seeding:** A seed value can now be set to ensure the AI produces the exact same response to the same prompt every time, a crucial feature for testing, development, and content generation. A "Random" button is provided for convenience.

#### **Amethyst: Keyboard Shortcuts - Command at the Speed of Thought**

A truly powerful tool should feel like an extension of your own thoughts, minimizing the friction between intent and action. This update introduces a comprehensive suite of keyboard shortcuts, designed to keep your hands on the keyboard and your mind focused on the conversation. My goal is to elevate the application from a simple point-and-click interface to a high-velocity command center for power users.

*   **New Conversation (Ctrl+N):** Instantly start a fresh chat without reaching for the mouse, keeping your workflow seamless.
*   **Access Settings (Ctrl+,):** Quickly open the settings dialog with a standard, universal shortcut to tweak the AI's model or appearance on the fly.
*   **Focus Input (Ctrl+L):** Immediately jump to the chat input field from anywhere in the application. This is a massive time-saver for rapid-fire questioning.
*   **Close Window (Ctrl+W):** A standard, convenient way to close the application window when your session is complete.
*   **Cross-Platform Native Feel:** These shortcuts are intelligently mapped, automatically translating to `Cmd` on macOS to ensure a native and intuitive experience on any operating system.

#### **Diamond: Hierarchical UI & Blur Overhaul**

I've re-architected the application's dialog system to create a proper sense of depth and focus. The previous implementation could incorrectly blur parent dialogs, leading to a confusing user experience. This has been resolved with a more intelligent, stack-based management system.

*   **Correct Visual Hierarchy:** The application now correctly tracks the layering of all open windows. When a new dialog appears, only the windows *behind* it are blurred, ensuring the active window is always sharp and clearly in focus.
*   **Robust Dialog Stacking:** The new system can flawlessly handle any number of nested dialogs (e.g., opening "Manage Memories" from within the "Settings" dialog) while maintaining the correct visual state. This is a crucial fix for UI professionalism and usability.

#### **Sandstone & Keystone: UX and Logic Enhancements**

These updates focus on improving the core conversational experience and providing greater control over your data.

*   **New Feature: Clear All Chat History (Sandstone):** You now have the ability to permanently delete your entire chat history. This action performs a "clean sweep" of all recorded conversations, resetting your history to a blank slate. This option is accessible by right-clicking the "+ New Chat" button. A confirmation prompt will appear to prevent accidental data loss.
*   **Resolved Repetition Bug (Keystone):** I've fixed a core logical issue where the AI would sometimes perceive the user's first message as a repeated statement, causing odd conversational artifacts (e.g., greeting you "again"). This is now fully resolved, significantly improving the model's contextual understanding from the very first turn.

#### **Shale: UI Polish & Theming Fixes**

A powerful tool should also be a pleasure to use. This update refines the application's user interface by addressing several visual inconsistencies and bugs.

*   **Instantaneous Theme Switching:** All UI elements now update their appearance instantly when switching between light and dark modes.
*   **Improved UI Clarity:** Corrected an issue where the text field in the System Instructions dialog could blend into the background in light mode.
*   **Stylesheet Stability:** Fixed a minor bug that could cause stylesheet parsing errors, leading to more stable and robust rendering of all UI components.

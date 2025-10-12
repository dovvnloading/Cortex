### **Version Update - 10/12/2025: Architectural Overhaul & Major New Features**

This is a landmark update that touches nearly every part of the application. I've focused on rebuilding core systems for performance and reliability, while also introducing powerful new features and quality-of-life improvements based on how I see the app evolving.

---

### **Bedrock: Architectural Overhaul - Next-Generation Data Storage**

I've fundamentally rebuilt how conversational data is stored and managed, transitioning from a system of individual text files to a robust, high-performance SQLite database. This architectural shift moves the application to a professional-grade storage solution, delivering massive improvements in speed, reliability, and scalability.

*   **Enhanced Performance & Scalability:** You will experience a dramatic increase in speed, especially when loading your chat history. What once required scanning multiple files on disk is now an instantaneous, indexed database query. The application will feel significantly more responsive, regardless of whether you have ten chats or ten thousand.
*   **Rock-Solid Data Integrity:** The new database system is transactional, which protects your chat history against corruption from unexpected application crashes or power failures. This ensures your conversations are always saved safely.
*   **Seamless, Automatic Migration:** For existing users, this transition is completely effortless. On its first run after the update, the application will automatically detect your old chat files and migrate them into the new database. All of your history will be preserved with no action required from you.
*   **Foundation for Future Features:** This new architecture isn't just about improving what's here; it's about unlocking what's next. It lays the groundwork for powerful future capabilities, such as a full-text search across all of your past conversations.

---

### **Flint: Automated Installer & Onboarding**

Getting started previously required manually finding, downloading, and installing Ollama, followed by using command-line tools to pull AI models. This process could be a significant barrier.

The new **Corted Startup** utility completely transforms this experience into a simple, guided workflow. My goal is to remove technical barriers and empower every user to get up and running in minutes.

*   **Guided Ollama Installation:** The utility now presents direct download links for Windows and macOS and a one-click copy command for Linux. There's no more need to search for installation instructions.
*   **Integrated Model Manager:** Forget the command line. You can now browse a curated list of available AI models directly within the application. Select the model you want, click "Pull," and monitor the download and installation progress in real-time.
*   **A Polished, All-in-One Interface:** This entire process is wrapped in a clean, modern interface featuring a draggable window and a light/dark theme toggle to match your workspace. It provides a professional and centralized starting point for the entire Corted experience.

---

### **Quartz: AI Customization - User-Defined System Instructions**

This update introduces a powerful new dimension of control over the AI assistant. You can now define a persistent persona and set global behavioral rules through the new **System Instructions** feature, allowing you to tailor the AI's core identity to your specific needs.

This feature was engineered with extreme care. The underlying AI prompt has been restructured to create an unmistakable hierarchy, ensuring the model perfectly understands its core function, your custom instructions, and the context of your conversation.

*   **Set a Persistent Persona:** Accessed via the Settings menu, the new System Instructions dialog allows you to provide high-level directives that apply to every conversation.
*   **Precision Control Over AI Behavior:** Your custom instructions are given the highest priority, giving you unprecedented influence over the tone, style, and structure of the AI's responses.
*   **Robust & Unambiguous Prompting:** The AI's core prompt has been meticulously re-architected to ensure your instructions are integrated without conflicting with its primary system functions, leading to a more predictable and reliable output.

---

### **Amethyst: Keyboard Shortcuts - Command at the Speed of Thought**

A truly powerful tool should feel like an extension of your own thoughts, minimizing the friction between intent and action. This update introduces a comprehensive suite of keyboard shortcuts, designed to keep your hands on the keyboard and your mind focused on the conversation. My goal is to elevate the application from a simple point-and-click interface to a high-velocity command center for power users.

*   **New Conversation (`Ctrl+N`):** Instantly start a fresh chat without reaching for the mouse, keeping your workflow seamless.
*   **Access Settings (`Ctrl+,`):** Quickly open the settings dialog with a standard, universal shortcut to tweak the AI's model or appearance on the fly.
*   **Focus Input (`Ctrl+L`):** Immediately jump to the chat input field from anywhere in the application. This is a massive time-saver for rapid-fire questioning.
*   **Close Window (`Ctrl+W`):** A standard, convenient way to close the application window when your session is complete.
*   **Cross-Platform Native Feel:** These shortcuts are intelligently mapped, automatically translating to `Cmd` on macOS to ensure a native and intuitive experience on any operating system.

---

### **Sandstone & Keystone: UX and Logic Enhancements**

These updates focus on improving the core conversational experience and providing greater control over your data.

*   **New Feature: Clear All Chat History (Sandstone):** You now have the ability to permanently delete your entire chat history. This action performs a "clean sweep" of all recorded conversations, resetting your history to a blank slate. This option is accessible by **right-clicking the "+ New Chat" button**. A confirmation prompt will appear to prevent accidental data loss.
*   **Resolved Repetition Bug (Keystone):** I've fixed a core logical issue where the AI would sometimes perceive the user's first message as a repeated statement, causing odd conversational artifacts (e.g., greeting you "again"). This is now fully resolved, significantly improving the model's contextual understanding from the very first turn.

---

### **Shale: UI Polish & Theming Fixes**

A powerful tool should also be a pleasure to use. This update refines the application's user interface by addressing several visual inconsistencies and bugs.

*   **Instantaneous Theme Switching:** All UI elements now update their appearance instantly when switching between light and dark modes.
*   **Improved UI Clarity:** Corrected an issue where the text field in the System Instructions dialog could blend into the background in light mode.
*   **Stylesheet Stability:** Fixed a minor bug that could cause stylesheet parsing errors, leading to more stable and robust rendering of all UI components.

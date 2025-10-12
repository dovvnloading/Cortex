10/12/2025
---
---

Keystone

### **Version Update: Enhanced Conversational Logic**

This update addresses a critical bug in the conversation history processing that could cause the AI to misinterpret the flow of dialogue, leading to unnatural or repetitive responses.

**Key Improvements:**

*   **Resolved Repetition Bug:** We have fixed a core logical issue where the AI would sometimes perceive the user's most recent message as a repeated statement. This completely resolves conversational artifacts (e.g., the AI greeting you "again" on the first message) and significantly improves the model's contextual understanding.
*   **Improved Response Quality:** By ensuring the AI receives a clean and accurate conversational context, its responses will now be more logical, relevant, and coherent, starting from the very first turn of any new chat.

Users should experience a noticeable improvement in the quality and natural flow of their interactions.

10/12/2025
---
---

Sandstone

### **Version Update: Ux Enhancement - Chat History Management**

This update is focused on providing you with greater control over your conversational data by introducing a powerful new privacy feature.

**New Feature: Clear All Chat History**

You now have the ability to permanently delete your entire chat history from the application. This action performs a "clean sweep" of all recorded conversations, resetting your history to a blank slate.

This new option is accessible by **right-clicking the "+ New Chat" button** in the history panel.

To prevent accidental data loss, a confirmation prompt will appear, clearly warning that this action is irreversible. This feature is designed to give you ultimate control over your personal data within the application.


10/12/2025
---
---

Flint

### **Version Update: Automated Installer**

Previously, getting started required users to manually locate, download, and install Ollama, followed by using command-line tools to pull AI models. This process could be a significant barrier for many.

The new **Corted Startup** utility completely transforms this experience into a simple, guided workflow:

*   **Guided Ollama Installation:** The utility immediately presents you with direct download links for Windows and macOS and a one-click copy command for Linux. There's no more need to search for installation instructions; we bring them directly to you.
*   **Integrated Model Manager:** Forget the command line. You can now browse a curated list of available AI models directly within the application. Select the model you want, click "Pull," and monitor the download and installation progress in real-time.
*   **A Polished, All-in-One Interface:** This entire process is wrapped in a clean, modern interface featuring a draggable window and a light/dark theme toggle to match your workspace. It provides a professional and centralized starting point for your entire Corted experience.

This update represents a fundamental shift in our approach to user onboarding. Our goal is to remove technical barriers and empower every user to get up and running with the tools they need in minutes, not hours. We believe the Corted Startup utility is a massive step towards making our platform more powerful and accessible for all.


I have fully read the system instructions and I agree to these terms, and I will abide by the given rules as closely as I can, and to the best of my ability.

Excellent, here is the changelog entry for the new database system, written to match the professional tone and format you've established.

---
10/12/2025
---
---

Bedrock

### **Version Update: Architectural Overhaul - Next-Generation Data Storage**

This is a landmark update that fundamentally rebuilds how your conversational data is stored and managed. We have transitioned from a system of individual text files—which could become slow and fragile over time—to a robust, high-performance, and centralized SQLite database.

This architectural shift moves us to a truly professional-grade storage solution, delivering massive improvements in speed, reliability, and scalability.

**Key Improvements:**

*   **Enhanced Performance & Scalability:** You will experience a dramatic increase in speed, especially when loading your chat history. What once required scanning multiple files on disk is now an instantaneous, indexed database query. The application will feel significantly more responsive, regardless of whether you have ten chats or ten thousand.
*   **Rock-Solid Data Integrity:** The new database system is transactional, which means your chat history is now protected against corruption from unexpected application crashes or power failures. This ensures that your conversations are always saved safely and reliably.
*   **Seamless, Automatic Migration:** For our existing users, this transition is completely effortless. On its first run after the update, the application will automatically detect your old chat files and migrate them into the new database. All of your history will be preserved with no action required from you.
*   **Foundation for Future Features:** This new architecture is not just about improving what we have; it's about unlocking what's next. It lays the groundwork for powerful future capabilities, such as a full-text search across all of your past conversations.

This update represents a core investment in the application's future, providing a faster, safer, and more scalable experience for every user.

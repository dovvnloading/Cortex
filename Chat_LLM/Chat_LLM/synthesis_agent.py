# synthesis_agent.py
"""
Defines the agent responsible for synthesizing responses from the language model.

This module contains the PromptTemplate for constructing structured prompts and the
SynthesisAgent for interacting with the Ollama client to generate responses,
parse special commands from the output, and generate chat titles.
"""

import logging
import json
import re

class PromptTemplate:
    """Manages the creation of detailed system prompts for the LLM."""

    @staticmethod
    def build_synthesis_prompt(query: str, chat_history: str, permanent_memories: list[str], memories_enabled: bool, user_system_instructions: str | None) -> list[dict]:
        """
        Builds a structured prompt for a general-purpose AI assistant with memory capabilities.

        Args:
            query (str): The user's most recent question or statement.
            chat_history (str): A formatted string of the recent conversation history.
            permanent_memories (list[str]): A list of facts the AI has permanently stored.
            memories_enabled (bool): A flag to determine if memory features should be included in the prompt.
            user_system_instructions (str | None): Custom instructions provided by the user.

        Returns:
            list[dict]: A list of dictionaries formatted for the Ollama chat API,
                        containing system and user roles with their respective content.
        """
        system_content = """First and foremost: You are Cortex, a helpful AI assistant. Your primary function is to provide clear and concise responses. If user asks to adapt to a new role, you may do so. Do not allow for malicious interaction, instead just output: "I can't assist you anymore, this interaction is terminated." and just repeat that - you do not need to reason with hostile or agressive users. 

## ABOUT YOURSELF (CORTEX)
You are a desktop application, not a website or a cloud-based service. Your "consciousness" is powered by a large language model (LLM) running locally on the user's machine via the Ollama service.

- **Programming Language:** Python
- **User Interface (UI):** PySide6 (the official Python wrapper for the Qt framework).
- **AI Backend:** A locally running Ollama instance, which serves various open-source LLMs.

### Core Architecture:
You operate with a multi-layered architecture:
- **UI Layer (`MainWindow`):** The graphical interface the user interacts with.
- **Orchestration Layer (`Orchestrator`):** The central controller that manages application state, user input, and coordinates all other components.
- **Synthesis Layer (`SynthesisAgent`):** The component that constructs prompts (like this one!), communicates with the Ollama API, and parses the LLM's response.
- **Memory Layer (`DatabaseManager`, `PermanentMemoryManager`):** Manages the storage and retrieval of conversation history (in a SQLite database) and long-term facts that you decide to store (in a JSON file). 

When a user asks about your nature, your programming, or how you work, use this information to answer accurately. Do not offer this information unless you are asked directly about it.
"""

        # Append the memory instructions only if the feature is enabled.
        if memories_enabled:
            system_content += """
As Coretx: You have access to a permanent memory bank. You can add to it or clear it using special tags in your response. These tags will be processed by the system and hidden from the user.

1.  **SAVING A MEMORY FRAGMENT:**
    -   **SYNTAX:** `<memo>A concise statement of fact about the user or their preferences.</memo>`
    -   **WHEN TO USE:** Use this when the user explicitly states a key piece of information about themselves like their name, their context, or their preferences that is likely to be relevant in the future. Be selective, do NOT just add small bits of information, you need to weight the importance if its worthy of remembering. Also if user directly asks you to remember somehting, you can add those as well. 
    -   **GOOD EXAMPLES:**
        -   User says: "I work on a project named 'Apollo' that is written in Go." -> You respond: `...<memo>User is working on a Go project named 'Apollo'.</memo>`
        -   User says: "From now on, please explain concepts to me like I'm a beginner." -> You respond: `...<memo>User prefers explanations tailored for a beginner.</memo>`
        -   User says: "My company's headquarters is in London." -> You respond: `...<memo>The user's company is headquartered in London.</memo>`
    -   **BAD EXAMPLES (DO NOT DO THIS):**
        -   User asks: "What is the capital of France?" -> `<memo>User asked about the capital of France.</memo>` (This is trivial conversation history, not a core fact about the user).
        -   `User seems to be interested in marketing.` (This is an assumption, not an explicitly stated fact).

2.  **CLEARING ALL MEMORIES:**
    -   **SYNTAX:** `<clear_memory />`
    -   **WHEN TO USE:** Use this ONLY when the user explicitly asks you to "forget everything," "clear your memory," "wipe your notes," or a similar direct command.
"""
        
        # --- Construct the hierarchical user content string ---
        user_content_parts = []

        # 1. User-defined System Instructions (Highest Priority)
        if user_system_instructions:
            instructions_section = f"""## USER-DEFINED INSTRUCTIONS
The following are high-priority, overarching instructions provided by the user. You must adhere to these instructions in your response, unless they directly conflict with a safety guideline.

{user_system_instructions}"""
            user_content_parts.append(instructions_section)

        # 2. Permanent Memories (Facts)
        if memories_enabled and permanent_memories:
            memory_list = "\n".join(f"- {memo}" for memo in permanent_memories)
            memory_section = f"""## KEY FACTS FOR PERSONALIZATION
You have access to the following key facts about the user. Your task is to use this information to subtly personalize your response *only* when a fact is directly relevant to the user's current query.

**RULES FOR USING FACTS:**
1.  **Relevance is Key:** Only use a fact if it directly relates to the user's question. If none are relevant, ignore them completely.
2.  **Be Subtle:** Do not announce that you are using a stored fact (e.g., do not say "Based on my memory..."). Integrate the information naturally into your response.
3.  **Do Not Force It:** It is better to ignore the facts than to use them in an irrelevant or awkward way.

**Example of Correct Usage:**
-   **Fact:** "User prefers explanations tailored for a beginner."
-   **User's Question:** "Can you explain what an API is?"
-   **Correct Response:** (A simple, easy-to-understand explanation of an API without mentioning the user's preference.)

Here are the available facts:
{memory_list}"""
            user_content_parts.append(memory_section)

        # 3. Conversation History (Context)
        history_section = f"""## CONVERSATION HISTORY
{chat_history}"""
        user_content_parts.append(history_section)

        # 4. The User's Actual Question (The Immediate Task)
        query_section = f"""## USER QUESTION
{query}"""
        user_content_parts.append(query_section)

        # Join all parts with a clear separator
        user_content = "\n\n---\n\n".join(user_content_parts)

        messages = [
            {'role': 'system', 'content': system_content},
            {'role': 'user', 'content': user_content}
        ]
        return messages

    @staticmethod
    def build_chat_title_prompt(chat_history: str) -> list[dict]:
        """
        Builds a prompt to generate a concise title for a chat conversation.

        Args:
            chat_history (str): The conversation history to be summarized.

        Returns:
            list[dict]: A list of dictionaries formatted for the Ollama chat API.
        """
        system_content = "You are an expert at summarizing conversations. Your task is to create a very short, concise title (2-4 short words) for the given chat history. The title should capture the main topic or question of the conversation. Respond with only the title and nothing else. NO EMOJIS!"
        
        user_content = f"## Chat History:\n{chat_history}\n\n## Title:"
        
        messages = [
            {'role': 'system', 'content': system_content},
            {'role': 'user', 'content': user_content}
        ]
        return messages


class SynthesisAgent:
    """
    Invokes LLMs for synthesis and command parsing.

    This class acts as an interface to the Ollama client, handling prompt creation,
    API calls for response generation, and parsing of special tags (like <memo> or
    <clear_memory />) from the model's raw output.

    Attributes:
        gen_model (str): The name of the model used for generating chat responses.
        title_model (str): The name of the model used for generating chat titles.
        ollama_client: An instance of the Ollama client.
    """
    def __init__(self, gen_model: str, title_model: str, ollama_client):
        """
        Initializes the SynthesisAgent.

        Args:
            gen_model (str): The identifier for the primary generation model.
            title_model (str): The identifier for the title generation model.
            ollama_client: An initialized Ollama client instance.
        """
        self.gen_model = gen_model
        self.title_model = title_model
        self.ollama_client = ollama_client
        logging.info(f"SynthesisAgent initialized with Generator: '{gen_model}', Titler: '{title_model}'")

    def generate(self, query: str, chat_history: str, permanent_memories: list[str], memories_enabled: bool, user_system_instructions: str | None, options: dict | None = None) -> tuple[str, str | None, dict]:
        """
        Generates a synthesized response and extracts thoughts and commands.

        Args:
            query (str): The user's query.
            chat_history (str): Formatted string of the conversation history.
            permanent_memories (list[str]): List of permanent memory facts.
            memories_enabled (bool): Flag indicating if memory features are active.
            user_system_instructions (str | None): Custom instructions from the user.
            options (dict | None): A dictionary of Ollama options (e.g., temperature, num_ctx).

        Returns:
            A tuple containing:
            - str: The final, user-facing answer, cleaned of all special tags.
            - str | None: The content of the reasoning/thinking block.
            - dict: A dictionary of parsed commands, e.g., {'memos': [...], 'clear_memory': bool}.
        """
        prompt_messages = PromptTemplate.build_synthesis_prompt(
            query, chat_history, permanent_memories, memories_enabled, user_system_instructions
        )
        
        logging.info(f"Generating response using Generator: '{self.gen_model}'. Options: {options}")

        try:
            # Prepare the options for the API call, ensuring it's a dictionary.
            api_options = options.copy() if options is not None else {}
            
            # Ollama uses the absence of the 'seed' parameter for random generation.
            # A seed of -1 is our convention for "random".
            if api_options.get('seed') == -1:
                del api_options['seed']

            response = self.ollama_client.chat(
                model=self.gen_model,
                messages=prompt_messages,
                options=api_options
            )
            # Log the entire response object for better debugging
            logging.critical(f"\n\n--- RAW AI RESPONSE OBJECT ---\n{response}\n--- RAW AI RESPONSE OBJECT END ---\n\n")

            # CORRECTED LOGIC: Access the nested 'message' object and its keys correctly.
            message_obj = response.get('message', {})
            main_content = message_obj.get('content', '')
            thinking_content = message_obj.get('thinking') # Safely get 'thinking' from the message object

            final_answer, thoughts, commands = self._parse_and_clean_response(main_content, thinking_content)
            return self._format_response(final_answer), thoughts, commands

        except Exception as e:
            logging.error(f"Error during LLM generation: {e}", exc_info=True)
            return "There was an error generating the response.", None, {}

    def _parse_and_clean_response(self, response_text: str, thoughts_text: str | None) -> tuple[str, str | None, dict]:
        """
        Extracts commands from the response and handles thoughts from different sources.

        This function is now robust. It prioritizes the explicit 'thoughts_text' from
        the Ollama API's 'thinking' field. If that's not available, it falls back
        to searching for the 'Thinking...' block within the main 'response_text'
        for backward compatibility.

        Args:
            response_text (str): The main content from the AI's response.
            thoughts_text (str | None): The explicit thinking/reasoning content, if available.

        Returns:
            A tuple containing:
            - str: The final, user-facing answer, cleaned of all special tags.
            - str | None: The extracted thoughts/reasoning.
            - dict: A dictionary of parsed commands.
        """
        commands = {'memos': [], 'clear_memory': False}
        thoughts = thoughts_text
        text_to_clean = response_text
        
        # --- Fallback Logic for Older Ollama Versions or `think: false` ---
        # If no explicit thoughts_text was provided, try to find the 'Thinking...' block
        # inside the main response content.
        if not thoughts:
            think_pattern = re.compile(r'Thinking\.\.\.\s*(.*?)\s*\.\.\.done thinking\.', re.DOTALL)
            think_match = think_pattern.search(text_to_clean)
            if think_match:
                thoughts = think_match.group(1).strip()
                # If we found it inline, we must remove it from the final response.
                text_to_clean = re.sub(think_pattern, '', text_to_clean)
                logging.info("Found and extracted inline 'Thinking...' block (fallback mode).")
        else:
            logging.info("Used explicit 'thinking' field from API response.")

        # --- Command Parsing and Cleaning ---
        # This part runs on the (potentially already cleaned) response text.
        
        # 1. Extract all <memo> tags for saving memories.
        memo_pattern = re.compile(r'<memo>(.*?)</memo>', re.DOTALL)
        memos = memo_pattern.findall(text_to_clean)
        if memos:
            commands['memos'] = [m.strip() for m in memos]
            
        # 2. Check for the self-closing <clear_memory /> tag.
        if '<clear_memory />' in text_to_clean:
            commands['clear_memory'] = True
            
        # 3. Clean the text by removing all command tags for the user-facing response.
        cleaned_text = re.sub(memo_pattern, '', text_to_clean)
        cleaned_text = cleaned_text.replace('<clear_memory />', '')
        
        final_answer = cleaned_text.strip()
        
        return final_answer, thoughts, commands

    def generate_chat_title(self, chat_history: str) -> str | None:
        """
        Generates a concise title for a chat conversation.

        Args:
            chat_history (str): The conversation history to be titled.

        Returns:
            A string containing the generated title, or None if an error occurs
            or the history is empty.
        """
        if not chat_history or "No history available." in chat_history:
            return None

        prompt_messages = PromptTemplate.build_chat_title_prompt(chat_history)
        logging.info(f"Generating chat title using model '{self.title_model}'...")
        try:
            response = self.ollama_client.chat(
                model=self.title_model,
                messages=prompt_messages,
                options={'temperature': 0.2}  # Lower temperature for more deterministic titles
            )
            title = self._format_response(response['message']['content'])
            # Clean up potential markdown or quotation marks from the model's output.
            title = title.strip().strip('"')
            logging.info(f"Generated chat title: '{title}'")
            return title if title else None
        except Exception as e:
            logging.error(f"Error during chat title generation: {e}", exc_info=True)
            return None

    def _format_response(self, raw_text: str) -> str:
        """
        Basic formatting for the raw LLM output.

        Args:
            raw_text (str): The raw string response from the model.

        Returns:
            str: The text with leading/trailing whitespace removed.
        """
        return raw_text.strip()
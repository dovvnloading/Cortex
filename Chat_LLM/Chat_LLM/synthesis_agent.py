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
    def build_synthesis_prompt(query: str, chat_history: str, permanent_memories: list[str], memories_enabled: bool) -> list[dict]:
        """
        Builds a structured prompt for a general-purpose AI assistant with memory capabilities.

        Args:
            query (str): The user's most recent question or statement.
            chat_history (str): A formatted string of the recent conversation history.
            permanent_memories (list[str]): A list of facts the AI has permanently stored.
            memories_enabled (bool): A flag to determine if memory features should be included in the prompt.

        Returns:
            list[dict]: A list of dictionaries formatted for the Ollama chat API,
                        containing system and user roles with their respective content.
        """
        system_content = """You are a helpful AI assistant. Respond to the user's query clearly and concisely. Use the provided conversation history to maintain context. Use Markdown for formatting when it improves readability."""

        # Append the memory instructions only if the feature is enabled.
        if memories_enabled:
            system_content += """

## PERMANENT MEMORY CHEAT SHEET
You have access to a permanent memory bank. You can add to it or clear it using special tags in your response. These tags will be processed by the system and hidden from the user.

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
        
        memory_section = ""
        # Construct the memory section of the prompt if memories exist and are enabled.
        if memories_enabled and permanent_memories:
            memory_list = "\n".join(f"- {memo}" for memo in permanent_memories)
            memory_section = f"""## PERMANENT MEMORIES
You have recorded the following facts. Use them to personalize your response accordingly, do not use memories just for the sake of use...Be calculated how you use them and how you select to use them. Use critical thinking when deciding what to use or not.
{memory_list}

---
"""

        user_content = f"""{memory_section}## CONVERSATION HISTORY
{chat_history}

---

## USER QUESTION
{query}
"""

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

    def generate(self, query: str, chat_history: str, permanent_memories: list[str], memories_enabled: bool) -> tuple[str, str | None, dict]:
        """
        Generates a synthesized response and extracts thoughts and commands.

        Args:
            query (str): The user's query.
            chat_history (str): Formatted string of the conversation history.
            permanent_memories (list[str]): List of permanent memory facts.
            memories_enabled (bool): Flag indicating if memory features are active.

        Returns:
            A tuple containing:
            - str: The final, user-facing answer, cleaned of all special tags.
            - str | None: The content of the <think> tag, if present.
            - dict: A dictionary of parsed commands, e.g., {'memos': [...], 'clear_memory': bool}.
        """
        prompt_messages = PromptTemplate.build_synthesis_prompt(query, chat_history, permanent_memories, memories_enabled)
        
        logging.info(f"Generating response using Generator: '{self.gen_model}'.")

        try:
            response = self.ollama_client.chat(
                model=self.gen_model,
                messages=prompt_messages,
                options={'num_ctx': 8192}  # Context window size
            )
            raw_content = response['message']['content']
            final_answer, thoughts, commands = self._parse_and_clean_response(raw_content)
            return self._format_response(final_answer), thoughts, commands

        except Exception as e:
            logging.error(f"Error during LLM generation: {e}", exc_info=True)
            return "There was an error generating the response.", None, {}

    def _parse_and_clean_response(self, text: str) -> tuple[str, str | None, dict]:
        """
        Extracts content from special tags and returns the cleaned final answer.

        This method uses regex to find and extract content from <think>, <memo>,
        and <clear_memory /> tags, then removes them from the text to produce a
        clean, user-facing response.

        Args:
            text (str): The raw text output from the language model.

        Returns:
            A tuple containing:
            - str: The cleaned final answer for the user.
            - str | None: The extracted thoughts from the <think> tag.
            - dict: A dictionary of parsed commands.
        """
        commands = {'memos': [], 'clear_memory': False}
        
        # 1. Extract <think> tags (assumed for internal reasoning, not currently used in prompt).
        think_pattern = re.compile(r'<think>(.*?)</think>', re.DOTALL)
        think_match = think_pattern.search(text)
        thoughts = think_match.group(1).strip() if think_match else None
        
        # 2. Extract all <memo> tags for saving memories.
        memo_pattern = re.compile(r'<memo>(.*?)</memo>', re.DOTALL)
        memos = memo_pattern.findall(text)
        if memos:
            commands['memos'] = [m.strip() for m in memos]
            
        # 3. Check for the self-closing <clear_memory /> tag.
        if '<clear_memory />' in text:
            commands['clear_memory'] = True
            
        # 4. Clean the text by removing all special tags for the user-facing response.
        cleaned_text = re.sub(think_pattern, '', text)
        cleaned_text = re.sub(memo_pattern, '', cleaned_text)
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
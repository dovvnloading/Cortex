/**
 * Render persisted conversation titles as plain application text.
 *
 * Older local sessions can contain Markdown wrappers emitted by a title
 * model. Conversation labels are never rich text, so this removes only full
 * outer wrappers while leaving ordinary punctuation intact.
 */
export function displayChatTitle(value: string | null | undefined, fallback = "Untitled chat"): string {
  let title = String(value ?? "")
    .split("")
    .map((character) => {
      const code = character.charCodeAt(0);
      return code <= 0x1f || code === 0x7f ? " " : character;
    })
    .join("")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^["'`]+|["'`]+$/g, "")
    .trim()
    .replace(/^(?:title\s*:\s*|#{1,6}\s+|[-+]\s+)/i, "");

  for (let index = 0; index < 3; index += 1) {
    const unwrapped = title
      .replace(/^(\*\*|__|`)(.+)\1$/, "$2")
      .replace(/^([*_])(.+)\1$/, "$2");
    if (unwrapped === title) break;
    title = unwrapped.trim();
  }

  title = title.replace(/^\[([^\]]+)\]\([^)]+\)$/, "$1").trim();
  return title || fallback;
}

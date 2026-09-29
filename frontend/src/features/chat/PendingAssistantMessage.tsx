import { useEffect } from "react";
import { GENERATION_GAP_NOTICE, humanizeGenerationStatus } from "../../lib/generationStatus";
import { useThrottledText } from "../../hooks/useThrottledText";
import { useChatStore } from "../../stores/useChatStore";
import { SafeMarkdown } from "../markdown/SafeMarkdown";

/**
 * How often the streamed text is handed to the Markdown parser. Tokens still
 * land in the store at animation-frame rate; parsing the whole partial answer
 * on every one of those frames is what this interval avoids.
 */
const MARKDOWN_INTERVAL_MS = 100;

type Props = {
  /**
   * Called after the visible reply changed, so the transcript can follow it
   * down (or offer "Jump to latest" to a reader who scrolled away). Keep it
   * referentially stable.
   */
  onOutputChange: () => void;
};

/**
 * The in-flight reply: the "thinking" line until text arrives, then the
 * partial answer and reasoning.
 *
 * It subscribes to the streamed text itself. ChatPage used to read the whole
 * `generation` object, which the store rebuilds on every flushed frame, so the
 * entire page -- every persisted message, the composer -- re-rendered up to
 * sixty times a second for text only this bubble draws. With the subscription
 * here, a frame re-renders this component and nothing else.
 */
export function PendingAssistantMessage({ onOutputChange }: Props) {
  const partialContent = useChatStore((state) => state.generation.partialContent);
  const partialThoughts = useChatStore((state) => state.generation.partialThoughts);
  const contentReady = useChatStore((state) => state.generation.contentReady);
  const statusText = useChatStore((state) => state.generation.statusText);
  const gap = useChatStore((state) => state.generation.gap);

  // Once the backend says the answer is complete the text is exact and final,
  // so throttling is switched off and it is shown without any delay.
  const content = useThrottledText(partialContent, MARKDOWN_INTERVAL_MS, !contentReady);
  const thoughts = useThrottledText(partialThoughts, MARKDOWN_INTERVAL_MS, !contentReady);

  const hasOutput = content !== "" || thoughts !== "";
  useEffect(() => {
    if (hasOutput) onOutputChange();
  }, [content, thoughts, hasOutput, onOutputChange]);

  if (partialContent === "" && partialThoughts === "") {
    return <GenerationStatus status={gap ? GENERATION_GAP_NOTICE : statusText} />;
  }

  return (
    /* Same markup and the same unframed treatment as a persisted assistant
       message, so when this is replaced by the real one nothing about the
       message changes shape or position. */
    <article className="message-card message-assistant message-pending" aria-label={contentReady ? "Cortex response ready, saving..." : "Cortex response in progress"}>
      <div className="message-bubble">
        {content && <div className="markdown-body"><SafeMarkdown content={content} finalized={contentReady} />{!contentReady && <span className="streaming-caret" aria-hidden="true" />}</div>}
        {!content && !contentReady && <span className="streaming-caret" aria-hidden="true" />}
        {/* Text after a hole is not shown (see useGenerationStream), so what is on screen may stop short. */}
        {gap && <p className="generation-gap-note" role="status">{GENERATION_GAP_NOTICE}</p>}
      </div>
      {/* Collapses in step with the "Live" badge, matching the persisted card's default state so the swap is invisible. */}
      {thoughts && <details className="reasoning" open={!contentReady}><summary><span>Reasoning</span>{!contentReady && <span className="disclosure-hint">Live</span>}</summary><div className="details-content"><div className="markdown-body"><SafeMarkdown content={thoughts} finalized={contentReady} /></div></div></details>}
    </article>
  );
}

function GenerationStatus({ status }: { status: string }) {
  return (
    <article className="message-card message-assistant message-pending" aria-label="Cortex response in progress">
      <div className="message-bubble">
        <div className="generation-status" role="status">
          {humanizeGenerationStatus(status)}
          <span className="generation-status-dots" aria-hidden="true"><i /><i /><i /></span>
        </div>
      </div>
    </article>
  );
}

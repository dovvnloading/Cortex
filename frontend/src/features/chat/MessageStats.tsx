import type { GenerationStats } from "../../../../contracts/cortex-api";

export function MessageStats({ stats }: { stats?: GenerationStats | null }) {
  // A stopped answer is the part the user saw before pressing Stop. The model
  // reported no usage for an unfinished turn, so say what happened instead.
  if (stats?.stopped) {
    return <span className="message-stats" title="You stopped this response; this is what had been written">Stopped</span>;
  }
  if (!stats || stats.tokens_per_second == null) return null;
  const seconds = stats.total_duration_ms ? (stats.total_duration_ms / 1000).toFixed(1) : null;
  return (
    <span className="message-stats" title="Generation performance for this response">
      {stats.eval_count ?? "?"} tok &middot; {stats.tokens_per_second} tok/s{seconds ? ` · ${seconds}s` : ""}
    </span>
  );
}

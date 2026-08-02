import type { GenerationStats } from "../../../../contracts/cortex-api";

export function MessageStats({ stats }: { stats?: GenerationStats | null }) {
  if (!stats || stats.tokens_per_second == null) return null;
  const seconds = stats.total_duration_ms ? (stats.total_duration_ms / 1000).toFixed(1) : null;
  return (
    <span className="message-stats" title="Generation performance for this response">
      {stats.eval_count ?? "?"} tok &middot; {stats.tokens_per_second} tok/s{seconds ? ` · ${seconds}s` : ""}
    </span>
  );
}

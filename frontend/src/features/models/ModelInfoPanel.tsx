import type { InstalledModel } from "../../../../contracts/cortex-api";
import { displayModelName, formatModelSize } from "../../lib/localModels";

function modelDetailSummary(model: InstalledModel): string | null {
  const parts: string[] = [];
  if (model.parameter_size) parts.push(`${model.parameter_size} params`);
  if (model.quantization_level) parts.push(model.quantization_level);
  if (model.context_length) parts.push(`${model.context_length.toLocaleString()} ctx`);
  const size = formatModelSize(model.size);
  if (size) parts.push(size);
  return parts.length > 0 ? parts.join(" · ") : null;
}

export function ModelInfoPanel({ model }: { model: InstalledModel }) {
  const detail = modelDetailSummary(model);
  return (
    <div className="model-info-row">
      <span className="model-chip">{displayModelName(model.name)}</span>
      <span className="model-info-source">{model.source === "gguf" ? "GGUF" : "Ollama"}</span>
      {detail && <span className="model-info-detail">{detail}</span>}
    </div>
  );
}

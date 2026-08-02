import { RotateCcw, SlidersHorizontal } from "lucide-react";
import type { GenerationOptionsOverride, GenerationSettings } from "../../../../contracts/cortex-api";
import { Popover, PopoverContent } from "../../shared/ui/Popover";

type OverrideField = keyof GenerationOptionsOverride;

type Props = {
  value: GenerationOptionsOverride | null;
  defaults: GenerationSettings;
  disabled?: boolean;
  onChange: (next: GenerationOptionsOverride | null) => void;
};

const FIELDS: { field: OverrideField; label: string; min: number; max: number; step: number }[] = [
  { field: "temperature", label: "Temperature", min: 0, max: 2, step: 0.1 },
  { field: "top_p", label: "Top P", min: 0, max: 1, step: 0.05 },
  { field: "top_k", label: "Top K", min: 0, max: 200, step: 1 },
  { field: "repeat_penalty", label: "Repeat penalty", min: 0.5, max: 2, step: 0.05 },
  { field: "num_ctx", label: "Context window", min: 2048, max: 16384, step: 1024 },
];

export function GenerationParamsPopover({ value, defaults, disabled = false, onChange }: Props) {
  const active = value !== null && Object.values(value).some((entry) => entry != null);

  const setField = (field: OverrideField, raw: string) => {
    const next: GenerationOptionsOverride = { ...(value ?? {}) };
    next[field] = raw === "" ? null : Number(raw);
    onChange(next);
  };

  return (
    <Popover.Root>
      <Popover.Trigger
        className={`icon-button icon-button-small${active ? " icon-button-active" : ""}`}
        aria-label="Generation parameters for this chat"
        title="Generation parameters for this chat"
        disabled={disabled}
      >
        <SlidersHorizontal size={15} aria-hidden="true" />
      </Popover.Trigger>
      <PopoverContent className="params-popover" aria-label="Generation parameters">
        <div className="params-popover-header">
          <span>Parameters for this chat</span>
          {active && (
            <button className="button button-quiet" type="button" onClick={() => onChange(null)}>
              <RotateCcw size={13} aria-hidden="true" /> Reset to defaults
            </button>
          )}
        </div>
        {FIELDS.map(({ field, label, min, max, step }) => {
          const current = value?.[field] ?? defaults[field];
          return (
            <label className="field-label params-popover-field" key={field} htmlFor={`param-${field}`}>
              {label} <span className="field-value">{current}</span>
              <input
                id={`param-${field}`}
                type="range"
                min={min}
                max={max}
                step={step}
                value={current ?? min}
                onChange={(event) => setField(field, event.target.value)}
              />
            </label>
          );
        })}
        <p className="params-popover-hint">Overrides apply to this chat only. Settings &rarr; AI Model controls the defaults.</p>
      </PopoverContent>
    </Popover.Root>
  );
}

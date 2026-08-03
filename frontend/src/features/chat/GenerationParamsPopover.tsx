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

const SAMPLING_FIELDS = FIELDS.slice(0, 4);
const CONTEXT_FIELD = FIELDS[4];

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
        className={`params-trigger${active ? " params-trigger-active icon-button-active" : ""}`}
        aria-label="Generation parameters for this chat"
        title="Generation parameters for this chat"
        disabled={disabled}
      >
        <SlidersHorizontal size={15} aria-hidden="true" />
        <span>Parameters</span>
        {active && <span className="params-trigger-dot" aria-hidden="true" />}
      </Popover.Trigger>
      <PopoverContent className="params-popover" aria-label="Generation parameters">
        <div className="params-popover-header">
          <div className="params-popover-title">
            <span className="params-popover-kicker">CHAT ONLY</span>
            <strong>Parameters for this chat</strong>
          </div>
          {active && (
            <button className="params-reset" type="button" aria-label="Reset to defaults" title="Reset to defaults" onClick={() => onChange(null)}>
              <RotateCcw size={12} aria-hidden="true" /> <span>Reset</span>
            </button>
          )}
        </div>
        <section className="params-popover-section" aria-labelledby="params-sampling-heading">
          <div className="params-popover-section-heading">
            <span id="params-sampling-heading">Sampling</span>
            <small>Fine-tune response style</small>
          </div>
          <div className="params-popover-grid">
            {SAMPLING_FIELDS.map(({ field, label, min, max, step }) => {
              const current = value?.[field] ?? defaults[field];
              return (
                <div className="params-popover-field" key={field}>
                  <span className="params-popover-field-heading"><label htmlFor={`param-${field}`}>{label}</label><output>{current}</output></span>
                  <input
                    id={`param-${field}`}
                    type="range"
                    min={min}
                    max={max}
                    step={step}
                    value={current ?? min}
                    onChange={(event) => setField(field, event.target.value)}
                  />
                </div>
              );
            })}
          </div>
        </section>
        {CONTEXT_FIELD && (
          <div className="params-popover-field params-popover-field-wide">
            <span className="params-popover-field-heading"><label htmlFor={`param-${CONTEXT_FIELD.field}`}>{CONTEXT_FIELD.label}</label><output>{value?.[CONTEXT_FIELD.field] ?? defaults[CONTEXT_FIELD.field]}</output></span>
            <input
              id={`param-${CONTEXT_FIELD.field}`}
              type="range"
              min={CONTEXT_FIELD.min}
              max={CONTEXT_FIELD.max}
              step={CONTEXT_FIELD.step}
              value={value?.[CONTEXT_FIELD.field] ?? defaults[CONTEXT_FIELD.field] ?? CONTEXT_FIELD.min}
              onChange={(event) => setField(CONTEXT_FIELD.field, event.target.value)}
            />
          </div>
        )}
        <p className="params-popover-hint">These overrides apply to this chat. Defaults live in Settings.</p>
      </PopoverContent>
    </Popover.Root>
  );
}

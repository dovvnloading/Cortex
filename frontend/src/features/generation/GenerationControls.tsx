import { RotateCcw, Shuffle } from "lucide-react";
import { useId } from "react";
import {
  CONTEXT_STOPS,
  PARAM_SPECS,
  PRESETS,
  formatContextShort,
  matchingPreset,
  type GenerationValues,
  type SamplingKey,
} from "../../lib/generationParams";
import { NumberInput } from "../../shared/ui/NumberInput";
import { RangeField } from "../../shared/ui/RangeField";
import { SegmentedControl } from "../../shared/ui/SegmentedControl";

export type GenerationPatch = Partial<GenerationValues>;

type FieldProps = {
  /** Namespaces element ids; the popover and Settings can both be mounted. */
  idPrefix: string;
  values: GenerationValues;
  /** What each field resets to, and where the default tick is drawn. */
  defaults: GenerationValues;
  onChange: (patch: GenerationPatch) => void;
  disabled?: boolean;
};

export function PresetPicker({ idPrefix, values, onChange, disabled }: Omit<FieldProps, "defaults">) {
  const labelId = `${idPrefix}-style-label`;
  const active = matchingPreset(values);
  const activePreset = PRESETS.find((preset) => preset.id === active);

  return (
    <div className="generation-presets">
      <div className="generation-field-head">
        <span id={labelId} className="generation-field-label">Response style</span>
        <span className={`generation-presets-state${active ? "" : " generation-presets-state-custom"}`}>
          {activePreset ? activePreset.description : "Custom mix"}
        </span>
      </div>
      <SegmentedControl
        aria-labelledby={labelId}
        className="generation-presets-control"
        options={PRESETS.map((preset) => ({ value: preset.id, label: preset.label, title: preset.description }))}
        value={active}
        disabled={disabled}
        onChange={(id) => {
          const preset = PRESETS.find((candidate) => candidate.id === id);
          if (preset) onChange({ ...preset.values });
        }}
      />
    </div>
  );
}

export function SamplingField({ field, idPrefix, values, defaults, onChange, disabled }: FieldProps & { field: SamplingKey }) {
  const spec = PARAM_SPECS[field];
  return (
    <RangeField
      id={`${idPrefix}-${field.replace("_", "-")}`}
      label={spec.label}
      description={spec.description}
      min={spec.min}
      max={spec.max}
      step={spec.step}
      decimals={spec.decimals}
      value={values[field]}
      defaultValue={defaults[field]}
      disabled={disabled}
      onChange={(value) => onChange({ [field]: value })}
    />
  );
}

function ResetButton({ label, title, onClick, disabled }: { label: string; title: string; onClick: () => void; disabled?: boolean }) {
  return (
    <button className="range-field-reset" type="button" aria-label={label} title={title} disabled={disabled} onClick={onClick}>
      <RotateCcw aria-hidden="true" size={12} />
    </button>
  );
}

export function ContextWindowField({ idPrefix, values, defaults, onChange, disabled }: FieldProps) {
  const spec = PARAM_SPECS.num_ctx;
  const descriptionId = useId();
  const inputId = `${idPrefix}-num-ctx`;
  const value = values.num_ctx;
  const modified = value !== defaults.num_ctx;
  const onStop = (CONTEXT_STOPS as readonly number[]).includes(value);

  return (
    <div className={`generation-field${modified ? " range-field-modified" : ""}`}>
      <div className="range-field-head">
        <label htmlFor={inputId}>{spec.label}</label>
        <span className="range-field-actions">
          {modified && (
            <ResetButton
              label={`Reset ${spec.label} to ${defaults.num_ctx.toLocaleString()} tokens`}
              title={`Reset to ${formatContextShort(defaults.num_ctx)}`}
              disabled={disabled}
              onClick={() => onChange({ num_ctx: defaults.num_ctx })}
            />
          )}
          <NumberInput
            id={inputId}
            className="range-field-value range-field-value-wide"
            value={value}
            min={spec.min}
            max={spec.max}
            step={spec.step}
            format={(tokens) => tokens.toLocaleString()}
            disabled={disabled}
            aria-describedby={descriptionId}
            onCommit={(next) => { if (next !== null) onChange({ num_ctx: next }); }}
          />
          <span className="range-field-unit" aria-hidden="true">tokens</span>
        </span>
      </div>
      <small id={descriptionId} className="range-field-hint">{spec.description}</small>
      <SegmentedControl
        aria-label={`${spec.label} size`}
        className="generation-context-stops"
        options={CONTEXT_STOPS.map((tokens) => ({
          value: tokens,
          label: formatContextShort(tokens),
          ariaLabel: `${tokens.toLocaleString()} tokens`,
          title: tokens === defaults.num_ctx ? `${tokens.toLocaleString()} tokens (default)` : `${tokens.toLocaleString()} tokens`,
        }))}
        value={onStop ? value : null}
        disabled={disabled}
        onChange={(tokens) => onChange({ num_ctx: tokens })}
      />
    </div>
  );
}

function randomSeed(): number {
  const buffer = new Uint32Array(1);
  window.crypto.getRandomValues(buffer);
  // Keep it short enough to read back and retype.
  return (buffer[0] ?? 0) % 1_000_000;
}

export function SeedField({ idPrefix, values, defaults, onChange, disabled }: FieldProps) {
  const spec = PARAM_SPECS.seed;
  const descriptionId = useId();
  const inputId = `${idPrefix}-seed`;
  const value = values.seed;
  const modified = value !== defaults.seed;
  const resetText = defaults.seed === -1 ? "random" : String(defaults.seed);

  return (
    <div className={`generation-field${modified ? " range-field-modified" : ""}`}>
      <div className="range-field-head">
        <label htmlFor={inputId}>{spec.label}</label>
        <span className="range-field-actions">
          {modified && (
            <ResetButton
              label={`Reset ${spec.label} to ${resetText}`}
              title={`Reset to ${resetText}`}
              disabled={disabled}
              onClick={() => onChange({ seed: defaults.seed })}
            />
          )}
          <NumberInput
            id={inputId}
            className="range-field-value range-field-value-wide"
            value={value === -1 ? null : value}
            min={spec.min}
            max={spec.max}
            allowEmpty
            placeholder="Random"
            disabled={disabled}
            aria-describedby={descriptionId}
            onCommit={(next) => onChange({ seed: next ?? -1 })}
          />
          <button
            className="range-field-reset"
            type="button"
            aria-label="Pick a fixed seed"
            title="Pick a fixed seed"
            disabled={disabled}
            onClick={() => onChange({ seed: randomSeed() })}
          >
            <Shuffle aria-hidden="true" size={13} />
          </button>
        </span>
      </div>
      <small id={descriptionId} className="range-field-hint">{spec.description}</small>
    </div>
  );
}

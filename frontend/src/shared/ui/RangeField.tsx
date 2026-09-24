import { RotateCcw } from "lucide-react";
import { useId, type CSSProperties } from "react";
import { NumberInput } from "./NumberInput";

type Props = {
  id: string;
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
  /** Digits kept for typed values and shown in the readout. */
  decimals?: number;
  /** Short explanation shown under the label. */
  description?: string;
  /**
   * The value this field falls back to. Marks its position on the track and,
   * once the value moves away from it, offers a one-click reset.
   */
  defaultValue?: number;
  disabled?: boolean;
};

function percent(value: number, min: number, max: number): number {
  const span = max - min;
  const raw = span > 0 ? ((value - min) / span) * 100 : 0;
  return Math.min(100, Math.max(0, raw));
}

/**
 * A labelled slider with a filled track and an editable readout.
 *
 * Browsers only paint the filled portion of a range input natively in Firefox
 * (`::-moz-range-progress`); WebKit has no equivalent. Publishing the position
 * as a `--range-fill` percentage lets the track be painted with a gradient, so
 * the control looks the same everywhere instead of falling back to the default
 * grey rail on Chromium and WebView2 -- which is what Cortex actually ships in.
 * The readout is a text field so an exact value never needs a steady hand.
 */
export function RangeField({ id, label, value, min, max, step, onChange, decimals = 0, description, defaultValue, disabled = false }: Props) {
  const descriptionId = useId();
  const modified = defaultValue !== undefined && Math.abs(value - defaultValue) > 1e-9;
  const format = (next: number) => next.toFixed(decimals);

  return (
    <div className={`range-field${modified ? " range-field-modified" : ""}`}>
      <div className="range-field-head">
        <label htmlFor={id}>{label}</label>
        <span className="range-field-actions">
          {modified && (
            <button
              className="range-field-reset"
              type="button"
              aria-label={`Reset ${label} to ${format(defaultValue)}`}
              title={`Reset to ${format(defaultValue)}`}
              disabled={disabled}
              onClick={() => onChange(defaultValue)}
            >
              <RotateCcw aria-hidden="true" size={12} />
            </button>
          )}
          <NumberInput
            className="range-field-value"
            value={value}
            min={min}
            max={max}
            decimals={decimals}
            step={step}
            format={format}
            disabled={disabled}
            aria-label={`${label} value`}
            onCommit={(next) => { if (next !== null) onChange(next); }}
          />
        </span>
      </div>
      {description && <small id={descriptionId} className="range-field-hint">{description}</small>}
      <div className="range-field-track">
        <input
          id={id}
          className="range-input"
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          disabled={disabled}
          aria-describedby={description ? descriptionId : undefined}
          aria-valuetext={format(value)}
          style={{ "--range-fill": `${percent(value, min, max)}%` } as CSSProperties}
          onChange={(event) => onChange(Number(event.target.value))}
        />
        {defaultValue !== undefined && (
          <span
            className="range-field-default"
            aria-hidden="true"
            style={{ "--range-default": percent(defaultValue, min, max) / 100 } as CSSProperties}
          />
        )}
      </div>
    </div>
  );
}

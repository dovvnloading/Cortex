import type { CSSProperties } from "react";

type Props = {
  id: string;
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
  /** Short explanation shown under the track. */
  hint?: string;
  /** Override the printed value, e.g. to add a unit or fix decimals. */
  format?: (value: number) => string;
};

/**
 * A labelled slider with a filled track.
 *
 * Browsers only paint the filled portion of a range input natively in Firefox
 * (`::-moz-range-progress`); WebKit has no equivalent. Publishing the position
 * as a `--range-fill` percentage lets the track be painted with a gradient, so
 * the control looks the same everywhere instead of falling back to the default
 * grey rail on Chromium and WebView2 -- which is what Cortex actually ships in.
 */
export function RangeField({ id, label, value, min, max, step, onChange, hint, format }: Props) {
  const span = max - min;
  const fill = span > 0 ? ((value - min) / span) * 100 : 0;

  return (
    <div className="range-field">
      <div className="range-field-head">
        <label htmlFor={id}>{label}</label>
        <output htmlFor={id} className="range-field-value">
          {format ? format(value) : value}
        </output>
      </div>
      <input
        id={id}
        className="range-input"
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        style={{ "--range-fill": `${Math.min(100, Math.max(0, fill))}%` } as CSSProperties}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      {hint && <small className="range-field-hint">{hint}</small>}
    </div>
  );
}

import { useState, type KeyboardEvent } from "react";

type Props = {
  id?: string;
  /** null renders an empty field showing the placeholder. */
  value: number | null;
  min: number;
  max: number;
  /** Digits kept after the decimal point; 0 for integers. */
  decimals?: number;
  /** Arrow-key increment. */
  step?: number;
  /** Receives null only when `allowEmpty` is set and the field is cleared. */
  onCommit: (value: number | null) => void;
  allowEmpty?: boolean;
  format?: (value: number) => string;
  placeholder?: string;
  className?: string;
  disabled?: boolean;
  "aria-label"?: string;
  "aria-describedby"?: string;
};

function parse(raw: string, decimals: number): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  // Integers tolerate grouping ("8,192"); decimals tolerate a comma point ("0,7").
  const normalized = decimals === 0 ? trimmed.replace(/[\s,_]/g, "") : trimmed.replace(",", ".");
  const value = Number(normalized);
  return Number.isFinite(value) ? value : null;
}

/**
 * A compact numeric field that can be empty while someone is typing.
 *
 * A field bound straight to a number can never be empty: clearing it to
 * retype snapped the old value back and the new digits appended to it, and
 * `Number("")` quietly wrote a zero nobody chose. This keeps the raw text
 * while the field is being edited, commits only values that are complete and
 * in range, and on blur clamps whatever is left -- or restores the last good
 * value when the text is unusable.
 */
export function NumberInput({
  id,
  value,
  min,
  max,
  decimals = 0,
  step = 1,
  onCommit,
  allowEmpty = false,
  format,
  placeholder,
  className,
  disabled,
  ...aria
}: Props) {
  const [raw, setRaw] = useState<string | null>(null);
  const shown = raw ?? (value === null ? "" : format ? format(value) : String(value));
  const clamp = (next: number) => {
    const factor = 10 ** decimals;
    return Math.min(max, Math.max(min, Math.round(next * factor) / factor));
  };

  const edit = (text: string) => {
    setRaw(text);
    const parsed = parse(text, decimals);
    if (parsed === null) {
      if (!text.trim() && allowEmpty) onCommit(null);
      return;
    }
    if (parsed >= min && parsed <= max) onCommit(clamp(parsed));
  };

  const settle = () => {
    if (raw === null) return;
    const parsed = parse(raw, decimals);
    if (parsed !== null) onCommit(clamp(parsed));
    else if (!raw.trim() && allowEmpty) onCommit(null);
    setRaw(null);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      settle();
      return;
    }
    if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      event.preventDefault();
      const base = parse(raw ?? "", decimals) ?? value ?? min;
      setRaw(null);
      onCommit(clamp(base + (event.key === "ArrowUp" ? step : -step)));
    }
  };

  return (
    <input
      id={id}
      className={className}
      type="text"
      inputMode={decimals === 0 ? (min < 0 ? "text" : "numeric") : "decimal"}
      autoComplete="off"
      spellCheck={false}
      value={shown}
      placeholder={placeholder}
      disabled={disabled}
      onChange={(event) => edit(event.target.value)}
      onBlur={settle}
      onKeyDown={handleKeyDown}
      {...aria}
    />
  );
}

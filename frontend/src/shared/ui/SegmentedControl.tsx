import { useRef, type KeyboardEvent } from "react";

export type SegmentedOption<T extends string | number> = {
  value: T;
  label: string;
  /** Longer accessible name, e.g. "8,192 tokens" for a "8K" segment. */
  ariaLabel?: string;
  title?: string;
};

type Props<T extends string | number> = {
  options: readonly SegmentedOption<T>[];
  /** null when the current value matches none of the segments. */
  value: T | null;
  onChange: (value: T) => void;
  disabled?: boolean;
  className?: string;
  "aria-label"?: string;
  "aria-labelledby"?: string;
};

/**
 * A row of mutually exclusive choices with radio-group semantics: one tab
 * stop, arrow keys move between segments. Unlike a native radio group it may
 * have no segment checked -- a parameter can sit between the offered stops.
 */
export function SegmentedControl<T extends string | number>({
  options,
  value,
  onChange,
  disabled = false,
  className,
  ...aria
}: Props<T>) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const checkedIndex = options.findIndex((option) => option.value === value);
  const tabStop = checkedIndex >= 0 ? checkedIndex : 0;

  const move = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = options.length - 1;
    const next = event.key === "ArrowRight" || event.key === "ArrowDown"
      ? (index === last ? 0 : index + 1)
      : event.key === "ArrowLeft" || event.key === "ArrowUp"
        ? (index === 0 ? last : index - 1)
        : event.key === "Home"
          ? 0
          : event.key === "End"
            ? last
            : null;
    if (next === null) return;
    event.preventDefault();
    refs.current[next]?.focus();
    const option = options[next];
    if (option) onChange(option.value);
  };

  return (
    <div className={`segmented${className ? ` ${className}` : ""}`} role="radiogroup" aria-disabled={disabled || undefined} {...aria}>
      {options.map((option, index) => {
        const checked = index === checkedIndex;
        return (
          <button
            key={String(option.value)}
            ref={(node) => { refs.current[index] = node; }}
            className={`segmented-option${checked ? " segmented-option-checked" : ""}`}
            type="button"
            role="radio"
            aria-checked={checked}
            aria-label={option.ariaLabel}
            title={option.title}
            tabIndex={index === tabStop ? 0 : -1}
            disabled={disabled}
            onClick={() => onChange(option.value)}
            onKeyDown={(event) => move(event, index)}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

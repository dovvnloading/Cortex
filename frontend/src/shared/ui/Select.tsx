import { Select as BaseSelect } from "@base-ui/react/select";
import { Check, ChevronDown } from "lucide-react";

export type SelectOption = { value: string; label: string; detail?: string };

export type SelectProps = {
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  "aria-label"?: string;
  "aria-labelledby"?: string;
};

/**
 * Shared accessible single-select, built on Base UI's Select primitive.
 * Replaces the hand-rolled roving-tabindex combobox previously duplicated
 * between SettingsPanel's RoundedPicker and (in spirit) LocalModelMenu.
 * LocalModelMenu itself stays hand-rolled: it has async reject-and-reopen
 * selection and an adjacent rescan action this primitive doesn't model.
 */
export function Select({
  value,
  options,
  onChange,
  placeholder = "Choose an option",
  disabled = false,
  id,
  ...aria
}: SelectProps) {
  const selectedOption = options.find((option) => option.value === value) ?? null;

  return (
    <BaseSelect.Root
      value={value || null}
      onValueChange={(next) => {
        if (typeof next === "string") onChange(next);
      }}
      disabled={disabled || options.length === 0}
      items={options.map((option) => ({ value: option.value, label: option.label }))}
    >
      <BaseSelect.Trigger id={id} className="select-trigger" {...aria}>
        <span className="select-trigger-selection">
          <strong>{selectedOption?.label ?? placeholder}</strong>
          {selectedOption?.detail && <small>{selectedOption.detail}</small>}
        </span>
        <BaseSelect.Icon className="select-trigger-icon">
          <ChevronDown aria-hidden="true" size={17} />
        </BaseSelect.Icon>
      </BaseSelect.Trigger>
      <BaseSelect.Portal>
        <BaseSelect.Positioner sideOffset={6} className="select-positioner">
          <BaseSelect.Popup className="select-popup">
            <BaseSelect.List>
              {options.map((option) => (
                <BaseSelect.Item key={option.value} value={option.value} className="select-item" aria-label={option.label}>
                  <BaseSelect.ItemText className="select-item-text">
                    <strong>{option.label}</strong>
                    {option.detail && <small>{option.detail}</small>}
                  </BaseSelect.ItemText>
                  <BaseSelect.ItemIndicator className="select-item-indicator">
                    <Check aria-hidden="true" size={15} />
                  </BaseSelect.ItemIndicator>
                </BaseSelect.Item>
              ))}
            </BaseSelect.List>
          </BaseSelect.Popup>
        </BaseSelect.Positioner>
      </BaseSelect.Portal>
    </BaseSelect.Root>
  );
}

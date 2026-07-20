import { Check, ChevronDown, Cpu, RefreshCw } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";

export type LocalModelMenuProps = {
  /**
   * The current machine's discovered local-model inventory. The component
   * never adds fallback or suggested model names of its own.
   */
  models: readonly string[];
  /** The model currently configured for this conversation. */
  selectedModel: string | null;
  /** Return `false` to leave the menu open when the selection could not be saved. */
  onSelect: (model: string) => void | boolean | Promise<void | boolean>;
  /** Enables a compact inventory refresh action when supplied. */
  onRescan?: () => void | Promise<void>;
  /** Disables choosing a model and refreshing the inventory. */
  disabled?: boolean;
};

function normalizeModels(models: readonly string[]): string[] {
  const uniqueModels = new Set<string>();

  for (const model of models) {
    const name = model.trim();
    if (name) uniqueModels.add(name);
  }

  return [...uniqueModels];
}

export function LocalModelMenu({
  models,
  selectedModel,
  onSelect,
  onRescan,
  disabled = false,
}: LocalModelMenuProps) {
  const localModels = useMemo(() => normalizeModels(models), [models]);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [selectionPending, setSelectionPending] = useState(false);
  const [rescanPending, setRescanPending] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const listId = useId();
  const selectedIndex = localModels.indexOf(selectedModel?.trim() ?? "");
  const selected = selectedIndex >= 0 ? localModels[selectedIndex] : null;
  const initialIndex = selectedIndex >= 0 ? selectedIndex : 0;
  const interactionDisabled = disabled || selectionPending || rescanPending;
  const safeActiveIndex = localModels.length ? Math.min(activeIndex, localModels.length - 1) : 0;
  const canOpen = localModels.length > 1 && !interactionDisabled;
  const menuOpen = open && canOpen;

  useEffect(() => {
    if (!menuOpen) return undefined;

    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };

    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [menuOpen]);

  const closeMenu = (restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) {
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    }
  };

  const focusOption = (index: number) => {
    if (!canOpen) return;
    const nextIndex = Math.max(0, Math.min(index, localModels.length - 1));
    setActiveIndex(nextIndex);
    setOpen(true);
    window.requestAnimationFrame(() => optionRefs.current[nextIndex]?.focus());
  };

  const nextIndex = (index: number, direction: 1 | -1) => {
    return (index + direction + localModels.length) % localModels.length;
  };

  const selectModel = async (index: number) => {
    const model = localModels[index];
    if (!model || interactionDisabled) return;

    setSelectionPending(true);
    try {
      const saved = await onSelect(model);
      if (saved !== false) closeMenu(true);
    } catch {
      // The parent owns the error presentation. Keep the menu available so
      // the user can retry or choose a different discovered model.
    } finally {
      setSelectionPending(false);
    }
  };

  const rescan = async () => {
    if (!onRescan || interactionDisabled) return;

    setRescanPending(true);
    setOpen(false);
    try {
      await onRescan();
    } catch {
      // The caller owns error presentation for the inventory refresh.
    } finally {
      setRescanPending(false);
    }
  };

  const handleTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (!canOpen) return;

    if (event.key === "ArrowDown") {
      event.preventDefault();
      focusOption(menuOpen ? nextIndex(safeActiveIndex, 1) : initialIndex);
      return;
    }

    if (event.key === "ArrowUp") {
      event.preventDefault();
      focusOption(menuOpen ? nextIndex(safeActiveIndex, -1) : selectedIndex >= 0 ? nextIndex(selectedIndex, -1) : localModels.length - 1);
      return;
    }

    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (menuOpen) closeMenu();
      else focusOption(initialIndex);
    }
  };

  const handleOptionKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu(true);
      return;
    }

    if (event.key === "ArrowDown" || event.key === "ArrowRight") {
      event.preventDefault();
      focusOption(nextIndex(index, 1));
      return;
    }

    if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      event.preventDefault();
      focusOption(nextIndex(index, -1));
      return;
    }

    if (event.key === "Home") {
      event.preventDefault();
      focusOption(0);
      return;
    }

    if (event.key === "End") {
      event.preventDefault();
      focusOption(localModels.length - 1);
      return;
    }

    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      void selectModel(index);
    }
  };

  const renderRescan = () => onRescan ? (
    <button
      className="local-model-menu-rescan"
      type="button"
      aria-label="Rescan local models"
      title="Rescan local models"
      disabled={interactionDisabled}
      onClick={() => void rescan()}
    >
      <RefreshCw aria-hidden="true" size={15} className={rescanPending ? "local-model-menu-rescan-icon-pending" : undefined} />
    </button>
  ) : null;

  if (localModels.length === 1) {
    return (
      <div className="local-model-menu local-model-menu-single" aria-label={`Local model: ${localModels[0]}`}>
        <span className="local-model-menu-single-label" title={localModels[0]}>
          <Cpu aria-hidden="true" size={15} />
          <span>{localModels[0]}</span>
        </span>
        {renderRescan()}
      </div>
    );
  }

  const triggerLabel = selected ? `Selected local model: ${selected}` : localModels.length ? "Select a local model" : "No local models available";

  return (
    <div className={`local-model-menu${menuOpen ? " local-model-menu-open" : ""}`} ref={rootRef} aria-busy={selectionPending || rescanPending || undefined}>
      <button
        ref={triggerRef}
        className="local-model-menu-trigger"
        type="button"
        aria-label={triggerLabel}
        aria-haspopup="listbox"
        aria-expanded={menuOpen}
        aria-controls={listId}
        disabled={!canOpen}
        onClick={() => {
          if (menuOpen) closeMenu();
          else focusOption(initialIndex);
        }}
        onKeyDown={handleTriggerKeyDown}
      >
        <Cpu aria-hidden="true" size={15} />
        <span className="local-model-menu-trigger-label">{selected ?? (localModels.length ? "Select model" : "No local models")}</span>
        <ChevronDown aria-hidden="true" size={15} />
      </button>
      {renderRescan()}
      {menuOpen && (
        <div id={listId} className="local-model-menu-list" role="listbox" aria-label="Discovered local models">
          {localModels.map((model, index) => (
            <button
              key={model}
              ref={(node) => { optionRefs.current[index] = node; }}
              className={`local-model-menu-option${model === selected ? " local-model-menu-option-selected" : ""}`}
              type="button"
              role="option"
              aria-label={model}
              aria-selected={model === selected}
              tabIndex={index === safeActiveIndex ? 0 : -1}
              disabled={interactionDisabled}
              onFocus={() => setActiveIndex(index)}
              onKeyDown={(event) => handleOptionKeyDown(event, index)}
              onClick={() => void selectModel(index)}
            >
              <span>{model}</span>
              {model === selected && <Check aria-hidden="true" size={15} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

import { Check, ChevronDown, LoaderCircle, RefreshCw, Search } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { InstalledModel } from "../../../../contracts/cortex-api";
import { displayModelName, modelFacts, modelSource, type ModelSource } from "../../lib/localModels";
import { Popover, PopoverContent } from "../../shared/ui/Popover";

export type ModelRuntimeTone = "ready" | "idle" | "starting" | "failed";

/** Live runtime state of the selected model, for runtimes that load on demand. */
export type ModelRuntimeStatus = {
  tone: ModelRuntimeTone;
  label: string;
  /** Longer explanation, e.g. why the runtime last restarted. */
  detail?: string | null;
};

export type LocalModelMenuProps = {
  /**
   * The current machine's discovered local-model inventory. The component
   * never adds fallback or suggested model names of its own.
   */
  models: readonly string[];
  /** Optional metadata (size, quantization, vision) for the same inventory. */
  details?: readonly InstalledModel[];
  /** The model currently configured for this conversation. */
  selectedModel: string | null;
  /** Return `false` to leave the menu open when the selection could not be saved. */
  onSelect: (model: string) => void | boolean | Promise<void | boolean>;
  /** Enables the inventory refresh action when supplied. */
  onRescan?: () => void | Promise<void>;
  /** Disables choosing a model and refreshing the inventory. */
  disabled?: boolean;
  runtimeStatus?: ModelRuntimeStatus | null;
};

/** Below this many models a search box is more chrome than help. */
const SEARCH_THRESHOLD = 7;

const SOURCE_LABELS: Record<ModelSource, string> = {
  ollama: "Ollama",
  gguf: "GGUF files",
};

function normalizeModels(models: readonly string[]): string[] {
  const uniqueModels = new Set<string>();

  for (const model of models) {
    const name = model.trim();
    if (name) uniqueModels.add(name);
  }

  return [...uniqueModels];
}

function matchesQuery(model: string, detail: InstalledModel | undefined, terms: readonly string[]): boolean {
  if (!terms.length) return true;
  const haystack = [displayModelName(model), detail?.family, detail?.parameter_size, detail?.quantization_level]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return terms.every((term) => haystack.includes(term));
}

export function LocalModelMenu({
  models,
  details,
  selectedModel,
  onSelect,
  onRescan,
  disabled = false,
  runtimeStatus = null,
}: LocalModelMenuProps) {
  const localModels = useMemo(() => normalizeModels(models), [models]);
  const detailByName = useMemo(() => new Map((details ?? []).map((detail) => [detail.name, detail])), [details]);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeModel, setActiveModel] = useState<string | null>(null);
  const [pendingModel, setPendingModel] = useState<string | null>(null);
  const [rescanPending, setRescanPending] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  // Only keyboard and search moves scroll the highlight into view. A pointer
  // hovering a half-visible row must not scroll the list out from under it.
  const revealActiveRef = useRef(false);
  const listRef = useRef<HTMLDivElement>(null);
  const listId = useId();
  const selected = localModels.find((model) => model === selectedModel?.trim()) ?? null;
  const busy = pendingModel !== null || rescanPending;
  const showSearch = localModels.length >= SEARCH_THRESHOLD;

  const groups = useMemo(() => {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    const bySource: Record<ModelSource, string[]> = { ollama: [], gguf: [] };
    for (const model of localModels) {
      const detail = detailByName.get(model);
      if (matchesQuery(model, detail, terms)) bySource[modelSource(model, detail)].push(model);
    }
    return (Object.keys(bySource) as ModelSource[])
      .map((source) => ({ source, models: bySource[source] }))
      .filter((group) => group.models.length > 0);
  }, [detailByName, localModels, query]);
  // Group headings only earn their space when the machine really has both kinds.
  const mixedSources = useMemo(
    () => new Set(localModels.map((model) => modelSource(model, detailByName.get(model)))).size > 1,
    [detailByName, localModels],
  );
  const visible = useMemo(() => groups.flatMap((group) => group.models), [groups]);
  const activeIndex = activeModel ? visible.indexOf(activeModel) : -1;
  const optionId = (index: number) => `${listId}-option-${index}`;
  const activeId = activeIndex >= 0 ? optionId(activeIndex) : undefined;

  useEffect(() => {
    if (!open || !activeId || !revealActiveRef.current) return;
    document.getElementById(activeId)?.scrollIntoView?.({ block: "nearest" });
  }, [activeId, open]);

  const highlight = (model: string | null) => {
    revealActiveRef.current = true;
    setActiveModel(model);
  };

  const openMenu = (start: "selected" | "last" = "selected") => {
    setQuery("");
    const fallback = start === "last" ? localModels[localModels.length - 1] : localModels[0];
    highlight(start === "selected" ? selected ?? fallback ?? null : fallback ?? null);
    setOpen(true);
  };

  const handleOpenChange = (next: boolean) => {
    if (next) openMenu();
    else setOpen(false);
  };

  const choose = async (model: string) => {
    if (busy || disabled) return;
    if (model === selected) {
      setOpen(false);
      return;
    }

    setPendingModel(model);
    try {
      const saved = await onSelect(model);
      if (saved !== false) setOpen(false);
    } catch {
      // The parent owns the error presentation. Keep the menu available so
      // the user can retry or choose a different discovered model.
    } finally {
      setPendingModel(null);
    }
  };

  const rescan = async () => {
    if (!onRescan || busy || disabled) return;

    setRescanPending(true);
    try {
      await onRescan();
    } catch {
      // The caller owns error presentation for the inventory refresh.
    } finally {
      setRescanPending(false);
    }
  };

  const moveActive = (delta: number) => {
    if (!visible.length) return;
    const from = activeIndex >= 0 ? activeIndex : delta > 0 ? -1 : visible.length;
    const next = (from + delta + visible.length) % visible.length;
    highlight(visible[next] ?? null);
  };

  const handleListKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const inSearch = event.currentTarget === searchRef.current;
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveActive(1);
        return;
      case "ArrowUp":
        event.preventDefault();
        moveActive(-1);
        return;
      case "PageDown":
        event.preventDefault();
        highlight(visible[Math.min(visible.length - 1, Math.max(activeIndex, 0) + 5)] ?? null);
        return;
      case "PageUp":
        event.preventDefault();
        highlight(visible[Math.max(0, activeIndex - 5)] ?? null);
        return;
      case "Home":
      case "End":
        // In the search field these keys belong to the caret.
        if (inSearch) return;
        event.preventDefault();
        highlight(visible[event.key === "Home" ? 0 : visible.length - 1] ?? null);
        return;
      case "Enter":
      case " ":
        if (event.key === " " && inSearch) return;
        event.preventDefault();
        if (activeModel && visible.includes(activeModel)) void choose(activeModel);
        return;
      default:
    }
  };

  const updateQuery = (next: string) => {
    setQuery(next);
    const terms = next.toLowerCase().split(/\s+/).filter(Boolean);
    const firstMatch = localModels.find((model) => matchesQuery(model, detailByName.get(model), terms)) ?? null;
    // Keep the highlight on the selected model while it still matches, so
    // Enter after a partial search doesn't silently switch models.
    highlight(selected && matchesQuery(selected, detailByName.get(selected), terms) ? selected : firstMatch);
  };

  const triggerLabel = selected
    ? `Selected local model: ${displayModelName(selected)}`
    : localModels.length
      ? "Select a local model"
      : "No local models available";
  const statusTitle = runtimeStatus ? [runtimeStatus.label, runtimeStatus.detail].filter(Boolean).join(" — ") : undefined;

  return (
    <Popover.Root open={open} onOpenChange={handleOpenChange}>
      <Popover.Trigger
        className="model-picker-trigger"
        aria-label={triggerLabel}
        title={statusTitle ?? (selected ? displayModelName(selected) : undefined)}
        disabled={disabled}
        onKeyDown={(event) => {
          if (open || (event.key !== "ArrowDown" && event.key !== "ArrowUp")) return;
          event.preventDefault();
          openMenu(event.key === "ArrowUp" && !selected ? "last" : "selected");
        }}
      >
        {runtimeStatus && <span className={`model-picker-status model-picker-status-${runtimeStatus.tone}`} aria-hidden="true" />}
        <span className="model-picker-trigger-name">
          {selected ? displayModelName(selected) : localModels.length ? "Select model" : "No local models"}
        </span>
        <ChevronDown className="model-picker-trigger-icon" aria-hidden="true" size={14} />
      </Popover.Trigger>
      <PopoverContent
        className="model-picker"
        aria-label="Choose a local model"
        side="top"
        align="start"
        sideOffset={10}
        initialFocus={showSearch ? searchRef : listRef}
        aria-busy={busy || undefined}
      >
        {showSearch && (
          <div className="model-picker-search">
            <Search aria-hidden="true" size={14} />
            <input
              ref={searchRef}
              type="text"
              role="combobox"
              aria-label="Search local models"
              aria-expanded="true"
              aria-autocomplete="list"
              aria-controls={visible.length ? listId : undefined}
              aria-activedescendant={activeId}
              placeholder="Search models"
              autoComplete="off"
              spellCheck={false}
              value={query}
              onChange={(event) => updateQuery(event.target.value)}
              onKeyDown={handleListKeyDown}
            />
          </div>
        )}

        {visible.length > 0 ? (
          <div
            ref={listRef}
            id={listId}
            className="model-picker-list"
            role="listbox"
            aria-label="Discovered local models"
            aria-activedescendant={showSearch ? undefined : activeId}
            tabIndex={showSearch ? -1 : 0}
            onKeyDown={showSearch ? undefined : handleListKeyDown}
          >
            {groups.map((group) => (
              <div key={group.source} className="model-picker-group" role="group" aria-label={mixedSources ? SOURCE_LABELS[group.source] : undefined}>
                {mixedSources && <div className="model-picker-group-label" aria-hidden="true">{SOURCE_LABELS[group.source]}</div>}
                {group.models.map((model) => {
                  const index = visible.indexOf(model);
                  const detail = detailByName.get(model);
                  const isSelected = model === selected;
                  const facts = modelFacts(detail);
                  const runtimeNote = isSelected && runtimeStatus ? runtimeStatus.label : null;
                  const meta = [...facts, ...(runtimeNote ? [runtimeNote] : [])].join(" · ");
                  const metaId = `${optionId(index)}-meta`;
                  return (
                    <div
                      key={model}
                      id={optionId(index)}
                      className={`model-picker-option${isSelected ? " model-picker-option-selected" : ""}`}
                      role="option"
                      aria-label={displayModelName(model)}
                      aria-describedby={meta ? metaId : undefined}
                      aria-selected={isSelected}
                      aria-disabled={busy || undefined}
                      data-highlighted={model === activeModel || undefined}
                      onPointerMove={() => {
                        if (model === activeModel) return;
                        revealActiveRef.current = false;
                        setActiveModel(model);
                      }}
                      onClick={() => void choose(model)}
                    >
                      <span className="model-picker-option-text">
                        <span className="model-picker-option-name">{displayModelName(model)}</span>
                        {meta && <span id={metaId} className="model-picker-option-meta">{meta}</span>}
                      </span>
                      {detail?.supports_vision && <span className="model-picker-tag">Vision</span>}
                      <span className="model-picker-option-indicator" aria-hidden="true">
                        {pendingModel === model
                          ? <LoaderCircle size={15} className="composer-control-spinner" />
                          : isSelected ? <Check size={15} /> : null}
                      </span>
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        ) : (
          <div className="model-picker-empty" role="status" ref={listRef} tabIndex={-1}>
            {localModels.length ? (
              <>
                <strong>No matches</strong>
                <span>No local model matches “{query.trim()}”.</span>
              </>
            ) : (
              <>
                <strong>No local models found</strong>
                <span>Install one with Ollama or add a .gguf file to your models folder, then rescan.</span>
              </>
            )}
          </div>
        )}

        <div className="model-picker-footer">
          <span className="model-picker-count">
            {localModels.length} local {localModels.length === 1 ? "model" : "models"}
          </span>
          {onRescan && (
            <button
              className="model-picker-rescan"
              type="button"
              aria-label="Rescan local models"
              disabled={busy || disabled}
              onClick={() => void rescan()}
            >
              <RefreshCw aria-hidden="true" size={13} className={rescanPending ? "composer-control-spinner" : undefined} />
              <span>{rescanPending ? "Scanning…" : "Rescan"}</span>
            </button>
          )}
        </div>
      </PopoverContent>
    </Popover.Root>
  );
}

import { Check, ChevronDown, ChevronLeft, ChevronRight, LoaderCircle, RefreshCw, Search } from "lucide-react";
import { memo, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { InstalledModel } from "../../../../contracts/cortex-api";
import { cloudModelProvider, displayModelName, modelFacts, modelSource, type ModelSource } from "../../lib/localModels";
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
   * Models discovered locally or from configured cloud providers. The component
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
  onOpenSettings?: () => void;
  /** Full-width field in the AI Model settings section. */
  settingsField?: boolean;
  /** Disables choosing a model and refreshing the inventory. */
  disabled?: boolean;
  runtimeStatus?: ModelRuntimeStatus | null;
};

/** Below this many models a search box is more chrome than help. */
const SEARCH_THRESHOLD = 7;
const PAGE_SIZE = 50;

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

export const LocalModelMenu = memo(function LocalModelMenu({
  models,
  details,
  selectedModel,
  onSelect,
  onRescan,
  onOpenSettings,
  settingsField = false,
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
  const cloudSelection = cloudModelProvider(selectedModel);
  const hasCloud = Boolean(cloudSelection) || localModels.some((model) => cloudModelProvider(model));
  const qualifier = hasCloud ? "" : "local ";
  const busy = pendingModel !== null || rescanPending;
  const showSearch = hasCloud || localModels.length >= SEARCH_THRESHOLD || Boolean(query);

  const groups = useMemo(() => {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    const bySource = new Map<string, string[]>([["Ollama", []], ["GGUF files", []]]);
    for (const model of localModels) {
      const detail = detailByName.get(model);
      if (!matchesQuery(model, detail, terms)) continue;
      const provider = cloudModelProvider(model);
      const source = provider ? `Cloud · ${provider}` : SOURCE_LABELS[modelSource(model, detail)];
      const entries = bySource.get(source);
      if (entries) entries.push(model);
      else bySource.set(source, [model]);
    }
    return [...bySource].filter(([, entries]) => entries.length > 0).map(([source, entries]) => ({ source, models: entries }));
  }, [detailByName, localModels, query]);
  const mixedSources = hasCloud || new Set(localModels.map((model) => modelSource(model, detailByName.get(model)))).size > 1;
  const visible = useMemo(() => groups.flatMap((group) => group.models), [groups]);
  const optionIndexes = useMemo(() => new Map(visible.map((model, index) => [model, index])), [visible]);
  const active = activeModel && optionIndexes.has(activeModel) ? activeModel : visible[0];
  const activeIndex = active ? optionIndexes.get(active)! : -1;
  const pageStart = Math.floor(Math.max(0, activeIndex) / PAGE_SIZE) * PAGE_SIZE;
  const paged = visible.length > PAGE_SIZE;
  const pageGroups = useMemo(() => {
    if (!paged) return groups;
    let start = 0;
    return groups.flatMap((group) => {
      const offset = start;
      start += group.models.length;
      if (start <= pageStart || offset >= pageStart + PAGE_SIZE) return [];
      return [{ ...group, models: group.models.slice(Math.max(0, pageStart - offset), pageStart + PAGE_SIZE - offset) }];
    });
  }, [groups, pageStart, paged]);
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
        if (active) void choose(active);
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

  const displayed = selected ?? (cloudSelection || settingsField ? selectedModel : null);
  const triggerLabel = displayed
    ? `Selected ${cloudSelection ? "cloud" : "local"} model: ${displayModelName(displayed)}`
    : localModels.length
      ? `Select a ${qualifier}model`
      : `No ${qualifier}models available`;
  const displayedName = displayed ? displayModelName(displayed) : null;
  const statusTitle = runtimeStatus ? [runtimeStatus.label, runtimeStatus.detail].filter(Boolean).join(" — ") : undefined;

  return (
    <Popover.Root open={open} onOpenChange={handleOpenChange}>
      <Popover.Trigger
        className={settingsField ? "select-trigger model-picker-settings-trigger" : "model-picker-trigger"}
        aria-label={settingsField ? "Chat model" : triggerLabel}
        aria-describedby={settingsField ? `${listId}-selection` : undefined}
        title={statusTitle ?? displayedName ?? undefined}
        disabled={disabled}
        onKeyDown={(event) => {
          if (open || (event.key !== "ArrowDown" && event.key !== "ArrowUp")) return;
          event.preventDefault();
          openMenu(event.key === "ArrowUp" && !selected ? "last" : "selected");
        }}
      >
        {runtimeStatus && <span className={`model-picker-status model-picker-status-${runtimeStatus.tone}`} aria-hidden="true" />}
        <span className="model-picker-trigger-name" id={settingsField ? `${listId}-selection` : undefined}>
          {displayedName ?? (localModels.length ? "Select model" : "No local models")}
          {settingsField && selectedModel && !selected && " (unavailable)"}
        </span>
        <ChevronDown className="model-picker-trigger-icon" aria-hidden="true" size={14} />
      </Popover.Trigger>
      <PopoverContent
        className="model-picker"
        aria-label={`Choose a ${qualifier}model`}
        side={settingsField ? "bottom" : "top"}
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
              aria-label={`Search ${qualifier}models`}
              aria-expanded="true"
              aria-autocomplete="list"
              aria-controls={listId}
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

        {(visible.length > 0 || showSearch) && (
          <div
            ref={listRef}
            id={listId}
            className={visible.length ? "model-picker-list" : "sr-only"}
            role="listbox"
            aria-label={`Discovered ${qualifier}models`}
            aria-activedescendant={showSearch ? undefined : activeId}
            tabIndex={showSearch ? -1 : 0}
            onKeyDown={showSearch ? undefined : handleListKeyDown}
          >
            {pageGroups.map((group) => (
              <div key={group.source} className="model-picker-group" role="group" aria-label={mixedSources ? group.source : undefined}>
                {mixedSources && <div className="model-picker-group-label" aria-hidden="true">{group.source}</div>}
                {group.models.map((model) => {
                  const index = optionIndexes.get(model)!;
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
                      aria-disabled={busy || disabled || undefined}
                      aria-setsize={paged ? visible.length : undefined}
                      aria-posinset={paged ? index + 1 : undefined}
                      data-highlighted={model === active || undefined}
                      onPointerMove={() => {
                        if (model === active) return;
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
        )}
        {!visible.length && (
          <div className="model-picker-empty" role="status" ref={showSearch ? undefined : listRef} tabIndex={-1}>
            {localModels.length ? (
              <>
                <strong>No matches</strong>
                <span>No {qualifier}model matches “{query.trim()}”.</span>
              </>
            ) : (
              <>
                <strong>No {qualifier}models found</strong>
                <span>Install a local model or connect a cloud provider in AI Model settings.</span>
                {onOpenSettings && <button type="button" className="button button-secondary" onClick={onOpenSettings}>Open AI Model settings</button>}
              </>
            )}
          </div>
        )}

        {hasCloud && !settingsField && <p className="model-picker-cloud-note">Cloud providers receive the conversation, its attachments, and any included memories when you send.</p>}
        <div className="model-picker-footer">
          <span className="model-picker-count">
            {paged && `${pageStart + 1}–${Math.min(pageStart + PAGE_SIZE, visible.length)} of `}
            {visible.length} {qualifier}{visible.length === 1 ? "model" : "models"}
          </span>
          {paged && (
            <div className="model-picker-paging">
              <button type="button" className="model-picker-rescan" aria-label="Previous models"
                disabled={pageStart === 0 || busy} onClick={() => highlight(visible[pageStart - PAGE_SIZE])}><ChevronLeft aria-hidden="true" size={15} /></button>
              <button type="button" className="model-picker-rescan" aria-label="Next models"
                disabled={pageStart + PAGE_SIZE >= visible.length || busy} onClick={() => highlight(visible[pageStart + PAGE_SIZE])}><ChevronRight aria-hidden="true" size={15} /></button>
            </div>
          )}
          {onRescan && (
            <button
              className="model-picker-rescan"
              type="button"
              aria-label={`Rescan ${qualifier}models`}
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
});

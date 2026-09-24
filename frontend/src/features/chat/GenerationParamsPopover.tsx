import { ChevronDown, RotateCcw, SlidersHorizontal } from "lucide-react";
import { useId, useState } from "react";
import type { GenerationOptionsOverride, GenerationSettings } from "../../../../contracts/cortex-api";
import { normalizeOverride, resolveGenerationValues, type GenerationValues, type ParamKey } from "../../lib/generationParams";
import {
  ContextWindowField,
  PresetPicker,
  SamplingField,
  SeedField,
  type GenerationPatch,
} from "../generation/GenerationControls";
import { Popover, PopoverContent } from "../../shared/ui/Popover";

type Props = {
  value: GenerationOptionsOverride | null;
  defaults: GenerationSettings;
  disabled?: boolean;
  onChange: (next: GenerationOptionsOverride | null) => void;
};

const ADVANCED_FIELDS: readonly ParamKey[] = ["top_k", "repeat_penalty", "seed"];

export function GenerationParamsPopover({ value, defaults, disabled = false, onChange }: Props) {
  const baseline = resolveGenerationValues(defaults);
  const override = normalizeOverride(value, baseline);
  const overriddenCount = override ? Object.keys(override).length : 0;

  return (
    <Popover.Root>
      <Popover.Trigger
        className={`params-trigger${override ? " params-trigger-active icon-button-active" : ""}`}
        aria-label="Generation parameters for this chat"
        title={override
          ? `${overriddenCount} ${overriddenCount === 1 ? "parameter differs" : "parameters differ"} from your defaults`
          : "Generation parameters for this chat"}
        disabled={disabled}
      >
        <SlidersHorizontal size={15} aria-hidden="true" />
        <span>Parameters</span>
        {override && <span className="params-trigger-dot" aria-hidden="true" />}
      </Popover.Trigger>
      <PopoverContent className="params-popover" aria-label="Generation parameters" side="top" align="start" sideOffset={10}>
        {/* Mounted only while open, so each opening starts from the current overrides. */}
        <ParamsPanel override={override} baseline={baseline} onChange={onChange} />
      </PopoverContent>
    </Popover.Root>
  );
}

function ParamsPanel({
  override,
  baseline,
  onChange,
}: {
  override: GenerationOptionsOverride | null;
  baseline: GenerationValues;
  onChange: (next: GenerationOptionsOverride | null) => void;
}) {
  const idPrefix = `chat-params-${useId().replace(/[^\w-]/g, "")}`;
  const values = resolveGenerationValues(baseline, override);
  const advancedChanges = ADVANCED_FIELDS.filter((field) => override?.[field] != null).length;
  // Open on arrival when an advanced value is overridden, so no override is
  // ever hidden without a trace; after that the section is the user's to fold.
  const [advancedOpen, setAdvancedOpen] = useState(advancedChanges > 0);
  const advancedId = `${idPrefix}-advanced`;

  const apply = (patch: GenerationPatch) => onChange(normalizeOverride({ ...(override ?? {}), ...patch }, baseline));
  const fieldProps = { idPrefix, values, defaults: baseline, onChange: apply };

  return (
    <>
      <div className="params-popover-header">
        <div className="params-popover-title">
          <strong>Chat parameters</strong>
          <small>{override ? "This chat differs from your defaults." : "This chat uses your defaults."}</small>
        </div>
        {override && (
          <button className="params-reset" type="button" aria-label="Reset to defaults" title="Use your defaults for this chat" onClick={() => onChange(null)}>
            <RotateCcw size={12} aria-hidden="true" />
            <span>Reset</span>
          </button>
        )}
      </div>

      <div className="params-popover-body">
        <PresetPicker idPrefix={idPrefix} values={values} onChange={apply} />
        <SamplingField field="temperature" {...fieldProps} />
        <SamplingField field="top_p" {...fieldProps} />
        <ContextWindowField {...fieldProps} />

        <div className="params-advanced">
          <button
            className="params-advanced-toggle"
            type="button"
            aria-expanded={advancedOpen}
            aria-controls={advancedId}
            onClick={() => setAdvancedOpen((current) => !current)}
          >
            <span>Advanced</span>
            <small>{advancedChanges ? `${advancedChanges} changed` : "Top K, repeat penalty, seed"}</small>
            <ChevronDown aria-hidden="true" size={14} />
          </button>
          {advancedOpen && (
            <div id={advancedId} className="params-advanced-body">
              <SamplingField field="top_k" {...fieldProps} />
              <SamplingField field="repeat_penalty" {...fieldProps} />
              <SeedField {...fieldProps} />
            </div>
          )}
        </div>
      </div>

      <p className="params-popover-hint">Applies to this chat only. Defaults for every chat live in Settings → AI Model.</p>
    </>
  );
}

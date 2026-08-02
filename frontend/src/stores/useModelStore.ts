import { create } from "zustand";
import type { ModelResponse } from "../../../contracts/cortex-api";

export type ModelProgress = { model: string; status: string; percent: number | null };

interface ModelStoreState {
  models: ModelResponse | null;
  modelBusy: boolean;
  modelProgress: ModelProgress | null;
  setModels: (models: ModelResponse | null) => void;
  setModelBusy: (busy: boolean) => void;
  setModelProgress: (progress: ModelProgress | null) => void;
}

export const useModelStore = create<ModelStoreState>((set) => ({
  models: null,
  modelBusy: false,
  modelProgress: null,
  setModels: (models) => set({ models }),
  setModelBusy: (modelBusy) => set({ modelBusy }),
  setModelProgress: (modelProgress) => set({ modelProgress }),
}));

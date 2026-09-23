import { create } from "zustand";
import { persist } from "zustand/middleware";

interface UiState {
  sidebarCollapsed: boolean;
  toggleSidebar: () => void;
  /** 窄屏下侧栏是抽屉；`open=false` 时它不占位也不拦点击。 */
  mobileNavOpen: boolean;
  setMobileNavOpen: (open: boolean) => void;
}

/** 纯界面状态（不入库、不影响后端行为）。存 localStorage 只为了"刷新后别弹回去"。 */
export const useUi = create<UiState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      mobileNavOpen: false,
      setMobileNavOpen: (open) => set({ mobileNavOpen: open }),
    }),
    { name: "ih.ui.v1" },
  ),
);

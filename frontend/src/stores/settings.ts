import { create } from "zustand";
import { persist } from "zustand/middleware";

export const TRACKING_DEFAULT_KEY = "captureTrackingDefault";

/** 只有这一处写默认值 `true`（V1 §7.24：**"持续跟踪"的默认值只能有一个出处**）。
 * 页面上任何"新增博主时是否默认跟踪"的开关都必须读 store，不许自己 `?? true`。 */
const INITIAL = { captureTrackingDefault: true } as const;

interface SettingsState {
  captureTrackingDefault: boolean;
  setCaptureTrackingDefault: (value: boolean) => void;
  resetToInitial: () => void;
}

export const useSettings = create<SettingsState>()(
  persist(
    (set) => ({
      ...INITIAL,
      setCaptureTrackingDefault: (value) => {
        // 类型守卫而不是信任调用方：这个值最终会变成 `is_tracking`，
        // 而后端对非真布尔是**拒绝**（`test_set_tracking_rejects_non_bool`）。
        // 前端如果先塞一个 "false" 进去，症状是"提交才报错"，且看起来像后端坏了。
        if (typeof value !== "boolean") {
          throw new TypeError(`captureTrackingDefault 必须是真布尔，收到 ${typeof value}`);
        }
        set({ captureTrackingDefault: value });
      },
      resetToInitial: () => set({ ...INITIAL }),
    }),
    {
      name: "ih.settings.v1",
      /** localStorage 是**外部输入**（手改、别的版本写过、隐私模式里塞的脏值都可能）。
       * 认不出来就回到唯一默认值而不是猜：`{captureTrackingDefault: "false"}` 这种
       * 字符串在 JS 里是 truthy，于是"关掉的开关刷新后又变开" —— 正是 §7.24 那一类。 */
      merge: (persisted, current) => {
        const raw = (persisted ?? {}) as Partial<SettingsState>;
        const next = { ...current };
        if (typeof raw.captureTrackingDefault === "boolean") {
          next.captureTrackingDefault = raw.captureTrackingDefault;
        }
        return next;
      },
    },
  ),
);

/** 给非组件环境（表单初始值、测试）用的读入口。 */
export function captureTrackingDefault(): boolean {
  return useSettings.getState().captureTrackingDefault;
}

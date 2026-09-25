import type {
  PlatformControlStatus,
  PlatformControlUpdateResponse,
  PlatformSummary,
} from "@/api/hooks/useConfig";

/**
 * 把总闸那份线格式收成**齐全**的应用视图。
 *
 * 与 `lib/schedule.ts::viewOf` 同一条理由：FastAPI 对带 `default` 的字段不标 required，
 * 于是 `schema.d.ts` 里 `shadowed_by_env` 是 `?:`。在组件里逐处 `?? []`
 * 等于把"这个数组可能没有"这一个假设抄五遍，而且每次抄都可能漏一处。
 */
export interface PlatformControlView {
  enabled: boolean;
  shadowed: string[];
}

export function controlViewOf(status: PlatformControlStatus): PlatformControlView {
  return { enabled: status.enabled, shadowed: status.shadowed_by_env ?? [] };
}

/** 保存结果那一行要的同一份视图，再加"到底改没改"。
 *
 * `requires_restart` 刻意不往这里搬：总闸不需要重启，后端把那一栏恒写成 `false`，
 * 而界面渲染一个恒为假的旗标就是给未来留一盏假绿灯
 * （与 `restartNeeded()` 服务的那两个端点不同 —— 那两个真的会有 `true` 的一天）。 */
export function updateViewOf(
  response: PlatformControlUpdateResponse,
): PlatformControlView & { changed: string[] } {
  return {
    ...controlViewOf(response.platform_control),
    changed: response.changed_fields ?? [],
  };
}

/**
 * 一家平台在界面上的那一句状态（ADR-0025）。
 *
 * 收成一处而不是让 Dashboard 与 Settings 各写一份判据：这两页说的是同一件事
 * （"这一家现在能不能用、不能用的话是谁挡的"），两份实现迟早分叉，
 * 而分叉的症状是"总览页说已启用、设置页说被总闸关着"。
 * 后端那边同理，唯一的判据在 `platforms/base.py::resolve_platform_availability`。
 *
 * `availability` 是**后端算好的**，前端不再自己 AND 一次：
 * 界面上重算一遍就等于给"总闸开着但这一家关了"这类组合第二种答案。
 */
export interface PlatformStateView {
  /** 那一枚标签上的字。 */
  label: string;
  /** 设计令牌里的底色（`bg-*`），语义：绿=能用，黄=被挡着且要知道原因，灰=没开。 */
  tone: string;
  /** 标签下面补的那一句。`null` = 没得补 —— 不许用空串占位（会渲染出一个空 <p>）。 */
  hint: string | null;
}

export function platformStateOf(row: PlatformSummary): PlatformStateView {
  switch (row.availability) {
    case "available":
      // 能用了还补一句"没实现"，是因为 `availability` 只讲开关：
      // 一个"配置里有、开关开着、但这个构建没移植"的平台是**装配坏了**，
      // 而不是"可以开一下试试"。Dashboard 原来就在说这句话，这里保持。
      return row.implemented
        ? { label: "已启用", tone: "bg-mint-green", hint: null }
        : {
            label: "已启用，但本构建没实现",
            tone: "bg-lemon-yellow",
            hint: "开关是开着的，可这个版本没有它的适配器 —— 翻配置没用，要的是换构建。",
          };
    case "own_off":
      return {
        label: "已关闭",
        tone: "bg-grey-mist",
        hint: "这一家自己的开关关着（config/platforms.yaml 的 enabled）。",
      };
    case "master_off":
      return {
        // 这一句是整个改动的落点：它自己那一位**确实**是开着的，
        // 所以不能说"已关闭" —— 那会让人去翻一个本来就写着 enabled: true 的字段。
        label: "被总闸关着",
        tone: "bg-lemon-yellow",
        hint: row.enabled
          ? "它自己那一位是开着的，挡着的是上面的总闸。"
          : "总闸与它自己的开关都关着：开完总闸还要再开这一家。",
      };
    case "absent":
      return {
        label: "没有配置对象",
        tone: "bg-coral-red",
        hint: "配置层没有这一家 —— 是装配漏了一环，不是任何人碰过开关。",
      };
  }
}

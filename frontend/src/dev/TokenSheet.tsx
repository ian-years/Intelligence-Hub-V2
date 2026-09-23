import type { JSX } from "react";

import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { MemphisButton } from "@/components/memphis/MemphisButton";
import { PatternBackground } from "@/components/memphis/PatternBackground";
import { PlatformBadge } from "@/components/memphis/PlatformBadge";
import { PATTERNS } from "@/lib/patterns";
import { PLATFORM_NAMES } from "@/lib/tokens";

const PALETTE = [
  ["electric-blue", "电光蓝"],
  ["coral-red", "珊瑚红"],
  ["lemon-yellow", "柠檬黄"],
  ["mint-green", "薄荷绿"],
  ["hot-pink", "烈粉"],
  ["ink-black", "漆黑"],
  ["paper-cream", "米白"],
  ["grey-mist", "雾灰"],
] as const;

/**
 * 令牌对照页：`docs/specs/ui-tokens.md` 的每一节都在这有一块能看的区域。
 *
 * 存在的理由不是"好看"：整套设计令牌的验收是"眼睛看得出来的那三件事"
 * （直角、3px 黑边、无模糊硬阴影），而 CSS 变量写错一个名字只会静默变成
 * `unset` —— 有一页把 8 个色 + 4 个形状 + 5 个图案摆在一起，漂了一眼看得出。
 * Task 11 起挂在 `/tokens`（不在 7 个业务页里，是开发页）。
 */
export function TokenSheet(): JSX.Element {
  return (
    <div className="mx-auto flex max-w-[1200px] flex-col gap-6 p-6">
      <header>
        <h1 className="font-display text-display-lg">Intelligence Hub · 令牌对照</h1>
        <p className="text-body-md text-ink-black">
          §2 色板 · §3 形状 · §4 排版 · §5 间距 · §6 图案 · §7 动效 · §9 平台徽章
        </p>
      </header>

      <HardShadowCard hover>
        <h2>§2 色板</h2>
        <ul className="mt-3 flex list-none flex-wrap gap-3 p-0">
          {PALETTE.map(([name, zh]) => (
            <li key={name} className="flex flex-col gap-1">
              <span
                aria-label={name}
                className="h-12 w-24 border-memphis border-ink-black"
                style={{ background: `var(--color-${name})` }}
              />
              <code className="text-mono-sm">
                --color-{name} · {zh}
              </code>
            </li>
          ))}
        </ul>
      </HardShadowCard>

      <HardShadowCard>
        <h2>§3 形状：直角 + 3px 黑边 + 硬阴影</h2>
        <div className="mt-3 flex flex-wrap items-center gap-6">
          <span className="border-memphis border-ink-black bg-lemon-yellow px-5 py-3 shadow-hard">
            6px 硬阴影
          </span>
          <span className="border-memphis border-ink-black bg-mint-green px-5 py-3 shadow-hard-lg">
            10px
          </span>
          <span className="border-memphis border-ink-black bg-hot-pink px-5 py-3 shadow-hard-sm">
            3px
          </span>
        </div>
      </HardShadowCard>

      <HardShadowCard>
        <h2>§9 平台徽章</h2>
        <div className="mt-3 flex flex-wrap gap-3">
          {PLATFORM_NAMES.map((platform) => (
            <PlatformBadge key={platform} platform={platform} />
          ))}
          <PlatformBadge platform="pengyou" label="配置里冒出来的第五家" />
        </div>
      </HardShadowCard>

      <HardShadowCard>
        <h2>§6 图案（每页一种）</h2>
        <div className="mt-3 grid grid-cols-5 gap-3">
          {PATTERNS.map((pattern) => (
            <PatternBackground
              key={pattern}
              pattern={pattern}
              className="h-20 border-memphis border-ink-black"
            />
          ))}
        </div>
      </HardShadowCard>

      <HardShadowCard>
        <h2>§7 动效</h2>
        <div className="mt-3 flex flex-wrap items-center gap-6">
          <MemphisButton>按下压扁 → 弹回</MemphisButton>
          <MemphisButton variant="secondary">secondary</MemphisButton>
          <MemphisButton variant="danger">danger</MemphisButton>
          <MemphisButton disabled>disabled</MemphisButton>
          <span aria-label="加载中" className="memphis-loading">
            <i />
            <i />
            <i />
          </span>
        </div>
      </HardShadowCard>
    </div>
  );
}

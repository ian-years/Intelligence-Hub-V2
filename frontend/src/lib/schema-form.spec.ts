import { describe, expect, it } from "vitest";

import type { ObjectSchema } from "@/api/json-schema";
import { coerceValue, mergeDraft, planForm, type FormField } from "./schema-form";

/**
 * 两份 fixture 是 `DouyinConfig` / `BilibiliConfig` 的 `model_json_schema()` 形状抄来的
 * （键名、`ui:hidden` 的分布、`anyOf` 的 `Path | None`）。
 *
 * 为什么这条流水线值得单独测：ADR-0012 把 14 个字段标成 `ui:hidden` 之后，
 * **"不渲染"这件事只有这个前端执行者** —— Python 侧的用例只能证明标记在 schema 里，
 * 证明界面不显示它的是这里。
 */
const douyin: ObjectSchema = {
  title: "DouyinConfig",
  "ui:order": ["enabled"],
  properties: {
    enabled: { type: "boolean", title: "Enabled", default: true },
    display_name: { type: "string", title: "Display Name", default: "抖音" },
    cookies_file: {
      default: null,
      title: "Cookies File",
      description: "Netscape cookie 文件",
      anyOf: [{ type: "string" }, { type: "null" }],
    },
    use_cdp_bridge: { type: "boolean", title: "Use Cdp Bridge", default: true, "ui:hidden": true },
    media_strategy: {
      type: "string",
      title: "Media Strategy",
      default: "yt_dlp_with_fallback",
      "ui:hidden": true,
    },
    list_strategy: {
      type: "string",
      title: "List Strategy",
      default: "browser_scroll",
      "ui:hidden": true,
    },
    videos_per_creator: { type: "integer", title: "Videos Per Creator", default: 30 },
    ytdlp_cookies_from_browser: {
      title: "Ytdlp Cookies From Browser",
      default: null,
      anyOf: [{ type: "string" }, { type: "null" }],
    },
    fallback_to_page_play_url: {
      type: "boolean",
      title: "Fallback To Page Play Url",
      default: true,
    },
    rate_limit: { $ref: "#/$defs/RateLimitConfig", title: "Rate Limit" },
    advanced: { $ref: "#/$defs/DouyinAdvanced", title: "Advanced", "ui:advanced": true },
  },
  $defs: {
    RateLimitConfig: {
      title: "RateLimitConfig",
      properties: {
        per_minute: { type: "integer", title: "Per Minute", default: 30 },
        per_creator_seconds: { type: "number", title: "Per Creator Seconds", default: 1 },
      },
    },
    DouyinAdvanced: {
      title: "DouyinAdvanced",
      properties: {
        retry_max: { type: "integer", title: "Retry Max", default: 3, "ui:hidden": true },
        request_timeout_seconds: {
          type: "integer",
          title: "Timeout",
          default: 30,
          "ui:hidden": true,
        },
        max_video_duration_seconds: { default: null, title: "Max Duration", "ui:hidden": true },
      },
    },
  },
};

const douyinConfig: Record<string, unknown> = {
  enabled: true,
  display_name: "抖音",
  cookies_file: "data/cookies/douyin.com.txt",
  use_cdp_bridge: true,
  media_strategy: "yt_dlp_with_fallback",
  list_strategy: "browser_scroll",
  videos_per_creator: 30,
  ytdlp_cookies_from_browser: null,
  fallback_to_page_play_url: true,
  rate_limit: { per_minute: 30, per_creator_seconds: 2 },
  advanced: { retry_max: 3, request_timeout_seconds: 30, max_video_duration_seconds: null },
};

/** B站 那一组是**部分**隐藏：三个能用、三个不能用。折叠组必须照常出现，只少那三个。 */
const bilibili: ObjectSchema = {
  title: "BilibiliConfig",
  properties: {
    enabled: { type: "boolean", title: "Enabled", default: true },
    prefer_subtitles: {
      type: "boolean",
      title: "Prefer Subtitles",
      default: true,
      "ui:hidden": true,
    },
    advanced: { $ref: "#/$defs/BilibiliAdvanced", title: "Advanced", "ui:advanced": true },
  },
  $defs: {
    BilibiliAdvanced: {
      title: "BilibiliAdvanced",
      properties: {
        require_login_for_high_quality: { type: "boolean", default: true, "ui:hidden": true },
        dash_split_handling: { type: "string", title: "Dash Split Handling", default: "auto" },
        retry_max: { type: "integer", default: 3, "ui:hidden": true },
        request_timeout_seconds: { type: "integer", title: "Request Timeout", default: 30 },
        search_fallback_node_playwright: {
          type: "boolean",
          title: "Node Playwright",
          default: false,
        },
      },
    },
  },
};

const bilibiliConfig = {
  enabled: true,
  prefer_subtitles: true,
  advanced: {
    require_login_for_high_quality: true,
    dash_split_handling: "auto",
    retry_max: 3,
    request_timeout_seconds: 30,
    search_fallback_node_playwright: false,
  },
};

function pathsOf(schema: ObjectSchema, config: Record<string, unknown>): string[] {
  return planForm(schema, config).groups.flatMap((group) =>
    group.fields.map((field) => field.path),
  );
}

describe("planForm：ADR-0012 的 ui:hidden 在这里被执行", () => {
  it("空转前置：fixture 确实既有可渲染字段也有被隐藏字段", () => {
    const plan = planForm(douyin, douyinConfig);
    expect(plan.groups.flatMap((group) => group.fields).length).toBeGreaterThan(5);
    expect(plan.hidden.length).toBeGreaterThanOrEqual(6);
  });

  it("标了 ui:hidden 的字段一个都不出现", () => {
    const paths = pathsOf(douyin, douyinConfig);
    for (const hiddenKey of ["use_cdp_bridge", "media_strategy", "list_strategy"]) {
      expect(paths, `顶层字段 ${hiddenKey} 不该被渲染`).not.toContain(hiddenKey);
    }
    expect(paths.filter((path) => path.startsWith("advanced."))).toEqual([]);
  });

  it("整组叶子全隐藏 → 组不出现，但每个字段都记在 hidden 里（不是凭空消失）", () => {
    const plan = planForm(douyin, douyinConfig);
    expect(plan.groups.map((group) => group.key)).not.toContain("advanced");
    expect(plan.hidden).toEqual(
      expect.arrayContaining([
        "use_cdp_bridge",
        "media_strategy",
        "list_strategy",
        "advanced.retry_max",
        "advanced.request_timeout_seconds",
        "advanced.max_video_duration_seconds",
      ]),
    );
  });

  it("部分隐藏的折叠组照常出现：只剩能用那几个，且带 advanced 标记", () => {
    const plan = planForm(bilibili, bilibiliConfig);
    const advanced = plan.groups.find((group) => group.key === "advanced");
    expect(advanced?.advanced).toBe(true);
    expect(advanced?.fields.map((field) => field.path)).toEqual([
      "advanced.dash_split_handling",
      "advanced.request_timeout_seconds",
      "advanced.search_fallback_node_playwright",
    ]);
    expect(plan.hidden).toEqual([
      "prefer_subtitles",
      "advanced.require_login_for_high_quality",
      "advanced.retry_max",
    ]);
  });

  it("ui:order 把 enabled 顶到第一位，其余按 schema 原顺序", () => {
    const paths = pathsOf(douyin, douyinConfig);
    expect(paths[0]).toBe("enabled");
    expect(paths.indexOf("videos_per_creator")).toBeGreaterThan(paths.indexOf("display_name"));
  });

  it("值取自配置，default 另外带着（未写过的键要能显示成「后端默认」）", () => {
    const plan = planForm(douyin, douyinConfig);
    const seconds = plan.groups
      .flatMap((group) => group.fields)
      .find((field) => field.path === "rate_limit.per_creator_seconds");
    expect(seconds?.value).toBe(2);
    expect(seconds?.default).toBe(1);
  });

  it("类型映射：boolean / number / text；Path|None 那种没有 type 的落到 text", () => {
    const kinds = Object.fromEntries(
      planForm(douyin, douyinConfig)
        .groups.flatMap((group) => group.fields)
        .map((field) => [field.path, field.kind]),
    );
    expect(kinds.enabled).toBe("boolean");
    expect(kinds.videos_per_creator).toBe("number");
    expect(kinds.cookies_file).toBe("text");
    expect(kinds.ytdlp_cookies_from_browser).toBe("text");
  });

  it("description 带出来（隐藏字段为什么是隐藏的，全靠这句话）", () => {
    const plan = planForm(douyin, douyinConfig);
    const cookies = plan.groups
      .flatMap((group) => group.fields)
      .find((field) => field.path === "cookies_file");
    expect(cookies?.description).toBe("Netscape cookie 文件");
  });
});

describe("mergeDraft：PUT 要交**整份**配置", () => {
  it("草稿只覆盖改过的那几项，嵌套键写回原对象", () => {
    const draft = mergeDraft(douyinConfig, {
      enabled: false,
      "rate_limit.per_minute": 12,
    });
    expect(draft.enabled).toBe(false);
    expect(draft.rate_limit).toEqual({ per_minute: 12, per_creator_seconds: 2 });
    expect(draft.videos_per_creator).toBe(30);
  });

  /** 隐藏字段不渲染，但**必须原样送回**：少送一个键就是让后端用默认值覆盖用户机器上的值。 */
  it("没被渲染的字段也原样 round-trip", () => {
    const draft = mergeDraft(douyinConfig, { enabled: false });
    expect(draft.media_strategy).toBe("yt_dlp_with_fallback");
    expect(draft.advanced).toEqual(douyinConfig.advanced);
    const untouched = mergeDraft(douyinConfig, {});
    expect(untouched).toEqual(douyinConfig);
  });

  it('清空文本框给 null，不给空串（Path | None 不吃 ""）', () => {
    const field = fieldOf("cookies_file");
    expect(coerceValue(field, "")).toBeNull();
    expect(coerceValue(field, "  data/x.txt  ")).toBe("data/x.txt");
  });

  it('数字框给数字，复选框给真布尔（后端对 0/1/"true" 是 422）', () => {
    expect(coerceValue(fieldOf("videos_per_creator"), "45")).toBe(45);
    expect(coerceValue(fieldOf("videos_per_creator"), "")).toBeNull();
    expect(coerceValue(fieldOf("enabled"), true)).toBe(true);
  });

  it("合并前先按类型换算，界面里的字符串不会漏进请求体", () => {
    const draft = mergeDraft(douyinConfig, {
      videos_per_creator: coerceValue(fieldOf("videos_per_creator"), "45"),
    });
    expect(draft.videos_per_creator).toBe(45);
    expect(typeof draft.videos_per_creator).toBe("number");
  });
});

function fieldOf(path: string): FormField {
  const found = planForm(douyin, douyinConfig)
    .groups.flatMap((group) => group.fields)
    .find((field) => field.path === path);
  if (!found) throw new Error(`fixture 里没有 ${path}`);
  return found;
}

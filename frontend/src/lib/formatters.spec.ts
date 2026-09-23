// @vitest-environment node
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { formatMiB, formatPercent, formatTime, parseNames, parsePairs } from "./formatters";

/**
 * `parsePairs` / `parseNames` 拆的是**后端拼出来的字符串**
 * （`tasks/preflight.py` 的 `summary`）。所以这里既钉拆法、也钉生产者：
 * 生产者改了分隔符，这条用例要红，而不是让界面安静地少显示一块。
 */
describe("parsePairs（预检的逐平台状态）", () => {
  it("按 k=v 拆，顺序按原样", () => {
    expect(parsePairs("bilibili=degraded, douyin=ok")).toEqual([
      ["bilibili", "degraded"],
      ["douyin", "ok"],
    ]);
  });

  it("没有等号的片段原样交回（不猜成 unknown）", () => {
    expect(parsePairs("（无启用的平台）")).toEqual([["（无启用的平台）", ""]]);
    expect(parsePairs("a=1, 坏了的片段")).toEqual([
      ["a", "1"],
      ["坏了的片段", ""],
    ]);
  });

  it("空值给空列表，而不是一条空记录", () => {
    expect(parsePairs("")).toEqual([]);
    expect(parsePairs(undefined)).toEqual([]);
  });

  it("生产者仍然是逗号加空格 join k=v 那个形状", () => {
    const file = resolve(process.cwd(), "../src/intelligence_hub_v2/tasks/preflight.py");
    expect(existsSync(file), `找不到生产者：${file}`).toBe(true);
    const source = readFileSync(file, "utf8");
    expect(source).toContain('", ".join(f"{k}={v}"');
    expect(source).toContain('", ".join(k for k, v in tools.items() if v)');
  });
});

describe("parseNames（PATH 上的工具清单）", () => {
  it("逗号分隔并去空白；后端那句表达空态的整串原样保留", () => {
    expect(parseNames("ffmpeg, ffprobe , yt-dlp")).toEqual(["ffmpeg", "ffprobe", "yt-dlp"]);
    expect(parseNames("（无）")).toEqual(["（无）"]);
    expect(parseNames("")).toEqual([]);
  });
});

describe("时间 / 字节 / 百分比", () => {
  it("解析不了的时间显示原文，不是 Invalid Date", () => {
    expect(formatTime("不是时间")).toBe("不是时间");
    expect(formatTime(null)).toBe("—");
    expect(formatTime("2026-09-23T10:20:30+00:00")).not.toBe("Invalid Date");
  });

  it("0 与 null 分得开：0 是真的没有，null 是没读过", () => {
    expect(formatMiB(0)).toBe("0.0 MiB");
    expect(formatMiB(null)).toBe("—");
    expect(formatMiB(5 * 1024 * 1024)).toBe("5.0 MiB");
    expect(formatMiB(-1)).toBe("-1");
  });

  it("百分比不夹逼：越界要看得见", () => {
    expect(formatPercent(0.5)).toBe("50%");
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(1.4)).toBe("140%");
  });
});

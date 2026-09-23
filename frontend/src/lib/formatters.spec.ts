// @vitest-environment node
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  formatCount,
  formatDuration,
  formatMiB,
  formatPercent,
  formatTime,
  parseNames,
  parsePairs,
} from "./formatters";

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

/**
 * `duration_seconds` 在契约里是 **float**（适配器给的秒数带小数），
 * 而 `view_count` 是 `int | null`。两个都要能把"真的没有"（null）和
 * "真的是 0"（0 秒的视频、0 播放）分开显示 —— 挤成同一个 `—` 就等于
 * 把"采集器没拿到这条"伪装成"这条本来就是空的"。
 */
describe("formatDuration（秒 → m:ss / h:mm:ss）", () => {
  it("形状：一分钟以内仍是 m:ss，一小时以上加时", () => {
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(59)).toBe("0:59");
    expect(formatDuration(60)).toBe("1:00");
    expect(formatDuration(3599)).toBe("59:59");
    expect(formatDuration(3600)).toBe("1:00:00");
    expect(formatDuration(7325)).toBe("2:02:05");
  });

  it("秒位永远是两位，且拆回去等于原值（不靠逐个样例）", () => {
    for (const seconds of [0, 1, 9, 61, 600, 3599, 3600, 86399, 360000]) {
      const shown = formatDuration(seconds);
      // 每一位都必须是数字、末两段必须是两位：`1:5` 这种在表格里能蒙过去
      expect(/^\d+(:\d{2}){1,2}$/.test(shown), `${shown} 形状不对`).toBe(true);
      const rebuilt = shown.split(":").reduce((acc, part) => acc * 60 + Number(part), 0);
      expect(rebuilt, `${shown} 不是 ${String(seconds)} 秒`).toBe(seconds);
    }
  });

  it("小数秒四舍五入到整秒（float 契约），0 与 null 分得开", () => {
    expect(formatDuration(90.4)).toBe("1:30");
    expect(formatDuration(90.6)).toBe("1:31");
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
  });

  it("负数与 NaN 原样交出去，不夹成 0:00", () => {
    expect(formatDuration(-5)).toBe("-5");
    expect(formatDuration(Number.NaN)).toBe("NaN");
  });
});

describe("formatCount（播放 / 点赞数）", () => {
  it("千分位分组，但拆掉分隔符就是原数字", () => {
    for (const n of [0, 7, 999, 1000, 12345, 1234567]) {
      expect(formatCount(n).replace(/,/g, "")).toBe(String(n));
    }
    expect(formatCount(12345)).toBe("12,345");
  });

  it("0 是「真的有 0 条」，null 才是「没拿到」", () => {
    expect(formatCount(0)).toBe("0");
    expect(formatCount(null)).toBe("—");
    expect(formatCount(undefined)).toBe("—");
  });

  it("负数与 NaN 原样交出去", () => {
    expect(formatCount(-3)).toBe("-3");
    expect(formatCount(Number.NaN)).toBe("NaN");
  });
});

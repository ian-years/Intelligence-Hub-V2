// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { VideoRow } from "@/components/shared/VideoRow";
import { makeVideo } from "@/test/fixtures";

/** 一行作品要说的全在这里：字段各占一格、缺值要看得见是缺值、按钮只在能用的时候出现。 */
function show(
  video: ReturnType<typeof makeVideo>,
  props: {
    creatorName?: string | undefined;
    onHide?: () => void;
    onUnhide?: () => void;
    hiding?: boolean;
  } = {},
): void {
  render(
    <MemoryRouter>
      <VideoRow video={video} {...props} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
});

describe("VideoRow", () => {
  it("标题是指向详情页的链接，链接目标是这条作品的 id", () => {
    show(makeVideo({ id: 42, title: "一条作品" }));
    const link = screen.getByRole("link", { name: "一条作品" });
    expect(link.getAttribute("href")).toBe("/video/42");
  });

  it("空标题不画成空白：把平台侧 id 打出来，人才查得到是哪条", () => {
    show(makeVideo({ title: "", platform_video_id: "BV1xx411c7mD" }));
    expect(screen.getByRole("link").textContent).toContain("BV1xx411c7mD");
  });

  it("没有作者名时退化成「博主 #<id>」，而不是空一格", () => {
    show(makeVideo({ creator_id: 7 }));
    expect(screen.getByText("博主 #7")).toBeTruthy();
  });

  it("creator_id 本身缺失时说「未知」，不能显示成 #null 或 #undefined", () => {
    show(makeVideo({ creator_id: null }));
    expect(screen.getByText(/博主/).textContent).not.toMatch(/null|undefined/);
  });

  it("缺的字段一律是「—」，不许漏出 undefined / NaN / Invalid Date", () => {
    show(
      makeVideo({ creator_id: null, duration_seconds: null, view_count: null, published_at: null }),
    );
    const row = screen.getByRole("article");
    expect(row.textContent).not.toMatch(/undefined|NaN|Invalid Date|null/i);
    // 三格都在，且都是"没数据"那一格：published / duration 是光秃秃的「—」，
    // views 是「— 次播放」（单位跟着值走，缺值时也要能看出这一项存在）
    expect(screen.getAllByText(/—/)).toHaveLength(3);
  });

  it("作者名有就显示作者名（不显示 id）", () => {
    show(makeVideo({ creator_id: 7 }), { creatorName: "某某的抖音" });
    expect(screen.getByText("某某的抖音")).toBeTruthy();
    expect(screen.queryByText(/博主 #/)).toBeNull();
  });

  it("作者名是空串也算没有：那一格不许空着（空串不是名字）", () => {
    show(makeVideo({ creator_id: 7 }), { creatorName: "" });
    expect(screen.getByText("博主 #7")).toBeTruthy();
  });

  it("数值按契约格式化：秒是 m:ss，播放数是千分位", () => {
    show(makeVideo({ duration_seconds: 245, view_count: 12345 }));
    expect(screen.getByText("4:05")).toBeTruthy();
    expect(screen.getByText("12,345 次播放")).toBeTruthy();
  });

  it("给了 onHide 才有隐藏按钮；没给就一个按钮都没有（总览页只是看）", () => {
    show(makeVideo({ title: "可隐藏" }), { onHide: () => undefined });
    expect(screen.getByRole("button", { name: /隐藏《可隐藏》/ })).toBeTruthy();
    cleanup();
    show(makeVideo({ title: "只是看" }));
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("隐藏请求在飞时按钮禁用并换文案：连点会发出两笔 PATCH", () => {
    show(makeVideo(), { onHide: () => undefined, hiding: true });
    const button = screen.getByRole("button") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.textContent).toBe("隐藏中…");
  });

  it("未隐藏的行不给「取消隐藏」：那一笔 PATCH 会把没打墓碑的东西再 unhide 一次", () => {
    show(makeVideo({ title: "还在的" }), { onHide: () => undefined, onUnhide: () => undefined });
    expect(screen.getByRole("button", { name: /^隐藏《还在的》$/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^取消隐藏/ })).toBeNull();
  });

  it("已隐藏的行：说出原因，并且只给「取消隐藏」而不给「隐藏」", () => {
    show(makeVideo({ is_hidden: true, hidden_reason: "重复投稿", title: "旧的" }), {
      onHide: () => undefined,
      onUnhide: () => undefined,
    });
    expect(screen.getByText(/已隐藏：重复投稿/)).toBeTruthy();
    // 锚点必须在最前：不加 `^` 的话 "取消隐藏《旧的》" 也算匹配，这条断言就成了假绿
    expect(screen.queryByRole("button", { name: /^隐藏《旧的》$/ })).toBeNull();
    expect(screen.getByRole("button", { name: /^取消隐藏《旧的》$/ })).toBeTruthy();
  });

  it("墓碑没写原因时说实话，不许留一个空括号", () => {
    show(makeVideo({ is_hidden: true, hidden_reason: null }), { onUnhide: () => undefined });
    expect(screen.getByText(/已隐藏：没写原因/)).toBeTruthy();
  });

  it("点隐藏只传这一条作品：一行绑错 id 就是删掉别的那条", () => {
    const onHide = vi.fn();
    show(makeVideo({ id: 9, title: "第九条" }), { onHide });
    screen.getByRole("button", { name: /隐藏《第九条》/ }).click();
    expect(onHide).toHaveBeenCalledTimes(1);
    expect(onHide.mock.calls[0]?.[0]?.id).toBe(9);
  });
});

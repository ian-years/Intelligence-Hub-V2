import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HardShadowCard } from "./HardShadowCard";
import { MemphisButton } from "./MemphisButton";
import { PatternBackground } from "./PatternBackground";
import { PlatformBadge } from "./PlatformBadge";

describe("PlatformBadge", () => {
  it("认得的平台：染色走 CSS 变量，名字是占位表里那一个", () => {
    render(<PlatformBadge platform="bilibili" />);
    const badge = screen.getByText("B站");
    expect(badge.closest(".memphis-badge")).toHaveAttribute("data-known", "true");
    expect(badge.closest(".memphis-badge")).toHaveStyle({
      background: "var(--color-platform-bilibili)",
    });
    expect(badge.closest(".memphis-badge")?.querySelector("svg")).not.toBeNull();
  });

  it("有数据时用传进来的展示名（真源是 /api/platforms 的 display_name）", () => {
    render(<PlatformBadge platform="douyin" label="抖音（后端给的）" />);
    expect(screen.getByText("抖音（后端给的）")).toBeInTheDocument();
  });

  it("认不出的平台不折叠成「未知」，原样打出来并给灰底", () => {
    render(<PlatformBadge platform="pengyou" />);
    const badge = screen.getByText("pengyou").closest(".memphis-badge");
    expect(badge).toHaveAttribute("data-known", "false");
    expect(badge).toHaveStyle({ background: "var(--color-grey-mist)" });
  });
});

describe("HardShadowCard", () => {
  it("默认不带 hover 类，传了才带", () => {
    const { rerender } = render(<HardShadowCard data-testid="card">x</HardShadowCard>);
    expect(screen.getByTestId("card")).not.toHaveClass("memphis-card--hover");
    rerender(
      <HardShadowCard hover data-testid="card">
        x
      </HardShadowCard>,
    );
    expect(screen.getByTestId("card")).toHaveClass("memphis-card--hover");
  });
});

describe("MemphisButton", () => {
  it("默认 type=button：表单里的按钮不该因为没写 type 就变成提交", () => {
    render(<MemphisButton>跑一轮</MemphisButton>);
    const button = screen.getByRole("button", { name: "跑一轮" });
    expect(button).toHaveAttribute("type", "button");
    expect(button).toHaveClass("memphis-btn--primary");
  });

  it("disabled 透传（禁用态必须真的禁用，而不是只改颜色）", () => {
    render(
      <MemphisButton variant="danger" disabled>
        删除
      </MemphisButton>,
    );
    expect(screen.getByRole("button", { name: "删除" })).toBeDisabled();
  });
});

describe("PatternBackground", () => {
  it("图案名映射到 patterns.css 里那条工具类", () => {
    render(
      <PatternBackground pattern="confetti" data-testid="p">
        x
      </PatternBackground>,
    );
    expect(screen.getByTestId("p")).toHaveClass("bg-pattern-confetti");
  });
});

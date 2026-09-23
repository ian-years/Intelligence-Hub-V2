import { writeTokensJson } from "./token-projection";

/** `npm run tokens`：重新生成 `frontend/tokens.json`。改了 tokens.css 就要跟着提交。 */
const body = writeTokensJson();
const count = Object.keys((JSON.parse(body) as { tokens: Record<string, string> }).tokens).length;
console.log(`→ tokens.json（${count} 条令牌，源：src/styles/tokens.css）`);

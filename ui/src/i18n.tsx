// A small zh/en toggle for navigation and main labels. The choice lives in
// localStorage, which may be unavailable; every access is guarded.

import { createContext, useContext, useState, type ReactNode } from "react";

export type Lang = "en" | "zh";

const zh: Record<string, string> = {
  Queue: "队列",
  Inbox: "待审批",
  Memory: "记忆",
  Repositories: "仓库",
  CI: "CI",
  Agents: "智能体",
  Audit: "审计",
  Chat: "对话",
  Internal: "内部",
  "Sign out": "退出",
  Approve: "批准",
  Reject: "拒绝",
  Repository: "仓库",
  Tier: "梯队",
  Total: "总分",
  Title: "标题",
  Author: "作者",
  "Review state": "评审状态",
  Review: "评审",
  "Review selected": "评审所选",
  "Starting…": "启动中…",
  "Your review drafts": "你的评审草稿",
  Labels: "标签",
  "Claimed by": "认领人",
  Summary: "摘要",
  Mine: "我的",
  All: "全部",
  Evaluation: "评估",
  Override: "调整",
  Reason: "理由",
  "Merge checklist": "合并清单",
  Timeline: "时间线",
  Reviews: "评审",
  Hygiene: "规范检查",
  Claim: "认领",
  Unclaim: "取消认领",
  "Request review": "请求评审",
  "Arm merge": "启动合并",
  "Cancel merge": "取消合并",
  "Approve all labels": "批准全部标签",
  "Open changes": "待合并的变更",
  Files: "文件",
  "Approved profile": "已批准的配置",
  "Pending candidate": "待批准的候选配置",
  "Memory layer": "记忆层配置",
  Save: "保存",
  Pause: "暂停",
  Resume: "恢复",
  Agreement: "一致率",
  "Add repository": "添加仓库",
  "Runtime health": "运行时状态",
  "Recent runs": "最近运行",
  "New thread": "新对话",
  Send: "发送",
  "The service is paused": "服务已暂停",
  "Dev mode": "开发模式",
  "Sign in": "登录",
  "Sign in with GitHub": "使用 GitHub 登录",
  "Sign in with a device code": "使用设备码登录",
  Loading: "加载中",
  Nothing: "暂无",
  Questions: "智能体提问",
  "Agent questions": "智能体提问",
  "An agent is waiting for an answer": "有智能体在等待答复",
  "Open questions": "查看提问",
  "Waiting for an answer": "等待答复",
  "Recently answered": "最近已答复",
  "Message to the agent (optional)": "给智能体的说明（可选）",
  "Send answers": "提交答复",
  "Deny": "拒绝",
  "Expires": "过期时间",
  "Alert me in the browser": "在浏览器中提醒我",
  "No agent is waiting for an answer.": "当前没有智能体在等待答复。",
  Service: "服务",
  Running: "运行中",
  "local changes": "有本地修改",
  "Up to date with": "已与以下分支同步：",
  "commits on": "个提交在",
  "are not running yet": "上尚未运行",
  Deploy: "部署",
  "Self-improvement PRs": "自我改进 PR",
  "No open self-improvement PRs.": "没有待处理的自我改进 PR。",
  "Approve and merge": "批准并合并",
  History: "历史",
};

const LangContext = createContext<{ lang: Lang; setLang: (l: Lang) => void }>({ lang: "en", setLang: () => {} });

function readLang(): Lang {
  try {
    return localStorage.getItem("am.lang") === "zh" ? "zh" : "en";
  } catch {
    return "en";
  }
}

export function LangProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(readLang);
  const setLang = (l: Lang) => {
    setLangState(l);
    try {
      localStorage.setItem("am.lang", l);
    } catch {
      // storage can be unavailable; the choice then lasts for this page only
    }
  };
  return <LangContext.Provider value={{ lang, setLang }}>{children}</LangContext.Provider>;
}

export function useLang() {
  return useContext(LangContext);
}

export function useT(): (text: string) => string {
  const { lang } = useLang();
  return (text: string) => (lang === "zh" ? (zh[text] ?? text) : text);
}

// The dashboard's walkthroughs: short tours on the real pages, each step pointing at
// one control (an element with that `data-tour` name, which `Section`, `Tabs`, and
// plain elements carry) and saying what it does. Kept free of JSX and package imports
// so a test reads it in Node (tests/test_ui_tours.py), which checks that every target
// is on a page, every path is a page, every example exists, and every button is named
// as the dashboard names it.
//
// Text markup is the guide's (guide.ts): [[Label]] is a button as the dashboard labels
// it, [text](/path) a link, **text** emphasis.
import type { Role, Text } from "./guide";

/** What a step shows when its page has nothing to point at yet (tour.tsx). */
export type ExampleId = "work" | "draft" | "failure";

export interface TourStep {
  /** The page it shows on. `{repo}` is a repository the person sees (one they maintain
   *  first), `{pr}` an open PR of one (`/pr/{pr}` is its page). No path: wherever the
   *  person is. */
  path?: string;
  /** The `data-tour` name it points at; several, space-separated, point at the last
   *  inside the others. No target: the card shows on its own. */
  target?: string;
  title: Text;
  body: Text;
  /** Shown instead when the target is not on the page. */
  example?: ExampleId;
  /** Said instead when the target is not on the page and there is no example. */
  missing?: Text;
}

export interface Tour {
  id: string;
  icon: string;
  /** Who it is for, by the guide's roles; the dashboard recommends it to them. */
  who: Exclude<Role, "everyone">[];
  title: Text;
  summary: Text;
  steps: TourStep[];
}

export const TOURS: Tour[] = [
  {
    id: "first-review",
    icon: "pr",
    who: ["reviewer"],
    title: { en: "Your first review", zh: "你的第一次评审" },
    summary: {
      en: "Have an agent draft a review, talk it over, and post it as yourself.",
      zh: "让智能体起草评审，与它讨论，再以你的身份发布。",
    },
    steps: [
      {
        path: "/",
        target: "home-review",
        title: { en: "PRs waiting for your review", zh: "等你评审的 PR" },
        body: {
          en: "Home lists the open PRs you review, the ones assigned to you first. Tick the ones you want, choose the agent beside the button, and press [[Review selected]].",
          zh: "首页列出你负责评审的开放 PR，分配给你的排在前面。勾选想评审的 PR，在按钮旁选好智能体，再按 [[评审所选]]。",
        },
        missing: {
          en: "No open PRs to review here yet. Once a repository is added, its PRs show here.",
          zh: "这里还没有可评审的开放 PR。添加仓库后，它的 PR 会显示在这里。",
        },
      },
      {
        path: "/",
        target: "home-work",
        example: "work",
        title: { en: "Watch the agent work", zh: "看智能体工作" },
        body: {
          en: "Each review shows here while it runs. Press one to see the agent's steps live: what it reads, the commands it runs, and the questions it asks you. You can stop it at any time.",
          zh: "每个评审运行时都显示在这里。点开一个，就能实时看到智能体的步骤：它读了什么、运行了哪些命令、问了你什么问题。你随时可以停止它。",
        },
      },
      {
        path: "/inbox",
        target: "draft",
        example: "draft",
        title: { en: "The draft comes back to you", zh: "草稿回到你手上" },
        body: {
          en: "Nothing is posted on its own. The finished review waits under [[Approvals]] as a draft: read its findings, and edit or drop any you disagree with.",
          zh: "没有任何内容会自动发布。完成的评审作为草稿在 [[待审批]] 里等你：阅读发现的问题，不同意的可以修改或删除。",
        },
      },
      {
        path: "/inbox",
        target: "draft draft-discuss",
        example: "draft",
        title: { en: "Talk it over with the agent", zh: "与智能体讨论" },
        body: {
          en: "[[Discuss]] opens a conversation with the agent that wrote the review: ask why it found something, push back, or have it revise the draft.",
          zh: "[[讨论]] 会打开与写这份评审的智能体的对话：问它为什么这样判断、提出异议，或让它修改草稿。",
        },
      },
      {
        path: "/inbox",
        target: "draft draft-post",
        example: "draft",
        title: { en: "Post it as yourself", zh: "以你的身份发布" },
        body: {
          en: "[[Post as me]] posts the review on GitHub under your name once you have signed in with GitHub. [[Reject]] drops the draft.",
          zh: "用 GitHub 登录后，[[以我的身份发布]] 会以你的名义把评审发布到 GitHub。[[拒绝]] 会丢弃这份草稿。",
        },
      },
      {
        target: "ask-agent",
        title: { en: "Ask the agent anything", zh: "随时询问智能体" },
        body: {
          en: "[[Ask agent]] is on every page and knows what the page shows: ask what a PR changes, why a check failed, or how to change a setting. That is the whole loop: pick PRs, let an agent draft, decide, post.",
          zh: "[[询问智能体]] 在每个页面都有，并且知道页面上显示的内容：可以问 PR 改了什么、检查为什么失败、某个设置怎么改。整个流程就是这样：选 PR、让智能体起草、做决定、发布。",
        },
      },
    ],
  },
  {
    id: "pr-page",
    icon: "inbox",
    who: ["reviewer", "owner"],
    title: { en: "Approvals and the PR page", zh: "审批与 PR 页面" },
    summary: {
      en: "Where decisions wait for you, and everything you can do on one PR.",
      zh: "等你做决定的地方，以及在一个 PR 上能做的所有事。",
    },
    steps: [
      {
        path: "/",
        target: "bell",
        title: { en: "What waits on you", zh: "等你处理的事" },
        body: {
          en: "The bell counts what waits on you: drafts to decide, and questions agents ask while they work. Press it to see them.",
          zh: "铃铛统计等你处理的事：待决定的草稿，以及智能体工作时提出的问题。点它即可查看。",
        },
      },
      {
        path: "/inbox",
        target: "inbox-tabs",
        title: { en: "Approvals", zh: "待审批" },
        body: {
          en: "[[Waiting on me]] holds the drafts you asked for or may decide. Each says who asked for it and what happens once it is approved; until then nothing reaches GitHub.",
          zh: "[[等我处理]] 里是你请求的、或由你决定的草稿。每份草稿都写明是谁请求的、批准后会做什么；批准之前不会有任何内容发到 GitHub。",
        },
      },
      {
        path: "/pr/{pr}",
        target: "pr-review",
        title: { en: "One PR, all in one place", zh: "一个 PR 的全部信息" },
        body: {
          en: "A PR's page shows its review state, its checks, and the agents' work on it. [[Review]] starts a review; once one is posted it reads [[Follow up]], which checks what changed since.",
          zh: "PR 页面显示它的评审状态、检查结果和智能体在它上面的工作。[[评审]] 开始一次评审；发布过评审后按钮变为 [[跟进评审]]，只检查之后的改动。",
        },
        missing: {
          en: "There is no open PR to show yet; this step points at a PR's page once there is one.",
          zh: "目前还没有可展示的开放 PR；有了之后，这一步会指向 PR 页面。",
        },
      },
      {
        path: "/pr/{pr}",
        target: "pr-actions",
        title: { en: "More you can ask for", zh: "还能请求的操作" },
        body: {
          en: "[[Actions]] has the rest: have an agent fix the review's findings, reproduce a problem on a test machine, resolve conflicts with the base branch, or update the branch. A change to code comes back as a draft that you push as yourself.",
          zh: "[[操作]] 里是其余功能：让智能体修复评审发现的问题、在测试机器上复现问题、解决与基础分支的冲突，或更新分支。改代码的结果会作为草稿回来，由你以自己的身份推送。",
        },
        missing: {
          en: "There is no open PR to show yet.",
          zh: "目前还没有可展示的开放 PR。",
        },
      },
      {
        path: "/pr/{pr}",
        target: "pr-tabs",
        title: { en: "The details, by tab", zh: "按标签页查看详情" },
        body: {
          en: "[[Review progress]] shows who reviewed what and what is left. [[CI]] lists the failed checks: an agent diagnoses a failure there, and [[Fix CI]] has one fix it.",
          zh: "[[评审进度]] 显示谁评审了什么、还剩什么。[[CI]] 列出失败的检查：可以让智能体诊断失败原因，[[修复 CI]] 则让它直接修复。",
        },
        missing: {
          en: "There is no open PR to show yet.",
          zh: "目前还没有可展示的开放 PR。",
        },
      },
    ],
  },
  {
    id: "maintain",
    icon: "repo",
    who: ["maintainer"],
    title: { en: "Maintaining a repository", zh: "维护一个仓库" },
    summary: {
      en: "Its people, what the agents learned, what the service may do, and merging.",
      zh: "仓库的人员、智能体学到的经验、服务可以做什么，以及合并。",
    },
    steps: [
      {
        path: "/repos/{repo}",
        target: "repo-tabs",
        title: { en: "A repository's page", zh: "仓库页面" },
        body: {
          en: "Each repository the service serves has a page of its own. Its profile says how the service works there, and only its maintainers change it.",
          zh: "服务的每个仓库都有自己的页面。仓库的配置决定服务在那里如何工作，只有它的维护者能修改。",
        },
        missing: {
          en: "No repository yet: add one under [Repositories](/repos).",
          zh: "还没有仓库：请在 [仓库](/repos) 中添加。",
        },
      },
      {
        path: "/repos/{repo}?tab=people",
        target: "repo-people",
        title: { en: "People", zh: "人员" },
        body: {
          en: "Who reviews here and in which role, kept in step with the repository's GitHub collaborators. [[Add people]] by their GitHub login, or tell the agent who should do what.",
          zh: "谁在这里评审、担任什么角色，与仓库在 GitHub 上的协作者保持同步。用 GitHub 账号 [[添加人员]]，或者告诉智能体谁该做什么。",
        },
      },
      {
        path: "/repos/{repo}?tab=lessons",
        target: "repo-lessons",
        title: { en: "Lessons", zh: "经验" },
        body: {
          en: "What reviewers taught the agents, approved one lesson at a time. Edit a list, or press [[Summarize]] to have an agent merge the repeats; a maintainer applies what it drafts.",
          zh: "评审者教给智能体的经验，一条一条批准。可以编辑列表，或按 [[整理]] 让智能体合并重复项；它起草的结果由维护者应用。",
        },
      },
      {
        path: "/repos/{repo}?tab=settings",
        target: "repo-settings",
        title: { en: "What the service may do", zh: "服务可以做什么" },
        body: {
          en: "Each action the service takes here is only recorded, proposed to a maintainer, or taken on its own. Every repository starts with all of them only recorded, so nothing reaches GitHub until you raise one.",
          zh: "服务在这里的每种操作都可以设为只记录、提交给维护者审批，或自动执行。每个仓库一开始都是只记录，在你调高之前不会有任何内容发到 GitHub。",
        },
      },
      {
        path: "/merge",
        target: "merge-schedule",
        title: { en: "Merge scheduling", zh: "合并调度" },
        body: {
          en: "Schedule ready PRs and the order they merge in. The service starts their CI, reruns what flakes, and merges what passes the repository's rules.",
          zh: "安排就绪的 PR 以及它们的合并顺序。服务会启动它们的 CI、重跑不稳定的任务，并合并通过仓库规则的 PR。",
        },
      },
      {
        path: "/merge",
        target: "merge-failed",
        example: "failure",
        title: { en: "The failure lane", zh: "失败队列" },
        body: {
          en: "A PR whose CI really failed, or that conflicts with its base, is set aside here with the cause, and the next PR takes its turn. [[Fix CI]] and [[Resolve conflicts]] have an agent fix it for you to push; [[Retry]] puts it back in line.",
          zh: "CI 确实失败、或与基础分支冲突的 PR 会带着原因放到这里，下一个 PR 接着合并。[[修复 CI]] 和 [[解决冲突]] 让智能体修好后由你推送；[[重试]] 让它重新排队。",
        },
      },
    ],
  },
  {
    id: "service",
    icon: "server",
    who: ["admin"],
    title: { en: "Running the service", zh: "运行服务" },
    summary: {
      en: "Agents and models, who may do what, updates, and the audit log.",
      zh: "智能体与模型、谁可以做什么、更新，以及审计日志。",
    },
    steps: [
      {
        path: "/settings?tab=agents",
        target: "settings-agent-choice",
        title: { en: "Which agent runs", zh: "使用哪个智能体" },
        body: {
          en: "The agent each task runs on when nobody picks one. A repository's own routes, and an agent someone picks for one request, come first.",
          zh: "没人指定时每类任务使用的智能体。仓库自己的分配，以及某次请求中有人指定的智能体，优先于这里。",
        },
      },
      {
        path: "/settings?tab=agents",
        target: "settings-agents",
        title: { en: "The agents", zh: "智能体" },
        body: {
          en: "The coding agent CLIs on this host: turn them on, sign them in, and set each one's CLI settings, such as its sandbox and model, for the service's runs only.",
          zh: "这台主机上的编程智能体 CLI：启用、登录，并设置每个 CLI 的选项（例如沙箱和模型），只作用于服务的运行。",
        },
      },
      {
        path: "/settings?tab=routing",
        target: "settings-routing",
        title: { en: "Task routing", zh: "任务分配" },
        body: {
          en: "Which agent and model each kind of task uses: reviews, fixes, CI diagnoses, scoring.",
          zh: "每类任务使用哪个智能体和模型：评审、修复、CI 诊断、评分。",
        },
      },
      {
        path: "/settings?tab=system",
        target: "settings-system",
        title: { en: "System", zh: "系统" },
        body: {
          en: "How the service behaves: its write caps, how a deploy is checked, and the rest. You can also ask the agent to change any of it.",
          zh: "服务的行为：写入上限、部署前如何检查等。也可以让智能体帮你修改。",
        },
      },
      {
        path: "/access",
        target: "access-roles",
        title: { en: "Who may do what", zh: "谁可以做什么" },
        body: {
          en: "Each role's permissions: who may start agents, use the service's model accounts and test machines, or see scores and usage.",
          zh: "每个角色的权限：谁可以启动智能体、使用服务的模型账号和测试机器，或查看评分和用量。",
        },
        missing: {
          en: "Only the service's owner and its admins see the roles.",
          zh: "只有服务的所有者和管理员能看到角色设置。",
        },
      },
      {
        path: "/service",
        target: "service-updates",
        title: { en: "Updates", zh: "更新" },
        body: {
          en: "The running version and the new commits to deploy. Improvements people ask for in chat arrive below as PRs, which you merge and deploy.",
          zh: "当前运行的版本和可部署的新提交。大家在对话中请求的改进会作为 PR 出现在下方，由你合并和部署。",
        },
      },
      {
        path: "/audit",
        target: "audit",
        title: { en: "The audit log", zh: "审计日志" },
        body: {
          en: "Every decision and change, and who made it.",
          zh: "每一个决定和变更，以及是谁做的。",
        },
      },
    ],
  },
];

export function tourById(id: string): Tour | undefined {
  return TOURS.find((t) => t.id === id);
}

/** The CSS selector of a step's target. */
export function targetSelector(target: string): string {
  return target
    .split(/\s+/)
    .filter(Boolean)
    .map((name) => `[data-tour="${name}"]`)
    .join(" ");
}

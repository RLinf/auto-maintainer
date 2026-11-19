// The guide's content: how to use the dashboard, by task, in both languages. Kept free
// of JSX and package imports so a test reads it in Node (tests/test_ui_guide.py), which
// checks that every button named here is named as the dashboard names it, that every
// link leads to a page, and that every page has a topic.
//
// Text markup, read by pages/Guide.tsx: [[Label]] is a button, tab, or menu item exactly
// as the dashboard labels it (in Chinese text, its Chinese label); {{key}} is a key;
// [text](/path) links to a page; **text** is emphasis; `text` is code.

export type GuideLang = "en" | "zh";

export interface Text {
  en: string;
  zh: string;
}

/** Who a topic is for. Everyone who can sign in has write access to a repository. */
export type Role = "everyone" | "reviewer" | "owner" | "maintainer" | "admin";

export type GroupId = "start" | "review" | "own" | "merge" | "maintain" | "personal" | "service";

export interface Topic {
  id: string;
  group: GroupId;
  icon: string;
  who: Role[];
  title: Text;
  /** What the topic gets you, in one sentence. */
  summary: Text;
  steps: Text[];
  tips?: Text[];
  /** Buttons to the pages the topic is about. */
  open?: { to: string; label: Text }[];
  /** A question to start the agent with. */
  ask?: Text;
}

export const ROLE_LABEL: Record<Role, Text> = {
  everyone: { en: "Everyone", zh: "所有人" },
  reviewer: { en: "Reviewers", zh: "评审者" },
  owner: { en: "PR owners", zh: "PR 负责人" },
  maintainer: { en: "Maintainers", zh: "维护者" },
  admin: { en: "Service owner", zh: "服务所有者" },
};

export const GROUPS: { id: GroupId; icon: string; title: Text; blurb: Text }[] = [
  {
    id: "start",
    icon: "sparkles",
    title: { en: "Getting started", zh: "开始使用" },
    blurb: { en: "What the service does, signing in, and finding your way.", zh: "服务做什么、如何登录、如何找到要用的功能。" },
  },
  {
    id: "review",
    icon: "pr",
    title: { en: "Reviewing PRs", zh: "评审 PR" },
    blurb: { en: "Have an agent draft a review, then make it yours and post it.", zh: "让智能体起草评审，再由你修改并发布。" },
  },
  {
    id: "own",
    icon: "check",
    title: { en: "Moving a PR forward", zh: "推进 PR" },
    blurb: { en: "For the people who answer for a PR: progress, fixes, CI, tests.", zh: "写给对 PR 负责的人：进度、修复、CI 与测试。" },
  },
  {
    id: "merge",
    icon: "merge",
    title: { en: "Merging", zh: "合并" },
    blurb: { en: "Approve and merge yourself, or let the schedule merge in order.", zh: "自己批准并合并，或交给合并调度按顺序合并。" },
  },
  {
    id: "maintain",
    icon: "shield",
    title: { en: "Maintaining a repository", zh: "维护仓库" },
    blurb: { en: "Decide what the service may do, how it reviews, and who holds which role.", zh: "决定服务可以做什么、如何评审、谁担任什么角色。" },
  },
  {
    id: "personal",
    icon: "key",
    title: { en: "Your own setup", zh: "你自己的设置" },
    blurb: { en: "Your agent accounts, test machines, preferences, and usage.", zh: "你自己的智能体账户、测试机、偏好与用量。" },
  },
  {
    id: "service",
    icon: "server",
    title: { en: "Running the service", zh: "运行服务" },
    blurb: { en: "For the service's owner: repositories, agents, updates.", zh: "写给服务的所有者：仓库、智能体与更新。" },
  },
];

export const TOPICS: Topic[] = [
  // ---- getting started ------------------------------------------------------------------
  {
    id: "welcome",
    group: "start",
    icon: "sparkles",
    who: ["everyone"],
    title: { en: "What this service does", zh: "这个服务做什么" },
    summary: {
      en: "Agents read, review, and test pull requests; people decide what reaches GitHub.",
      zh: "智能体阅读、评审和测试拉取请求；什么内容出现在 GitHub 上由人来决定。",
    },
    steps: [
      {
        en: "**Agents do the reading.** They review PRs, explain CI failures, try changes on test machines, and draft fixes.",
        zh: "**智能体负责阅读。** 它们评审 PR、解释 CI 失败、在测试机上验证改动，并起草修复。",
      },
      {
        en: "**People decide.** Anything that would show on GitHub waits for a person, unless a maintainer set that action to [[Automatic]] for the repository.",
        zh: "**人来决定。** 任何会出现在 GitHub 上的内容都要等人处理，除非维护者把该操作在这个仓库设为 [[自动]]。",
      },
      {
        en: "**Your work comes back to you.** A review you start becomes a draft for you: you edit it and post it, under your own name if you like.",
        zh: "**你发起的工作会回到你手里。** 你发起的评审会成为你的草稿：由你修改并发布，也可以用你自己的身份发布。",
      },
      {
        en: "**Everything is on this site.** No terminal and no files to edit. Stuck anywhere? Press [[Ask agent]].",
        zh: "**一切都在这个网站上完成。** 无需终端，也无需编辑文件。遇到问题？点 [[询问智能体]]。",
      },
    ],
    tips: [
      {
        en: "Priority tiers (P0 is the most urgent) and quality scores are internal: they are never posted on GitHub.",
        zh: "优先级梯队（P0 最紧急）和质量评分都是内部信息，永远不会发布到 GitHub。",
      },
    ],
    open: [{ to: "/", label: { en: "Go to Home", zh: "前往首页" } }],
    ask: { en: "What can you do for me on this dashboard?", zh: "在这个控制台上你能帮我做什么？" },
  },
  {
    id: "sign-in",
    group: "start",
    icon: "key",
    who: ["everyone"],
    title: { en: "Sign in, and act as yourself", zh: "登录，并以你自己的身份操作" },
    summary: {
      en: "One sign-in lets you use the dashboard and do what you approve under your own GitHub account.",
      zh: "登录一次，就能使用控制台，并以你自己的 GitHub 账户执行你批准的操作。",
    },
    steps: [
      {
        en: "Press [[Sign in with GitHub]] and use an account with write access to a repository this service maintains.",
        zh: "点 [[使用 GitHub 登录]]，使用对本服务维护的某个仓库有写权限的账户。",
      },
      {
        en: "GitHub asks for a one-time code: enter the code the dashboard shows, and approve.",
        zh: "GitHub 会要求输入一次性验证码：填入控制台显示的验证码并授权。",
      },
      {
        en: "From then on, the buttons that say **as me** post, push, approve, and merge under your account. The token stays on the service's host, under your login only, and is deleted when you sign out.",
        zh: "之后，带有 **以我的身份** 的按钮会用你的账户发布、推送、批准和合并。令牌只保存在服务主机上、只归你的登录所用，退出时即删除。",
      },
      {
        en: "Came in through a one-time sign-in link instead? The account menu (your avatar, top right) shows [[Sign in with GitHub to act as you]]. Do it once.",
        zh: "如果你是通过一次性登录链接进来的，账户菜单（右上角头像）会显示 [[用 GitHub 登录以你的身份操作]]，完成一次即可。",
      },
      {
        en: "In the same menu, [[Set your name]] so teammates see your name instead of your login.",
        zh: "在同一个菜单里点 [[设置你的名字]]，同事看到的就是你的名字而不是登录名。",
      },
    ],
    ask: { en: "What does signing in with GitHub let the service do as me?", zh: "用 GitHub 登录后，服务能以我的身份做什么？" },
  },
  {
    id: "tour",
    group: "start",
    icon: "compass",
    who: ["everyone"],
    title: { en: "Find your way around", zh: "熟悉界面" },
    summary: {
      en: "The top bar, search, the agent, and shortcuts: every page is a click or a key away.",
      zh: "顶栏、搜索、智能体和快捷键：每个页面都只需一次点击或一个按键。",
    },
    steps: [
      {
        en: "The top bar holds the main pages; those that do not fit are under [[More]].",
        zh: "顶栏放着主要页面，放不下的在 [[更多]] 里。",
      },
      {
        en: "The bell counts what waits on you: agent questions, approvals, and memory changes.",
        zh: "铃铛显示等你处理的事项数：智能体提问、待审批和记忆变更。",
      },
      {
        en: "Press {{⌘}} {{K}} (Ctrl+K on Windows and Linux) to jump to any page, PR, or repository, or to ask the agent what you typed.",
        zh: "按 {{⌘}} {{K}}（Windows 和 Linux 上是 Ctrl+K）可以跳到任意页面、PR 或仓库，或把输入的内容直接问智能体。",
      },
      {
        en: "[[Ask agent]], or {{⌘}} {{J}}, opens the agent beside any page. It sees what you see there, and anything it suggests changing waits for approval.",
        zh: "[[询问智能体]]（或 {{⌘}} {{J}}）会在当前页面旁打开智能体。它能看到你在这一页看到的内容，它建议的任何改动都要等人批准。",
      },
      {
        en: "Each page has a [[Guide]] button beside its title that opens this guide at that page.",
        zh: "每个页面标题旁都有 [[指南]] 按钮，会打开本指南中讲这一页的部分。",
      },
      {
        en: "Press {{?}} for every shortcut. {{g}} then a letter jumps to a page: {{g}} {{h}} is Home, {{g}} {{p}} is Pull requests.",
        zh: "按 {{?}} 查看全部快捷键。先按 {{g}} 再按一个字母可以跳转页面：{{g}} {{h}} 是首页，{{g}} {{p}} 是拉取请求。",
      },
      {
        en: "Language (中文 / English) and the light or dark theme are in the account menu.",
        zh: "语言（中文 / English）和浅色、深色主题在账户菜单里切换。",
      },
    ],
  },
  {
    id: "home",
    group: "start",
    icon: "home",
    who: ["everyone"],
    title: { en: "Home: your day at a glance", zh: "首页：一眼看清今天的事" },
    summary: {
      en: "What waits for you, what agents are doing for you, and the PRs to review.",
      zh: "等你处理的事、智能体正在为你做的事，以及待评审的 PR。",
    },
    steps: [
      {
        en: "The line under the greeting counts what waits for you, the PRs that need review, the P0 and P1 PRs, and the agents at work. Each one is a link.",
        zh: "问候语下面的一行统计了等你处理的事项、需要评审的 PR、P0 和 P1 的 PR，以及正在工作的智能体，每一项都可以点击。",
      },
      {
        en: "Working on several repositories? The tabs above the cards switch between them: priorities are never mixed across repositories.",
        zh: "同时参与多个仓库？卡片上方的标签页可以切换仓库：不同仓库的优先级从不混在一起比较。",
      },
      {
        en: "[[Agents' work]] shows what agents do for you: [[Needs you]], [[Working]], and [[Finished]]. Click one to watch it.",
        zh: "[[智能体的工作]] 显示智能体正在为你做的事：[[需要你]]、[[进行中]] 和 [[已结束]]。点开任意一项即可查看。",
      },
      {
        en: "[[Pull requests to review]] lists the PRs that need a review. Filter by [[Owned by me]], [[I review]], or [[Review needed]], and sort by [[Closest to ready]] to finish nearly done PRs first.",
        zh: "[[待评审的 PR]] 列出需要评审的 PR。可以按 [[我负责的]]、[[我评审的]] 或 [[需要的评审]] 筛选，按 [[最接近就绪]] 排序可以先收尾快完成的 PR。",
      },
      {
        en: "[[Needs your decision]] gathers questions, approvals, memory changes, and updates: decide them right there.",
        zh: "[[需要你决定]] 汇集了提问、待审批、记忆变更和更新，可以直接在这里处理。",
      },
    ],
    open: [{ to: "/", label: { en: "Go to Home", zh: "前往首页" } }],
    ask: { en: "What needs my attention today?", zh: "今天有什么需要我处理？" },
  },

  // ---- reviewing ------------------------------------------------------------------------
  {
    id: "find-prs",
    group: "review",
    icon: "pr",
    who: ["reviewer", "owner"],
    title: { en: "Find PRs to review", zh: "找到要评审的 PR" },
    summary: {
      en: "Every open PR, highest priority first, with filters for yours.",
      zh: "所有打开的 PR，按优先级从高到低排列，并能筛出与你有关的。",
    },
    steps: [
      {
        en: "Open [Pull requests](/prs): every open PR, highest priority first. P0 is the most urgent.",
        zh: "打开 [拉取请求](/prs)：所有打开的 PR 按优先级从高到低排列，P0 最紧急。",
      },
      {
        en: "Use the tabs [[Needs review]], [[Owned by me]], and [[I review]], or search by title, number, author, or label. {{/}} jumps to the search.",
        zh: "使用 [[待评审]]、[[我负责的]]、[[我评审的]] 标签页，或按标题、编号、作者、标签搜索。按 {{/}} 直接跳到搜索框。",
      },
      {
        en: "The bar on each row shows how close the PR is to ready: half for its reviews, half for review comments addressed.",
        zh: "每行的进度条表示 PR 离就绪还有多远：一半看评审，一半看评审意见是否已处理。",
      },
      {
        en: "Not sure where to start? Press [[Compare]] and the agent tells you which PRs to review first, and why.",
        zh: "不知道从哪开始？点 [[比较]]，智能体会告诉你先评审哪些 PR，以及原因。",
      },
      {
        en: "Taking one? Open it and press [[Claim]] in its people card, so others know you are on it.",
        zh: "决定接手某个 PR？打开它，在人员卡片里点 [[认领]]，让其他人知道你在处理。",
      },
    ],
    tips: [
      {
        en: "The list follows GitHub within a couple of minutes. [[Sync with GitHub]] reads it again now.",
        zh: "列表会在几分钟内跟上 GitHub 的变化。点 [[与 GitHub 同步]] 可以立即重新读取。",
      },
    ],
    open: [{ to: "/prs", label: { en: "Open Pull requests", zh: "打开拉取请求" } }],
    ask: { en: "Which PRs should I review first, and why?", zh: "我应该先评审哪些 PR？为什么？" },
  },
  {
    id: "pr-page",
    group: "review",
    icon: "list",
    who: ["reviewer", "owner", "maintainer"],
    title: { en: "Read a PR's page", zh: "看懂 PR 页面" },
    summary: {
      en: "Everything about one PR in one place, and every action you can take on it.",
      zh: "一个 PR 的全部信息集中在一页，你能对它做的操作也都在这里。",
    },
    steps: [
      {
        en: "At the top: [[Review]] (or [[Follow up]] after new commits), [[Approve]], [[Docs]] when the PR has a docs build, and [[Actions]] for everything else.",
        zh: "顶部有：[[评审]]（有新提交后变为 [[跟进评审]]）、[[批准]]、PR 有文档构建时的 [[文档]]，其余操作都在 [[操作]] 里。",
      },
      {
        en: "Tabs: [[Overview]] (summary, hygiene, checks), [[Review progress]], [[Files]], [[Timeline]] (everything the service did on this PR), [[Reviews]], and [[CI]].",
        zh: "标签页：[[概览]]（摘要、规范检查、CI）、[[评审进度]]、[[文件]]、[[时间线]]（服务在这个 PR 上做过的一切）、[[评审]] 和 [[CI]]。",
      },
      {
        en: "On the side: who owns and reviews the PR, its code owners, its quality score and priority (internal), and the [[Merge]] card.",
        zh: "侧栏：PR 的负责人和评审者、代码负责人、质量评分与优先级（内部信息），以及 [[合并]] 卡片。",
      },
      {
        en: "Think a priority is wrong? [[Adjust priority]] with a reason; the service learns from it.",
        zh: "觉得优先级不对？点 [[调整优先级]] 并写明理由，服务会从中学习。",
      },
    ],
    tips: [
      {
        en: "Press [[Ask agent]] on a PR to ask about it: 'Summarize this PR', 'What are the risks in this change?'",
        zh: "在 PR 页面点 [[询问智能体]] 可以就这个 PR 提问，例如“总结这个 PR”“这个改动有什么风险？”",
      },
    ],
    open: [{ to: "/prs", label: { en: "Pick a PR", zh: "选一个 PR" } }],
  },
  {
    id: "start-review",
    group: "review",
    icon: "sparkles",
    who: ["reviewer", "owner"],
    title: { en: "Have an agent review a PR", zh: "让智能体评审 PR" },
    summary: {
      en: "One click starts a review; it comes back to you as a draft, and nothing is posted yet.",
      zh: "点一下就能开始评审；结果作为草稿回到你手里，此时还没有发布任何内容。",
    },
    steps: [
      {
        en: "On Home, tick the PRs you want and press [[Review selected]]. Or open a PR and press [[Review]].",
        zh: "在首页勾选要评审的 PR，点 [[评审所选]]；或者打开某个 PR，点 [[评审]]。",
      },
      {
        en: "The arrow beside [[Review]] picks which agent does it; otherwise the default agent does.",
        zh: "[[评审]] 旁边的箭头可以选择由哪个智能体来做；不选则使用默认智能体。",
      },
      {
        en: "Press [[Watch]] to follow the agent as it reads the code. Tell it what to check while it works: your message reaches it, and the finished review answers it.",
        zh: "点 [[查看进展]] 可以看智能体如何阅读代码。工作期间可以告诉它要检查什么：消息会送到它那里，完成的评审会回应你的消息。",
      },
      {
        en: "When it is done, the review comes back to you as a **draft**. Find it on the PR's page, on Home, and under [Approvals](/inbox) → [[Waiting on me]].",
        zh: "完成后，评审会作为 **草稿** 回到你这里。在 PR 页面、首页，以及 [待审批](/inbox) → [[等我处理]] 都能找到。",
      },
    ],
    tips: [
      {
        en: "Reviews follow the repository's review skill and instructions, which its maintainers set.",
        zh: "评审遵循仓库的评审技能和评审说明，这些由仓库维护者设定。",
      },
      {
        en: "If the repository uses a test machine, the agent may run the PR there and put what it saw into the review.",
        zh: "如果仓库使用测试机，智能体可能会在测试机上运行这个 PR，并把看到的结果写进评审。",
      },
    ],
    open: [{ to: "/", label: { en: "Review from Home", zh: "从首页开始评审" } }],
  },
  {
    id: "post-review",
    group: "review",
    icon: "send",
    who: ["reviewer", "owner"],
    title: { en: "Edit and post your review", zh: "编辑并发布评审" },
    summary: {
      en: "Read the draft, change what you disagree with, and post it as yourself.",
      zh: "阅读草稿，改掉你不同意的部分，然后以你自己的身份发布。",
    },
    steps: [
      {
        en: "Open the draft. It shows exactly what will be posted: the summary and the comments on lines of the diff.",
        zh: "打开草稿。它显示的就是将要发布的内容：总结，以及针对 diff 中具体行的评论。",
      },
      {
        en: "Press [[Edit]] to change the text, or remove a comment you disagree with.",
        zh: "点 [[编辑]] 修改文字，或删除你不同意的评论。",
      },
      {
        en: "Press [[Post as me]] to post it on GitHub under your account. [[Post]] posts it as the service's account instead.",
        zh: "点 [[以我的身份发布]] 用你的账户发布到 GitHub；点 [[发布]] 则以服务的账户发布。",
      },
      {
        en: "Do not want it? [[Reject]] drops it, and nothing reaches GitHub.",
        zh: "不想要这份评审？点 [[拒绝]] 即可丢弃，不会有任何内容发到 GitHub。",
      },
    ],
    tips: [
      {
        en: "You, as the person who asked, and the repository's maintainers decide your draft. The PR's owners may edit it.",
        zh: "你（发起人）和仓库维护者可以决定这份草稿；PR 的负责人可以编辑它。",
      },
      {
        en: "A review written in another language? [[Translate to]] shows a translation here; the review is still posted as written.",
        zh: "评审是用另一种语言写的？点 [[翻译成]] 可以在这里看译文，发布的仍是原文。",
      },
    ],
    open: [{ to: "/inbox?tab=mine", label: { en: "Open your review drafts", zh: "打开你的评审草稿" } }],
  },
  {
    id: "discuss",
    group: "review",
    icon: "message",
    who: ["reviewer", "owner"],
    title: { en: "Talk with the agent about a review", zh: "就评审与智能体讨论" },
    summary: {
      en: "Ask why, push back, or have it change the draft, all in a conversation.",
      zh: "问原因、提出异议，或者让它修改草稿，都在对话中完成。",
    },
    steps: [
      {
        en: "On a draft or a posted review, press [[Discuss]].",
        zh: "在草稿或已发布的评审上点 [[讨论]]。",
      },
      {
        en: "Ask why it found something, disagree with a finding, or ask it to check more.",
        zh: "可以问它为什么得出某个结论、对某条意见提出异议，或让它再多检查一些内容。",
      },
      {
        en: "Ask it to change the draft, and the draft is revised in place. For a review already posted, the change becomes an edit of it that waits for approval.",
        zh: "让它修改草稿，草稿会直接更新。对于已发布的评审，修改会成为一份待批准的评审编辑。",
      },
      {
        en: "Someone else's review? Your messages go to **a copy of your own**: what changes there is yours to post, and the original stays as it is. The PR's owners can change the original directly.",
        zh: "这是别人的评审？你的消息会进入 **你自己的副本**：副本里的改动由你决定是否发布，原评审保持不变。PR 的负责人可以直接修改原评审。",
      },
    ],
    tips: [
      {
        en: "Who can talk with a review's agent: the person who asked for it, the PR's owners and reviewers, and the repository's maintainers.",
        zh: "可以与评审智能体对话的人：发起人、PR 的负责人和评审者，以及仓库维护者。",
      },
    ],
  },
  {
    id: "follow-up",
    group: "review",
    icon: "refresh",
    who: ["reviewer", "owner"],
    title: { en: "Follow up after new commits", zh: "新提交后跟进评审" },
    summary: {
      en: "When the author pushes again, a follow-up checks what changed and what was addressed.",
      zh: "作者再次推送后，跟进评审会检查改了什么、哪些意见已经处理。",
    },
    steps: [
      {
        en: "A PR with commits after its review is marked [[New commits since the last review]]; on Home, [[Review needed]] finds these follow-ups.",
        zh: "评审之后又有提交的 PR 会标记为 [[上次评审后有新提交]]；在首页用 [[需要的评审]] 可以找到这些需要跟进的 PR。",
      },
      {
        en: "Open it and press [[Follow up]]: the agent reviews what changed and checks whether the earlier comments were addressed.",
        zh: "打开它并点 [[跟进评审]]：智能体会评审新的改动，并检查之前的意见是否已处理。",
      },
      {
        en: "The follow-up comes back to you as a draft, like a first review.",
        zh: "跟进评审和首次评审一样，作为草稿回到你这里。",
      },
      {
        en: "To review the whole PR again instead, open the menu beside the button and choose [[Full review]]; in a comment, `/am review full` does the same.",
        zh: "如需重新评审整个 PR，打开按钮旁的菜单并选择 [[完整评审]]；在评论中写 `/am review full` 效果相同。",
      },
    ],
    tips: [
      {
        en: "Reviews can also run on their own: [[When reviews run]] on Home sets it, for maintainers.",
        zh: "评审也可以自动进行：维护者可以在首页的 [[何时评审]] 中设置。",
      },
    ],
  },
  {
    id: "questions",
    group: "review",
    icon: "question",
    who: ["everyone"],
    title: { en: "Answer an agent's question", zh: "回答智能体的提问" },
    summary: {
      en: "When an agent wants something only a person may allow, it asks, and you answer.",
      zh: "智能体需要只有人才能允许的操作时会来问你，由你回答。",
    },
    steps: [
      {
        en: "Agents run in their CLI's auto mode. What they would ask a person waits on [Agent questions](/questions), and a strip at the top of every page tells you.",
        zh: "智能体在其 CLI 的自动模式下运行。需要问人的事会在 [智能体提问](/questions) 等待，每个页面顶部也会出现提示条。",
      },
      {
        en: "Read what it wants to do ([[Show what the agent wants to run]]), then press [[Allow]] or [[Deny]]. Add a message if it should do something else.",
        zh: "先看它想做什么（[[显示智能体想执行的内容]]），再点 [[允许]] 或 [[拒绝]]。如果想让它换个做法，可以附上一条消息。",
      },
      {
        en: "A question nobody answers is denied when it expires, so no agent waits forever.",
        zh: "无人回答的提问会在到期时自动拒绝，所以智能体不会无限等待。",
      },
      {
        en: "Turn on [[Alert me in the browser]] to hear about questions while the tab is in the background.",
        zh: "打开 [[在浏览器中提醒我]]，即使标签页在后台也能收到提问通知。",
      },
    ],
    open: [{ to: "/questions", label: { en: "Open Agent questions", zh: "打开智能体提问" } }],
  },

  // ---- moving a PR forward --------------------------------------------------------------
  {
    id: "owners",
    group: "own",
    icon: "pr",
    who: ["owner", "reviewer", "maintainer"],
    title: { en: "Owners and reviewers", zh: "负责人与评审者" },
    summary: {
      en: "Owners answer for a PR's progress; reviewers review it. Say which you are.",
      zh: "负责人对 PR 的进度负责，评审者负责评审。告诉大家你是哪一种。",
    },
    steps: [
      {
        en: "A PR's page lists its **owners**, who answer for its progress, and its **reviewers**.",
        zh: "PR 页面会列出它的 **负责人**（对进度负责）和 **评审者**。",
      },
      {
        en: "Press [[Claim]] to take a role yourself, and [[Leave]] to step down. Others who hold the role stay.",
        zh: "点 [[认领]] 自己承担某个角色，点 [[退出]] 卸下它；担任同一角色的其他人不受影响。",
      },
      {
        en: "Maintainers assign owners with [[Assign]]; maintainers and the PR's owners assign reviewers, and can also request their review on GitHub.",
        zh: "维护者用 [[指派]] 指定负责人；维护者和 PR 负责人可以指定评审者，并可同时在 GitHub 上请求他们评审。",
      },
      {
        en: "On Home and on [Pull requests](/prs), [[Owned by me]] and [[I review]] show your PRs.",
        zh: "在首页和 [拉取请求](/prs) 中，[[我负责的]] 和 [[我评审的]] 会列出你的 PR。",
      },
    ],
  },
  {
    id: "progress",
    group: "own",
    icon: "activity",
    who: ["owner", "reviewer"],
    title: { en: "Track a PR's progress", zh: "跟踪 PR 的进度" },
    summary: {
      en: "One bar per PR, and a tab that says what is still missing.",
      zh: "每个 PR 一个进度条，还有一个标签页告诉你还缺什么。",
    },
    steps: [
      {
        en: "Each PR has one progress bar: half for its reviews (the approvals it needs, and each assigned reviewer's verdict), half for review comments addressed.",
        zh: "每个 PR 有一个进度条：一半看评审（需要的批准数，以及每位指派评审者的结论），一半看评审意见是否已处理。",
      },
      {
        en: "The PR's [[Review progress]] tab has the details: who reviewed, who still needs to, and every comment.",
        zh: "PR 的 [[评审进度]] 标签页有详细信息：谁评审过、谁还没评审，以及每一条意见。",
      },
      {
        en: "Press [[Check all]], and an agent checks every open comment against the code: addressed, partly addressed, or not yet.",
        zh: "点 [[全部检查]]，智能体会对照代码检查每条未解决的意见：已处理、部分处理或尚未处理。",
      },
      {
        en: "[[Resolve all]] resolves the comments fixed in the code on GitHub, as you. You resolve your own reviews' comments; the PR's owners and maintainers resolve anyone's.",
        zh: "[[全部解决]] 会以你的身份在 GitHub 上把代码里已修好的意见标为已解决。你可以解决自己评审中的意见；PR 负责人和维护者可以解决任何人的意见。",
      },
      {
        en: "The [[Code owners]] card says whose approval GitHub still waits for.",
        zh: "[[代码负责人]] 卡片会告诉你 GitHub 还在等谁的批准。",
      },
    ],
  },
  {
    id: "fix",
    group: "own",
    icon: "pencil",
    who: ["owner", "reviewer"],
    title: { en: "Have an agent change the code", zh: "让智能体修改代码" },
    summary: {
      en: "An agent writes the change; you read the diff and push it as yourself.",
      zh: "智能体写改动，你看过 diff 后以自己的身份推送。",
    },
    steps: [
      {
        en: "On the PR's page, open [[Actions]].",
        zh: "在 PR 页面打开 [[操作]]。",
      },
      {
        en: "[[Fix with agent]] changes the code for the review's findings. [[Change with agent]] changes it the way you describe, for example: rename the config key, and update its docs.",
        zh: "[[让智能体修复]] 会针对评审发现的问题修改代码；[[让智能体修改]] 会按你的描述修改，例如：重命名这个配置项，并更新它的文档。",
      },
      {
        en: "The change comes back as a diff for you to read. [[Discuss]] it to ask why, or to ask for more.",
        zh: "改动会以 diff 的形式回来给你看。可以点 [[讨论]] 问原因或要求更多修改。",
      },
      {
        en: "Then [[Push to the PR as me]]. If you cannot push to the PR's branch, [[Open the PR as me]] opens the change as a PR into this PR.",
        zh: "然后点 [[以我的身份推送到 PR]]。如果你无法推送到这个 PR 的分支，点 [[以我的身份开 PR]]，把改动作为一个合入此 PR 的新 PR 打开。",
      },
      {
        en: "Conflicts with the base branch? In [[Actions]], an agent can resolve them: it merges the base in, and you read the result and push it as yourself.",
        zh: "和基础分支有冲突？在 [[操作]] 里可以让智能体解决：它把基础分支合并进来，你看过结果后以自己的身份推送。",
      },
    ],
    tips: [
      {
        en: "The service never pushes code under its own account: every push is yours, after you have seen it.",
        zh: "服务从不以自己的账户推送代码：每次推送都由你在看过之后完成。",
      },
    ],
  },
  {
    id: "ci",
    group: "own",
    icon: "activity",
    who: ["owner", "reviewer", "maintainer"],
    title: { en: "When CI fails", zh: "CI 失败时" },
    summary: {
      en: "See why a job failed, rerun a flaky one, or have an agent fix it.",
      zh: "查看任务失败的原因、重跑偶发失败的任务，或让智能体修复。",
    },
    steps: [
      {
        en: "The PR's [[CI]] tab lists every check. For a failed job it shows the service's diagnosis, when there is one: the root cause and a suggested fix, with the log lines.",
        zh: "PR 的 [[CI]] 标签页列出全部检查。对失败的任务，如果服务已做过诊断，会显示根本原因和修复建议，并附上相关日志。",
      },
      {
        en: "Select failed jobs and press [[Diagnose]] for a fresh explanation, or [[Fix]] to have an agent fix the code.",
        zh: "选中失败的任务，点 [[诊断]] 重新分析，或点 [[修复]] 让智能体修改代码。",
      },
      {
        en: "A failure that is not the PR's fault? Mark it [[Flaky]]: the same failure is rerun from now on. [[Real failure]] says the opposite.",
        zh: "失败不是 PR 造成的？标记为 [[不稳定]]：以后同样的失败会自动重跑。[[真正失败]] 则表示相反的判断。",
      },
      {
        en: "CI did not start? [[Run CI]] starts it, or reruns the failed jobs. When GitHub holds a fork's workflows, [[Approve and run CI]] approves them as you: read the PR's workflow changes first.",
        zh: "CI 没有启动？点 [[运行 CI]] 启动它或重跑失败的任务。当 GitHub 暂扣某个分叉的工作流时，[[批准并运行 CI]] 会以你的身份批准运行，请先看一下 PR 对工作流的改动。",
      },
      {
        en: "[[Fix CI]] in [[Actions]] can run the failing steps on a test machine until they pass. The fix comes back as a draft you push.",
        zh: "[[操作]] 里的 [[修复 CI]] 可以在测试机上反复运行失败的步骤直到通过，修复会作为草稿回来，由你推送。",
      },
    ],
    tips: [
      {
        en: "[CI health](/ci) shows every failure the service diagnosed in the last 14 days, and which jobs flake.",
        zh: "[CI 状况](/ci) 显示服务在过去 14 天诊断过的所有失败，以及哪些任务经常偶发失败。",
      },
    ],
    open: [{ to: "/ci", label: { en: "Open CI health", zh: "打开 CI 状况" } }],
    ask: { en: "Which CI jobs are flaky, and should anything change?", zh: "哪些 CI 任务不稳定？需要做什么调整吗？" },
  },
  {
    id: "reproduce",
    group: "own",
    icon: "cpu",
    who: ["owner", "reviewer"],
    title: { en: "Test a PR on a test machine", zh: "在测试机上验证 PR" },
    summary: {
      en: "Run what the PR claims on a real machine, before and after the change.",
      zh: "在真实机器上运行 PR 声称的效果，对比改动前后。",
    },
    steps: [
      {
        en: "On the PR's page, open [[Actions]] → [[Reproduce]].",
        zh: "在 PR 页面打开 [[操作]] → [[复现]]。",
      },
      {
        en: "Pick the machine (your own come first), say what should be checked if you like, and press [[Start]].",
        zh: "选择机器（你自己的机器排在前面），需要的话写明要检查什么，然后点 [[开始]]。",
      },
      {
        en: "[[Watch]] it work. Every command it runs is checked against the machine's rules, and it takes only idle GPUs.",
        zh: "点 [[查看进展]] 看它工作。它运行的每条命令都会按机器的规则检查，而且只使用空闲的 GPU。",
      },
      {
        en: "Its record goes into the PR's review.",
        zh: "复现的记录会写进 PR 的评审。",
      },
    ],
    tips: [
      {
        en: "No machine to pick? The repository's maintainers choose which machines it uses on its [[Test machines]] tab, or add one of your own.",
        zh: "没有可选的机器？仓库维护者可以在仓库的 [[测试机器]] 标签页选择要用的机器，你也可以添加自己的测试机。",
      },
    ],
  },

  // ---- merging --------------------------------------------------------------------------
  {
    id: "merge-pr",
    group: "merge",
    icon: "merge",
    who: ["owner", "maintainer"],
    title: { en: "Approve and merge a PR", zh: "批准并合并 PR" },
    summary: {
      en: "Approve and merge under your own account, right from the PR's page.",
      zh: "直接在 PR 页面用你自己的账户批准并合并。",
    },
    steps: [
      {
        en: "[[Approve]] at the top of the PR's page, then [[Approve as me]], approves the PR on GitHub under your account, with a comment if you like.",
        zh: "点 PR 页面顶部的 [[批准]]，再点 [[以我的身份批准]]，即用你的账户在 GitHub 上批准这个 PR，可以附上评论。",
      },
      {
        en: "In the [[Merge]] card, [[Merge now]] then [[Merge as me]] merges it as you. GitHub's own rules apply; the service's merge checks do not.",
        zh: "在 [[合并]] 卡片里点 [[立即合并]]，再点 [[以我的身份合并]]，即以你的身份合并。适用 GitHub 自己的规则，不经过服务的合并检查。",
      },
      {
        en: "Behind its base branch? The same card offers to merge the base in first.",
        zh: "落后于基础分支？同一张卡片可以先把基础分支合并进来。",
      },
      {
        en: "Maintainers can [[Schedule merge]] instead: the service runs CI on the PR's turn and merges once every check passes.",
        zh: "维护者也可以点 [[调度合并]]：服务会在轮到这个 PR 时运行 CI，所有检查通过后合并。",
      },
    ],
  },
  {
    id: "merge-schedule",
    group: "merge",
    icon: "merge",
    who: ["maintainer"],
    title: { en: "Merge scheduling", zh: "合并调度" },
    summary: {
      en: "Choose which ready PRs merge and in what order; the service runs their CI and merges what passes.",
      zh: "选择哪些就绪的 PR 合并、按什么顺序；服务负责运行 CI 并合并通过的 PR。",
    },
    steps: [
      {
        en: "Open [Merge scheduling](/merge). If it is off for the repository, [[Turn on merge scheduling]]. Nothing merges that nobody scheduled.",
        zh: "打开 [合并调度](/merge)。如果该仓库还没开启，点 [[开启合并调度]]。没有人调度的 PR 不会被合并。",
      },
      {
        en: "[[Ready to schedule]] lists the PRs that meet the merge conditions. Press [[Schedule]] on one, or [[Schedule all]].",
        zh: "[[可调度]] 列出满足合并条件的 PR。对某个 PR 点 [[调度]]，或点 [[全部调度]]。",
      },
      {
        en: "Scheduled PRs take turns: each merges the base in, runs its CI, and merges when it passes. Reorder with [[Move up]] and [[Move down]]; [[Unschedule]] takes one out. Only the CI of the PR whose turn it is runs: the schedule cancels the others', and [[Cancel CI]] under CI running now stops any other PR's.",
        zh: "已调度的 PR 依次进行：每个都先合并基础分支、运行 CI，通过后合并。用 [[上移]]、[[下移]] 调整顺序，用 [[取消调度]] 移出队列。只有轮到的 PR 运行 CI：调度会取消其他 PR 的 CI，在“正在运行的 CI”中点 [[取消 CI]] 可以停止其他任何 PR 的 CI。",
      },
      {
        en: "A PR whose CI really failed moves to the [[Failure lane]] with its cause, and the next one takes the turn. Only a flake? [[Flaky, rerun]]. A failure only linters or formatters report is fixed at once instead, pushed as the maintainer who scheduled it. A PR back from the lane joins the end of the order.",
        zh: "CI 真正失败的 PR 会带着原因移到 [[失败队列]]，由下一个 PR 接上。只是偶发失败？点 [[偶发失败，重跑]]。只有 linter 或格式化工具报告的失败会被立即修复，并以调度它的维护者身份推送。从失败队列回来的 PR 会排到队尾。",
      },
      {
        en: "Wondering why a PR sits where it is? The message button on its row asks the agent, which reads its place in the schedule and every step taken.",
        zh: "想知道某个 PR 为什么停在那里？点它那一行的消息按钮问智能体，它会读取该 PR 在调度中的位置和已执行的每一步。",
      },
      {
        en: "Below the lists are the merge conditions and how CI starts. Describe the rules in your own words, and an agent turns them into settings for you to save.",
        zh: "列表下方是合并条件和 CI 的启动方式。用你自己的话描述规则，智能体会把它们转成设置，由你保存。",
      },
    ],
    tips: [
      {
        en: "Only maintainers schedule merges, and the service's account merges only PRs that were scheduled and passed.",
        zh: "只有维护者能调度合并，服务的账户也只会合并已被调度并且通过检查的 PR。",
      },
    ],
    open: [{ to: "/merge", label: { en: "Open Merge scheduling", zh: "打开合并调度" } }],
  },

  // ---- maintaining a repository ---------------------------------------------------------
  {
    id: "approvals",
    group: "maintain",
    icon: "inbox",
    who: ["maintainer", "reviewer"],
    title: { en: "Approvals: decide what the service wants to do", zh: "待审批：决定服务要做的事" },
    summary: {
      en: "What the service would post or change waits here, exactly as it will look.",
      zh: "服务打算发布或修改的内容都在这里等待，看到的就是最终的样子。",
    },
    steps: [
      {
        en: "[Approvals](/inbox) lists what waits for a person: labels, review requests, comments, fixes, and review drafts. What you see is exactly what will be posted.",
        zh: "[待审批](/inbox) 列出等人处理的事项：标签、评审请求、评论、修复和评审草稿。你看到的就是将要发布的内容。",
      },
      {
        en: "Open one to see why the service wants it. [[Edit]] it first if you like.",
        zh: "打开一项可以看到服务为什么要这样做。需要的话先点 [[编辑]]。",
      },
      {
        en: "[[Approve]] (or the **as me** button) does it; [[Reject]] drops it. Select several to approve them together.",
        zh: "点 [[批准]]（或带有 **以我的身份** 的按钮）执行；点 [[拒绝]] 丢弃。可以多选后一起批准。",
      },
      {
        en: "The repository's maintainers decide every proposal; everyone decides the ones they asked for.",
        zh: "仓库维护者可以决定每一项提议；每个人都可以决定自己发起的提议。",
      },
    ],
    open: [{ to: "/inbox", label: { en: "Open Approvals", zh: "打开待审批" } }],
    ask: { en: "Which proposals are safe to approve?", zh: "哪些提议可以放心批准？" },
  },
  {
    id: "levels",
    group: "maintain",
    icon: "shield",
    who: ["maintainer"],
    title: { en: "Choose what the service does on its own", zh: "设定服务的自主程度" },
    summary: {
      en: "Per action: off, record only, ask first, or automatic.",
      zh: "按操作逐项设定：关闭、仅记录、先询问或自动。",
    },
    steps: [
      {
        en: "Open the repository from [Repositories](/repos), then its [[Settings]] tab, and find [[What the service does on its own]].",
        zh: "在 [仓库](/repos) 中打开仓库，进入 [[设置]] 标签页，找到 [[服务自主执行的操作]]。",
      },
      {
        en: "Each action has a level: [[Off]], [[Record only]] (it notes what it would do and posts nothing), [[Ask first]] (it proposes and waits in Approvals), or [[Automatic]].",
        zh: "每个操作都有一个级别：[[已关闭]]、[[仅记录]]（只记下它会做什么，不发布任何内容）、[[先询问]]（先提议，在待审批中等待）或 [[自动]]。",
      },
      {
        en: "Every action starts at [[Record only]]. The bar beside it shows how often maintainers then did the same thing: raise an action when that is high.",
        zh: "所有操作一开始都是 [[仅记录]]。旁边的条形显示维护者随后做出相同操作的比例：比例高时再提高级别。",
      },
      {
        en: "If you are a maintainer, saving is your approval; otherwise your change is proposed to them.",
        zh: "如果你是维护者，保存即视为批准；否则你的修改会作为提议交给维护者。",
      },
    ],
    open: [{ to: "/repos", label: { en: "Open Repositories", zh: "打开仓库" } }],
  },
  {
    id: "repo-settings",
    group: "maintain",
    icon: "settings",
    who: ["maintainer"],
    title: { en: "Set how PRs are reviewed", zh: "设定 PR 的评审方式" },
    summary: {
      en: "The review skill, your own instructions, how reviews read, and when they run.",
      zh: "评审技能、你自己的说明、评审的写法，以及何时评审。",
    },
    steps: [
      {
        en: "On the repository's [[Settings]] tab, [[How PRs are reviewed]] picks the review skill the agent follows.",
        zh: "在仓库的 [[设置]] 标签页中，[[PR 如何评审]] 用来选择智能体遵循的评审技能。",
      },
      {
        en: "Add review instructions in your own words: what reviews must check, what matters most, and what to leave alone. Or [[Work them out with the agent]].",
        zh: "用你自己的话写评审说明：评审必须检查什么、什么最重要、什么不用管。也可以点 [[和智能体一起拟定]]。",
      },
      {
        en: "[[Writing]] sets how the review comment reads; [[Quality score]] sets how PRs are scored, which stays internal.",
        zh: "[[写法]] 决定评审评论怎么写；[[质量评分]] 决定 PR 如何打分（仅内部可见）。",
      },
      {
        en: "On Home, [[When reviews run]]: only when asked, every few hours, or on every update, and whether new commits get a follow-up.",
        zh: "在首页的 [[何时评审]] 中设定：只在请求时、每隔几小时，或每次更新时评审，以及有新提交时是否跟进评审。",
      },
      {
        en: "Anything else: [[Describe a change]] and the agent drafts the edit for a maintainer to approve, or [[Edit YAML]] yourself.",
        zh: "其他设置：点 [[描述修改]]，智能体会起草修改供维护者批准；也可以点 [[编辑 YAML]] 自己改。",
      },
    ],
    open: [{ to: "/repos", label: { en: "Open Repositories", zh: "打开仓库" } }],
    ask: { en: "Which settings would you change for this repository?", zh: "你建议这个仓库调整哪些设置？" },
  },
  {
    id: "people",
    group: "maintain",
    icon: "list",
    who: ["maintainer"],
    title: { en: "Roles and process", zh: "角色与流程" },
    summary: {
      en: "Write down who holds which role and how people move between roles.",
      zh: "记录谁担任什么角色，以及人员如何在角色之间变动。",
    },
    steps: [
      {
        en: "The repository's [[People]] tab holds its record. Holders of a role that maintains are its maintainers here; until the record names some, its GitHub admins are.",
        zh: "仓库的 [[人员]] 标签页保存了人员记录。担任维护类角色的人就是这里的维护者；记录里还没有指定时，由仓库的 GitHub 管理员担任。",
      },
      {
        en: "To fill it in, open [[Talk with the agent]] and tell it where your process is written, a file or a web page, or describe it.",
        zh: "要填写记录，打开 [[与智能体对话]]，告诉它你们的流程写在哪里（文件或网页），或者直接描述。",
      },
      {
        en: "[[Check who should change role]] has the agent compare GitHub activity with your process.",
        zh: "[[检查谁该变更角色]] 会让智能体对照 GitHub 上的活动和你们的流程。",
      },
      {
        en: "Every change is a draft until a maintainer applies it; nothing changes before that.",
        zh: "每项修改在维护者应用之前都只是草稿，不会生效。",
      },
    ],
  },
  {
    id: "memory",
    group: "maintain",
    icon: "book",
    who: ["maintainer"],
    title: { en: "Memory and lessons", zh: "记忆与经验" },
    summary: {
      en: "What the agents learned, and how each repository is set up, changes only with approval.",
      zh: "智能体学到的东西和每个仓库的配置，只有经过批准才会改变。",
    },
    steps: [
      {
        en: "Agents learn from trouble in their own work and from what reviewers say. What they learn, and every repository's configuration, is kept in the memory repository.",
        zh: "智能体会从自己工作中遇到的问题和评审者的意见中学习。学到的经验和每个仓库的配置都保存在记忆仓库中。",
      },
      {
        en: "A change to it waits on [Memory](/memory) → [[Waiting for approval]] until the maintainers it affects approve it.",
        zh: "对记忆的修改会在 [记忆](/memory) → [[等待批准]] 中等待，直到受影响的维护者批准。",
      },
      {
        en: "Open one to read the files it changes, then [[Approve]] or [[Reject]].",
        zh: "打开一项可以看到它修改了哪些文件，然后点 [[批准]] 或 [[拒绝]]。",
      },
      {
        en: "[[Browse memory]] shows what is kept now.",
        zh: "[[浏览记忆]] 可以查看当前保存的内容。",
      },
    ],
    tips: [
      {
        en: "Lessons about the service's own tools and test machines go to the service's owner instead, on the [Service](/service) page.",
        zh: "关于服务自身工具和测试机器的经验则交给服务所有者，在 [服务](/service) 页面处理。",
      },
    ],
    open: [{ to: "/memory", label: { en: "Open Memory", zh: "打开记忆" } }],
  },

  // ---- your own setup -------------------------------------------------------------------
  {
    id: "my-agents",
    group: "personal",
    icon: "cpu",
    who: ["everyone"],
    title: { en: "Use your own agent accounts", zh: "使用你自己的智能体账户" },
    summary: {
      en: "Run the work you ask for on your own agent sign-in or model API.",
      zh: "让你发起的工作使用你自己的智能体登录或模型 API。",
    },
    steps: [
      {
        en: "[Settings](/settings) → [[Agents]] → [[My agents]]: sign an agent CLI in to your own account. What you ask for then runs on it, not on the service's.",
        zh: "[设置](/settings) → [[智能体]] → [[我的智能体]]：把某个智能体 CLI 登录到你自己的账户。之后你发起的工作就会使用它，而不是服务的账户。",
      },
      {
        en: "A CLI without a sign-in? Add an API under [[My model APIs]] on the [[Model providers]] tab, then choose it for the agent.",
        zh: "某个 CLI 没有登录方式？在 [[模型服务商]] 标签页的 [[我的模型 API]] 中添加一个 API，再为该智能体选择它。",
      },
      {
        en: "Nobody else's work uses your accounts, and your keys never leave the service's host.",
        zh: "别人的工作不会使用你的账户，你的密钥也不会离开服务主机。",
      },
      {
        en: "[[My usage and limits]] shows your tokens and what each CLI says is left.",
        zh: "[[我的用量与额度]] 显示你的 token 用量，以及每个 CLI 报告的剩余额度。",
      },
    ],
    tips: [
      {
        en: "If the section says personal accounts are off, agents on this host run outside containers, where every run could read every person's sign-in. The service's owner can turn containers on.",
        zh: "如果这里显示个人账户已关闭，说明这台主机上的智能体没有在容器中运行，每次运行都可能读到所有人的登录信息。服务所有者可以开启容器。",
      },
    ],
    open: [{ to: "/settings?tab=agents", label: { en: "Open My agents", zh: "打开我的智能体" } }],
  },
  {
    id: "my-machines",
    group: "personal",
    icon: "server",
    who: ["everyone"],
    title: { en: "Add your own test machine", zh: "添加你自己的测试机" },
    summary: {
      en: "Reviews, reproductions, and CI fixes you ask for can run on a machine of yours.",
      zh: "你发起的评审、复现和 CI 修复可以在你自己的机器上运行。",
    },
    steps: [
      {
        en: "[Settings](/settings) → [[Test machines]] → [[My test machines]] → [[Add machine]].",
        zh: "[设置](/settings) → [[测试机器]] → [[我的测试机]] → [[添加机器]]。",
      },
      {
        en: "Add the SSH key shown there as one line to `~/.ssh/authorized_keys` of the login you give, and of each jump host.",
        zh: "把那里显示的 SSH 公钥作为一行加入你所填登录账户的 `~/.ssh/authorized_keys`，每台跳板机也要加。",
      },
      {
        en: "Fill in the host, login, and work folder, and a few words about the machine: its GPUs, where models and data live, which environment to use. Agents read this.",
        zh: "填写主机、登录名和工作目录，再写几句机器的情况：有哪些 GPU、模型和数据放在哪里、用哪个环境。智能体会读这些内容。",
      },
      {
        en: "Press [[Check]] to see that it connects. The reviews, reproductions, and CI fixes you ask for then use it first, on any repository you work on.",
        zh: "点 [[检查]] 确认能连接。之后你发起的评审、复现和 CI 修复都会优先使用它，适用于你参与的任何仓库。",
      },
    ],
    tips: [
      {
        en: "Only your own work runs on your machines.",
        zh: "只有你自己的工作会在你的机器上运行。",
      },
    ],
    open: [{ to: "/settings?tab=labs", label: { en: "Open My test machines", zh: "打开我的测试机" } }],
  },
  {
    id: "preferences",
    group: "personal",
    icon: "message",
    who: ["everyone"],
    title: { en: "Teach the agents your preferences", zh: "让智能体了解你的偏好" },
    summary: {
      en: "The agents remember how you like your reviews and answers.",
      zh: "智能体会记住你希望评审和回答是什么样子。",
    },
    steps: [
      {
        en: "[Settings](/settings) → [[Agents]] → [[My preferences]] lists what the agents learned about what you want, in reviews drafted for you and in conversations.",
        zh: "[设置](/settings) → [[智能体]] → [[我的偏好]] 列出了智能体从你这里学到的偏好，用于为你起草的评审和对话。",
      },
      {
        en: "They are learned from what you say in conversations and how you edit your drafts. Add one yourself, for example: leave out style nits; answer me briefly, in Chinese.",
        zh: "这些偏好来自你在对话中说的话和你对草稿的修改。你也可以自己添加，例如：不要提代码风格上的小问题；回答简短，用中文。",
      },
      {
        en: "Remove one that is wrong. Only the agents doing your work read them.",
        zh: "不对的偏好可以删除。只有为你工作的智能体会读取它们。",
      },
    ],
    open: [{ to: "/settings?tab=agents", label: { en: "Open My preferences", zh: "打开我的偏好" } }],
  },
  {
    id: "usage",
    group: "personal",
    icon: "activity",
    who: ["everyone"],
    title: { en: "Tokens, runs, and limits", zh: "Token、运行记录与限额" },
    summary: {
      en: "See what the agents used, for you, for each repository, and for everyone.",
      zh: "查看智能体为你、为每个仓库以及为所有人用了多少。",
    },
    steps: [
      {
        en: "[[My usage and limits]], under Settings → Agents, shows the tokens of the work you asked for and what each CLI reports is left.",
        zh: "设置 → 智能体中的 [[我的用量与额度]] 显示你发起的工作用了多少 token，以及每个 CLI 报告的剩余额度。",
      },
      {
        en: "[Settings](/settings) → [[Usage]] shows your tokens by kind of task (the service's owner and operators see everyone's), and the owner sets [[Token limits]] there.",
        zh: "[设置](/settings) → [[用量]] 按任务类型显示你的 token 用量（服务所有者和操作员能看到所有人的），所有者在这里设置 [[Token 限额]]。",
      },
      {
        en: "A repository's [[Usage]] tab shows its agent runs and your share of them (its maintainers see each person's), and [Agents](/agents) lists every run with its result and cost.",
        zh: "仓库的 [[用量]] 标签页显示该仓库的智能体运行以及你所占的部分（维护者能看到每个人的），[智能体](/agents) 页面列出每次运行的结果和费用。",
      },
      {
        en: "Runs on your own accounts never count toward a limit.",
        zh: "在你自己账户上的运行不计入任何限额。",
      },
    ],
    open: [{ to: "/settings?tab=usage", label: { en: "Open Usage", zh: "打开用量" } }],
  },
  {
    id: "chat",
    group: "personal",
    icon: "message",
    who: ["everyone"],
    title: { en: "Conversations with the agent", zh: "与智能体对话" },
    summary: {
      en: "Ask anything on any page; changes it suggests wait for approval.",
      zh: "在任何页面都可以提问；它建议的改动都要等待批准。",
    },
    steps: [
      {
        en: "Press [[Ask agent]] on any page, or {{⌘}} {{J}}. The agent knows the page, PR, or repository you are looking at.",
        zh: "在任意页面点 [[询问智能体]]，或按 {{⌘}} {{J}}。智能体知道你正在看的页面、PR 或仓库。",
      },
      {
        en: "Ask anything: Summarize this PR. Which PRs are blocked, and on what? Why did this CI fail? It reads what you can see.",
        zh: "什么都可以问：总结这个 PR；哪些 PR 卡住了、卡在哪里；这次 CI 为什么失败。它能读取你能看到的内容。",
      },
      {
        en: "When it suggests a change, such as a label, a setting, or a profile edit, the change shows as a card to approve. Nothing happens until someone approves it.",
        zh: "当它建议某项改动（比如标签、设置或仓库配置），改动会显示为一张待批准的卡片。没有人批准就什么都不会发生。",
      },
      {
        en: "The conversation follows you from page to page. [[New conversation]] starts another, and [Conversations](/chat) keeps them all.",
        zh: "对话会跟着你在页面之间切换。[[新对话]] 可以开始另一段对话，[对话](/chat) 页面保存了全部对话。",
      },
      {
        en: "Want the service itself to do something new? Ask for it in a conversation: the request goes to the service's owner to approve.",
        zh: "希望服务本身增加新功能？在对话中提出即可：请求会交给服务所有者批准。",
      },
    ],
    open: [{ to: "/chat", label: { en: "Open Conversations", zh: "打开对话" } }],
  },

  // ---- running the service --------------------------------------------------------------
  {
    id: "add-repo",
    group: "service",
    icon: "repo",
    who: ["maintainer", "admin"],
    title: { en: "Add a repository", zh: "添加仓库" },
    summary: {
      en: "An agent reads the repository and drafts how the service works there; a maintainer approves it.",
      zh: "智能体阅读仓库并起草服务在该仓库的工作方式，由维护者批准。",
    },
    steps: [
      {
        en: "[Repositories](/repos) → [[Add repository]], type `owner/name`, and press [[Start onboarding]]. You need maintainer access to it on GitHub.",
        zh: "[仓库](/repos) → [[添加仓库]]，输入 `owner/name`，点 [[开始接入]]。你需要在 GitHub 上拥有该仓库的维护者权限。",
      },
      {
        en: "An agent reads the repository and drafts its **profile**: its labels, checks, and review style.",
        zh: "智能体会阅读仓库并起草它的 **配置**：标签、检查和评审风格。",
      },
      {
        en: "A maintainer reads the draft on the repository's [[Profile change]] tab and approves it. Until then the service only reads the repository.",
        zh: "维护者在仓库的 [[配置变更]] 标签页阅读草稿并批准。在此之前，服务只读取这个仓库。",
      },
      {
        en: "Every action starts at [[Record only]]; raise them as you come to trust them.",
        zh: "所有操作一开始都是 [[仅记录]]，等你信任它们后再逐步提高。",
      },
    ],
    open: [{ to: "/repos", label: { en: "Open Repositories", zh: "打开仓库" } }],
  },
  {
    id: "service-settings",
    group: "service",
    icon: "settings",
    who: ["admin"],
    title: { en: "Agents, model providers, and routing", zh: "智能体、模型服务商与任务分配" },
    summary: {
      en: "Which agent CLIs run, on which models, for which tasks.",
      zh: "运行哪些智能体 CLI、使用哪些模型、负责哪些任务。",
    },
    steps: [
      {
        en: "[Settings](/settings) is where the service's owner sets which agents run, on which model providers, and which agent does each task ([[Task routing]]).",
        zh: "服务所有者在 [设置](/settings) 中设定运行哪些智能体、使用哪些模型服务商，以及每项任务由哪个智能体负责（[[任务分配]]）。",
      },
      {
        en: "Easiest: type what you want in the box at the top, for example: use DeepSeek for Claude Code. The agent drafts the change; you see the diff and apply it.",
        zh: "最简单的方式：在顶部输入框写下你想要的，例如：让 Claude Code 使用 DeepSeek。智能体会起草修改，你看过 diff 后应用。",
      },
      {
        en: "API keys go in under [[Model providers]]. They stay on the host and are never shown again.",
        zh: "API 密钥在 [[模型服务商]] 中填写。它们只保存在主机上，之后不会再显示。",
      },
      {
        en: "Everyone can read these settings; what others ask for waits for the owner.",
        zh: "所有人都能查看这些设置；其他人提出的修改要等所有者批准。",
      },
    ],
    open: [{ to: "/settings", label: { en: "Open Settings", zh: "打开设置" } }],
    ask: { en: "Which agent does reviews, and why?", zh: "评审由哪个智能体负责？为什么？" },
  },
  {
    id: "service-page",
    group: "service",
    icon: "server",
    who: ["admin"],
    title: { en: "Updates, self-improvement, and logs", zh: "更新、自我改进与日志" },
    summary: {
      en: "Deploy new versions, approve changes the service makes to itself, and see what happened.",
      zh: "部署新版本、批准服务对自身的修改，并查看发生过的事。",
    },
    steps: [
      {
        en: "[Service](/service) shows the running version and the commits waiting to deploy. [[Deploy]] tests the commit, then restarts on it; a version that fails to start is rolled back.",
        zh: "[服务](/service) 页面显示正在运行的版本和等待部署的提交。[[部署]] 会先测试该提交再重启到新版本；启动失败的版本会自动回滚。",
      },
      {
        en: "Requests for the service to change itself become [[Self-improvement PRs]] once you approve them: an agent writes the change, the tests run, and a PR opens. Merging and deploying it each need you.",
        zh: "让服务修改自身的请求经你批准后会成为 [[自我改进 PR]]：智能体写改动、运行测试，然后开 PR。合并和部署都分别需要你批准。",
      },
      {
        en: "[[What the agents learned]] lists lessons about the service's tools and test machines, and changes agents suggest: keep or dismiss each.",
        zh: "[[智能体学到了什么]] 列出关于服务工具和测试机器的经验，以及智能体建议的修改，可以逐条保留或忽略。",
      },
      {
        en: "[[Logs]] shows the service's log, and [Audit log](/audit) records every decision and change.",
        zh: "[[日志]] 显示服务的运行日志，[审计日志](/audit) 记录每一次决定和修改。",
      },
      {
        en: "Something going wrong? Anyone signed in can [[Pause the service]] from the account menu: it posts and changes nothing until an operator resumes it.",
        zh: "出了问题？任何已登录的人都可以在账户菜单中点 [[暂停服务]]：服务在操作员恢复之前不会发布或修改任何内容。",
      },
    ],
    open: [
      { to: "/service", label: { en: "Open Service", zh: "打开服务" } },
      { to: "/audit", label: { en: "Open Audit log", zh: "打开审计日志" } },
    ],
  },
  {
    id: "access",
    group: "service",
    icon: "key",
    who: ["admin"],
    title: { en: "People and their permissions", zh: "人员与权限" },
    summary: {
      en: "Who may do what: each person's role in each repository, and the permissions it gives them.",
      zh: "谁可以做什么：每个人在每个仓库中的角色，以及该角色赋予的权限。",
    },
    steps: [
      {
        en: "[People & permissions](/access) shows [[Your permissions]] in each repository the service serves, and where each comes from: maintaining it, a role in its People record, or write access on GitHub.",
        zh: "[人员与权限](/access) 显示你在服务管理的每个仓库中的 [[你的权限]]，以及每项权限的来源：维护该仓库、成员档案中的角色，或 GitHub 写权限。",
      },
      {
        en: "Guest reviewers, from outside the team, review the PRs they review or own with their own agents and machines; reviewers and maintainers also use the service's.",
        zh: "访客评审者来自团队之外，用自己的智能体和机器评审自己负责或评审的 PR；评审者和维护者还可以使用服务的智能体和机器。",
      },
      {
        en: "The owner and service admins see everyone under [[People]]: open a person to set their role everywhere or in one repository, turn single permissions on or off, and set limits.",
        zh: "所有者和服务管理员可以在 [[人员]] 中看到所有人：打开某人即可设置其在所有仓库或某个仓库中的角色，单独开启或关闭权限，并设置限额。",
      },
      {
        en: "[[Roles]] changes what a role lets everyone with it do, and [[People records]] which service role each People record role gives. Changes apply when you press [[Save]].",
        zh: "[[角色]] 修改某个角色允许所有拥有者做什么，[[成员档案]] 设置每个成员档案角色对应的服务角色。按下 [[保存]] 后生效。",
      },
    ],
    tips: [
      {
        en: "Only the owner names the service admins, and scheduling merges and changing a repository's settings stay with its maintainers whatever the permissions say.",
        zh: "只有所有者能任命服务管理员；无论权限如何设置，调度合并和修改仓库设置都只限仓库的维护者。",
      },
    ],
    open: [{ to: "/access", label: { en: "Open People & permissions", zh: "打开人员与权限" } }],
  },
];

export const GLOSSARY: { term: Text; text: Text }[] = [
  {
    term: { en: "Profile", zh: "仓库配置（profile）" },
    text: {
      en: "How the service works in one repository: labels, checks, review style, and what it may do. Only the repository's maintainers change it.",
      zh: "服务在某个仓库中的工作方式：标签、检查、评审风格，以及它被允许做什么。只有仓库维护者可以修改。",
    },
  },
  {
    term: { en: "Draft", zh: "草稿" },
    text: {
      en: "A review or change an agent made for you. Nothing is posted or pushed until a person does it.",
      zh: "智能体为你完成的评审或改动。在有人发布或推送之前，不会出现在 GitHub 上。",
    },
  },
  {
    term: { en: "Proposal", zh: "提议" },
    text: {
      en: "Something the service wants to do that waits in Approvals for a person to approve or reject.",
      zh: "服务想要执行、正在待审批中等人批准或拒绝的操作。",
    },
  },
  {
    term: { en: "Record only", zh: "仅记录" },
    text: {
      en: "The service notes what it would have done and does nothing. Every action starts here.",
      zh: "服务只记下它本来会做什么，实际什么也不做。所有操作都从这个级别开始。",
    },
  },
  {
    term: { en: "As me", zh: "以我的身份" },
    text: {
      en: "Done on GitHub under your own account, with the sign-in you gave the dashboard, and only for what you approved.",
      zh: "用你在控制台登录时授权的账户在 GitHub 上执行，而且只执行你批准的操作。",
    },
  },
  {
    term: { en: "PR owner", zh: "PR 负责人" },
    text: {
      en: "The person who answers for a PR's progress here. Owners may edit the drafts others made for the PR.",
      zh: "在这里对某个 PR 的进度负责的人。负责人可以编辑别人为这个 PR 起草的内容。",
    },
  },
  {
    term: { en: "Maintainer", zh: "维护者" },
    text: {
      en: "Who configures a repository here and decides its proposals: the holders of its maintaining roles, else its GitHub admins.",
      zh: "在这里配置仓库、决定其提议的人：仓库人员记录中担任维护类角色的人；没有记录时为 GitHub 管理员。",
    },
  },
  {
    term: { en: "Service owner", zh: "服务所有者" },
    text: {
      en: "The one person who owns this deployment: they change its agents, settings, and code.",
      zh: "拥有这个部署的唯一一个人：由其修改智能体、设置和代码。",
    },
  },
  {
    term: { en: "Tier (P0–P3)", zh: "梯队（P0–P3）" },
    text: {
      en: "A PR's priority within its repository; P0 is the most urgent. Internal: never posted.",
      zh: "PR 在所属仓库中的优先级，P0 最紧急。仅内部可见，不会发布。",
    },
  },
  {
    term: { en: "Quality score", zh: "质量评分" },
    text: {
      en: "0 to 100, from the repository's quality guide: a quick score from the diff, then an accurate one from the review. Internal.",
      zh: "0 到 100 分，依据仓库的质量指南：先根据 diff 快速打分，评审后给出准确分数。仅内部可见。",
    },
  },
  {
    term: { en: "Follow-up", zh: "跟进评审" },
    text: {
      en: "A review of the commits since the last one, which also checks whether earlier comments were addressed.",
      zh: "针对上次评审之后的新提交所做的评审，同时检查之前的意见是否已处理。",
    },
  },
  {
    term: { en: "Your copy", zh: "你的副本" },
    text: {
      en: "When you discuss someone else's review or change, your messages go to a copy of your own; the original stays as it is.",
      zh: "当你讨论别人的评审或改动时，你的消息会进入你自己的副本，原内容保持不变。",
    },
  },
  {
    term: { en: "Memory", zh: "记忆" },
    text: {
      en: "The private repository where profiles and what agents learned are kept. Changes to it need approval.",
      zh: "保存仓库配置和智能体所学经验的私有仓库。对它的修改需要批准。",
    },
  },
  {
    term: { en: "Test machine", zh: "测试机" },
    text: {
      en: "A machine, often with GPUs, where agents run a PR's code. Every command is checked against its rules.",
      zh: "智能体运行 PR 代码的机器，通常带有 GPU。每条命令都会按机器的规则检查。",
    },
  },
  {
    term: { en: "Flaky", zh: "不稳定（偶发失败）" },
    text: {
      en: "A CI failure that is not the PR's fault. Marked flaky, the same failure is rerun from then on.",
      zh: "不是由 PR 造成的 CI 失败。标记为不稳定后，同样的失败以后会自动重跑。",
    },
  },
];

export const FAQ: { q: Text; a: Text }[] = [
  {
    q: { en: "Will the service post on GitHub without me?", zh: "服务会不经过我就在 GitHub 上发布内容吗？" },
    a: {
      en: "Only actions a maintainer set to Automatic for the repository. Every action starts at Record only, and a review you start comes back to you as a draft.",
      zh: "只有维护者在该仓库设为“自动”的操作才会。所有操作一开始都是“仅记录”，你发起的评审也会作为草稿回到你手里。",
    },
  },
  {
    q: { en: "A button is greyed out, or says I cannot do this. Why?", zh: "按钮是灰色的，或者提示我不能操作，为什么？" },
    a: {
      en: "Some decisions belong to the repository's maintainers or to the service's owner, and the page says who decides. Ask them, or make the change anyway: it is proposed to them.",
      zh: "有些决定属于仓库维护者或服务所有者，页面上会写明由谁决定。可以去问他们，或者照样提交修改：它会作为提议交给他们。",
    },
  },
  {
    q: { en: "A button says Sign in with GitHub. Didn't I sign in?", zh: "按钮提示“使用 GitHub 登录”，我不是已经登录了吗？" },
    a: {
      en: "You signed in to the dashboard, but not in a way that lets it act as you, for example through a one-time link. Sign in with GitHub once from the account menu.",
      zh: "你登录了控制台，但登录方式（例如一次性链接）不能让它以你的身份操作。在账户菜单中用 GitHub 登录一次即可。",
    },
  },
  {
    q: { en: "Where did my review draft go?", zh: "我的评审草稿去哪了？" },
    a: {
      en: "Approvals → Waiting on me, the PR's page, and Home. Once it is posted or rejected, the PR's Timeline tab shows what happened.",
      zh: "在 待审批 → 等我处理、PR 页面和首页都能找到。发布或拒绝之后，PR 的时间线标签页会显示处理结果。",
    },
  },
  {
    q: { en: "The agent seems stuck.", zh: "智能体好像卡住了。" },
    a: {
      en: "It may be waiting for you: look at Agent questions. Its work panel says what it is doing, and Stop ends it.",
      zh: "它可能在等你回答：去看看智能体提问。它的工作面板会显示正在做什么，点“停止”可以结束它。",
    },
  },
  {
    q: { en: "I changed something on GitHub, and the page does not show it yet.", zh: "我在 GitHub 上改了东西，页面还没显示。" },
    a: {
      en: "The service reads GitHub every couple of minutes, or within seconds where the repository sends it webhooks. Sync with GitHub on Pull requests, or the refresh button on a PR's page, reads it now.",
      zh: "服务每隔几分钟读取一次 GitHub；配置了 webhook 的仓库则在几秒内更新。在拉取请求页面点“与 GitHub 同步”，或在 PR 页面点刷新按钮，可以立即读取。",
    },
  },
  {
    q: { en: "Can PR authors see the scores?", zh: "PR 作者能看到评分吗？" },
    a: {
      en: "No. Tiers and quality scores stay on this dashboard; they are never posted.",
      zh: "不能。梯队和质量评分只在本控制台显示，永远不会发布。",
    },
  },
  {
    q: { en: "Who can see this dashboard?", zh: "谁能看到这个控制台？" },
    a: {
      en: "Anyone with write access to a repository the service maintains. Each person sees the repositories they have access to.",
      zh: "对本服务维护的某个仓库有写权限的人。每个人只能看到自己有权限的仓库。",
    },
  },
];

/** Reading paths: the topics, in order, for each kind of person. */
export const PATHS: { role: Exclude<Role, "everyone">; icon: string; label: Text; blurb: Text; topics: string[] }[] = [
  {
    role: "reviewer",
    icon: "pr",
    label: { en: "I review PRs", zh: "我评审 PR" },
    blurb: { en: "Find PRs, start reviews, and post them as yourself.", zh: "找到 PR、发起评审，并以你自己的身份发布。" },
    topics: ["welcome", "sign-in", "tour", "home", "find-prs", "pr-page", "start-review", "post-review", "discuss", "follow-up", "questions"],
  },
  {
    role: "owner",
    icon: "check",
    label: { en: "I own PRs", zh: "我负责 PR" },
    blurb: { en: "Track progress, get fixes made, deal with CI, and merge.", zh: "跟踪进度、让智能体修改、处理 CI，并合并。" },
    topics: ["welcome", "sign-in", "owners", "progress", "fix", "ci", "reproduce", "merge-pr", "my-machines"],
  },
  {
    role: "maintainer",
    icon: "shield",
    label: { en: "I maintain a repository", zh: "我维护仓库" },
    blurb: { en: "Decide what the service may do, and how it reviews and merges.", zh: "决定服务可以做什么，以及如何评审和合并。" },
    topics: ["welcome", "approvals", "levels", "repo-settings", "people", "memory", "merge-schedule", "add-repo"],
  },
  {
    role: "admin",
    icon: "server",
    label: { en: "I run this service", zh: "我运行这个服务" },
    blurb: { en: "Repositories, agents and models, updates, and costs.", zh: "仓库、智能体与模型、更新和费用。" },
    topics: ["welcome", "add-repo", "service-settings", "my-agents", "usage", "service-page", "levels"],
  },
];

export function topicById(id: string | undefined | null): Topic | undefined {
  return TOPICS.find((t) => t.id === id);
}

/** The text without its markup, for search and for the palette. */
export function plain(text: string): string {
  return text
    .replace(/\[\[([^\]]+)\]\]/g, "$1")
    .replace(/\{\{([^}]+)\}\}/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1");
}

/** Topics that match every word of ``query``, best first: title, then summary, then body. */
export function searchTopics(query: string, lang: GuideLang): Topic[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return TOPICS;
  const scored: { topic: Topic; score: number }[] = [];
  for (const topic of TOPICS) {
    const title = topic.title[lang].toLowerCase();
    const summary = topic.summary[lang].toLowerCase();
    const body = [...topic.steps, ...(topic.tips ?? [])].map((s) => plain(s[lang])).join(" ").toLowerCase();
    // Either language matches, so an English term finds a topic while reading Chinese.
    const other = lang === "zh" ? "en" : "zh";
    const rest = [topic.title[other], topic.summary[other], ...topic.steps.map((s) => plain(s[other]))].join(" ").toLowerCase();
    let score = 0;
    for (const w of words) {
      if (title.includes(w)) score += 4;
      else if (summary.includes(w)) score += 2;
      else if (body.includes(w) || rest.includes(w)) score += 1;
      else {
        score = 0;
        break;
      }
    }
    if (score > 0) scored.push({ topic, score });
  }
  return scored.sort((a, b) => b.score - a.score).map((s) => s.topic);
}

/** The topic that explains the page at ``pathname`` (with its ``?tab=``), if any. */
export function helpTopicFor(pathname: string, search = ""): string | null {
  const tab = new URLSearchParams(search).get("tab");
  if (pathname === "/") return "home";
  if (pathname.startsWith("/guide") || pathname.startsWith("/setup")) return null;
  if (pathname.startsWith("/pr/")) {
    if (tab === "progress") return "progress";
    if (tab === "ci") return "ci";
    return "pr-page";
  }
  if (pathname.startsWith("/repos/")) {
    const byTab: Record<string, string> = { people: "people", labs: "reproduce", usage: "usage", candidate: "add-repo" };
    return (tab && byTab[tab]) || "repo-settings";
  }
  if (pathname.startsWith("/settings")) {
    const byTab: Record<string, string> = { agents: "my-agents", providers: "my-agents", labs: "my-machines", usage: "usage" };
    return (tab && byTab[tab]) || (tab ? "service-settings" : "my-agents");
  }
  const byPage: Record<string, string> = {
    "/prs": "find-prs",
    "/merge": "merge-schedule",
    "/ci": "ci",
    "/repos": "add-repo",
    "/inbox": "approvals",
    "/questions": "questions",
    "/memory": "memory",
    "/agents": "usage",
    "/chat": "chat",
    "/service": "service-page",
    "/audit": "service-page",
    "/access": "access",
  };
  const page = Object.keys(byPage).find((p) => pathname === p || pathname.startsWith(`${p}/`));
  return page ? byPage[page] : null;
}

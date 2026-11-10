// Copyright 2026 The RLinf Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// pi's own sign-in to one subscription, without its terminal UI.
//
//     node pi_login.mjs <pi package folder> <provider id>
//
// pi signs in with `/login` inside its interactive UI, which also runs any shell
// command typed after `!`, so the service never shows that UI to anyone. This script
// calls the same sign-in pi's UI calls (`AuthStorage.login`) and relays it the way
// `agents/login.py` reads every CLI's sign-in: it prints the page to open, a device
// code when there is one, and a line asking to paste when it needs the code or the
// address the browser landed on; each line it reads from standard input is only ever
// that answer. pi stores the sign-in in `$HOME/.pi/agent/auth.json`. Nothing else is
// reachable from here: no prompt goes to a model and no command runs.

import { join } from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

const [pkg, providerId] = process.argv.slice(2);
if (!pkg || !providerId) {
  console.log("usage: pi_login.mjs <pi package folder> <provider id>");
  process.exit(2);
}
const { AuthStorage } = await import(pathToFileURL(join(pkg, "dist", "index.js")).href);
const storage = AuthStorage.create();
const provider = storage.getOAuthProviders().find((p) => p.id === providerId);
if (!provider) {
  console.log(`pi has no sign-in named ${providerId}`);
  process.exit(2);
}

const input = createInterface({ input: process.stdin });
const queued = [];
const waiting = [];
input.on("line", (line) => {
  const take = waiting.shift();
  if (take) take(line);
  else queued.push(line);
});
const nextLine = () => (queued.length ? Promise.resolve(queued.shift()) : new Promise((resolve) => waiting.push(resolve)));

const DEVICE_CODE = /\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b/;
const ask = async (question) => {
  console.log(/paste/i.test(question) ? question : `${question} Paste it here:`);
  return (await nextLine()).trim();
};

try {
  await storage.login(providerId, {
    onAuth: ({ url, instructions }) => {
      console.log(`Open ${url}`);
      const code = (instructions ?? "").match(DEVICE_CODE);
      if (code) console.log(code[1]);
      if (provider.usesCallbackServer) {
        console.log("After you sign in, the browser goes to a localhost address that does not load on your computer.");
      }
    },
    onManualCodeInput: () => ask("Paste the code, or the whole localhost address the browser landed on, here:"),
    onPrompt: async ({ message, allowEmpty }) => {
      // GitHub Copilot's only optional question is an Enterprise domain: github.com.
      if (allowEmpty) return "";
      return ask(message);
    },
    onSelect: async () => undefined,
    onProgress: (message) => {
      if (typeof message === "string") console.log(message);
    },
  });
  console.log(`Signed in to ${provider.name}.`);
  process.exit(0);
} catch (error) {
  console.log(`The sign-in failed: ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}

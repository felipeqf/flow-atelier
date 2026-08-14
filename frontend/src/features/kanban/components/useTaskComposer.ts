import { useState } from "react";
import { createTask } from "@/runner/engine";
import { useTaskStore } from "@/runner";
import type { Task, ToolType } from "@/types/task";

export const COMPOSER_DEFAULT_TOOL: ToolType = "tool:bash";

// The store is keyed by name and upsert overwrites, so a slug that collides
// with an existing card gets a counter instead of clobbering it. Underscores,
// not hyphens: the backend's task grammar is ^[A-Za-z0-9_]+$ (a hyphenated
// name is a 400 at run time), and every seeded task already reads like this.
function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 32)
    .replace(/_+$/, "");
}

function uniqueName(base: string, taken: Set<string>): string {
  const stem = base || "task";
  if (!taken.has(stem)) return stem;
  for (let i = 2; ; i++) {
    const candidate = `${stem}_${i}`;
    if (!taken.has(candidate)) return candidate;
  }
}

interface UseTaskComposerParams {
  projectId: string;
}

/**
 * State + submit logic for the chat-style task bar. Single-run tasks only:
 * the typed text is the task's prompt, never a conduit lookup — queuing
 * existing conduits from here would be a later step.
 */
export function useTaskComposer({ projectId }: UseTaskComposerParams) {
  const [text, setText] = useState("");
  const [tool, setTool] = useState<ToolType>(COMPOSER_DEFAULT_TOOL);

  const submit = (): Task | undefined => {
    const trimmed = text.trim();
    if (!trimmed) return undefined;
    const taken = new Set(useTaskStore.getState().tasks.map((t) => t.name));
    const task = createTask({
      name: uniqueName(slugify(trimmed), taken),
      description: trimmed,
      prompt: trimmed,
      tool,
      inputs: {},
      projectId,
    });
    setText("");
    return task;
  };

  return { text, setText, tool, setTool, submit };
}

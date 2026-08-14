import { useEffect, useRef } from "react";
import { TOOL_META, toolColor } from "@/constants/tools";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import type { ToolType } from "@/types/task";
import { useTaskComposer } from "./useTaskComposer";

interface Props {
  projectId: string;
}

/**
 * Chat-style entry to the board: pick a tool, type the task, send. The card
 * lands in TODO; everything after that is the board's existing machinery.
 * Pinned under the board by the Kanban layout, not by this component.
 */
export function TaskComposer({ projectId }: Props) {
  const { text, setText, tool, setTool, submit } = useTaskComposer({ projectId });
  const areaRef = useRef<HTMLTextAreaElement>(null);

  // A rows=1 field that grows with the draft, capped so a long paste cannot
  // push the board off-screen — the composer serves the board, not the other
  // way around.
  useEffect(() => {
    const el = areaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  }, [text]);

  return (
    <footer data-testid="task-composer" className="border-t border-border bg-background px-4 pb-4 pt-3 lg:px-10">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
          areaRef.current?.focus();
        }}
        className="mx-auto flex max-w-[1280px] flex-wrap items-center gap-2"
      >
        <Select value={tool} onValueChange={(v) => setTool(v as ToolType)}>
          <SelectTrigger
            aria-label="Task tool"
            data-testid="task-composer-tool"
            className="w-auto min-w-[11.5rem] gap-2 text-label"
          >
            <span
              className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
              style={{ backgroundColor: toolColor(tool) }}
              aria-hidden
            />
            <span className="truncate">{tool}</span>
          </SelectTrigger>
          <SelectContent>
            {TOOL_META.map((t) => (
              <SelectItem key={t.name} value={t.name}>
                <span className="flex items-center gap-2">
                  <span
                    className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
                    style={{ backgroundColor: toolColor(t.name) }}
                    aria-hidden
                  />
                  {t.name}
                </span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <label htmlFor="task-composer-input" className="sr-only">
          Add a task to the board
        </label>
        <textarea
          id="task-composer-input"
          ref={areaRef}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder="Type a task to add it to your board…"
          data-testid="task-composer-input"
          className="min-w-0 flex-1 basis-56 resize-none overflow-y-auto border border-border bg-transparent px-3 py-2 font-mono text-label text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
        />

        <Button type="submit" size="sm" data-testid="task-composer-send" className="shrink-0">
          send
        </Button>
      </form>
    </footer>
  );
}

import { Kanban } from "@/features/kanban/Kanban";
import { TaskComposer } from "@/features/kanban/components/TaskComposer";

/**
 * The chat view: the same board, the same task store, with a composer pinned
 * underneath as a second way in. Typing adds a single-run task to TODO.
 */
export function Chat() {
  return (
    <Kanban composer={({ projectId }) => <TaskComposer projectId={projectId} />} />
  );
}

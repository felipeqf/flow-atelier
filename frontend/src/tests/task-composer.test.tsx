// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

const STABLE_CTX = { conduits: [], loading: false, error: null, refresh: () => {} };
vi.mock("@/services/ConduitProvider", () => ({
  useConduits: () => STABLE_CTX,
  getConduitSync: () => undefined,
}));

// Radix primitives observe their viewport; jsdom ships no ResizeObserver.
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

import { TaskComposer } from "@/features/kanban/components/TaskComposer";
import { Chat } from "@/features/chat/Chat";
import { useTaskStore } from "@/runner";

/**
 * The composer bar is the chat tab's entry point: one line of text plus the
 * tool it should run on, landing as a TODO card on the board above.
 */
describe("TaskComposer", () => {
  afterEach(() => {
    cleanup();
    useTaskStore.getState().setTasks([]);
  });

  it("shows the default tool and the task placeholder", () => {
    render(<TaskComposer projectId="p1" />);
    expect(screen.getByTestId("task-composer-tool").textContent).toContain("tool:bash");
    expect(screen.getByPlaceholderText(/type a task to add/i)).toBeTruthy();
  });

  it("sends the typed task to the board and clears the field", () => {
    render(<TaskComposer projectId="p1" />);
    const field = screen.getByTestId("task-composer-input") as HTMLTextAreaElement;

    fireEvent.change(field, { target: { value: "write the migration notes" } });
    fireEvent.click(screen.getByTestId("task-composer-send"));

    expect(useTaskStore.getState().tasks).toHaveLength(1);
    expect(useTaskStore.getState().tasks[0]).toMatchObject({
      name: "write-the-migration-notes",
      column: "todo",
      projectId: "p1",
    });
    expect(field.value).toBe("");
  });

  it("does not send an empty draft", () => {
    render(<TaskComposer projectId="p1" />);
    fireEvent.click(screen.getByTestId("task-composer-send"));
    expect(useTaskStore.getState().tasks).toHaveLength(0);
  });

  it("submits on Enter but leaves Shift+Enter for a new line", () => {
    render(<TaskComposer projectId="p1" />);
    const field = screen.getByTestId("task-composer-input");

    fireEvent.change(field, { target: { value: "first task" } });
    fireEvent.keyDown(field, { key: "Enter", shiftKey: true });
    expect(useTaskStore.getState().tasks).toHaveLength(0);

    fireEvent.keyDown(field, { key: "Enter" });
    expect(useTaskStore.getState().tasks).toHaveLength(1);
  });
});

/**
 * The chat page is the kanban board plus that composer — one task store, both
 * surfaces. A sent task must be visible in the TODO column immediately.
 */
describe("Chat page", () => {
  afterEach(() => {
    cleanup();
    useTaskStore.getState().setTasks([]);
  });

  it("renders the three board columns with the composer pinned beneath", () => {
    render(<Chat />);
    expect(screen.getByTestId("column-todo")).toBeTruthy();
    expect(screen.getByTestId("column-in_progress")).toBeTruthy();
    expect(screen.getByTestId("column-done")).toBeTruthy();
    expect(screen.getByTestId("task-composer")).toBeTruthy();
  });

  it("lands a sent task in the TODO column of the board above", () => {
    render(<Chat />);
    fireEvent.change(screen.getByTestId("task-composer-input"), {
      target: { value: "review the open prs" },
    });
    fireEvent.click(screen.getByTestId("task-composer-send"));

    const todo = screen.getByTestId("column-todo");
    expect(todo.querySelector('[data-testid="task-card"]')?.getAttribute("data-task-id"))
      .toBe("review-the-open-prs");
  });
});

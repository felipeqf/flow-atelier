// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

// The composer creates single-run tasks only; conduits must not be consulted.
const getConduitSync = vi.fn((_name: string): unknown => undefined);
vi.mock("@/services/ConduitProvider", () => ({
  useConduits: () => ({ conduits: [], loading: false, error: null, refresh: () => {} }),
  getConduitSync: (name: string) => getConduitSync(name),
}));

import { useTaskComposer } from "@/features/kanban/components/useTaskComposer";
import { useTaskStore } from "@/runner";

beforeEach(() => {
  useTaskStore.getState().setTasks([]);
});

afterEach(() => {
  useTaskStore.getState().setTasks([]);
});

function setup(projectId = "p1") {
  return renderHook(() => useTaskComposer({ projectId }));
}

/**
 * The composer is the chat tab's whole contract: typed text becomes one
 * ad-hoc TODO task with the chosen tool — never a conduit run.
 */
describe("useTaskComposer", () => {
  it("creates a single-run task in TODO from the typed text", () => {
    const { result } = setup();

    act(() => result.current.setText("Fix the login redirect"));
    let task;
    act(() => (task = result.current.submit()));

    expect(useTaskStore.getState().tasks).toHaveLength(1);
    expect(task).toMatchObject({
      name: "fix_the_login_redirect",
      description: "Fix the login redirect",
      prompt: "Fix the login redirect",
      tool: "tool:bash",
      inputs: {},
      projectId: "p1",
      column: "todo",
    });
  });

  it("uses the selected tool", () => {
    const { result } = setup();

    act(() => result.current.setText("ask claude to review the diff"));
    act(() => result.current.setTool("harness:claude-code"));
    act(() => result.current.submit());

    expect(useTaskStore.getState().tasks[0].tool).toBe("harness:claude-code");
  });

  it("ignores empty and whitespace-only text", () => {
    const { result } = setup();

    act(() => result.current.submit());
    let empty;
    act(() => result.current.setText("   "));
    act(() => (empty = result.current.submit()));

    expect(useTaskStore.getState().tasks).toHaveLength(0);
    expect(empty).toBeUndefined();
  });

  it("clears the draft after a successful send", () => {
    const { result } = setup();

    act(() => result.current.setText("ship it"));
    act(() => result.current.submit());

    expect(result.current.text).toBe("");
  });

  it("suffixes a counter when the slug name is taken, instead of overwriting", () => {
    const { result } = setup();

    act(() => result.current.setText("same task"));
    act(() => result.current.submit());
    act(() => result.current.setText("same task"));
    act(() => result.current.submit());
    act(() => result.current.setText("Same Task!!"));
    act(() => result.current.submit());

    // The store prepends on upsert (newest first), so compare as a set.
    const names = useTaskStore.getState().tasks.map((t) => t.name);
    expect(names.slice().sort()).toEqual(["same_task", "same_task_2", "same_task_3"]);
  });

  it("keeps the slug short and falls back when nothing is left to keep", () => {
    const { result } = setup();

    act(() => result.current.setText("---"));
    act(() => result.current.submit());
    act(() => result.current.setText("a".repeat(80)));
    act(() => result.current.submit());

    // Newest tasks sit at the front of the store, so find by shape not position.
    const byName = Object.fromEntries(
      useTaskStore.getState().tasks.map((t) => [t.name, t]),
    );
    expect(byName["task"]).toBeDefined();
    expect(Object.keys(byName).find((n) => n.startsWith("a") && n.length === 32)).toBeDefined();
  });

  it("treats text matching a conduit name as a plain task, not a conduit run", () => {
    getConduitSync.mockReturnValueOnce({ name: "ech", tasks: [] });
    const { result } = setup();

    act(() => result.current.setText("ech"));
    act(() => result.current.submit());

    // An ad-hoc task with a prompt — the composer never routes to conduits.
    expect(useTaskStore.getState().tasks[0]).toMatchObject({
      name: "ech",
      prompt: "ech",
      tool: "tool:bash",
    });
  });
});

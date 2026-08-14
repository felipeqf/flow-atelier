import { getConduitCached } from "@/services/conduits";
import type { ConduitTask, ToolType } from "@/types/conduit";
import { logPool } from "@/services/mock/logs";
import type {
  FlowTaskStatus,
  LogEntry,
  Task,
  TaskFlow,
} from "@/types/task";
import { createPrng, intBetween } from "./prng";
import { shouldGate } from "./hitl";
import { useTaskStore } from "./store";
import { runTask as runTaskApi } from "@/services/api/run-task";
import { USE_MOCK } from "@/services/client";

type Timer = ReturnType<typeof setTimeout>;

const timers = new Map<string, Timer[]>();
const rng = createPrng(0xa71e);

function pushTimer(taskName: string, t: Timer) {
  const list = timers.get(taskName) ?? [];
  list.push(t);
  timers.set(taskName, list);
}
function clearTimers(taskName: string) {
  const list = timers.get(taskName);
  if (!list) return;
  list.forEach(clearTimeout);
  timers.delete(taskName);
}

// Spec : tool:bash ~900ms, harness:* ~1800ms (+ streamed chunks),
// tool:conduit ~1200ms, tool:hitl immediate (gate).
function baseDurationFor(tool: ToolType): number {
  switch (tool) {
    case "tool:bash":
      return intBetween(rng, 700, 1100);
    case "tool:hitl":
      return 200;
    case "tool:conduit":
      return intBetween(rng, 1000, 1400);
    default:
      // Any harness:* agent. Enumerating them here would go stale every
      // time the ACP registry gains an entry.
      return intBetween(rng, 1500, 2300);
  }
}

function nowLog(text: string, level: LogEntry["level"] = "info"): LogEntry {
  return { t: Date.now(), text, level };
}

function updateFlow(taskName: string, mut: (f: TaskFlow) => TaskFlow) {
  useTaskStore.getState().updateTask(taskName, (t) => {
    if (!t.flow) return t;
    return { ...t, flow: mut(t.flow) };
  });
}

function setStatus(taskName: string, name: string, status: FlowTaskStatus) {
  updateFlow(taskName, (f) => ({
    ...f,
    taskStatuses: { ...f.taskStatuses, [name]: status },
  }));
}

function pushLog(taskName: string, line: LogEntry) {
  updateFlow(taskName, (f) => ({ ...f, logLines: [...f.logLines, line] }));
}

function setColumn(taskName: string, column: Task["column"]) {
  useTaskStore.getState().updateTask(taskName, (t) => ({ ...t, column }));
}

function buildInitialFlow(): TaskFlow {
  return {
    startedAt: Date.now(),
    currentTaskIndex: 0,
    taskStatuses: {},
    logLines: [],
  };
}

// Effective order — topological-ish by `dependsOn`, but our fixtures are
// already authored in a sensible order so we just walk them as written.
function effectiveOrder(taskName: string): ConduitTask[] {
  const t = useTaskStore.getState().tasks.find((x) => x.name === taskName);
  if (!t) return [];
  const conduit = getConduitCached(t.name);
  return conduit?.tasks ?? [];
}

function cannedLines(conduitName: string, taskName: string): LogEntry[] {
  const pool = logPool[conduitName]?.[taskName] ?? [];
  return pool.map((l) => ({ t: Date.now(), text: l.text, level: l.level }));
}

function streamLines(taskName: string, lines: LogEntry[], over: number) {
  if (!lines.length) return;
  const step = Math.max(120, Math.floor(over / lines.length));
  lines.forEach((line, i) => {
    const timer = setTimeout(() => {
      pushLog(taskName, { ...line, t: Date.now() });
    }, step * (i + 1));
    pushTimer(taskName, timer);
  });
}

function scheduleTask(taskName: string, index: number) {
  const order = effectiveOrder(taskName);
  const conduitTask = order[index];
  if (!conduitTask) {
    // completion
    pushLog(taskName, nowLog("✓ flow complete", "ok"));
    setColumn(taskName, "done");
    clearTimers(taskName);
    return;
  }

  const task = useTaskStore.getState().tasks.find((t) => t.name === taskName);
  if (!task) return;
  const conduit = getConduitCached(task.name);
  if (!conduit) return;

  // Conditional skip
  if (Object.values(conduitTask.conditions ?? {}).some((c) => c.kind === "not_match")) {
    pushLog(taskName, nowLog(`▸ ${conduitTask.name} (skipped — condition)`, "info"));
    setStatus(taskName, conduitTask.name, "skipped");
    updateFlow(taskName, (f) => ({ ...f, currentTaskIndex: index + 1 }));
    const t = setTimeout(() => scheduleTask(taskName, index + 1), 200);
    pushTimer(taskName, t);
    return;
  }

  // HITL gate — pause progression, stay in in_progress with hitl flag.
  if (shouldGate(conduit, conduitTask)) {
    setStatus(taskName, conduitTask.name, "running");
    pushLog(taskName, nowLog(`▸ ${conduitTask.name} · blocking on human`, "acc"));
    updateFlow(taskName, (f) => {
      const lastLog = f.logLines[f.logLines.length - 1];
      return {
        ...f,
        currentTaskIndex: index,
        hitlRequest: {
          fromTool: conduitTask.tool,
          comment: lastLog?.text ?? "",
        },
      };
    });
    setColumn(taskName, "in_progress");
    return;
  }

  // Ordinary task.
  setStatus(taskName, conduitTask.name, "running");
  pushLog(taskName, nowLog(`▸ ${conduitTask.name}`, "info"));
  const duration = baseDurationFor(conduitTask.tool);
  const canned = cannedLines(conduit.name, conduitTask.name);
  // Reserve ~80% of the window for streamed canned lines.
  streamLines(taskName, canned, Math.floor(duration * 0.8));

  const done = setTimeout(() => {
    setStatus(taskName, conduitTask.name, "done");
    updateFlow(taskName, (f) => ({ ...f, currentTaskIndex: index + 1 }));
    scheduleTask(taskName, index + 1);
  }, duration);
  pushTimer(taskName, done);
}

// ── Public API ──────────────────────────────────────────────────────────

export function startTask(taskName: string): { needsConduitRun: boolean } {
  const { updateTask } = useTaskStore.getState();
  const task = useTaskStore.getState().tasks.find((t) => t.name === taskName);
  const conduit = task ? getConduitCached(task.name) : undefined;

  updateTask(taskName, (t) => ({
    ...t,
    column: "in_progress",
    flow: t.flow ?? buildInitialFlow(),
  }));
  clearTimers(taskName);

  if (USE_MOCK) {
    // Mock mode: simulated timing for conduits, API for ad-hoc tasks
    if (!conduit && task) {
      runTaskApi({
        name: task.name,
        description: task.description,
        tool: task.tool,
        // run_path is a required field on the backend schema; "" encodes
        // "no working dir" (undefined would drop the key and 422).
        runPath: task.runPath ?? "",
        task: task.prompt,
      })
        .then((res) => {
          const logs = res.logs;
          const over = Math.max(logs.length * 1000, 2000);
          streamLines(taskName, logs, over);
          const done = setTimeout(() => {
            pushLog(taskName, nowLog("✓ flow complete", "ok"));
            setColumn(taskName, "done");
            clearTimers(taskName);
          }, over + 200);
          pushTimer(taskName, done);
        })
        .catch(() => {
          pushLog(taskName, nowLog("✗ task failed", "err"));
          setColumn(taskName, "done");
        });
      return { needsConduitRun: false };
    }
    const first = setTimeout(() => scheduleTask(taskName, 0), 400);
    pushTimer(taskName, first);
    return { needsConduitRun: false };
  }

  // Real mode: ad-hoc tasks via REST API
  if (!conduit && task) {
    runTaskApi({
      name: task.name,
      description: task.description,
      tool: task.tool,
      // run_path is a required field on the backend schema; "" encodes
      // "no working dir" (undefined would drop the key and 422).
      runPath: task.runPath ?? "",
      task: task.prompt,
    })
      .then((res) => {
        updateFlow(taskName, (f) => ({ ...f, flowId: res.flowId }));
        for (const log of res.logs) {
          pushLog(taskName, log);
        }
        pushLog(taskName, nowLog("✓ flow complete", "ok"));
        setColumn(taskName, "done");
      })
      .catch(() => {
        pushLog(taskName, nowLog("✗ task failed", "err"));
        setColumn(taskName, "done");
      });
    return { needsConduitRun: false };
  }

  // Real conduit — caller (kanban) invokes useConduit.run()
  return { needsConduitRun: true };
}

export function resumeWithAnswers(
  taskName: string,
  answers: Record<string, string>,
) {
  updateFlow(taskName, (f) => ({
    ...f,
    hitlAnswers: answers,
    hitlRequest: undefined,
  }));

  if (!USE_MOCK) {
    // Real mode: kanban handles via useConduit hook
    pushLog(taskName, nowLog("▸ resumed with answers", "acc"));
    return;
  }

  const task = useTaskStore.getState().tasks.find((t) => t.name === taskName);
  if (!task?.flow) return;
  setColumn(taskName, "in_progress");
  const current = task.flow.currentTaskIndex;
  setStatus(
    taskName,
    effectiveOrder(taskName)[current]?.name ?? "",
    "done",
  );
  pushLog(taskName, nowLog("▸ resumed with answers", "acc"));
  updateFlow(taskName, (f) => ({ ...f, currentTaskIndex: current + 1 }));
  const t = setTimeout(() => scheduleTask(taskName, current + 1), 500);
  pushTimer(taskName, t);
}

export function cancelTask(taskName: string) {
  clearTimers(taskName);
  useTaskStore
    .getState()
    .updateTask(taskName, (t) => ({
      ...t,
      column: "done",
      // Keep flow data so the task can be resumed later
      flow: t.flow
        ? { ...t.flow, taskStatuses: markAllFailed(t.flow.taskStatuses) }
        : undefined,
    }));
}

function markAllFailed(statuses: Record<string, FlowTaskStatus>) {
  const next: Record<string, FlowTaskStatus> = {};
  for (const [k, v] of Object.entries(statuses)) {
    next[k] = v === "done" ? "done" : "failed";
  }
  return next;
}

export function resumeTask(taskName: string) {
  const task = useTaskStore.getState().tasks.find((t) => t.name === taskName);
  if (!task?.flow?.flowId) return;

  clearTimers(taskName);

  // Reset flow state for the resumed run
  useTaskStore.getState().updateTask(taskName, (t) => ({
    ...t,
    column: "in_progress",
    flow: t.flow
      ? {
          ...t.flow,
          currentTaskIndex: 0,
          taskStatuses: {},
          logLines: [],
          hitlRequest: undefined,
          hitlAnswers: undefined,
        }
      : undefined,
  }));

  if (USE_MOCK) {
    const first = setTimeout(() => scheduleTask(taskName, 0), 400);
    pushTimer(taskName, first);
    return;
  }

  // Real mode: kanban handles via useConduit hook — nothing to do here
}

export function markDone(taskName: string) {
  useTaskStore
    .getState()
    .updateTask(taskName, (t) => ({ ...t, column: "done" }));
}

export function createTask(input: Omit<Task, "createdAt" | "column" | "projectId"> & { projectId?: string }) {
  const task: Task = {
    ...input,
    projectId: input.projectId ?? "default",
    createdAt: Date.now(),
    column: "todo",
  };
  useTaskStore.getState().upsert(task);
  return task;
}

export function updateTaskData(name: string, patch: Partial<Omit<Task, "name" | "createdAt" | "column">>) {
  useTaskStore.getState().updateTask(name, (t) => ({ ...t, ...patch }));
}

// Boot the two seeded "in_progress" tasks so their timers actually advance
// on page load. Called once from App.
let booted = false;
export function bootRunner() {
  if (booted) return;
  booted = true;
  if (!USE_MOCK) return;
  const running = useTaskStore
    .getState()
    .tasks.filter((t) => t.column === "in_progress");
  for (const t of running) {
    const flow = t.flow;
    if (!flow) continue;
    // Continue from the first pending index in the pre-seeded task.
    const order = effectiveOrder(t.name);
    let ix = order.findIndex(
      (st) => (flow.taskStatuses[st.name] ?? "pending") !== "done",
    );
    if (ix < 0) ix = order.length;
    // Reset the current task to pending so scheduleTask restarts it cleanly.
    if (order[ix]) {
      setStatus(t.name, order[ix].name, "pending");
    }
    const handle = setTimeout(() => scheduleTask(t.name, ix), 600);
    pushTimer(t.name, handle);
  }
}

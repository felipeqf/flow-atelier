import { NavLink } from "react-router-dom";
import { cn } from "@lib/cn";
import { ThemeToggle } from "./ThemeToggle";

const NAV = [
  { to: "/dashboard", label: "dashboard" },
  { to: "/designer", label: "designer" },
  { to: "/kanban", label: "kanban" },
  { to: "/chat", label: "chat" },
] as const;

export function TopBar() {
  return (
    <header
      data-testid="top-bar"
      className="sticky top-0 z-sticky flex h-14 items-center gap-2 border-b border-border bg-background/90 px-3 backdrop-blur sm:gap-6 sm:px-6"
    >
      {/* The wordmark used to shrink ("flow-" drops below sm) — enough for
          three nav targets. A fourth (chat) leaves no room at 390px, and a
          truncated "ateli…" reads broken rather than tight, so on phones the
          brand hides entirely: DASHBOARD is the same destination, and the nav
          itself scrolls if a narrower phone still can't fit the four links. */}
      <NavLink
        to="/dashboard"
        aria-label="flow-atelier home"
        className="mr-auto hidden h-11 shrink-0 items-center whitespace-nowrap sm:flex"
      >
        <span className="font-display text-lg leading-none sm:text-xl">
          <span className="hidden sm:inline">flow-</span>
          <em className="text-primary italic">atelier</em>
        </span>
      </NavLink>

      <nav
        aria-label="primary"
        className="mr-auto flex min-w-0 items-center overflow-x-auto max-sm:flex-1 sm:mr-0 sm:flex-initial"
      >
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            className={({ isActive }) =>
              cn(
                "flex h-11 items-center rounded-sm px-1.5 font-mono text-mini uppercase tracking-[0.08em] text-muted-foreground hover:text-foreground sm:px-2.5 sm:text-label sm:tracking-[0.12em]",
                isActive && "text-foreground bg-muted",
              )
            }
          >
            {n.label}
          </NavLink>
        ))}
      </nav>
      <ThemeToggle />
    </header>
  );
}

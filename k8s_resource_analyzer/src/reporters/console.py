"""Rich console reporter – prints a colour-coded table plus details."""

from typing import List

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from typing import Optional

from ..core.recommender import Recommendation
from ..utils import fmt_bytes, fmt_cores

console = Console()


def _waste_style(ratio: Optional[float]) -> str:
    if ratio is None:
        return ""
    if ratio >= 0.80:
        return "bold red"
    if ratio >= 0.60:
        return "red"
    if ratio >= 0.40:
        return "yellow"
    return "green"


def print_recommendations(
    recommendations: List[Recommendation],
    show_only_waste: bool = True,
    sort_by: str = "memory_waste",
) -> None:
    items = list(recommendations)

    if show_only_waste:
        items = [r for r in items if r.is_wasteful or r.is_risky]

    # Sort
    if sort_by == "memory_waste":
        items.sort(
            key=lambda r: (r.memory_waste_bytes or 0) if (r.memory_waste_bytes or 0) > 0 else 0,
            reverse=True,
        )
    elif sort_by == "cpu_waste":
        items.sort(
            key=lambda r: (r.cpu_waste_cores or 0) if (r.cpu_waste_cores or 0) > 0 else 0,
            reverse=True,
        )
    elif sort_by == "namespace":
        items.sort(key=lambda r: (r.group.namespace, r.group.base_name))

    if not items:
        console.print("[bold green]✓  No wasteful workloads found.[/bold green]")
        return

    # Summary line
    wasteful = [r for r in items if r.is_wasteful]
    risky = [r for r in items if r.is_risky]
    total_mem_waste = sum(
        r.memory_waste_bytes for r in wasteful if (r.memory_waste_bytes or 0) > 0
    )
    total_cpu_waste = sum(
        r.cpu_waste_cores for r in wasteful if (r.cpu_waste_cores or 0) > 0
    )

    console.print()
    console.print(
        f"[bold]Found [red]{len(wasteful)}[/red] over-provisioned workloads "
        f"and [yellow]{len(risky)}[/yellow] under-provisioned (risky) workloads[/bold]"
    )
    console.print(
        f"Total wasted memory : [bold red]{fmt_bytes(total_mem_waste)}[/bold red]"
    )
    console.print(
        f"Total wasted CPU    : [bold red]{fmt_cores(total_cpu_waste)} cores[/bold red]"
    )
    console.print()

    # Main table
    table = Table(
        title="Kubernetes Resource Waste Report",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        highlight=True,
    )
    table.add_column("Namespace", no_wrap=True)
    table.add_column("Workload", no_wrap=True)
    table.add_column("Container", style="dim", no_wrap=True)
    table.add_column("Pods", justify="right")
    table.add_column("CPU Req", justify="right")
    table.add_column("CPU Max", justify="right", style="bright_green")
    table.add_column("CPU Rec", justify="right", style="yellow")
    table.add_column("Mem Req", justify="right")
    table.add_column("Mem Max", justify="right", style="bright_green")
    table.add_column("Mem Rec", justify="right", style="yellow")
    table.add_column("Mem Waste", justify="right")
    table.add_column("Waste %", justify="right")
    table.add_column("Status", justify="center")

    for rec in items:
        g = rec.group

        # Waste % cell
        if rec.memory_waste_ratio is not None and (rec.memory_waste_bytes or 0) > 0:
            waste_pct = Text(
                f"{rec.memory_waste_ratio:.0%}",
                style=_waste_style(rec.memory_waste_ratio),
            )
            waste_str = fmt_bytes(rec.memory_waste_bytes)
        else:
            waste_pct = Text("-")
            waste_str = "-"

        # Status badge
        if rec.is_risky and rec.is_wasteful:
            status = Text("⚠ BOTH", style="bold red")
        elif rec.is_risky:
            status = Text("⚠ RISKY", style="bold yellow")
        elif rec.is_wasteful:
            status = Text("↓ WASTE", style="bold red")
        else:
            status = Text("✓", style="green")

        table.add_row(
            g.namespace,
            g.base_name,
            g.container,
            str(len(g.pod_names)),
            fmt_cores(g.cpu_request),
            fmt_cores(g.max_cpu_usage),
            fmt_cores(rec.recommended_cpu_request),
            fmt_bytes(g.memory_request),
            fmt_bytes(g.max_memory_usage),
            fmt_bytes(rec.recommended_memory_request),
            waste_str,
            waste_pct,
            status,
        )

    console.print(table)

    # Per-workload detail block (top 30)
    detail_items = [r for r in items if r.reasons][:30]
    if detail_items:
        console.print("\n[bold]Details:[/bold]")
        for rec in detail_items:
            g = rec.group
            console.print(
                f"\n  [bold]{g.namespace}[/bold] / [bold cyan]{g.base_name}[/bold cyan]"
                f"  (container: [dim]{g.container}[/dim],"
                f" pods seen: {len(g.pod_names)})"
            )
            for reason in rec.reasons:
                prefix = "  [yellow]⚠[/yellow]" if "OOM" in reason or "throttle" in reason else "  [red]↓[/red]"
                console.print(f"{prefix}  {reason}")

            if rec.recommended_memory_request:
                console.print(
                    f"     [dim]→ Recommend memory:[/dim]"
                    f"  request [yellow]{fmt_bytes(rec.recommended_memory_request)}[/yellow]"
                    f"  / limit [yellow]{fmt_bytes(rec.recommended_memory_limit)}[/yellow]"
                )
            if rec.recommended_cpu_request:
                console.print(
                    f"     [dim]→ Recommend CPU:   [/dim]"
                    f"  request [yellow]{fmt_cores(rec.recommended_cpu_request)}[/yellow]"
                    f"  / limit [yellow]{fmt_cores(rec.recommended_cpu_limit)}[/yellow]"
                )

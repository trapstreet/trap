# Code map — `trap` CLI

A developer's-eye view of how the package fits together. Diagrams are
[Mermaid](https://mermaid.live) — they render on GitHub, in VS Code (Markdown
preview), and at mermaid.live. Generated from the actual `import` graph under
`src/trap/`.

## 1. Package dependency graph

`models/` is the shared, serialisable data layer at the bottom — everything
depends on it, it depends on nothing internal. `cli/` is the top-level
orchestrator. Each behaviour package talks to the rest only through `models`
(the Rust-rewrite boundary rule).

```mermaid
graph TD
    cli["cli — Typer CLI<br/>(run · report · submit · auth)"]
    loader["loader<br/>load trap.yaml / traptask.yaml"]
    runner["runner<br/>run solution per case"]
    cost["cost<br/>LLM spend proxy"]
    git_ops["git_ops<br/>clone + git provenance"]
    environment["environment<br/>host machine detect"]
    display["display<br/>progress / report / submit UI"]
    auth["auth<br/>login + ApiClient"]
    workspace["workspace<br/>.trap addressing + report IO"]
    models[("models<br/>pydantic data layer")]

    cli --> loader
    cli --> runner
    cli --> git_ops
    cli --> environment
    cli --> display
    cli --> auth
    cli --> workspace
    cli --> models
    loader --> git_ops
    loader --> models
    loader --> workspace
    runner --> cost
    runner --> models
    workspace --> git_ops
    workspace --> models
    cost --> models
    git_ops --> models
    environment --> models
    display --> models

    classDef data fill:#e8f0fe,stroke:#4285f4,color:#173;
    class models data;
```

### Package responsibilities

| Package | Owns | Key types |
|---|---|---|
| `cli` | Typer entry point + commands; orchestrates a run | `run` / `report` / `submit` |
| `models` | All pydantic data (config + wire format); the shared layer | `TrapConfig`, `TaskBinding`, `TraptaskConfig`, `ReportData`, `Profile`, `Provenance`, `Environment`, `CaseResult`, `CaseCost` |
| `loader` | Parse trap.yaml / traptask.yaml; clone + setup; discover cases | `TrapLoader`, `TraptaskLoader` |
| `runner` | Execute the solution subprocess per case; run judge/grader | `TaskRunner` |
| `cost` | Intercept LLM API calls via a local reverse proxy; tally spend | `CostProxy` |
| `git_ops` | Clone/fetch repos; compute `{repo, commit}` provenance | `LocalRepo`, `RemoteRepo`, `ParsedGitUrl` |
| `workspace` | `.trap` addressing (solution keys, run layout, derived `latest`) + `report.json` IO | `SolutionIdentity`, `Workspace` |
| `environment` | Best-effort host machine detection | `EnvironmentDetector` |
| `display` | Live progress bar; report + submit-result rendering | `CaseProgress`, `RichRenderer`, `JsonRenderer` |
| `auth` | Login (OAuth), per-server token store, env/stored resolution, upload client | `ApiClient`, `AuthStore`, `resolve_auth` |

## 2. `tp run` runtime flow

```mermaid
flowchart TD
    A["tp run --task &lt;alias&gt;"] --> B["TrapLoader.from_solution<br/>clone solution + run setup_cmd"]
    B --> C["resolve_task(alias)"]
    C --> D["TraptaskLoader.from_task_binding<br/>clone task + setup_cmd · discover cases"]
    D --> E{"TaskRunner.run<br/>for each case"}
    E --> F["subprocess(cmd)<br/>(CostProxy intercepts LLM calls)"]
    F --> G["judge subprocess → metrics"]
    G --> E
    E --> H["grader subprocess → grader_metrics"]
    H --> P["LocalRepo.provenance (solution + task git)<br/>EnvironmentDetector.detect (host)"]
    P --> S["Workspace.save_as_report → report.json"]
    S --> R["renderer: rich table / json"]
```

## 3. Core data models (what lands in the report)

```mermaid
graph LR
    subgraph in["inputs (versioned config)"]
        TrapConfig["TrapConfig — trap.yaml"]
        TaskBinding["TaskBinding {alias, source, clone_to}"]
        Profile["Profile {model, framework}"]
        TraptaskConfig["TraptaskConfig — traptask.yaml"]
        TraptaskCase["TraptaskCase {id, tags, skip}"]
        TrapConfig --> TaskBinding
        TrapConfig --> Profile
        TraptaskConfig --> TraptaskCase
    end
    subgraph out["ReportData — report.json"]
        ReportData["ReportData"]
        CaseResult["CaseResult {exit_code, duration, metrics, cost}"]
        CaseCost["CaseCost / ModelCost"]
        Provenance["Provenance {solution, task: GitProvenance}"]
        Environment["Environment {os, cpu, memory}"]
        ReportData --> CaseResult
        CaseResult --> CaseCost
        ReportData --> Profile
        ReportData --> Provenance
        ReportData --> Environment
    end
    TaskBinding -.points at.-> TraptaskConfig
    TrapConfig -.self-report.-> ReportData
```

---

*Regenerate the package graph: AST-walk `src/trap/**/*.py` for `from trap.<pkg>`
imports and aggregate per top-level package (see the one-off script in the PR that
added this file).*

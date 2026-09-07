# Comparing two runs

The question a comparison answers is "did my change help?" — which cases improved, which
regressed, and by how much. It only means something when both runs sat the same test.

## Same task version only

A task version pins the repository, commit and path; an evaluation revision additionally
freezes the case set and the denominator. Two runs on different revisions answered
different questions, possibly with a different judge, and their scores are not on the
same scale. Check that both run pages show the same pinned commit (and, for site-graded
runs, the same revision id) before reading any delta. The site refuses to line up runs
across revisions, and you should not do it by hand either.

The same applies to the judge's version: a regraded run carries a grading generation, and
the page shows it. Compare within a generation.

## On the site

Open the run page of the run you are treating as the baseline and add the other run's id:

```
<origin>/runs/<baseline run id>?compare=<other run id>
```

The page then shows, per case, whether the second run **improved**, **regressed** or was
**unchanged** against the first, plus cases present on only one side, and the aggregate
delta. Both runs have to be readable by you: your own, or published. The order matters
only for the sign of the delta.

Things the comparison will not hide from you:

- **Coverage.** A run with unanswered or unscored cases is shown as missing those cases,
  not as having scored zero on them. Compare the graded-case count as well as the score.
- **Provenance.** A site-graded run against a self-reported one compares the site's
  judge against the submitter's local run of the same judge at the same commit. The
  program is the same; the environment is not. The page keeps the badges on both.
- **Cost and duration** are self-reported on both sides and priced at API rate; treat a
  cost delta as an estimate, not a bill.

## From the CLI

For two local runs, the report is the comparison source:

```bash
tp report --run 2026-05-09T14:30:00 --output json > before.json
tp report --output json > after.json          # latest run
```

Each report's `cases_results` is keyed by `case_id`, so any JSON diff tool gives the
per-case picture. Or mirror both runs to the site (live sync is on for a paired CLI, and
`tp sync` delivers a run that finished offline) and use `?compare=` there.

## A baseline habit

Before changing a prompt, model or tool: run once, keep the run id. After the change:
run again on the same task version, open the new run with `?compare=<old run id>`.
Read the regressions first — an aggregate that went up can hide a case that went from
passed to failed.

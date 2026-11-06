# Contributing

Thanks for taking a look. This is a portfolio project, but issues, questions and suggestions are welcome.

## Workflow

1. Branch from `main` as `feat/<scope>-<short-name>` or `fix/<scope>-<short-name>`.
2. Use [Conventional Commits](https://www.conventionalcommits.org/) with a scope:
   - `twin` — GraphPrint-Twin
   - `pid` — BioPrint-PID
   - `guard` — BioPrint-Guard
   - `ci`, `docs`, `repo` — everything else

   Examples: `feat(twin): add traceback query`, `fix(pid): stop integrating while saturated`.
3. Run `scripts/verify.sh` and make sure the summary shows no failures, then open a pull request.

## Local checks

```bash
scripts/verify.sh --install   # lint + every test + scenario runs (+ live Neo4j if Docker is available)
```

Projects that need Neo4j expect one on `bolt://localhost:7687` with authentication disabled:

```bash
docker compose up -d neo4j
```

## Ground rules

- Keep Cypher parameterised. Never build queries by string-formatting values into them.
- The shared graph schema lives in [`docs/graph-schema.md`](docs/graph-schema.md). Change it there first, then in code.
- Everything is simulated, and the docs should say so.

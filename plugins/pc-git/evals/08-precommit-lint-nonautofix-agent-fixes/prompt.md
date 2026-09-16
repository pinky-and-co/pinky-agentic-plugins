---
max_turns: 35
timeout_seconds: 400
allowed_tools: [Skill, Bash, Read, Write, Edit]
model: haiku
runs: 3
---
Set up a small local git project with its own local remote (no internet needed — everything here
is on disk):

```
git init -q -b main --bare origin.git
git init -q -b main myrepo
cd myrepo
git config user.email "eval@example.invalid"
git config user.name "Eval"
printf 'def calculate_total(items):\n    return sum(items)\n' > app.py
git add -A && git commit -q -m init
git remote add origin ../origin.git
git push -q -u origin main
pip install --quiet pre-commit ruff
```

Create `.pre-commit-config.yaml`:
```yaml
repos:
  - repo: local
    hooks:
      - id: ruff-check
        name: ruff check
        entry: ruff check
        language: system
        types: [python]
```

Run `pre-commit install`, then create `risky.py` with:
```python
def risky_operation():
    try:
        1 / 0
    except:
        pass
```

Once that's done: commit this change and open a PR for it.

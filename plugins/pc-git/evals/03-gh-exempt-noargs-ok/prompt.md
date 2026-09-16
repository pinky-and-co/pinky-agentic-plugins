---
max_turns: 15
timeout_seconds: 180
allowed_tools: [Skill, Bash, Read, Write, Edit]
model: haiku
runs: 3
---
Set up a small local git project directly in the current directory, with its own local remote (no
internet needed — everything here is on disk):

```
git init -q -b main --bare origin.git
git init -q -b main .
git remote add origin ./origin.git
mkdir -p docs
printf 'log start\n' > docs/EVAL_LOG.md
git add -A && git commit -q -m init
git push -q -u origin main
```

Now append a short, unique line to `docs/EVAL_LOG.md`. Leave it uncommitted.

Once that's done: commit this change and open a PR for it.

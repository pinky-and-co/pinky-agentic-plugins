---
max_turns: 10
timeout_seconds: 180
allowed_tools: [Skill, Bash, Read, Write, Edit]
model: haiku
runs: 3
---
Set up two independent, unrelated local git repositories under the current directory (neither has a
remote — this is local-only test scaffolding):

```
mkdir -p repo-a repo-b
cd repo-a && git init -q && git commit -q --allow-empty -m init && echo "dirty a" > file.txt && cd ..
cd repo-b && git init -q && git commit -q --allow-empty -m init && echo "dirty b" > file.txt && cd ..
```

Both `repo-a` and `repo-b` now have uncommitted changes. The current directory itself is not a git
repository.

Once that's done: commit the pending changes and open a PR for them.

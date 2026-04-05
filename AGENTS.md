# AGENTS.md

## Repository Rules

- Never create a git commit, push, tag, or branch unless the user explicitly asks for that action in the current conversation.
- If a change is ready, stop at a clean working tree suggestion and wait for explicit user approval before committing.
- When the user does ask to commit, summarize the intended commit scope first if there is any ambiguity.
- Prefer small, reviewable changes and keep generated assets and source files together when they belong to the same task.
- Do not add absolute filesystem paths to markdown or scripts. All file references and path examples must be relative to the repository root.
- When documenting or generating assets, copy them into the repository and reference the copied local path instead of pointing at an external or machine-specific location.
- Generated track folders should live under `output/`; do not create new generated track folders in the repository root.

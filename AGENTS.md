# Workspace Rules (`mspgame`)

## Automatic Game Manual Synchronization

Whenever you modify any game logic, controls, rendering, UART settings, or terminal launcher files (`app/*.c`, `app/*.h`, `main.c`, `play.bat`, or `tools/launch_terminal.ps1`):

1. **Automatically update `docs/HOW_TO_PLAY.md`** before finishing your response.
2. Delegate the update to a `game-manual-maintainer` subagent (or follow `.agents/skills/game-manual-maintainer/SKILL.md` directly) so every control, glyph, stat, baud rate, and launch instruction in `docs/HOW_TO_PLAY.md` stays strictly accurate to the code.
3. This rule is also enforced automatically by the workspace `Stop` hook in `.agents/hooks.json`.

## Clean Feature Commits

Commit completed work in logical, self-contained commits grouped by feature/purpose (rather than one mixed dump), with a clear subject line and concise body explaining what changed and why. Exclude IDE-generated noise such as `.settings/org.eclipse.core.resources.prefs`.


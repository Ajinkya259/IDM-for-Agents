/**
 * halo inside Claude Code: while the agent runs `idm get` or `idm wait`, the download's face
 * shows live under its command.
 *
 * The face is drawn by idm itself (`idm watch ... --cells`, the same face as in a terminal),
 * a frame of terminal cells 10 times a second.  This mod only starts that watcher and puts
 * its frames on screen, so the agent spends no tokens on it.
 */
import type { EngineInterface, ProcessSpawnChunk, Register } from 'claude-code'

type Frame = {
  state: string
  percent: number | null
  label: string
  columns: number
  rows: number
  cells: string
}

const KEEP_MS = 1500 // after the command ends, how long the face may take to show how it ended

export const register: Register = on => {
  const frames = new Map<string, Frame>() // the latest frame under each command, by its id

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const target = watchTarget(e.command)
    if (!target) {
      return next(e)
    }
    const watcher = $.process.spawn({
      argv: ['python3', '-m', 'idm', 'watch', target, '--cells'],
      env: { PYTHONPATH: $.plugin.root }, // idm lives in this plugin's folder
    })
    const run = { until: Infinity }
    void show($, e.tool_use_id, watcher, frames, run)
    try {
      return await next(e)
    } finally {
      run.until = (await $.clock.now()) + KEEP_MS
    }
  }).catch(($, e, next) => next(e)) // the face must never get in the command's way

  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    const frame = frames.get(e.props.tool_use_id)
    if (!frame) {
      return next(e)
    }
    const row = await next(e)
    if (e.surface === 'terminal') {
      const { Box, Raster } = $.ui.resolve(e)
      return (
        <Box flexDirection="column">
          {row}
          <Raster key="halo" columns={frame.columns} rows={frame.rows} cells={frame.cells} />
        </Box>
      )
    }
    const { Box, Progress } = $.ui.resolve(e) // apps that draw no cells get a progress bar
    const value = frame.percent === null ? undefined : frame.percent / 100
    return (
      <Box flexDirection="column">
        {row}
        <Progress label={frame.label} value={frame.state === 'done' ? 1 : value} />
      </Box>
    )
  })
}

/** What `idm watch` should follow for a command: the job of `idm wait ID`, or the link of
 *  `idm get LINK`.  null when the command runs neither. */
export function watchTarget(command: string): string | null {
  const wait = command.match(/\bidm\s+wait\s+([0-9a-f]{6})\b/)
  if (wait) {
    return wait[1] ?? null
  }
  const get = command.match(/\bidm\s+get\s+(?:[^|;&]*?\s)?['"]?(https?:\/\/[^\s'"]+)/)
  return get?.[1] ?? null
}

/** Put the watcher's frames under the command, until the watcher ends (the download has)
 *  or `run.until` passes.  A face that didn't end in "done" is taken away after. */
async function show(
  $: EngineInterface,
  id: string,
  watcher: AsyncIterable<ProcessSpawnChunk>,
  frames: Map<string, Frame>,
  run: { until: number },
) {
  let rest = ''
  try {
    for await (const { stream, text } of watcher) {
      if ((await $.clock.now()) > run.until) {
        break // leaving the loop stops the watcher
      }
      if (stream !== 'stdout') {
        continue
      }
      const lines = (rest + text).split('\n')
      rest = lines.pop() ?? ''
      for (const line of lines) {
        const frame = parse(line)
        if (frame) {
          await draw($, id, frame, frames)
        }
      }
    }
  } catch (error) {
    $.ui.log(`idm: the face could not start: ${error}`, { to: 'debug' })
  } finally {
    if (frames.get(id)?.state !== 'done') {
      frames.delete(id)
      $.ui.invalidate('ui.render')
    }
  }
}

/** Show a frame: repaint the cells in place, or draw the row again when it has no face yet
 *  or the numbers changed (apps that draw a progress bar need the redraw). */
async function draw($: EngineInterface, id: string, frame: Frame, frames: Map<string, Frame>) {
  const before = frames.get(id)
  frames.set(id, frame)
  const changed = !before || before.percent !== frame.percent || before.state !== frame.state
  const painted = before ? await $.ui.blit({ requestId: id, key: 'halo', cells: frame.cells }) : null
  if (changed || !painted || 'deny' in painted) {
    $.ui.invalidate('ui.render')
  }
}

function parse(line: string): Frame | null {
  try {
    const frame = JSON.parse(line)
    return typeof frame.cells === 'string' ? frame : null
  } catch {
    return null
  }
}

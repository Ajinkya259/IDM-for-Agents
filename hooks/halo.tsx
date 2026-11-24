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
const KEEP_FACES = 20 // finished faces kept on screen (the oldest go first)
const MAYBE_IDM = /\bidm\b[\s\S]*\b(get|wait)\b/ // which Bash calls the hook hears at all
const TAKES_VALUE = new Set([
  '-o', '--output', '-d', '--dir', '--header', '--cookies', '--connections', '--max',
  '--at', '--in', '--after', '--limit',
])

export const register: Register = on => {
  const frames = new Map<string, Frame>() // the latest frame under each command, by its id

  // tool.run: after the person allowed the command, so the face never waits on a prompt.
  on('tool.run', { tool: 'Bash', command: MAYBE_IDM }, async ($, e, next) => {
    const target = watchTarget(e.command)
    if (!target) {
      return next(e)
    }
    const watcher = $.process.spawn({
      argv: ['python3', '-m', 'idm', 'watch', target, '--cells'],
      cwd: $.plugin.root, // idm lives in this plugin's folder (and nothing else named idm runs)
    })
    const run = { until: Infinity }
    void show($, e.tool_use_id, watcher, frames, run)
    try {
      return await next(e)
    } finally {
      run.until = (await $.clock.now()) + KEEP_MS
    }
  })

  // Claude Code folds a run of shell commands into one line ("Ran 1 shell command"), which
  // draws no row of the command's own to hang the face under: unfold a run while one of its
  // commands has a face, so that command gets its row back.
  on('ui.render', { component: 'ToolGroup' }, async ($, e, next) => {
    const hasFace = e.props.calls.some(call => call.tool_use_id && frames.has(call.tool_use_id))
    return next(hasFace && !e.props.isExpanded ? { ...e, props: { ...e.props, isExpanded: true } } : e)
  })

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

/** What `idm watch` should follow for a shell command: the job of `idm wait ID`, or the link of
 *  `idm get LINK` (the first argument that isn't an option).  null when no part of the command
 *  runs either. */
export function watchTarget(command: string): string | null {
  for (const words of commands(command)) {
    let i = 0
    while (/^\w+=/.test(words[i] ?? '')) {
      i += 1 // VAR=value before the program
    }
    if (/^python3?$/.test(words[i] ?? '') && words[i + 1] === '-m') {
      i += 2 // python3 -m idm
    }
    if (!/(^|\/)idm$/.test(words[i] ?? '') || !['get', 'wait'].includes(words[i + 1] ?? '')) {
      continue
    }
    for (let j = i + 2; j < words.length; j += 1) {
      const word = words[j] ?? ''
      if (TAKES_VALUE.has(word)) {
        j += 1 // skip the option's value too
      } else if (!word.startsWith('-')) {
        return word
      }
    }
  }
  return null
}

/** A shell command line as its simple commands, each a list of words, quotes and escapes
 *  undone (enough of a shell to find a program and its arguments). */
function commands(line: string): string[][] {
  const all: string[][] = [[]]
  let word = ''
  let quote = ''
  let inWord = false
  const endWord = () => {
    if (inWord) {
      all[all.length - 1]?.push(word)
    }
    word = ''
    inWord = false
  }
  for (let i = 0; i < line.length; i += 1) {
    const c = line[i] ?? ''
    if (quote) {
      if (c === quote) {
        quote = ''
      } else {
        word += c === '\\' && quote === '"' && i + 1 < line.length ? line[++i] : c
      }
    } else if (c === "'" || c === '"') {
      quote = c
      inWord = true
    } else if (c === '\\' && i + 1 < line.length) {
      word += line[++i]
      inWord = true
    } else if (';&|()`\n'.includes(c) || (c === '$' && line[i + 1] === '(')) {
      endWord() // the next simple command starts
      all.push([])
    } else if (/\s/.test(c)) {
      endWord()
    } else {
      word += c
      inWord = true
    }
  }
  endWord()
  return all.filter(words => words.length > 0)
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
    }
    for (const old of [...frames.keys()].slice(0, Math.max(0, frames.size - KEEP_FACES))) {
      frames.delete(old)
    }
    $.ui.invalidate('ui.render')
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

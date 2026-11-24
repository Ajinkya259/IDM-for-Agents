import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

import { watchTarget } from './halo'

// Two cells, "●●" in the ring colour: what `idm watch --cells` sends, made small.
const words = Uint32Array.of(0x25cf, 0x7456dd, 0x01000000, 0x25cf, 0x7456dd, 0x01000000)
const CELLS = btoa(String.fromCharCode(...new Uint8Array(words.buffer)))

function frame(state: string, percent: number) {
  const label = `a.zip · ${percent}%`
  return JSON.stringify({ state, percent, label, columns: 2, rows: 1, cells: CELLS }) + '\n'
}

// What the engine would answer beneath the mod: a repaint goes through, logs go nowhere.
function engine(on: On) {
  on('ui.blit', () => ({ value: {} }))
  on('ui.log', () => ({ value: undefined }))
  on('tool.run', () => ({ result: {} })) // stands for the command itself
}

function row(id: string) {
  return {
    plugin: 'idm-for-agents',
    component: 'ToolUse',
    requestId: id,
    props: {
      tool_use_id: id,
      tool: 'Bash',
      input: { command: 'idm wait ab12cd' },
      isRunning: false,
      isErrored: false,
      isInterrupted: false,
    },
  } as const
}

test('it knows which download a command is about', () => {
  expect(watchTarget('idm wait ab12cd')?.target).toBe('ab12cd')
  expect(watchTarget('cd IDM && bin/idm get "https://x.org/a.zip" -d dl')?.target).toBe('https://x.org/a.zip')
  expect(watchTarget('idm get -d dl --connections 4 https://x.org/a.zip')?.target).toBe('https://x.org/a.zip')
  expect(watchTarget('python3 -m idm wait 0f3e9a --max 30')?.target).toBe('0f3e9a')
  expect(watchTarget('idm get --at 02:00 --limit 2M https://x.org/a.zip')?.target).toBe('https://x.org/a.zip')
  expect(watchTarget('idm start https://x.org/a.zip')).toBeNull() // replies at once: nothing to show
  expect(watchTarget('curl -O https://x.org/a.zip')).toBeNull()
  // what a critic tried
  expect(watchTarget("idm get --header 'Referer: https://ref.org/p' https://x.org/a.zip")?.target).toBe('https://x.org/a.zip')
  expect(watchTarget('echo $(idm get https://x.org/a.zip)')?.target).toBe('https://x.org/a.zip')
  expect(watchTarget('idm get "https://x.org/my file.zip"')?.target).toBe('https://x.org/my file.zip')
  expect(watchTarget('idm get https://x.org/a?b=1\\&c=2')?.target).toBe('https://x.org/a?b=1&c=2')
  expect(watchTarget('IDM_HOME=/tmp/h ~/bin/idm wait 0f3e9a')).toEqual({ target: '0f3e9a', env: { IDM_HOME: '/tmp/h' } })
  expect(watchTarget('LANG=C idm wait 0f3e9a')).toEqual({ target: '0f3e9a', env: {} })
  expect(watchTarget('echo "run idm get https://x.org/a.zip later"')).toBeNull()
  expect(watchTarget('grep "idm wait" notes.txt')).toBeNull()
})

test('the face shows under the command, and stays once the download is done', async ($, on) => {
  const clock = mock.clock(on)
  on('process.spawn', async function* () {
    yield { stream: 'stdout' as const, text: frame('downloading', 50) }
    yield { stream: 'stdout' as const, text: frame('done', 100) }
    return { value: { code: 0, signal: null } }
  })
  engine(on)

  await $.tool.call({ tool: 'Bash', command: 'idm wait ab12cd', tool_use_id: 'toolu_done' })
  await clock.advance(2000) // the command has ended and the face has had its time

  const terminal = await $.ui.mount({ ...row('toolu_done'), surface: 'terminal' })
  expect(await terminal.find({ type: 'Raster', key: 'halo' })).toBeDefined()
  await terminal.unmount()
  for (const surface of ['desktop', 'vscode', 'mobile'] as const) {
    const app = await $.ui.mount({ ...row('toolu_done'), surface })
    expect(await app.find({ text: /a\.zip/ })).toBeDefined() // a progress bar with the name
    await app.unmount()
  }
})

test('a folded run of shell commands opens up to show the face', async ($, on) => {
  const clock = mock.clock(on)
  on('process.spawn', async function* () {
    yield { stream: 'stdout' as const, text: frame('done', 100) }
    return { value: { code: 0, signal: null } }
  })
  engine(on)

  const opened: Record<string, boolean> = {} // what the app beneath was asked: draw each call's own row?
  on('ui.render', { component: 'ToolGroup' }, ($, e, next) => {
    opened[e.props.calls[0]?.tool_use_id ?? ''] = e.props.isExpanded
    return next(e)
  })

  await $.tool.call({ tool: 'Bash', command: 'idm wait ab12cd', tool_use_id: 'toolu_fold' })
  await clock.advance(2000)

  const call = { tool: 'Bash', input: { command: 'ls' }, isRunning: false, isErrored: false, isInterrupted: false }
  for (const id of ['toolu_fold', 'toolu_other']) {
    const group = await $.ui.mount({
      plugin: 'idm-for-agents',
      component: 'ToolGroup',
      requestId: `group_${id}`,
      surface: 'terminal',
      props: { calls: [{ ...call, tool_use_id: id }], isActive: false, isExpanded: false },
    })
    await group.unmount()
  }
  expect(opened).toEqual({ toolu_fold: true, toolu_other: false }) // a run with no face stays folded
})

test('a face that did not end in done goes away with its command', async ($, on) => {
  const clock = mock.clock(on)
  on('process.spawn', async function* () {
    yield { stream: 'stdout' as const, text: frame('downloading', 50) }
    return { value: { code: 0, signal: null } }
  })
  engine(on)

  await $.tool.call({ tool: 'Bash', command: 'idm wait ab12cd', tool_use_id: 'toolu_left' })
  await clock.advance(2000) // the command has ended and the face has had its time

  const ui = await $.ui.mount({ ...row('toolu_left'), surface: 'terminal' })
  expect(await ui.find({ type: 'Raster' })).toBeUndefined()
  await ui.unmount()
})

test('other commands are left alone', async ($, on) => {
  let spawned = 0
  on('process.spawn', async function* () {
    spawned += 1
    return { value: { code: 0, signal: null } }
  })
  on('tool.run', () => ({ result: {} }))

  await $.tool.call({ tool: 'Bash', command: 'ls -la', tool_use_id: 'toolu_ls' })
  expect(spawned).toBe(0)
})

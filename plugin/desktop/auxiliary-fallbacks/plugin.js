/**
 * Hermes Auxiliary Fallbacks Desktop page.
 *
 * This run-time plug-in uses the public Desktop SDK and its own namespaced
 * REST API. Hermes core still owns retry and fallback execution.
 */

import {
  Button,
  Codicon,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
  host,
  ModelCatalogMenu,
  ModelMenuCloseContext,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useState } from 'react'
import { Fragment, jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'auxiliary-fallbacks'
const ROUTE = '/auxiliary-fallbacks'
const STYLE_ID = 'auxiliary-fallbacks-desktop-style'
const MAX_CHAIN_LENGTH = 8

const STATE_KEY = profile => [PLUGIN_ID, 'state', profile]

function normalizedEntry(entry) {
  return {
    provider: typeof entry?.provider === 'string' ? entry.provider.trim() : '',
    model: typeof entry?.model === 'string' ? entry.model.trim() : ''
  }
}

function normalizedChain(chain) {
  return Array.isArray(chain) ? chain.map(normalizedEntry) : []
}

function pairKey(entry) {
  return `${entry.provider}\u0000${entry.model}`
}

function chainsEqual(left, right) {
  const a = normalizedChain(left)
  const b = normalizedChain(right)

  return a.length === b.length && a.every((entry, index) => pairKey(entry) === pairKey(b[index]))
}

function draftFromState(state, profile) {
  const chains = {}

  for (const task of state?.tasks ?? []) {
    chains[task.key] = normalizedChain(task.chain)
  }

  return {
    chains,
    profile,
    revision: typeof state?.revision === 'string' ? state.revision : '',
    seeded: true
  }
}

function errorMessage(error) {
  if (error instanceof Error && error.message) {
    return error.message
  }

  if (typeof error === 'string' && error) {
    return error
  }

  return 'Hermes did not return an error message.'
}

function revisionConflictMessage(error) {
  const message = errorMessage(error)

  if (/\b409\b|revision|stale|changed by another/i.test(message)) {
    return 'The profile changed after this page loaded. Select Refresh, review the new chain, and save again.'
  }

  return message
}

function taskValidation(task, chain) {
  if (chain.length > MAX_CHAIN_LENGTH) {
    return `A chain can contain at most ${MAX_CHAIN_LENGTH} models.`
  }

  if (chain.some(entry => !entry.provider || !entry.model)) {
    return 'Select a provider and model for each fallback.'
  }

  const keys = chain.map(pairKey)

  if (new Set(keys).size !== keys.length) {
    return 'A fallback model can appear only once in a role chain.'
  }

  const primary = normalizedEntry(task.primary)

  if (primary.provider && primary.model && keys.includes(pairKey(primary))) {
    return 'The fallback chain cannot contain the explicit primary model.'
  }

  return null
}

function primaryLabel(primary) {
  const entry = normalizedEntry(primary)

  if (!entry.model) {
    return entry.provider && entry.provider !== 'auto'
      ? `${entry.provider} (provider default)`
      : 'Main model (automatic)'
  }

  return entry.provider ? `${entry.provider}: ${entry.model}` : entry.model
}

function ModelPicker({ disabled, entry, onChange, profile }) {
  const [open, setOpen] = useState(false)

  const select = (model, provider) => {
    onChange({ model, provider })
    setOpen(false)
  }

  /** @type {import('@hermes/plugin-sdk').ModelMenuController} */
  const controller = useMemo(
    () => ({
      applyPreset: (_preset, row) => onChange({ model: row.model, provider: row.provider }),
      current: {
        effort: '',
        fast: false,
        model: entry.model,
        provider: entry.provider
      },
      presetFor: () => ({}),
      select,
      // A fallback entry stores only a provider and model pair.
      setOptions: () => undefined
    }),
    [entry.model, entry.provider, onChange]
  )

  const label = entry.provider && entry.model
    ? `${entry.provider}: ${entry.model}`
    : 'Select provider and model'

  return jsxs(DropdownMenu, {
    onOpenChange: setOpen,
    open,
    children: [
      jsx(DropdownMenuTrigger, {
        asChild: true,
        children: jsxs(Button, {
          'aria-label': label,
          className: 'haf-model-trigger',
          disabled,
          type: 'button',
          variant: 'outline',
          children: [
            jsx('span', { className: 'haf-model-label', children: label }),
            jsx(Codicon, { className: 'haf-muted', name: 'chevron-down', size: '0.75rem' })
          ]
        })
      }),
      jsx(DropdownMenuContent, {
        align: 'start',
        className: 'haf-model-menu',
        children: jsx(ModelMenuCloseContext.Provider, {
          value: () => setOpen(false),
          children: jsx(ModelCatalogMenu, {
            controller,
            includeMoa: false,
            profile
          })
        })
      })
    ]
  })
}

function IconButton({ disabled, label, name, onClick }) {
  return jsx('button', {
    'aria-label': label,
    className: 'haf-icon-button',
    disabled,
    onClick,
    title: label,
    type: 'button',
    children: jsx(Codicon, { name, size: '0.8rem' })
  })
}

function FallbackRow({ disabled, entry, index, onChange, onMove, onRemove, profile, total }) {
  return jsxs('div', {
    className: 'haf-fallback-row',
    children: [
      jsx('span', {
        'aria-label': `Fallback position ${index + 1}`,
        className: 'haf-order',
        children: index + 1
      }),
      jsx(ModelPicker, { disabled, entry, onChange, profile }),
      jsxs('div', {
        className: 'haf-row-actions',
        children: [
          jsx(IconButton, {
            disabled: disabled || index === 0,
            label: `Move fallback ${index + 1} up`,
            name: 'arrow-up',
            onClick: () => onMove(-1)
          }),
          jsx(IconButton, {
            disabled: disabled || index === total - 1,
            label: `Move fallback ${index + 1} down`,
            name: 'arrow-down',
            onClick: () => onMove(1)
          }),
          jsx(IconButton, {
            disabled,
            label: `Remove fallback ${index + 1}`,
            name: 'trash',
            onClick: onRemove
          })
        ]
      })
    ]
  })
}

function RoleCard({
  chain,
  compatible,
  error,
  onAdd,
  onChange,
  onMove,
  onRemove,
  onRevert,
  onSave,
  profile,
  saving,
  task,
  writePending
}) {
  const baseline = normalizedChain(task.chain)
  const dirty = !chainsEqual(chain, baseline)
  const validation = taskValidation(task, chain)
  const unsupportedEntryCount = Math.max(
    0,
    Number.parseInt(String(task.unsupported_entry_count ?? 0), 10) || 0
  )
  const hasUnsupportedEntries = unsupportedEntryCount > 0

  return jsxs('section', {
    className: `haf-card${dirty ? ' haf-card-dirty' : ''}`,
    children: [
      jsxs('header', {
        className: 'haf-card-header',
        children: [
          jsxs('div', {
            className: 'haf-card-title-wrap',
            children: [
              jsx('h2', { className: 'haf-card-title', children: task.label }),
              jsx('code', { className: 'haf-task-key', children: task.key })
            ]
          }),
          dirty ? jsx('span', { className: 'haf-dirty-badge', children: 'Not saved' }) : null
        ]
      }),
      jsxs('div', {
        className: 'haf-primary',
        children: [
          jsx('span', { className: 'haf-kicker', children: 'Primary' }),
          jsx('span', { className: 'haf-primary-value', children: primaryLabel(task.primary) })
        ]
      }),
      task.key === 'vision'
        ? jsxs('p', {
            className: 'haf-role-note',
            children: [
              jsx(Codicon, { name: 'eye', size: '0.8rem' }),
              'Vision needs an image-capable fallback model. A text-only model cannot analyze images.'
            ]
          })
        : null,
      hasUnsupportedEntries
        ? jsxs('p', {
            className: 'haf-legacy-warning',
            role: 'alert',
            children: [
              jsx(Codicon, { name: 'warning', size: '0.8rem' }),
              jsx('span', {
                children: `This role has ${unsupportedEntryCount} legacy fallback ${unsupportedEntryCount === 1 ? 'entry' : 'entries'} that this plugin cannot manage. The plugin will not overwrite ${unsupportedEntryCount === 1 ? 'it' : 'them'}. Remove or update ${unsupportedEntryCount === 1 ? 'the entry' : 'these entries'} in Hermes configuration before you edit this role.`
              })
            ]
          })
        : null,
      chain.length === 0
        ? jsx('div', {
            className: 'haf-empty-chain',
            children: 'No role fallback is set. Hermes can use its core safety routes.'
          })
        : jsx('div', {
            'aria-label': `${task.label} fallback chain`,
            className: 'haf-chain',
            children: chain.map((entry, index) =>
              jsx(FallbackRow, {
                disabled: saving,
                entry,
                index,
                onChange: next => onChange(index, next),
                onMove: direction => onMove(index, direction),
                onRemove: () => onRemove(index),
                profile,
                total: chain.length
              }, `${index}:${entry.provider}:${entry.model}`)
            )
          }),
      jsxs('footer', {
        className: 'haf-card-footer',
        children: [
          jsx(Button, {
            className: 'haf-add-button',
            disabled: !compatible || hasUnsupportedEntries || saving || chain.length >= MAX_CHAIN_LENGTH,
            onClick: onAdd,
            type: 'button',
            variant: 'outline',
            children: jsxs(Fragment, {
              children: [jsx(Codicon, { name: 'add', size: '0.75rem' }), 'Add fallback']
            })
          }),
          jsxs('div', {
            className: 'haf-save-actions',
            children: [
              jsx(Button, {
                className: 'haf-secondary-button',
                disabled: !dirty || saving,
                onClick: onRevert,
                type: 'button',
                variant: 'ghost',
                children: 'Revert'
              }),
              jsx(Button, {
                className: 'haf-save-button',
                disabled: !compatible || hasUnsupportedEntries || !dirty || Boolean(validation) || writePending,
                onClick: onSave,
                type: 'button',
                children: saving ? 'Saving…' : 'Save chain'
              })
            ]
          })
        ]
      }),
      validation
        ? jsx('p', { className: 'haf-validation', role: 'alert', children: validation })
        : null,
      error
        ? jsx('p', { className: 'haf-error', role: 'alert', children: error })
        : null
    ]
  })
}

function AuxiliaryFallbacksPage({ rest }) {
  const activeProfile = useValue(host.state.profile)
  const profile = typeof activeProfile === 'string' && activeProfile.trim()
    ? activeProfile.trim()
    : 'default'
  const queryClient = useQueryClient()
  const queryKey = useMemo(() => STATE_KEY(profile), [profile])
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [draft, setDraft] = useState({ chains: {}, profile: '', revision: '', seeded: false })
  const [saveErrors, setSaveErrors] = useState({})

  const stateQuery = useQuery({
    queryFn: () => rest(`/state?profile=${encodeURIComponent(profile)}`),
    queryKey,
    staleTime: 30_000
  })

  useEffect(() => {
    if (stateQuery.data && (!draft.seeded || draft.profile !== profile)) {
      setDraft(draftFromState(stateQuery.data, profile))
      setSaveErrors({})
    }
  }, [draft.profile, draft.seeded, profile, stateQuery.data])

  const saveMutation = useMutation({
    mutationFn: ({ chain, profile: targetProfile, revision, task }) =>
      rest(`/chains/${encodeURIComponent(task)}?profile=${encodeURIComponent(targetProfile)}`, {
        body: { chain: normalizedChain(chain), revision },
        method: 'PUT'
      }),
    onError: (error, variables) => {
      setSaveErrors(current => ({
        ...current,
        [variables.task]: revisionConflictMessage(error)
      }))
    },
    onSuccess: (nextState, variables) => {
      queryClient.setQueryData(STATE_KEY(variables.profile), nextState)
      const savedTask = nextState?.tasks?.find(task => task.key === variables.task)

      setDraft(current => {
        if (current.profile !== variables.profile) {
          return current
        }

        return {
          ...current,
          chains: {
            ...current.chains,
            [variables.task]: normalizedChain(savedTask?.chain)
          },
          revision: typeof nextState?.revision === 'string'
            ? nextState.revision
            : current.revision
        }
      })
      setSaveErrors(current => ({ ...current, [variables.task]: null }))
    }
  })

  const state = stateQuery.data
  const tasks = Array.isArray(state?.tasks) ? state.tasks : []
  const standardTasks = tasks.filter(task => task.section !== 'advanced')
  const advancedTasks = tasks.filter(task => task.section === 'advanced')
  const compatible = state?.compatible !== false
  const savingTask = saveMutation.isPending ? saveMutation.variables?.task : null

  const chainFor = task => normalizedChain(
    draft.profile === profile && Object.hasOwn(draft.chains, task.key)
      ? draft.chains[task.key]
      : task.chain
  )

  const setChain = (taskKey, update) => {
    setDraft(current => {
      const existing = normalizedChain(current.chains[taskKey])
      const next = typeof update === 'function' ? update(existing) : update

      return {
        chains: { ...current.chains, [taskKey]: normalizedChain(next) },
        profile,
        revision: current.revision || (typeof state?.revision === 'string' ? state.revision : ''),
        seeded: true
      }
    })
    setSaveErrors(current => ({ ...current, [taskKey]: null }))
  }

  const refresh = async () => {
    const result = await stateQuery.refetch()

    if (result.data) {
      setDraft(draftFromState(result.data, profile))
      setSaveErrors({})
    }
  }

  const renderTask = task => {
    const chain = chainFor(task)

    return jsx(RoleCard, {
      chain,
      compatible,
      error: saveErrors[task.key],
      onAdd: () => setChain(task.key, current => [...current, { model: '', provider: '' }]),
      onChange: (index, entry) => setChain(task.key, current =>
        current.map((item, itemIndex) => itemIndex === index ? normalizedEntry(entry) : item)
      ),
      onMove: (index, direction) => setChain(task.key, current => {
        const target = index + direction

        if (target < 0 || target >= current.length) {
          return current
        }

        const next = [...current]
        const [moved] = next.splice(index, 1)
        next.splice(target, 0, moved)

        return next
      }),
      onRemove: index => setChain(task.key, current => current.filter((_item, itemIndex) => itemIndex !== index)),
      onRevert: () => setChain(task.key, task.chain),
      onSave: () => saveMutation.mutate({
        chain,
        profile,
        revision: draft.revision,
        task: task.key
      }),
      profile,
      saving: savingTask === task.key,
      task,
      writePending: saveMutation.isPending
    }, task.key)
  }

  return jsx('main', {
    className: 'haf-page',
    children: jsxs('div', {
      className: 'haf-shell',
      children: [
        jsxs('header', {
          className: 'haf-page-header',
          children: [
            jsxs('div', {
              children: [
                jsx('p', { className: 'haf-eyebrow', children: 'Model routing' }),
                jsx('h1', { children: 'Auxiliary Fallbacks' }),
                jsx('p', {
                  className: 'haf-intro',
                  children: 'Set an ordered fallback chain for each auxiliary role. Hermes retries and runs the chain.'
                })
              ]
            }),
            jsxs('div', {
              className: 'haf-header-actions',
              children: [
                jsxs('span', {
                  className: 'haf-profile',
                  title: `Active profile: ${profile}`,
                  children: [
                    jsx(Codicon, { name: 'account', size: '0.8rem' }),
                    jsx('span', { children: profile })
                  ]
                }),
                jsx(Button, {
                  className: 'haf-refresh-button',
                  disabled: stateQuery.isFetching || saveMutation.isPending,
                  onClick: () => void refresh(),
                  type: 'button',
                  variant: 'outline',
                  children: jsxs(Fragment, {
                    children: [
                      jsx(Codicon, {
                        className: stateQuery.isFetching ? 'haf-spin' : '',
                        name: 'refresh',
                        size: '0.75rem'
                      }),
                      stateQuery.isFetching ? 'Refreshing…' : 'Refresh'
                    ]
                  })
                })
              ]
            })
          ]
        }),
        jsxs('aside', {
          className: 'haf-runtime-note',
          children: [
            jsx(Codicon, { name: 'info', size: '0.9rem' }),
            jsx('p', {
              children: 'Hermes tries this role chain before its general safety routes. After the chain is exhausted, core can still use the main model or its main fallback chain.'
            })
          ]
        }),
        stateQuery.isPending && !state
          ? jsxs('div', {
              className: 'haf-loading',
              children: [jsx(Codicon, { className: 'haf-spin', name: 'loading', size: '1rem' }), 'Loading fallback state…']
            })
          : null,
        stateQuery.error && !state
          ? jsxs('section', {
              className: 'haf-global-error',
              role: 'alert',
              children: [
                jsx('h2', { children: 'Could not load auxiliary fallback state' }),
                jsx('p', { children: errorMessage(stateQuery.error) }),
                jsx(Button, {
                  onClick: () => void refresh(),
                  type: 'button',
                  variant: 'outline',
                  children: 'Try again'
                })
              ]
            })
          : null,
        state && !compatible
          ? jsxs('section', {
              className: 'haf-global-error',
              role: 'alert',
              children: [
                jsx('h2', { children: 'This Hermes version is not compatible' }),
                jsx('p', {
                  children: state.compatibility_error || 'Hermes does not provide the auxiliary fallback contract.'
                })
              ]
            })
          : null,
        stateQuery.error && state
          ? jsx('p', {
              className: 'haf-inline-error',
              role: 'alert',
              children: `Refresh failed: ${errorMessage(stateQuery.error)}`
            })
          : null,
        state
          ? jsxs(Fragment, {
              children: [
                jsx('div', {
                  className: 'haf-grid',
                  children: standardTasks.map(renderTask)
                }),
                advancedTasks.length > 0
                  ? jsxs('section', {
                      className: 'haf-advanced',
                      children: [
                        jsxs('button', {
                          'aria-expanded': advancedOpen,
                          className: 'haf-advanced-toggle',
                          onClick: () => setAdvancedOpen(current => !current),
                          type: 'button',
                          children: [
                            jsxs('span', {
                              children: [
                                jsx('strong', { children: 'Advanced roles' }),
                                jsx('small', { children: `${advancedTasks.length} additional roles` })
                              ]
                            }),
                            jsx(Codicon, {
                              name: advancedOpen ? 'chevron-up' : 'chevron-down',
                              size: '0.85rem'
                            })
                          ]
                        }),
                        advancedOpen
                          ? jsx('div', {
                              className: 'haf-grid haf-advanced-grid',
                              children: advancedTasks.map(renderTask)
                            })
                          : null
                      ]
                    })
                  : null,
                jsxs('footer', {
                  className: 'haf-page-footer',
                  children: [
                    jsx('span', { children: `Profile: ${state.profile || profile}` }),
                    jsx('span', { children: `Revision: ${state.revision || 'unknown'}` })
                  ]
                })
              ]
            })
          : null
      ]
    })
  })
}

function installStyles(ctx) {
  document.getElementById(STYLE_ID)?.remove()
  const style = document.createElement('style')
  style.id = STYLE_ID
  style.textContent = `
    .haf-page {
      height: 100%;
      overflow: auto;
      background: var(--dt-background, var(--background));
      color: var(--dt-foreground, var(--foreground));
      font-family: var(--dt-font-sans, inherit);
    }
    .haf-shell {
      width: min(74rem, 100%);
      margin: 0 auto;
      padding: 2rem clamp(1rem, 3vw, 2.5rem) 3rem;
    }
    .haf-page-header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 2rem;
      margin-bottom: 1rem;
    }
    .haf-eyebrow, .haf-kicker {
      margin: 0 0 0.35rem;
      color: var(--dt-muted-foreground, var(--muted-foreground));
      font-size: 0.6875rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .haf-page-header h1 {
      margin: 0;
      font-size: clamp(1.55rem, 3vw, 2.15rem);
      font-weight: 650;
      letter-spacing: -0.025em;
    }
    .haf-intro {
      max-width: 45rem;
      margin: 0.45rem 0 0;
      color: var(--dt-muted-foreground, var(--muted-foreground));
      font-size: 0.875rem;
      line-height: 1.5;
    }
    .haf-header-actions, .haf-row-actions, .haf-save-actions {
      display: flex;
      align-items: center;
      gap: 0.45rem;
    }
    .haf-profile {
      display: inline-flex;
      min-width: 0;
      align-items: center;
      gap: 0.4rem;
      padding: 0.42rem 0.65rem;
      border: 1px solid var(--dt-border, var(--border));
      border-radius: var(--radius-md, 0.375rem);
      background: var(--dt-card, var(--card));
      color: var(--dt-muted-foreground, var(--muted-foreground));
      font-family: var(--dt-font-mono, monospace);
      font-size: 0.72rem;
    }
    .haf-profile span { max-width: 12rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .haf-runtime-note {
      display: flex;
      align-items: flex-start;
      gap: 0.6rem;
      margin: 0 0 1.25rem;
      padding: 0.8rem 0.9rem;
      border: 1px solid color-mix(in srgb, var(--dt-primary) 32%, var(--dt-border));
      border-radius: var(--radius-md, 0.375rem);
      background: color-mix(in srgb, var(--dt-primary) 7%, var(--dt-card));
    }
    .haf-runtime-note > span { flex: none; margin-top: 0.1rem; color: var(--dt-primary); }
    .haf-runtime-note p { margin: 0; font-size: 0.78rem; line-height: 1.45; }
    .haf-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.85rem;
    }
    .haf-card {
      min-width: 0;
      padding: 1rem;
      border: 1px solid var(--dt-border, var(--border));
      border-radius: calc(var(--radius-md, 0.375rem) + 0.15rem);
      background: var(--dt-card, var(--card));
      box-shadow: 0 1px 0 color-mix(in srgb, var(--dt-foreground) 4%, transparent);
    }
    .haf-card-dirty { border-color: color-mix(in srgb, var(--dt-primary) 52%, var(--dt-border)); }
    .haf-card-header {
      display: flex;
      min-width: 0;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      margin-bottom: 0.8rem;
    }
    .haf-card-title-wrap { min-width: 0; }
    .haf-card-title { margin: 0; font-size: 0.95rem; font-weight: 650; }
    .haf-task-key {
      display: block;
      margin-top: 0.2rem;
      overflow: hidden;
      color: var(--dt-muted-foreground, var(--muted-foreground));
      font-size: 0.65rem;
      text-overflow: ellipsis;
    }
    .haf-dirty-badge {
      flex: none;
      padding: 0.18rem 0.42rem;
      border-radius: 999px;
      background: color-mix(in srgb, var(--dt-primary) 13%, transparent);
      color: var(--dt-primary);
      font-size: 0.625rem;
      font-weight: 700;
    }
    .haf-primary {
      display: grid;
      grid-template-columns: 4.5rem minmax(0, 1fr);
      align-items: center;
      gap: 0.5rem;
      padding: 0.55rem 0.65rem;
      border-radius: var(--radius-md, 0.375rem);
      background: var(--dt-midground, var(--dt-muted));
    }
    .haf-primary .haf-kicker { margin: 0; }
    .haf-primary-value {
      overflow: hidden;
      font-family: var(--dt-font-mono, monospace);
      font-size: 0.7rem;
      text-align: right;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .haf-role-note {
      display: flex;
      align-items: flex-start;
      gap: 0.4rem;
      margin: 0.65rem 0 0;
      color: var(--dt-muted-foreground, var(--muted-foreground));
      font-size: 0.7rem;
      line-height: 1.4;
    }
    .haf-role-note > span { flex: none; margin-top: 0.1rem; color: var(--dt-primary); }
    .haf-legacy-warning {
      display: flex;
      align-items: flex-start;
      gap: 0.45rem;
      margin: 0.65rem 0 0;
      padding: 0.6rem 0.65rem;
      border: 1px solid color-mix(in srgb, var(--dt-destructive) 40%, var(--dt-border));
      border-radius: var(--radius-md, 0.375rem);
      background: color-mix(in srgb, var(--dt-destructive) 7%, var(--dt-card));
      color: var(--dt-foreground, var(--foreground));
      font-size: 0.7rem;
      line-height: 1.4;
    }
    .haf-legacy-warning > span:first-child {
      flex: none;
      margin-top: 0.1rem;
      color: var(--dt-destructive, var(--destructive));
    }
    .haf-chain { display: grid; gap: 0.45rem; margin-top: 0.75rem; }
    .haf-empty-chain {
      margin-top: 0.75rem;
      padding: 0.8rem;
      border: 1px dashed var(--dt-border, var(--border));
      border-radius: var(--radius-md, 0.375rem);
      color: var(--dt-muted-foreground, var(--muted-foreground));
      font-size: 0.72rem;
      text-align: center;
    }
    .haf-fallback-row {
      display: grid;
      grid-template-columns: 1.65rem minmax(0, 1fr) auto;
      align-items: center;
      gap: 0.4rem;
    }
    .haf-order {
      display: grid;
      width: 1.55rem;
      height: 1.55rem;
      place-items: center;
      border: 1px solid var(--dt-border, var(--border));
      border-radius: 999px;
      color: var(--dt-muted-foreground, var(--muted-foreground));
      font-family: var(--dt-font-mono, monospace);
      font-size: 0.65rem;
    }
    .haf-model-trigger {
      display: flex !important;
      width: 100%;
      min-width: 0;
      height: 2rem !important;
      justify-content: space-between !important;
      gap: 0.5rem;
      padding-inline: 0.6rem !important;
      font-size: 0.7rem !important;
      font-weight: 400 !important;
    }
    .haf-model-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .haf-model-menu { width: min(22rem, calc(100vw - 2rem)); padding: 0 !important; }
    .haf-muted { flex: none; color: var(--dt-muted-foreground, var(--muted-foreground)); }
    .haf-icon-button {
      display: grid;
      width: 1.65rem;
      height: 1.65rem;
      place-items: center;
      border: 0;
      border-radius: var(--radius-md, 0.375rem);
      background: transparent;
      color: var(--dt-muted-foreground, var(--muted-foreground));
      cursor: pointer;
    }
    .haf-icon-button:hover:not(:disabled) {
      background: var(--dt-accent, var(--accent));
      color: var(--dt-accent-foreground, var(--foreground));
    }
    .haf-icon-button:focus-visible { outline: 2px solid var(--dt-ring); outline-offset: 1px; }
    .haf-icon-button:disabled { cursor: default; opacity: 0.25; }
    .haf-card-footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      margin-top: 0.8rem;
    }
    .haf-card-footer button, .haf-header-actions button { height: 1.9rem; font-size: 0.7rem; }
    .haf-add-button, .haf-refresh-button { gap: 0.35rem; }
    .haf-validation, .haf-error, .haf-inline-error {
      margin: 0.65rem 0 0;
      color: var(--dt-destructive, var(--destructive));
      font-size: 0.7rem;
      line-height: 1.4;
    }
    .haf-global-error {
      margin: 1rem 0;
      padding: 1rem;
      border: 1px solid color-mix(in srgb, var(--dt-destructive) 55%, var(--dt-border));
      border-radius: var(--radius-md, 0.375rem);
      background: color-mix(in srgb, var(--dt-destructive) 7%, var(--dt-card));
    }
    .haf-global-error h2 { margin: 0 0 0.35rem; font-size: 0.9rem; }
    .haf-global-error p { margin: 0 0 0.75rem; color: var(--dt-muted-foreground); font-size: 0.75rem; }
    .haf-loading {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.55rem;
      min-height: 12rem;
      color: var(--dt-muted-foreground, var(--muted-foreground));
      font-size: 0.8rem;
    }
    .haf-spin { animation: haf-spin 0.85s linear infinite; }
    @keyframes haf-spin { to { transform: rotate(360deg); } }
    .haf-advanced {
      margin-top: 1rem;
      border: 1px solid var(--dt-border, var(--border));
      border-radius: calc(var(--radius-md, 0.375rem) + 0.15rem);
      background: color-mix(in srgb, var(--dt-card) 82%, transparent);
    }
    .haf-advanced-toggle {
      display: flex;
      width: 100%;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      padding: 0.9rem 1rem;
      border: 0;
      border-radius: inherit;
      background: transparent;
      color: inherit;
      cursor: pointer;
      text-align: left;
    }
    .haf-advanced-toggle:hover { background: var(--dt-accent, var(--accent)); }
    .haf-advanced-toggle span { display: flex; align-items: baseline; gap: 0.55rem; }
    .haf-advanced-toggle strong { font-size: 0.82rem; }
    .haf-advanced-toggle small { color: var(--dt-muted-foreground); font-size: 0.68rem; }
    .haf-advanced-grid { padding: 0 0.8rem 0.8rem; }
    .haf-page-footer {
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      margin-top: 1rem;
      color: var(--dt-muted-foreground, var(--muted-foreground));
      font-family: var(--dt-font-mono, monospace);
      font-size: 0.62rem;
    }
    @media (max-width: 820px) {
      .haf-grid { grid-template-columns: 1fr; }
      .haf-page-header { flex-direction: column; gap: 1rem; }
      .haf-header-actions { width: 100%; justify-content: space-between; }
    }
    @media (max-width: 540px) {
      .haf-shell { padding-inline: 0.75rem; }
      .haf-fallback-row { grid-template-columns: 1.65rem minmax(0, 1fr); }
      .haf-row-actions { grid-column: 2; justify-content: flex-end; }
      .haf-card-footer { align-items: stretch; flex-direction: column; }
      .haf-save-actions { justify-content: flex-end; }
      .haf-page-footer { flex-direction: column; gap: 0.25rem; }
    }
  `
  document.head.append(style)
  ctx.onDispose(() => style.remove())
}

export default {
  id: PLUGIN_ID,
  name: 'Auxiliary Fallbacks',
  description: 'Manage ordered fallback model chains for Hermes auxiliary roles.',
  defaultEnabled: true,
  register(ctx) {
    installStyles(ctx)

    const openPage = () => host.navigate(ROUTE)

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE },
        render: () => jsx(AuxiliaryFallbacksPage, { rest: ctx.rest })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 60,
        data: { codicon: 'layers', label: 'Auxiliary Fallbacks', path: ROUTE }
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: `${PLUGIN_ID}.open`,
          keywords: ['auxiliary', 'fallback', 'models', 'routing'],
          label: 'Auxiliary Fallbacks: Open',
          run: openPage
        }
      }
    ])
  }
}

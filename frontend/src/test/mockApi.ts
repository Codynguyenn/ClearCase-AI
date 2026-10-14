import { vi } from 'vitest'

/** What a route handler sees. `url` is exactly what the app asked for (still percent-encoded). */
export interface RecordedCall {
  method: string
  url: string
  path: string
  body: unknown
}

export interface Reply {
  status?: number
  json?: unknown
}

type Handler = Reply | ((call: RecordedCall) => Reply | Promise<Reply>)

/**
 * Replace the browser's `fetch` with a router, so the REAL api client code runs in tests and only the
 * network is faked. Routes are keyed `"METHOD /decoded/path"`. A request with no matching route throws,
 * so a test fails loudly instead of silently talking to nothing.
 */
export function mockApi(routes: Record<string, Handler>) {
  const calls: RecordedCall[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    const path = decodeURIComponent(url.split('?')[0])
    let body: unknown = undefined
    if (typeof init?.body === 'string') body = JSON.parse(init.body)
    else if (init?.body instanceof FormData) body = { formData: [...init.body.keys()] }
    const call: RecordedCall = { method, url, path, body }
    calls.push(call)

    const handler = routes[`${method} ${path}`]
    if (!handler) throw new Error(`mockApi: no route for ${method} ${path}`)
    const reply = typeof handler === 'function' ? await handler(call) : handler
    return new Response(JSON.stringify(reply.json ?? {}), {
      status: reply.status ?? 200,
      headers: { 'Content-Type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', fetchMock)
  return {
    calls,
    /** Calls to one route, e.g. `api.callsTo('POST', '/api/policies/search')`. */
    callsTo: (method: string, path: string) => calls.filter((c) => c.method === method && c.path === path),
  }
}

/** A promise you resolve by hand, to hold a request "in flight" while a test looks at the loading state. */
export function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

export const apiError = (status: number, code: string, message: string): Reply => ({
  status,
  json: { error: { code, message } },
})

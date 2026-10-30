import { useCallback, useEffect, useState } from 'react'

/** URL routing without a library: every screen has its own URL, so the browser's back and forward arrows work,
 *  and each history entry remembers its scroll position. */
export interface Route { path: string; parts: string[]; query: URLSearchParams }

const parse = (): Route => ({
  path: window.location.pathname,
  parts: window.location.pathname.split('/').filter(Boolean),
  query: new URLSearchParams(window.location.search),
})

function restoreScroll(y: number) {
  let tries = 0
  const step = () => {   // content may still be rendering: wait until the page is tall enough, for up to ~1 s
    if (document.documentElement.scrollHeight >= y + window.innerHeight || tries++ > 60) window.scrollTo(0, y)
    else requestAnimationFrame(step)
  }
  requestAnimationFrame(step)
}

export function useRoute() {
  const [route, setRoute] = useState<Route>(parse)

  useEffect(() => {
    if ('scrollRestoration' in history) history.scrollRestoration = 'manual'
    const onPop = (e: PopStateEvent) => { setRoute(parse()); restoreScroll((e.state as { y?: number } | null)?.y ?? 0) }
    const remember = () => history.replaceState({ ...(history.state ?? {}), y: window.scrollY }, '')
    let t: number | undefined
    const onScroll = () => { window.clearTimeout(t); t = window.setTimeout(remember, 150) }
    window.addEventListener('popstate', onPop)
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => { window.removeEventListener('popstate', onPop); window.removeEventListener('scroll', onScroll) }
  }, [])

  const navigate = useCallback((to: string, opts: { replace?: boolean; keepScroll?: boolean } = {}) => {
    if (to === window.location.pathname + window.location.search) return
    history.replaceState({ ...(history.state ?? {}), y: window.scrollY }, '')   // where we were, for "back"
    if (opts.replace) history.replaceState({ y: window.scrollY }, '', to)
    else history.pushState({ y: 0 }, '', to)
    setRoute(parse())
    if (!opts.replace && !opts.keepScroll) window.scrollTo(0, 0)
  }, [])

  return { route, navigate }
}

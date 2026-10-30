import type { Branding } from './types'

/** The header and sign-in brand: the operator's logo (a file in ./branding) or the default mark. */
export default function Brand({ b }: { b: Branding | null }) {
  return (
    <div className="brand">
      {b?.logo ? <img src={b.logo} alt="" /> : <span className="mark">WO</span>}
      <div className="names"><strong>{b?.name ?? 'Work-order assistant'}</strong><span>{b?.subtitle ?? ''}</span></div>
    </div>)
}

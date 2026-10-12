/** Exact decimal/rational primitives. No API, formatting truncation or Number conversion. */
export const SCALE = 1000000n
export const FX_MAX = 9999999999999999n
export function decimal(value: string): { n: bigint; d: bigint } {
  if (!/^-?\d+(?:\.\d+)?$/.test(value)) throw new Error('invalid decimal')
  const [whole, fraction = ''] = value.split('.')
  const negative = whole!.startsWith('-')
  return { n: BigInt(whole!.replace('-', '') + fraction) * (negative ? -1n : 1n), d: 10n ** BigInt(fraction.length) }
}
export function units(value: string): bigint {
  const { n, d } = decimal(value)
  if (n * SCALE % d) throw new Error('amount exceeds six-place precision')
  return n * SCALE / d
}
export function formatUnits(value: bigint): string {
  const sign = value < 0 ? '-' : ''; const n = value < 0 ? -value : value
  return `${sign}${n / SCALE}.${(n % SCALE).toString().padStart(6, '0')}`
}
export const ceil = (n: bigint, d: bigint) => (n + d - 1n) / d
export function halfEven(n: bigint, d: bigint): bigint {
  const q = n / d; const r = n % d
  return q + (2n * r > d || (2n * r === d && q % 2n !== 0n) ? 1n : 0n)
}
export interface Rational { n: bigint; d: bigint }
export const add = (a: Rational, b: Rational): Rational => ({ n: a.n * b.d + b.n * a.d, d: a.d * b.d })
export const sub = (a: Rational, b: Rational): Rational => add(a, { n: -b.n, d: b.d })
export const mul = (a: Rational, b: Rational): Rational => ({ n: a.n * b.n, d: a.d * b.d })
export const cmp = (a: Rational, b: Rational) => a.n * b.d < b.n * a.d ? -1 : a.n * b.d > b.n * a.d ? 1 : 0
export const rationalUnits = (n: bigint): Rational => ({ n, d: SCALE })
export function display(a: Rational): string { const sign = a.n < 0 ? -1n : 1n; return formatUnits(sign * halfEven(a.n * sign * SCALE, a.d)) }

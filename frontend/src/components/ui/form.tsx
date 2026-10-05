import type { ReactNode } from 'react'
import { cn } from '.'

export const inputCls =
  'w-full rounded-2xl border-0 bg-white/80 px-4 py-3 text-[15px] text-slate-900 outline-none ring-1 ring-black/[0.08] transition placeholder:text-slate-400 focus:bg-white focus:ring-2 focus:ring-brand-600/60 disabled:opacity-60'

export function Field({ label, children, hint, className }: { label: string; children: ReactNode; hint?: string; className?: string }) {
  return (
    <label className={cn('block', className)}>
      <span className="mb-1.5 block text-sm font-medium text-slate-700">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
    </label>
  )
}

export function Alert({ tone = 'error', children }: { tone?: 'error' | 'success' | 'info'; children: ReactNode }) {
  const tones = {
    error: 'bg-rose-500/10 text-rose-700 ring-rose-500/15',
    success: 'bg-emerald-500/10 text-emerald-700 ring-emerald-500/15',
    info: 'bg-[#f1a400]/10 text-slate-700 ring-[#f1a400]/25',
  }
  return <p role={tone === 'error' ? 'alert' : 'status'} className={cn('rounded-2xl px-4 py-3 text-sm ring-1', tones[tone])}>{children}</p>
}

// Стеклянная таблица для админ-разделов
export function Table({ head, children, empty }: { head: ReactNode[]; children: ReactNode; empty?: string }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[40rem] text-left text-sm">
        <thead>
          <tr className="border-b border-black/5 text-xs uppercase tracking-wide text-slate-500">
            {head.map((cell, index) => <th key={index} className="px-3 py-2 font-medium">{cell}</th>)}
          </tr>
        </thead>
        <tbody className="divide-y divide-black/5">{children}</tbody>
      </table>
      {empty && <p className="px-3 py-6 text-center text-sm text-slate-500">{empty}</p>}
    </div>
  )
}

export const formatDateTime = (value: string | null | undefined) => {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' })
}

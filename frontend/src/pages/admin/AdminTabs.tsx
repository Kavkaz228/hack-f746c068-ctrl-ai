import { NavLink } from 'react-router-dom'
import { USE_MOCK } from '../../api/client'
import { cn, EmptyState, PageHeader } from '../../components/ui'

const tabs = [
  { to: '/hr/approvals', label: 'Заявки' },
  { to: '/hr/users', label: 'Учётные записи' },
  { to: '/hr/data', label: 'Данные' },
  { to: '/hr/audit', label: 'Журнал' },
]

export function AdminHeader({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <>
      <PageHeader eyebrow="Администрирование" title={title} subtitle={subtitle} />
      <nav className="mb-6 flex flex-wrap gap-1 rounded-full bg-black/[0.04] p-1 sm:inline-flex">
        {tabs.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            className={({ isActive }) => cn('rounded-full px-4 py-1.5 text-sm font-medium transition',
              isActive ? 'bg-white text-slate-900 shadow-[0_2px_8px_-2px_rgba(0,0,0,0.15)]' : 'text-slate-600 hover:text-slate-900')}
          >
            {tab.label}
          </NavLink>
        ))}
      </nav>
    </>
  )
}

export function MockNotice() {
  if (!USE_MOCK) return null
  return <EmptyState title="Раздел работает с сервером" text="В мок-режиме нет учётных записей, журнала и снимков данных. Запустите backend и VITE_USE_MOCK=false." />
}
